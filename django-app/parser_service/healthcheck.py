import json
import socket
import sys

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.settimeout(3)
    connection.connect('/run/primer-parser/parser.sock')
    connection.sendall(b'{"health":true}\n')
    with connection.makefile('rb') as stream:
        reply = json.loads(stream.readline(512))
        sys.exit(0 if reply == {'status': 'ok', 'size': 0} else 1)
