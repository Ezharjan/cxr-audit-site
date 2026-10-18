"""Synthetic tests runnable without Django or Docker on Linux."""
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from parser_client import ParserUnavailable, read_header, request_parse
from server import Handler, Server


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix sockets required')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.socket = str(self.root / 'parser.sock')
        self.server = Server(self.socket, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def file(self, body, suffix='.csv'):
        path = self.root / ('synthetic' + suffix)
        path.write_bytes(body)
        return path

    def test_csv_roundtrip_exact_identifiers(self):
        result = request_parse(self.socket, self.file(b'anon_id,text_report\n9007199254740993,Synthetic normal report\n'), 5)
        self.assertEqual(result, {'headers': ['anon_id', 'text_report'], 'rows': [['9007199254740993', 'Synthetic normal report']]})

    def test_invalid_upload_does_not_kill_service_or_expose_values(self):
        with self.assertRaisesRegex(ValueError, '^file_parse_failed_or_limit_exceeded$'):
            request_parse(self.socket, self.file(b'anon_id,anon_id\nPRIVATE,PRIVATE\n'), 5)
        result = request_parse(self.socket, self.file(b'anon_id\n1\n'), 5)
        self.assertEqual(result['rows'], [['1']])

    def test_rejects_paths_sizes_and_unknown_fields(self):
        for header in ({'suffix': '../../etc/passwd', 'size': 1}, {'suffix': '.csv', 'size': True},
                       {'suffix': '.csv', 'size': 49 * 1024**2 + 1}, {'suffix': '.csv', 'size': 1, 'path': '/etc/passwd'}):
            with self.subTest(header=header), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(self.socket)
                client.sendall(json.dumps(header).encode() + b'\n')
                with client.makefile('rb') as stream:
                    self.assertEqual(read_header(stream), {'status': 'invalid', 'size': 0})

    def test_unavailable_does_not_expose_socket_path(self):
        with self.assertRaises(ParserUnavailable) as caught:
            request_parse(str(self.root / 'missing.sock'), self.file(b'a\n1\n'), 1)
        self.assertEqual(str(caught.exception), '')

    def test_child_timeout_is_bounded_and_cleans_staging(self):
        import subprocess
        with patch('server.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic', 25)) as run:
            with self.assertRaises(ValueError):
                request_parse(self.socket, self.file(b'a\n1\n'), 5)
        self.assertFalse(Path(run.call_args.kwargs['cwd']).exists())
        self.assertEqual(run.call_args.kwargs['timeout'], 25)

    def test_xlsx_and_formula_rejection(self):
        import openpyxl
        for formula in (False, True):
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.append(['anon_id', 'text_report'])
            sheet.append(['1', '=1+1' if formula else 'Synthetic normal report'])
            output = io.BytesIO()
            workbook.save(output)
            if formula:
                with self.assertRaises(ValueError):
                    request_parse(self.socket, self.file(output.getvalue(), '.xlsx'), 5)
            else:
                result = request_parse(self.socket, self.file(output.getvalue(), '.xlsx'), 5)
                self.assertEqual(result['rows'][0][0], '1')

    def test_encrypted_xlsx(self):
        import openpyxl
        from msoffcrypto.format.ooxml import OOXMLFile
        workbook = openpyxl.Workbook()
        workbook.active.append(['anon_id'])
        workbook.active.append(['1'])
        plain, encrypted = io.BytesIO(), io.BytesIO()
        workbook.save(plain)
        plain.seek(0)
        OOXMLFile(plain).encrypt('synthetic-password', encrypted)
        with patch.dict(os.environ, {'XLSX_DECRYPT_PASSWORD': 'synthetic-password'}):
            result = request_parse(self.socket, self.file(encrypted.getvalue(), '.xlsx'), 10)
        self.assertEqual(result['rows'], [['1']])


if __name__ == '__main__':
    unittest.main()
