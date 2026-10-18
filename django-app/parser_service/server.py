"""No TCP listener, application imports, uploaded filenames or payload logging."""
import json
import os
import socketserver
import subprocess
import sys
import tempfile
from pathlib import Path

from parser_client import MAX_INPUT, MAX_OUTPUT, read_header, read_exact

SOCKET = '/run/primer-parser/parser.sock'
JOB = Path(__file__).with_name('job.py')


class Handler(socketserver.StreamRequestHandler):
    def reply(self, status, body=b''):
        self.wfile.write(json.dumps({'status': status, 'size': len(body)}).encode() + b'\n')
        self.wfile.write(body)

    def handle(self):
        self.request.settimeout(25)
        try:
            header = read_header(self.rfile)
            if header == {'health': True}:
                self.reply('ok')
                return
            size, suffix = header.get('size'), header.get('suffix')
            if set(header) != {'size', 'suffix'} or type(size) is not int or not 0 < size <= MAX_INPUT or suffix not in ('.csv', '.xls', '.xlsx'):
                self.reply('invalid')
                return
            with tempfile.TemporaryDirectory(prefix='primer-parse-') as directory:
                path = Path(directory) / ('input' + suffix)
                # Stage incrementally; original bytes exist only in private tmpfs.
                with path.open('wb') as output:
                    remaining = size
                    while remaining:
                        chunk = read_exact(self.rfile, min(remaining, 65536))
                        output.write(chunk)
                        remaining -= len(chunk)
                result = Path(directory) / 'parsed.json'
                env = {key: value for key, value in os.environ.items() if key in ('PATH', 'XLSX_DECRYPT_PASSWORD')}
                subprocess.run([sys.executable, '-I', str(JOB), str(path), str(result)],
                               env=env, cwd=directory, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, check=True)
                if result.stat().st_size > MAX_OUTPUT:
                    raise ValueError('parsed_size_limit')
                self.reply('ok', result.read_bytes())
        except (ValueError, subprocess.SubprocessError):
            self.reply('invalid')
        except OSError:
            # Includes disconnects and staging failures; never expose paths or values.
            try:
                self.reply('unavailable')
            except OSError:
                pass


class Server(socketserver.UnixStreamServer):
    request_queue_size = 4

    def handle_error(self, request, client_address):
        # Base implementation prints tracebacks which could contain uploaded values.
        pass


def main():
    os.umask(0o077)
    Path(SOCKET).unlink(missing_ok=True)
    with Server(SOCKET, Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
