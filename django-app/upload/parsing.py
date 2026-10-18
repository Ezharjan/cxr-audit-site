"""Bounded private staging with one isolated parser process per file."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from .parser_client import request_parse

MAX_FILES = 20
MAX_UPLOAD_BYTES = 49 * 1024 * 1024


def parse_files(files):
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError('invalid_file_count')
    if sum(f.size for f in files) > MAX_UPLOAD_BYTES:
        raise ValueError('upload_too_large')
    started = time.monotonic()
    output = []
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix='primer-parse-') as directory:
        os.chmod(directory, 0o700)
        for index, file in enumerate(files):
            if time.monotonic() - started >= 60:
                raise ValueError('submission_parse_timeout')
            suffix = Path(file.name).suffix.lower()
            if not file.size or suffix not in ('.csv', '.xls', '.xlsx') or len(file.name) > 255 or any(c in file.name for c in '\\ /\r\n\x00'.replace(' ', '')):
                raise ValueError('invalid_file')
            path = Path(directory) / (str(index) + suffix)
            size = 0
            digest.update(json.dumps([index, file.name, file.size]).encode())
            with path.open('wb') as stream:
                os.chmod(path, 0o600)
                for chunk in file.chunks():
                    size += len(chunk)
                    if size > file.size or size > MAX_UPLOAD_BYTES:
                        raise ValueError('upload_too_large')
                    digest.update(chunk)
                    stream.write(chunk)
            if size != file.size:
                raise ValueError('file_size_mismatch')
            result = Path(directory) / 'parsed.json'
            env = {k: v for k, v in os.environ.items() if k in ('SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'XLSX_DECRYPT_PASSWORD')}
            timeout = min(25, max(0.1, 60 - (time.monotonic() - started)))
            socket_path = os.environ.get('PARSER_SOCKET_PATH')
            if socket_path:
                # Service failure never falls back to parsing inside Django.
                parsed = request_parse(socket_path, path, timeout)
            else:
                try:
                    subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('parser_worker.py')), str(path), str(result)],
                                   cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=timeout, check=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                except (subprocess.SubprocessError, OSError):
                    raise ValueError('file_parse_failed_or_limit_exceeded') from None
                if result.stat().st_size > 128 * 1024**2:
                    raise ValueError('parsed_size_limit')
                parsed = json.loads(result.read_text(encoding='utf-8'))
            parsed['name'] = file.name
            output.append(parsed)
            result.unlink(missing_ok=True)
            path.unlink()
    if sum(len(f['rows']) for f in output) > 50000:
        raise ValueError('submission_row_limit')
    return output, digest.hexdigest()
