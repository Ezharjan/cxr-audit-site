"""Unit tests for the Windows launcher logic that can run on any OS (stdlib only)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import primer_local as pl  # noqa: E402


class GuardTests(unittest.TestCase):
    def test_requires_cg_name_and_matching_prefix(self):
        good = {"CONDA_DEFAULT_ENV": "cg", "CONDA_PREFIX": sys.prefix}
        with patch.dict(os.environ, good):
            pl.require_cg()
        for env in ({"CONDA_DEFAULT_ENV": "base", "CONDA_PREFIX": sys.prefix},
                    {"CONDA_DEFAULT_ENV": "cg", "CONDA_PREFIX": ""},
                    {"CONDA_DEFAULT_ENV": "cg", "CONDA_PREFIX": tempfile.gettempdir()},
                    {"CONDA_DEFAULT_ENV": "", "CONDA_PREFIX": sys.prefix}):
            with self.subTest(env=env), patch.dict(os.environ, env), self.assertRaises(SystemExit) as raised:
                pl.require_cg()
            self.assertEqual(raised.exception.code, 3)

    def test_main_checks_environment_before_any_command(self):
        with patch.dict(os.environ, {"CONDA_DEFAULT_ENV": "base"}), patch.object(pl, "cmd_init") as init:
            with self.assertRaises(SystemExit):
                pl.main(["init"])
            init.assert_not_called()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PRIMER_LOCAL_HOME": self.home.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.home.cleanup()

    def test_init_generates_private_secrets_once(self):
        self.assertEqual(pl.cmd_init(None), 0)
        config = pl.load_config()
        self.assertGreaterEqual(len(config["DJANGO_SECRET_KEY"]), 50)
        self.assertGreaterEqual(len(config["API_SECRET_KEY"]), 40)
        self.assertNotEqual(config["DJANGO_SECRET_KEY"], config["API_SECRET_KEY"])
        self.assertEqual(config["OLLAMA_MODEL"], "qwen3:4b")
        self.assertEqual(config["OLLAMA_MAX_WORKERS"], "1")
        before = pl.paths()["config"].read_text()
        pl.cmd_init(None)
        self.assertEqual(pl.paths()["config"].read_text(), before)
        self.assertFalse(str(pl.paths()["config"]).startswith(str(pl.REPO)))

    def test_forbidden_keys_ignored_and_loopback_enforced(self):
        pl.cmd_init(None)
        path = pl.paths()["config"]
        path.write_text(path.read_text() + "\nDATABASE_ENGINE=postgres\nOLLAMA_BASE_URL=http://10.0.0.1:11434/v1\n"
                        "API_REQUIRE_AUTH=False\nDJANGO_ALLOWED_HOSTS=*\nOLLAMA_MODEL=qwen3:8b\n")
        config = pl.load_config()
        with patch.dict(os.environ, {"DATABASE_ENGINE": "postgres", "PARSER_SOCKET_PATH": "/x", "API_SECRET_KEY": "stale"}):
            env = pl.service_env(config)
        self.assertEqual(env["DATABASE_ENGINE"], "sqlite")
        self.assertEqual(env["AUDIT_DATABASE_ENGINE"], "sqlite")
        self.assertEqual(env["OLLAMA_BASE_URL"], "http://127.0.0.1:11434/v1")
        self.assertEqual(env["API_REQUIRE_AUTH"], "True")
        self.assertEqual(env["DJANGO_ALLOWED_HOSTS"], "127.0.0.1,localhost")
        self.assertEqual(env["API_BIND_HOST"], "127.0.0.1")
        self.assertEqual(env["CXR_API_IP"], "127.0.0.1")
        self.assertEqual(env["PRIMER_LOCAL_DEV"], "True")
        self.assertEqual(env["DJANGO_DEBUG"], "False")
        self.assertEqual(env["API_SECRET_KEY"], config["API_SECRET_KEY"])
        self.assertNotIn("PARSER_SOCKET_PATH", env)
        self.assertEqual(env["OLLAMA_MODEL"], "qwen3:8b")
        self.assertIn("127.0.0.1", env["NO_PROXY"])
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertTrue(env["DATABASE_NAME"].startswith(self.home.name))

    def test_weak_or_invalid_config_rejected(self):
        pl.cmd_init(None)
        path = pl.paths()["config"]
        original = path.read_text()
        for extra in ("API_SECRET_KEY=short", "DJANGO_PORT=abc", "API_PORT=8000", "not a pair"):
            with self.subTest(extra=extra):
                path.write_text(original + "\n" + extra + "\n")
                with self.assertRaises(pl.LauncherError):
                    pl.load_config()

    def test_missing_config_message(self):
        with self.assertRaises(pl.LauncherError):
            pl.load_config()
        self.assertIsNone(pl.load_config(required=False))


class HelperTests(unittest.TestCase):
    def test_redact(self):
        config = {"API_SECRET_KEY": "A" * 40, "DJANGO_SECRET_KEY": "B" * 50, "XLSX_DECRYPT_PASSWORD": ""}
        text = pl.redact(f"key {'A' * 40} and {'B' * 50}; password=hunter2 X-API-Key: abc123", config)
        self.assertNotIn("A" * 40, text)
        self.assertNotIn("B" * 50, text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc123", text)

    def test_version_tuple(self):
        self.assertGreater(pl.version_tuple("5.2.1"), pl.version_tuple("5.1"))
        self.assertLess(pl.version_tuple("0.0.6"), pl.version_tuple("0.0.20"))
        self.assertEqual(pl.version_tuple("2.0.0rc1"), (2, 0, 0))

    def test_dependency_plan_lists_only_missing(self):
        with patch.object(pl, "installed_version", side_effect=lambda d: None if d == "json-repair" else "999"), \
                patch.object(pl, "importable", return_value=True), patch("builtins.print") as out:
            code = pl.cmd_check_deps(type("A", (), {"json": False})())
        printed = "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertEqual(code, 1)
        self.assertIn('"json-repair>=0.7.0"', printed)
        self.assertNotIn('"Django>=', printed)
        self.assertIn("--dry-run", printed)

    def test_port_busy_detected_for_wildcard_listener(self):
        import socket
        with socket.socket() as server:
            server.bind(("0.0.0.0", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertFalse(pl.port_free(port))
        self.assertTrue(pl.port_free(port))

    @unittest.skipIf(pl.IS_WINDOWS, "Linux /proc identity check")
    def test_process_identity_and_pid_reuse_protection(self):
        identity = pl.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"PRIMER_LOCAL_HOME": home}):
            pl.paths()["run"].mkdir(parents=True)
            import json
            pl.pid_file("api").write_text(json.dumps({"pid": os.getpid(), "created": identity[0], "exe": identity[1]}))
            self.assertEqual(pl.verified_pid("api"), os.getpid())
            pl.pid_file("api").write_text(json.dumps({"pid": os.getpid(), "created": identity[0] + 1, "exe": identity[1]}))
            self.assertIsNone(pl.verified_pid("api"))
            with patch.object(pl, "terminate") as terminate:
                pl._stop_role("api")
                terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
