"""No real datasets, network services or deployment databases."""
import io
import json
import os
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, RequestFactory
from .parser_client import MAX_OUTPUT, ParserUnavailable, read_header, request_parse
from .parsing import parse_files


class ParserTransportTests(SimpleTestCase):
    def test_service_receives_staged_bytes_and_no_subprocess_fallback(self):
        paths = []
        def parse(socket_path, path, timeout):
            self.assertEqual(socket_path, '/private/parser.sock')
            self.assertEqual(path.read_bytes(), b'anon_id\n1\n')
            self.assertLessEqual(timeout, 25)
            paths.append(path)
            return {'headers': ['anon_id'], 'rows': [['1']]}
        with patch.dict(os.environ, {'PARSER_SOCKET_PATH': '/private/parser.sock'}), patch('upload.parsing.request_parse', side_effect=parse), patch('upload.parsing.subprocess.run') as local:
            result, digest = parse_files([SimpleUploadedFile('synthetic.csv', b'anon_id\n1\n')])
        self.assertEqual(result[0]['name'], 'synthetic.csv')
        self.assertEqual(len(digest), 64)
        self.assertFalse(paths[0].exists())
        local.assert_not_called()

    def test_unavailable_service_fails_closed_and_cleans_staging(self):
        paths = []
        def unavailable(socket_path, path, timeout):
            paths.append(path)
            raise ParserUnavailable()
        with patch.dict(os.environ, {'PARSER_SOCKET_PATH': '/private/parser.sock'}), patch('upload.parsing.request_parse', side_effect=unavailable), patch('upload.parsing.subprocess.run') as local:
            with self.assertRaises(ParserUnavailable):
                parse_files([SimpleUploadedFile('synthetic.csv', b'a\n1\n')])
        self.assertFalse(paths[0].parent.exists())
        local.assert_not_called()

    def test_precheck_returns_safe_503(self):
        from .workflow_views import precheck
        from django.contrib.auth.models import AnonymousUser
        request = RequestFactory().post('/upload/api/precheck', {})
        request.user = AnonymousUser()
        with patch('upload.workflow_views.inspect', side_effect=ParserUnavailable()):
            response = precheck.__wrapped__(request)
        self.assertEqual(response.status_code, 503)
        self.assertIn('parser unavailable', json.loads(response.content)['error'])

    def test_protocol_headers_are_bounded(self):
        for body in (b'x' * 513 + b'\n', b'{}', b'[]\n'):
            with self.subTest(body=body[:10]), self.assertRaises(ValueError):
                read_header(io.BytesIO(body))

    def test_oversized_service_response_is_not_read(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.csv'
            path.write_bytes(b'a\n1\n')
            with patch('upload.parser_client.socket.AF_UNIX', 1, create=True), patch('upload.parser_client.socket.socket') as factory:
                stream = factory.return_value.__enter__.return_value.makefile.return_value.__enter__.return_value
                header = json.dumps({'status': 'ok', 'size': MAX_OUTPUT + 1}).encode() + b'\n'
                stream.read1.side_effect = [bytes([byte]) for byte in header]
                with self.assertRaises(ParserUnavailable):
                    request_parse('/private/parser.sock', path, 1)
                stream.read.assert_not_called()
