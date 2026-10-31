"""Regression checks for server bootstrap and inference isolation; no Docker required."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("init_h100", ROOT / "deploy/init_h100.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class H100DeploymentTests(unittest.TestCase):
    def test_secret_pairs_and_noninteractive_bootstrap_disabled(self):
        text = setup.build_env((ROOT / ".env.example-prod").read_text(encoding="utf-8"),
                               "primer.example.org", "a" * 40)
        env = dict(line.split("=", 1) for line in text.splitlines()
                   if line and not line.startswith("#"))
        self.assertEqual(env["DATABASE_PASSWORD"], env["POSTGRES_APP_PASSWORD"])
        self.assertEqual(env["AUDIT_DATABASE_PASSWORD"], env["POSTGRES_AUDIT_PASSWORD"])
        keys = [env[key] for key in ("DJANGO_SECRET_KEY", "API_SECRET_KEY", "OLLAMA_API_KEY",
                                    "POSTGRES_PASSWORD", "DATABASE_PASSWORD", "AUDIT_DATABASE_PASSWORD")]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(len(key) >= 64 for key in keys))
        self.assertEqual(env["DJANGO_SUPERUSER_USERNAME"], "")
        self.assertEqual(env["DJANGO_CSRF_TRUSTED_ORIGINS"], "https://primer.example.org")

    def test_reject_config_injection_and_mutable_revision(self):
        for host, revision in [("https://example.org", "a" * 40),
                               ("example.org\nDJANGO_DEBUG=True", "a" * 40),
                               ("example.org", "main")]:
            with self.assertRaises(ValueError):
                setup.build_env("", host, revision)

    def test_existing_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            setup.write_new(path, "original\n")
            with self.assertRaises(FileExistsError):
                setup.write_new(path, "replacement\n")
            self.assertEqual(path.read_text(), "original\n")

    def test_inference_is_private_authenticated_and_offline(self):
        config = yaml.safe_load((ROOT / "docker-compose.h100.yml").read_text())
        llm = config["services"]["llm"]
        self.assertNotIn("ports", llm)
        self.assertEqual(llm["networks"], ["primer-inference"])
        self.assertTrue(config["networks"]["primer-inference"]["internal"])
        self.assertIn(":?", llm["environment"]["VLLM_API_KEY"])
        self.assertEqual(llm["environment"]["HF_HUB_OFFLINE"], "1")
        self.assertIn("--no-enable-log-requests", llm["command"])
        self.assertEqual(config["services"]["model-download"]["profiles"], ["download"])
        self.assertEqual(config["services"]["api"]["depends_on"]["llm"]["condition"], "service_healthy")


if __name__ == "__main__":
    unittest.main()
