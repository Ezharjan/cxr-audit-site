"""Private subprocess entry point. No Django, DB, credentials or network clients."""
import csv
import io
import json
import math
import os
import sys
import zipfile
from datetime import datetime, date
from pathlib import Path

MAX_ROWS = 50000
MAX_COLUMNS = 128
MAX_CELL = 65536
MAX_EXPANDED = 128 * 1024 * 1024


def limits():
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024**2, 128 * 1024**2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    elif os.name == 'nt':
        windows_limits()
    else:
        raise ValueError('unsupported_parser_platform')


def windows_limits():
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong), ('flags', w.DWORD),
                    ('minimum', c.c_size_t), ('maximum', c.c_size_t), ('active', w.DWORD),
                    ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                    ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
    global _job_handle
    _job_handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    # Per-process memory (0x100), CPU time (0x2), one active process so the parser
    # cannot spawn children (0x8), and no crash dialog that would stall until timeout (0x400).
    info.basic.flags = 0x100 | 0x2 | 0x8 | 0x400
    info.basic.active = 1
    info.basic.process_time = 20 * 10_000_000
    info.process_memory = 768 * 1024**2
    if not _job_handle or not kernel.SetInformationJobObject(_job_handle,9,c.byref(info),c.sizeof(info)) or not kernel.AssignProcessToJobObject(_job_handle,kernel.GetCurrentProcess()):
        raise ValueError('parser_job_limits_unavailable')


