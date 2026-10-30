"""Windows parser limits and local subprocess path (synthetic data, no network)."""
import ctypes
import os
import sys
from unittest import skipUnless
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from . import parser_worker
from .parsing import parse_files

try:
    from ctypes import wintypes  # noqa: F401  (importable on Linux from Python 3.12)
    HAVE_WINTYPES = True
except (ImportError, ValueError):
    HAVE_WINTYPES = False


class FakeKernel:
    """Stand-in for kernel32; plain functions so ctypes restype/argtypes can be set."""

    def __init__(self, ok=True):
        self.info = self.info_class = None

        def create(*args):
            return 1234

        def current():
            return 5678

        def set_information(handle, info_class, pointer, size):
            self.info_class = info_class
            self.info = ctypes.string_at(pointer, size)
            return 1

        def assign(job, process):
            return 1 if ok else 0

        self.CreateJobObjectW, self.GetCurrentProcess = create, current
        self.SetInformationJobObject, self.AssignProcessToJobObject = set_information, assign


class WindowsJobLimitTests(SimpleTestCase):
    @skipUnless(HAVE_WINTYPES, "ctypes.wintypes unavailable on this Python")
    def test_job_limits_memory_cpu_single_process(self):
        kernel = FakeKernel()
        with patch.object(ctypes, "WinDLL", lambda *a, **k: kernel, create=True):
            parser_worker.windows_limits()
        self.assertEqual(kernel.info_class, 9)  # JobObjectExtendedLimitInformation
        flags = int.from_bytes(kernel.info[16:20], "little")
        self.assertEqual(flags, 0x100 | 0x2 | 0x8 | 0x400)
        self.assertEqual(int.from_bytes(kernel.info[0:8], "little"), 20 * 10_000_000)
        pointer = ctypes.sizeof(ctypes.c_size_t)
        # ActiveProcessLimit follows flags + minimum/maximum working set (aligned).
        active_offset = 24 + 2 * pointer if pointer == 8 else 20 + 2 * pointer
        self.assertEqual(int.from_bytes(kernel.info[active_offset:active_offset + 4], "little"), 1)

    @skipUnless(HAVE_WINTYPES, "ctypes.wintypes unavailable on this Python")
    def test_job_assignment_failure_fails_closed(self):
        with patch.object(ctypes, "WinDLL", lambda *a, **k: FakeKernel(ok=False), create=True):
            with self.assertRaisesRegex(ValueError, "parser_job_limits_unavailable"):
                parser_worker.windows_limits()

    def test_unknown_platform_fails_closed(self):
        with patch.object(sys, "platform", "darwin"), patch.object(os, "name", "posix"):
            with self.assertRaisesRegex(ValueError, "unsupported_parser_platform"):
                parser_worker.limits()


class LocalSubprocessPathTests(SimpleTestCase):
    """Without PARSER_SOCKET_PATH (Windows local profile) parsing uses the subprocess."""

    def test_worker_runs_isolated_with_minimal_environment(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            from pathlib import Path
            Path(args[-1]).write_text('{"headers": ["anon_id"], "rows": [["1"]]}', encoding="utf-8")

        env = {"PARSER_SOCKET_PATH": "", "API_SECRET_KEY": "s" * 48, "DJANGO_SECRET_KEY": "k" * 50, "XLSX_DECRYPT_PASSWORD": "synthetic"}
        with patch.dict(os.environ, env), patch("upload.parsing.subprocess.run", side_effect=fake_run):
            result, _ = parse_files([SimpleUploadedFile("synthetic.csv", b"anon_id\n1\n")])
        self.assertEqual(result[0]["rows"], [["1"]])
        args, kwargs = calls[0]
        self.assertEqual(args[:2], [sys.executable, "-I"])
        self.assertTrue(args[2].endswith("parser_worker.py"))
        self.assertLessEqual(kwargs["timeout"], 25)
        self.assertTrue(set(kwargs["env"]) <= {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "XLSX_DECRYPT_PASSWORD"})
        self.assertNotIn("API_SECRET_KEY", kwargs["env"])
        self.assertNotIn("DJANGO_SECRET_KEY", kwargs["env"])

    def test_worker_failure_is_a_validation_error_not_a_bypass(self):
        import subprocess
        with patch.dict(os.environ, {"PARSER_SOCKET_PATH": ""}), \
                patch("upload.parsing.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
            with self.assertRaisesRegex(ValueError, "file_parse_failed_or_limit_exceeded"):
                parse_files([SimpleUploadedFile("synthetic.csv", b"anon_id\n1\n")])

    def test_limits_still_enforced_before_worker(self):
        for files, error in (([], "invalid_file_count"),
                             ([SimpleUploadedFile("synthetic.txt", b"a\n1\n")], "invalid_file"),
                             ([SimpleUploadedFile("synthetic.csv", b"")], "invalid_file")):
            with self.subTest(error=error), patch.dict(os.environ, {"PARSER_SOCKET_PATH": ""}), \
                    patch("upload.parsing.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, error):
                    parse_files(files)
                run.assert_not_called()
