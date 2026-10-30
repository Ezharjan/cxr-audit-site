import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(API_ROOT / "cxr_audit"))
os.chdir(API_ROOT)

from combined_server import api_app, local_dev_startup_errors

STRONG = "s" * 48
LOCAL = {
    "PRIMER_LOCAL_DEV": "True",
    "API_REQUIRE_AUTH": "True",
    "API_SECRET_KEY": STRONG,
    "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
}


class LocalDevStartupTests(unittest.TestCase):
    def test_profile_off_keeps_existing_behaviour(self):
        with patch.dict(os.environ, {"PRIMER_LOCAL_DEV": ""}, clear=False):
            self.assertEqual(local_dev_startup_errors("0.0.0.0"), [])

    def test_valid_local_profile(self):
        with patch.dict(os.environ, LOCAL, clear=False):
            self.assertEqual(local_dev_startup_errors("127.0.0.1"), [])
            self.assertEqual(local_dev_startup_errors("::1"), [])

    def test_local_profile_fails_closed(self):
        cases = [
            ({}, "0.0.0.0", "loopback bind"),
            ({}, "192.168.1.10", "loopback bind"),
            ({"API_REQUIRE_AUTH": "False"}, "127.0.0.1", "API_REQUIRE_AUTH"),
            ({"API_SECRET_KEY": ""}, "127.0.0.1", "API_SECRET_KEY"),
            ({"API_SECRET_KEY": "change-me-to-a-random-secret"}, "127.0.0.1", "API_SECRET_KEY"),
            ({"OLLAMA_BASE_URL": "http://10.0.0.5:11434/v1"}, "127.0.0.1", "OLLAMA_BASE_URL"),
        ]
        for env, host, needle in cases:
            with self.subTest(env=env, host=host), patch.dict(os.environ, {**LOCAL, **env}, clear=False):
                errors = local_dev_startup_errors(host)
                self.assertTrue(any(needle in e for e in errors), errors)
                self.assertFalse(any(STRONG in e for e in errors))

    def test_server_refuses_non_loopback_bind_in_local_profile(self):
        env = {**os.environ, **LOCAL, "PYTHONPATH": str(API_ROOT)}
        result = subprocess.run([sys.executable, "combined_server.py", "--api-only", "--host", "0.0.0.0", "--port", "9"],
                                cwd=API_ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2)
        self.assertIn("loopback bind", result.stdout)
        self.assertNotIn(STRONG, result.stdout + result.stderr)

    def test_api_key_still_required_in_local_profile(self):
        with patch.dict(os.environ, LOCAL, clear=False):
            client = TestClient(api_app)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/tasks").status_code, 403)
            self.assertEqual(client.get("/tasks", headers={"X-API-Key": "wrong"}).status_code, 403)
            self.assertEqual(client.get("/tasks", headers={"X-API-Key": STRONG}).status_code, 200)


class PipelineTempDirTests(unittest.TestCase):
    def test_intermediate_files_use_platform_temp_dir(self):
        import pandas as pd
        from grade_batch_async import BatchCXRProcessor

        processor = BatchCXRProcessor({}, {}, {}, max_workers=1, rate_limit_delay=0)
        processor._process_single_report_llm = lambda text, index: (index, {"grade": 2})
        with tempfile.TemporaryDirectory() as directory, patch("tempfile.gettempdir", return_value=directory):
            processor.process_full_pipeline(pd.DataFrame({"R": ["SYNTHETIC normal"]}), report_column="R", steps=["llm"])
            self.assertTrue((Path(directory) / "intermediate_llm.csv").exists())


if __name__ == "__main__":
    unittest.main()