def linux_isolation(directory):
    # Fail closed unless the deployment kernel supports unprivileged Landlock and seccomp.
    import ctypes as c
    import platform
    libc = c.CDLL(None, use_errno=True)
    libc.syscall.restype = c.c_long
    libc.prctl.restype = c.c_int
    class Ruleset(c.Structure):
        _fields_ = [('handled', c.c_uint64)]
    class PathRule(c.Structure):
        _pack_ = 1
        _fields_ = [('allowed',c.c_uint64),('parent',c.c_int32)]
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise ValueError('parser_landlock_unavailable')
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13
    if abi >= 3:
        handled |= 1 << 14
    attr = Ruleset(handled)
    fd = libc.syscall(444,c.byref(attr),c.sizeof(attr),0)
    if fd < 0:
        raise ValueError('parser_ruleset_unavailable')
    try:
        roots = {Path(sys.base_prefix) / 'lib', Path('/usr/lib'), Path('/lib')}
        roots |= {Path(path) for path in sys.path if path and Path(path).is_dir()}
        for root, rights in [(root,4|8) for root in roots if root.is_dir()] + [(Path(directory),handled & ~1)]:
            parent = os.open(root,os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(rights,parent)
                if libc.syscall(445,fd,1,c.byref(rule),0) < 0:
                    raise ValueError('parser_path_rule_failed')
            finally:
                os.close(parent)
        if libc.prctl(38,1,0,0,0) != 0 or libc.syscall(446,fd,0) < 0:
            raise ValueError('parser_restriction_failed')
    finally:
        os.close(fd)
    architecture = platform.machine().lower()
    if architecture in ('x86_64','amd64'):
        arch, denied = 0xC000003E, (41,42,53,59,322)
    elif architecture in ('aarch64','arm64'):
        arch, denied = 0xC00000B7, (198,199,203,221,281)
    else:
        raise ValueError('unsupported_parser_architecture')
    class Filter(c.Structure):
        _fields_ = [('code',c.c_ushort),('jt',c.c_ubyte),('jf',c.c_ubyte),('k',c.c_uint)]
    class Program(c.Structure):
        _fields_ = [('length',c.c_ushort),('filters',c.POINTER(Filter))]
    filters = [Filter(0x20,0,0,4),Filter(0x15,1,0,arch),Filter(0x06,0,0,0x80000000),Filter(0x20,0,0,0)]
    for number in denied:
        filters.extend([Filter(0x15,0,1,number),Filter(0x06,0,0,0x50000 | 1)])
    filters.append(Filter(0x06,0,0,0x7fff0000))
    array = (Filter * len(filters))(*filters)
    program = Program(len(filters),array)
    if libc.prctl(22,2,c.byref(program),0,0) != 0:
        raise ValueError('parser_seccomp_unavailable')


def value(cell):
    if isinstance(cell, (datetime, date)):
        return cell.isoformat()
    if isinstance(cell, float) and not math.isfinite(cell):
        raise ValueError('invalid_cell')
    if cell is not None and len(str(cell)) > MAX_CELL:
        raise ValueError('cell_too_large')
    return cell


def xlsx_rows(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError('workbook_expansion_limit')
        if any('vbaProject' in e.filename or 'externalLinks/' in e.filename for e in entries):
            raise ValueError('unsupported_workbook_feature')
    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        if len(workbook.worksheets) != 1:
            raise ValueError('single_worksheet_required')
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        for row in sheet.iter_rows():
            if len(row) > MAX_COLUMNS:
                raise ValueError('column_limit')
            values = []
            for cell in row:
                if cell.data_type in ('f', 'e'):
                    raise ValueError('formula_or_error_cell')
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    if abs(cell.value) >= 10**15 or cell.number_format.startswith('00'):
                        raise ValueError('precision_or_identifier_format')
                values.append(value(int(cell.value) if isinstance(cell.value, float) and cell.value.is_integer() else cell.value))
            yield values
    finally:
        workbook.close()


def rows(path):
    raw = path.read_bytes()
    suffix = path.suffix
    if suffix == '.csv':
        csv.field_size_limit(MAX_CELL)
        yield from csv.reader(io.StringIO(raw.decode('utf-8-sig')), strict=True)
        return
    if raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        import msoffcrypto
        office = msoffcrypto.OfficeFile(io.BytesIO(raw))
        if office.is_encrypted():
            class BoundedBuffer(io.BytesIO):
                def write(self, data):
                    if self.tell() + len(data) > MAX_EXPANDED:
                        raise ValueError('decrypted_size_limit')
                    return super().write(data)
            output = BoundedBuffer()
            office.load_key(password=os.environ.get('XLSX_DECRYPT_PASSWORD', ''))
            office.decrypt(output)
            raw = output.getvalue()
    if raw.startswith(b'PK'):
        yield from xlsx_rows(raw)
    elif suffix == '.xls':
        # Reject BIFF formulas/macros rather than trusting cached workbook results.
        import olefile
        import struct
        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
            if any('vba' in '/'.join(parts).lower() or 'macros' in '/'.join(parts).lower() for parts in ole.listdir()):
                raise ValueError('unsupported_workbook_feature')
            stream = ole.openstream('Workbook' if ole.exists('Workbook') else 'Book').read(MAX_EXPANDED + 1)
        if len(stream) > MAX_EXPANDED:
            raise ValueError('workbook_expansion_limit')
        position = 0
        while position + 4 <= len(stream):
            code, size = struct.unpack_from('<HH', stream, position)
            if code == 6:
                raise ValueError('formula_cell')
            position += 4 + size
        import xlrd
        workbook = xlrd.open_workbook(file_contents=raw, on_demand=True, formatting_info=True)
        try:
            if workbook.nsheets != 1:
                raise ValueError('single_worksheet_required')
            sheet = workbook.sheet_by_index(0)
            if sheet.nrows > MAX_ROWS + 1 or sheet.ncols > MAX_COLUMNS:
                raise ValueError('worksheet_limit')
            for index in range(sheet.nrows):
                row = []
                for cell in sheet.row(index):
                    if cell.ctype == xlrd.XL_CELL_ERROR:
                        raise ValueError('invalid_cell')
                    v = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        v = xlrd.xldate_as_datetime(v, workbook.datemode).isoformat()
                    elif cell.ctype == xlrd.XL_CELL_NUMBER and (abs(v) >= 10**15 or workbook.format_map[workbook.xf_list[cell.xf_index].format_key].format_str.startswith('00')):
                        raise ValueError('precision_lost')
                    row.append(value(int(v) if isinstance(v,float) and v.is_integer() else v))
                yield row
        finally:
            workbook.release_resources()
    else:
        raise ValueError('invalid_workbook')


def parse(path):
    iterator = iter(rows(path))
    headers = next(iterator, [])
    if not headers or len(headers) > MAX_COLUMNS or any(not isinstance(h, str) or not h.strip() for h in headers):
        raise ValueError('invalid_headers')
    headers = [h.strip() for h in headers]
    if len(set(headers)) != len(headers):
        raise ValueError('duplicate_headers')
    output = []
    for index, row in enumerate(iterator):
        if index >= MAX_ROWS or len(row) > len(headers):
            raise ValueError('row_limit_or_width')
        row = [value(v) for v in row]
        output.append(row + [None] * (len(headers) - len(row)))
    return dict(headers=headers, rows=output)


if __name__ == '__main__':
    try:
        limits()
        if sys.platform == 'linux':
            linux_isolation(Path(sys.argv[1]).parent)
        result = parse(Path(sys.argv[1]))
        Path(sys.argv[2]).write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    except Exception:
        # Parser diagnostics never include uploaded values or a password.
        sys.exit(2)
