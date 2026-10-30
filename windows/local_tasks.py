"""Django-aware helper for the Windows local launcher.

Run by primer_local.py with the local-profile environment and cwd=django-app.
Uses only repository synthetic data.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DJANGO_DIR = REPO / "django-app"
SYNTHETIC = REPO / "docs" / "test-data" / "uploader"
sys.path.insert(0, str(DJANGO_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lunit_audit.settings")


def setup_django():
    if os.environ.get("PRIMER_LOCAL_DEV", "").lower() not in ("true", "1", "yes"):
        raise SystemExit("local_tasks.py only runs in the PRIMER_LOCAL_DEV profile")
    import django
    django.setup()


def publish_reports():
    """Publish the packaged overview/showcase layouts once; never overwrite edits."""
    setup_django()
    from django.conf import settings
    from report_v2.definitions.repository import DefinitionRepository
    from report_v2.seed import report_seed_text, seed_text

    root = Path(settings.REPORT_V2_ROOT)
    repository = DefinitionRepository(root, project_id="prime")
    for def_id, text in (("overview", report_seed_text()), ("showcase", seed_text("showcase.v1.yaml"))):
        if repository.is_deleted(def_id):
            print(f"{def_id}: deleted by an administrator; not recreated")
            continue
        if repository.get_current_version(def_id):
            print(f"{def_id}: already published; preserved")
            continue
        draft = root / "drafts" / def_id
        if draft.exists():
            if draft.read_text(encoding="utf-8") != text.replace("\r\n", "\n"):
                print(f"{def_id}: edited draft exists; publish it in the editor")
                continue
        else:
            repository.save_draft(def_id, text, expected_revision=None, create_only=True)
        repository.publish(def_id, draft.read_text(encoding="utf-8"))
        print(f"{def_id}: published")
    return 0


def admin_count():
    setup_django()
    from django.contrib.auth import get_user_model
    print(get_user_model().objects.filter(is_superuser=True).count())
    return 0


def _probe(code, timeout=60):
    """Run code in the same isolated interpreter mode the parser uses."""
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="primer-probe-") as directory:
        return subprocess.run([sys.executable, "-I", "-c", code], cwd=directory, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, creationflags=flags)


def parser_selftest():
    setup_django()
    from django.core.files.uploadedfile import SimpleUploadedFile
    from upload.parsing import parse_files

    results = []

    def check(name, passed, detail=""):
        results.append(passed)
        print(f"    {'PASS' if passed else 'FAIL'}  {name}" + (f" ({detail})" if detail else ""))

    socket_path = os.environ.get("PARSER_SOCKET_PATH")
    check("local profile uses the subprocess parser (no Unix socket)", not socket_path)

    def parse(name, data):
        return parse_files([SimpleUploadedFile(name, data)])

    try:
        parsed, digest = parse("carpl-valid.csv", (SYNTHETIC / "carpl-valid.csv").read_bytes())
        check("synthetic CARPL CSV parsed", len(parsed[0]["rows"]) == 6 and "Accession Number" in parsed[0]["headers"],
              f"{len(parsed[0]['rows'])} rows")
    except Exception as exc:  # report type only; never values
        check("synthetic CARPL CSV parsed", False, type(exc).__name__)
    try:
        parsed, _ = parse("ris-valid.xlsx", (SYNTHETIC / "ris-valid.xlsx").read_bytes())
        check("synthetic RIS XLSX parsed", len(parsed[0]["rows"]) == 6, f"{len(parsed[0]['rows'])} rows")
    except Exception as exc:
        check("synthetic RIS XLSX parsed", False, type(exc).__name__)
    for name, data, label in (("broken.xlsx", b"PK\x03\x04not-a-workbook", "corrupt XLSX rejected"),
                              ("synthetic.txt", b"a\n1\n", "unsupported extension rejected"),
                              ("dup.csv", b"a,a\n1,2\n", "duplicate headers rejected")):
        try:
            parse(name, data)
            check(label, False, "accepted")
        except ValueError:
            check(label, True)
        except Exception as exc:
            check(label, False, type(exc).__name__)

    worker = DJANGO_DIR / "upload"
    prefix = f"import sys; sys.path.insert(0, {str(worker)!r}); import parser_worker as p; p.limits(); "
    control = _probe(prefix + "print('limited')")
    check("resource limits can be applied", control.returncode == 0 and "limited" in control.stdout,
          f"exit {control.returncode}")
    memory = _probe(prefix + "b = bytearray(1536 * 1024 * 1024); print('allocated')")
    check("1.5 GiB allocation blocked (768 MiB limit)", memory.returncode != 0 and "allocated" not in memory.stdout,
          f"exit {memory.returncode}")
    if os.name == "nt":
        spawn = _probe(prefix + "import subprocess; subprocess.run([sys.executable, '-c', 'pass'], check=True); print('spawned')")
        check("child process creation blocked (Job Object)", spawn.returncode != 0 and "spawned" not in spawn.stdout,
              f"exit {spawn.returncode}")
        print("    NOTE  Windows has no filesystem/network sandbox for the parser (Linux uses Landlock + seccomp).")
    return 0 if all(results) else 1


COMMANDS = {"publish-reports": publish_reports, "admin-count": admin_count, "parser-selftest": parser_selftest}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit("usage: local_tasks.py " + "|".join(COMMANDS))
    sys.exit(COMMANDS[sys.argv[1]]())
