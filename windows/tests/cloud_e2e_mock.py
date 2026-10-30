"""CLOUD/CI harness: drive the real launcher commands against a MOCK LLM.

This is NOT the user's machine and NOT the Conda env "cg". The cg guard is
bypassed here (and only here) because the isolated CI interpreter is not cg;
every other launcher step runs unchanged. LLM answers come from
mock_llm_server.py, so a pass verifies the application flow only, never real
Ollama inference or model quality.

Usage: python windows/tests/cloud_e2e_mock.py [--mode structured|qwen-think]
"""
import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import primer_local as pl  # noqa: E402
import mock_llm_server  # noqa: E402


def step(title, code):
    print(f"\n>>> {title}: exit {code}", flush=True)
    if code != 0:
        raise SystemExit(f"CLOUD E2E FAILED at: {title}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("structured", "qwen-think"), default="structured")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data directory")
    args = parser.parse_args()
    print("=" * 72)
    print("CLOUD CI RUN — mock LLM, cg guard bypassed. Not a local Windows/Ollama verification.")
    print("=" * 72)
    home = tempfile.mkdtemp(prefix="primer-cloud-e2e-")
    os.environ["PRIMER_LOCAL_HOME"] = home
    pl.require_cg = lambda: print("[CLOUD CI] cg guard bypassed for this isolated test interpreter")
    mock = mock_llm_server.serve(11434, args.mode)
    try:
        step("check-deps", pl.main(["check-deps"]))
        step("setup (init + migrate + collectstatic + seed)", pl.main(["setup"]))
        config = pl.load_config()
        password = secrets.token_urlsafe(18)
        env = pl.service_env(config)
        env.update(DJANGO_SUPERUSER_USERNAME="synthetic-admin", DJANGO_SUPERUSER_PASSWORD=password,
                   DJANGO_SUPERUSER_EMAIL="synthetic-admin@example.invalid")
        created = subprocess.run([sys.executable, "manage.py", "createsuperuser", "--noinput"], cwd=pl.DJANGO_DIR,
                                 env=env, capture_output=True, text=True)
        step("create synthetic superuser (password via env, not argv)", created.returncode)
        step("start", pl.main(["start"]))
        step("status", pl.main(["status"]))
        step("start again is idempotent", pl.main(["start"]))
        os.environ.update(PRIMER_E2E_USER="synthetic-admin", PRIMER_E2E_PASSWORD=password)
        if sys.platform == "linux":
            # Pre-existing: the Linux parser's Landlock rules deny /etc/mime.types on this
            # CI host, so XLSX parsing cannot be verified here (Windows does not use Landlock).
            os.environ["PRIMER_E2E_KNOWN_XLSX_LIMITATION"] = "1"
        step("e2e synthetic (MOCK LLM)", pl.main(["e2e", "--timeout", "300"]))
        step("e2e re-run is idempotent", pl.main(["e2e", "--timeout", "300"]))
        step("llm-smoke against MOCK (must be labelled MOCK)", pl.main(["llm-smoke"]))
        doctor = pl.main(["doctor"])
        print(f"\n>>> doctor: exit {doctor} (parser XLSX check may fail on this CI kernel; see summary)")
        step("collect-logs", pl.main(["collect-logs"]))
        logs = list(pl.paths()["logs"].glob("primer-diagnostics-*.zip"))
        import zipfile
        with zipfile.ZipFile(logs[0]) as archive:
            blob = "".join(archive.read(n).decode("utf-8", "replace") for n in archive.namelist())
        leaked = [k for k in pl.SECRET_KEYS if config.get(k) and config[k] in blob] + (["admin password"] if password in blob else [])
        step("diagnostics zip contains no secrets", 1 if leaked else 0)
        log_text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in pl.paths()["logs"].glob("*.log"))
        step("service logs contain no secrets", 1 if any(config[k] in log_text for k in ("API_SECRET_KEY", "DJANGO_SECRET_KEY")) or password in log_text else 0)
    finally:
        pl.main(["stop", "--force"])
        mock.shutdown()
        print(f"\nMock LLM handled {mock_llm_server.Handler.calls} chat requests (mode={args.mode}).")
        if not args.keep:
            import shutil
            shutil.rmtree(home, ignore_errors=True)
    print("\nCLOUD E2E PASSED (mock LLM; application flow only)")


if __name__ == "__main__":
    main()
