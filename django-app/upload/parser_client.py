"""Bounded parser protocol over a private, filesystem-authorized Unix socket."""
import json
import socket
import time

MAX_INPUT = 49 * 1024**2
MAX_OUTPUT = 128 * 1024**2
MAX_HEADER = 512


class ParserUnavailable(Exception):
    """Only a fixed public error is exposed; never include transport diagnostics."""


def read_header(stream, before_read=None):
    if before_read is None:
        raw = stream.readline(MAX_HEADER + 1)
    else:
        raw = b''
        while len(raw) <= MAX_HEADER and not raw.endswith(b'\n'):
            raw += read_exact(stream, 1, before_read)
    if len(raw) > MAX_HEADER or not raw.endswith(b'\n'):
        raise ValueError('invalid_parser_header')
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError('invalid_parser_header')
    return header


def read_exact(stream, size, before_read=None):
    chunks = []
    remaining = size
    read = getattr(stream, 'read1', stream.read)
    while remaining:
        if before_read:
            before_read()
        chunk = read(min(remaining, 65536))
        if not chunk:
            raise ValueError('truncated_parser_message')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def request_parse(socket_path, path, timeout):
    if not hasattr(socket, 'AF_UNIX'):
        raise ParserUnavailable()
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            def before_read():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                connection.settimeout(remaining)
            before_read()
            connection.connect(socket_path)
            header = {'suffix': path.suffix, 'size': path.stat().st_size}
            connection.sendall(json.dumps(header).encode() + b'\n')
            with path.open('rb') as source:
                while chunk := source.read(65536):
                    connection.settimeout(max(0.01, deadline - time.monotonic()))
                    connection.sendall(chunk)
            connection.settimeout(max(0.01, deadline - time.monotonic()))
            with connection.makefile('rb') as stream:
                reply = read_header(stream, before_read)
                size = reply.get('size')
                if type(size) is not int or not 0 <= size <= MAX_OUTPUT:
                    raise ValueError('invalid_parser_size')
                if reply.get('status') == 'invalid':
                    raise ValueError('file_parse_failed_or_limit_exceeded')
                if reply.get('status') != 'ok':
                    raise ParserUnavailable()
                parsed = json.loads(read_exact(stream, size, before_read))
                if not isinstance(parsed, dict) or set(parsed) != {'headers', 'rows'}:
                    raise ValueError('invalid_parser_result')
                return parsed
    except ValueError as exc:
        if str(exc) == 'file_parse_failed_or_limit_exceeded':
            raise
        raise ParserUnavailable() from None
    except (OSError, EOFError):
        raise ParserUnavailable() from None
