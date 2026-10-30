"""Local development profile guards (synthetic configuration only)."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from lunit_audit import checks

BASE_DIR = Path(__file__).resolve().parent.parent
STRONG = "s" * 48
SQLITE = {alias: {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"} for alias in ("default", "audit")}
LOCAL = dict(
    PRIMER_LOCAL_DEV=True,
    DEBUG=False,
    ALLOWED_HOSTS=["127.0.0.1", "localhost"],
    DATABASES=SQLITE,
    CXR_API_CONFIG={"API_IP": "127.0.0.1", "API_PORT": 1221},
    LLM_BASE_URL="http://127.0.0.1:11434/v1",
    SECRET_KEY="k" * 50,
    LLM_ALLOW_INSECURE_TRANSPORT=False,
)
ENV = {"API_SECRET_KEY": STRONG, "API_REQUIRE_AUTH": "True"}


def ids(messages):
    return sorted(m.id for m in messages)


class LoopbackTests(SimpleTestCase):
    def test_loopback_names(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"):
            self.assertTrue(checks.is_loopback_host(host), host)
        for host in ("0.0.0.0", "*", "", "192.168.1.5", "example.com", "localhost.example.com"):
            self.assertFalse(checks.is_loopback_host(host), host)


class LocalProfileCheckTests(SimpleTestCase):
    def run_check(self, env=ENV, **overrides):
        with override_settings(**{**LOCAL, **overrides}), patch.dict(os.environ, env, clear=False):
            return checks.check_local_dev_profile(None) + checks.check_llm_transport(None)

    def test_valid_local_profile_has_no_errors_or_http_warning(self):
        self.assertEqual(self.run_check(), [])

    def test_profile_off_is_ignored(self):
        self.assertEqual(ids(self.run_check(PRIMER_LOCAL_DEV=False, ALLOWED_HOSTS=["*"])), ["lunit_audit.W002"])

    def test_non_loopback_and_weak_values_fail_closed(self):
        cases = {
            "lunit_audit.E101": dict(ALLOWED_HOSTS=["127.0.0.1", "0.0.0.0"]),
            "lunit_audit.E102": dict(DATABASES={**SQLITE, "default": {"ENGINE": "django.db.backends.postgresql"}}),
            "lunit_audit.E103": dict(CXR_API_CONFIG={"API_IP": "api", "API_PORT": 1221}),
            "lunit_audit.E104": dict(LLM_BASE_URL="http://192.168.1.20:11434/v1"),
            "lunit_audit.E106": dict(SECRET_KEY="django-insecure-" + "x" * 50),
        }
        for expected, overrides in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, ids(self.run_check(**overrides)))

    def test_api_secret_and_auth_required(self):
        for secret in ("", "change-me-to-a-random-secret", "short"):
            with self.subTest(secret=secret):
                self.assertIn("lunit_audit.E105", ids(self.run_check(env={**ENV, "API_SECRET_KEY": secret})))
        self.assertIn("lunit_audit.E107", ids(self.run_check(env={**ENV, "API_REQUIRE_AUTH": "False"})))

    def test_debug_is_warned(self):
        self.assertEqual(ids(self.run_check(DEBUG=True)), ["lunit_audit.W110"])


class SettingsImportTests(SimpleTestCase):
    """Settings are evaluated at import, so check them in a clean interpreter."""

    def load(self, extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PRIMER_", "DJANGO_", "REPORT_V2_", "EMAIL_"))}
        env.update(extra)
        code = ("import json, lunit_audit.settings as s; print(json.dumps({k: str(getattr(s, k, None)) for k in "
                "['SECURE_SSL_REDIRECT','SESSION_COOKIE_SECURE','CSRF_COOKIE_SECURE','SECURE_HSTS_SECONDS',"
                "'STATIC_ROOT','MEDIA_ROOT','REPORT_V2_ROOT','REPORT_V2_SNAPSHOT_ROOT','EMAIL_FILE_PATH']}))")
        out = subprocess.run([sys.executable, "-c", code], cwd=BASE_DIR, env=env, capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_default_deployment_settings_are_unchanged(self):
        values = self.load({"DJANGO_DEBUG": "False"})
        self.assertEqual(values["SECURE_SSL_REDIRECT"], "True")
        self.assertEqual(values["SESSION_COOKIE_SECURE"], "True")
        self.assertEqual(values["CSRF_COOKIE_SECURE"], "True")
        self.assertEqual(values["SECURE_HSTS_SECONDS"], "31536000")
        self.assertEqual(Path(values["STATIC_ROOT"]), BASE_DIR / "staticfiles")
        self.assertEqual(Path(values["MEDIA_ROOT"]), BASE_DIR / "media")
        self.assertEqual(values["REPORT_V2_ROOT"], "None")
        self.assertEqual(values["EMAIL_FILE_PATH"], "None")

    def test_local_profile_uses_http_loopback_defaults_and_private_paths(self):
        root = Path("synthetic-local-root").resolve()
        values = self.load({
            "DJANGO_DEBUG": "False", "PRIMER_LOCAL_DEV": "True",
            "DJANGO_STATIC_ROOT": str(root / "static"), "DJANGO_MEDIA_ROOT": str(root / "media"),
            "REPORT_V2_ROOT": str(root / "reports"), "REPORT_V2_SNAPSHOT_ROOT": str(root / "snapshots"),
            "EMAIL_FILE_PATH": str(root / "mail"),
        })
        self.assertEqual(values["SECURE_SSL_REDIRECT"], "False")
        self.assertEqual(values["SESSION_COOKIE_SECURE"], "False")
        self.assertEqual(values["CSRF_COOKIE_SECURE"], "False")
        self.assertEqual(values["SECURE_HSTS_SECONDS"], "0")
        self.assertEqual(Path(values["STATIC_ROOT"]), root / "static")
        self.assertEqual(Path(values["REPORT_V2_ROOT"]), root / "reports")
        self.assertEqual(Path(values["REPORT_V2_SNAPSHOT_ROOT"]), root / "snapshots")
        self.assertEqual(Path(values["EMAIL_FILE_PATH"]), root / "mail")
