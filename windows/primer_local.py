"""PRIMER-LLM Windows native local launcher (development profile only).

Run through the .cmd wrappers in this folder after ``conda activate cg``.
Standard library only, so ``check-deps`` and ``init`` work before any project
dependency is installed. Never prints secrets; never installs packages.

Commands:
  check-deps   Check Python and package versions; print a minimal pip plan.
  init         Create the private local config with generated secrets.
  setup        Migrate SQLite DBs, collect static files, seed upload formats,
               publish packaged report layouts.
  create-admin Create a Django superuser interactively (password is typed, not passed).
  start        Start FastAPI and Django on 127.0.0.1 and wait for health.
  stop         Stop the processes this launcher started (identity-checked).
  status       Process, port and HTTP health summary.
  doctor       Full diagnostics, including the parser self-test.
  ollama       Check the local Ollama endpoint and model.
  llm-smoke    Real inference smoke test on synthetic reports (no mock).
  e2e          Synthetic upload -> grading -> viewer -> report -> CSV run.
  collect-logs Zip redacted logs and diagnostics for troubleshooting.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

EXPECTED_CONDA_ENV = "cg"
REPO = Path(__file__).resolve().parent.parent
DJANGO_DIR = REPO / "django-app"
API_DIR = REPO / "cxr-audit-api"
WINDOWS_DIR = REPO / "windows"
IS_WINDOWS = os.name == "nt"

OLLAMA_ROOT = "http://127.0.0.1:11434"
OLLAMA_BASE_URL = OLLAMA_ROOT + "/v1"
DEFAULT_MODEL = "qwen3:4b"
LOOPBACK = "127.0.0.1"

# Keys a user may edit in local.env. Anything else is ignored (with a warning)
# so the config file cannot switch databases, bind addresses or disable auth.
EDITABLE_KEYS = {
    "DJANGO_SECRET_KEY": None,
    "API_SECRET_KEY": None,
    "OLLAMA_MODEL": DEFAULT_MODEL,
    "OLLAMA_MAX_WORKERS": "1",
    "LLM_DISABLE_THINKING": "False",
    "DJANGO_PORT": "8000",
    "API_PORT": "1221",
    "XLSX_DECRYPT_PASSWORD": "",
    "DEFAULT_FROM_EMAIL": "PRIMER-LLM Local <primer@localhost>",
}
SECRET_KEYS = ("DJANGO_SECRET_KEY", "API_SECRET_KEY", "XLSX_DECRYPT_PASSWORD")
# Inherited variables with these prefixes are dropped before the launcher's own
# values are applied, so a stale shell variable cannot redirect a service.
SCRUB_PREFIXES = ("DJANGO_", "DATABASE_", "AUDIT_", "OLLAMA_", "API_", "LLM_", "PARSER_",
                  "EMAIL_", "CXR_", "REPORT_V2_", "XLSX_", "FASTAPI_", "PRIMER_", "GUNICORN_")

# (distribution, import name, minimum version, used by)
REQUIRED = [
    ("Django", "django", "5.1", "web"),
    ("whitenoise", "whitenoise", "6.0", "web"),
    ("requests", "requests", "2.31", "web+api"),
    ("jsonschema", "jsonschema", "4.17", "web reports"),
    ("PyYAML", "yaml", "6.0", "web reports"),
    ("pandas", "pandas", "2.0", "web+api"),
    ("numpy", "numpy", "1.24", "web+api"),
    ("openpyxl", "openpyxl", "3.1", "parser"),
    ("xlrd", "xlrd", "2.0.1", "parser"),
    ("msoffcrypto-tool", "msoffcrypto", "5.0", "parser"),
    ("olefile", "olefile", "0.46", "parser"),
    ("scikit-learn", "sklearn", "1.3", "api"),
    ("statsmodels", "statsmodels", "0.14", "api"),
    ("matplotlib", "matplotlib", "3.7", "api"),
    ("openai", "openai", "1.40", "api LLM"),
    ("tqdm", "tqdm", "4.65", "api"),
    ("fastapi", "fastapi", "0.104.1", "api"),
    ("uvicorn", "uvicorn", "0.24.0", "api"),
    ("python-multipart", None, "0.0.6", "api"),
    ("pydantic", "pydantic", "2.0", "api"),
    ("json-repair", "json_repair", "0.7.0", "api LLM"),
]
OPTIONAL = [
    ("playwright", "playwright", "1.62.0", "Agent API server-side PDF only (needs a separate Chromium download)"),
]


# --------------------------------------------------------------------------- output

def say(message=""):
    print(message, flush=True)


def ok(message):
    say(f"  [OK]   {message}")


def warn(message):
    say(f"  [WARN] {message}")


def fail(message):
    say(f"  [FAIL] {message}")


class LauncherError(Exception):
    pass


# --------------------------------------------------------------------------- environment guard

def require_cg():
    """Stop unless this interpreter is the activated Conda env ``cg``."""
    name = os.environ.get("CONDA_DEFAULT_ENV", "")
    prefix = os.environ.get("CONDA_PREFIX", "")
    problems = []
    if name != EXPECTED_CONDA_ENV:
        problems.append(f"CONDA_DEFAULT_ENV={name or '(未设置)'}，需要 {EXPECTED_CONDA_ENV}")
    if not prefix:
        problems.append("CONDA_PREFIX 未设置")
    elif os.path.normcase(os.path.realpath(sys.prefix)) != os.path.normcase(os.path.realpath(prefix)):
        problems.append("当前 Python 不属于已激活的 Conda 环境 (sys.prefix != CONDA_PREFIX)")
    if problems:
        say("错误：必须在 Conda 环境 cg 中运行。请先执行：")
        say("    conda activate cg")
        for problem in problems:
            say(f"  - {problem}")
        raise SystemExit(3)


# --------------------------------------------------------------------------- versions

def version_tuple(text):
    parts = []
    for piece in re.split(r"[.+-]", str(text)):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts) or (0,)


def installed_version(dist):
    from importlib import metadata
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def importable(module):
    if module is None:
        return importlib.util.find_spec("python_multipart") is not None or importlib.util.find_spec("multipart") is not None
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def dependency_report():
    rows = []
    for dist, module, minimum, use in REQUIRED:
        version = installed_version(dist)
        status = "ok"
        if version is None or not importable(module):
            status = "missing"
        elif version_tuple(version) < version_tuple(minimum):
            status = "too_old"
        rows.append(dict(dist=dist, version=version, minimum=minimum, use=use, status=status))
    optional = []
    for dist, module, minimum, use in OPTIONAL:
        version = installed_version(dist)
        optional.append(dict(dist=dist, version=version, minimum=minimum, use=use,
                             status="ok" if version else "not_installed"))
    return rows, optional


def cmd_check_deps(args):
    say(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    python_ok = sys.version_info >= (3, 10)
    if not python_ok:
        fail("需要 Python 3.10 及以上（Django 5.1 要求）；Docker 部署使用 3.11")
    elif sys.version_info[:2] not in ((3, 11), (3, 12), (3, 13)):
        warn("已测试 Python 3.11/3.13；当前版本未在本仓库验证")
    else:
        ok("Python 版本可用")
    rows, optional = dependency_report()
    say("")
    say(f"  {'package':<18} {'installed':<12} {'minimum':<9} {'status':<8} used by")
    for row in rows:
        say(f"  {row['dist']:<18} {str(row['version'] or '-'):<12} {row['minimum']:<9} {row['status']:<8} {row['use']}")
    say("")
    say("可选组件（核心流程不需要）：")
    for row in optional:
        say(f"  {row['dist']:<18} {str(row['version'] or '-'):<12} {row['status']:<14} {row['use']}")
    needed = [r for r in rows if r["status"] != "ok"]
    say("")
    if not needed:
        ok("核心依赖齐全，无需安装任何包。")
    else:
        specs = " ".join(f'"{r["dist"]}>={r["minimum"]}"' for r in needed)
        say("最小安装方案（只安装缺失或低于下限的包；不会升级其他已满足的包）：")
        say("  1) 先预览将发生的变化（不安装）：")
        say(f"     python -m pip install --dry-run --upgrade-strategy only-if-needed {specs}")
        say("  2) 确认后在 cg 中安装：")
        say(f"     python -m pip install --upgrade-strategy only-if-needed {specs}")
        say("  如果 cg 主要用 conda 管理，也可以用 conda install 安装同名包，二选一，不要混装同一包。")
    if args.json:
        say(json.dumps({"python": sys.version.split()[0], "required": rows, "optional": optional}, ensure_ascii=False))
    return 0 if python_ok and not needed else 1


# --------------------------------------------------------------------------- config

def data_home():
    override = os.environ.get("PRIMER_LOCAL_HOME")
    if override:
        return Path(override).resolve()
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "PRIMER-LLM"
    return Path.home() / ".local" / "share" / "primer-llm-local"


def paths():
    home = data_home()
    return {
        "home": home,
        "config": home / "config" / "local.env",
        "db": home / "db",
        "media": home / "media",
        "static": home / "static",
        "audit_artifacts": home / "audit_artifacts",
        "reports": home / "private" / "report_v2",
        "snapshots": home / "private" / "report_v2_snapshots",
        "mail": home / "mail",
        "logs": home / "logs",
        "run": home / "run",
        "mpl": home / "matplotlib",
    }


def parse_env_file(path):
    values, ignored = {}, []
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LauncherError(f"{path.name} 第 {number} 行格式无效（需要 KEY=VALUE）")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in EDITABLE_KEYS:
            values[key] = value
        else:
            ignored.append(key)
    return values, ignored


def load_config(required=True):
    p = paths()
    if not p["config"].exists():
        if required:
            raise LauncherError(f"未找到本地配置 {p['config']}；请先运行 setup.cmd（或 primer.cmd init）")
        return None
    values, ignored = parse_env_file(p["config"])
    for key in ignored:
        warn(f"local.env 中的 {key} 不允许修改，已忽略（本地模式固定该值）")
    config = {k: (values.get(k) if values.get(k) is not None else v) for k, v in EDITABLE_KEYS.items()}
    for key in ("DJANGO_PORT", "API_PORT", "OLLAMA_MAX_WORKERS"):
        if not str(config[key]).isdigit():
            raise LauncherError(f"{key} 必须是整数")
    if int(config["DJANGO_PORT"]) == int(config["API_PORT"]):
        raise LauncherError("DJANGO_PORT 与 API_PORT 不能相同")
    for key in ("DJANGO_SECRET_KEY", "API_SECRET_KEY"):
        if len(config.get(key) or "") < 32:
            raise LauncherError(f"{key} 缺失或太短；删除 local.env 后重新运行 init 以重新生成")
    return config


def cmd_init(args):
    p = paths()
    if p["config"].exists():
        ok(f"本地配置已存在，未覆盖：{p['config']}")
        return 0
    p["config"].parent.mkdir(parents=True, exist_ok=True)
    template = (WINDOWS_DIR / "local.env.example").read_text(encoding="utf-8")
    text = template.replace("DJANGO_SECRET_KEY=", "DJANGO_SECRET_KEY=" + secrets.token_urlsafe(48), 1)
    text = text.replace("API_SECRET_KEY=", "API_SECRET_KEY=" + secrets.token_urlsafe(36), 1)
    fd = os.open(p["config"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    ok(f"已生成本地配置（含随机密钥，不在 Git 仓库内）：{p['config']}")
    return 0


def service_env(config):
    """Environment for every child process. Values never go on a command line."""
    p = paths()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SCRUB_PREFIXES)}
    django_port, api_port = int(config["DJANGO_PORT"]), int(config["API_PORT"])
    no_proxy = ",".join(filter(None, [env.get("NO_PROXY", ""), "127.0.0.1,localhost,::1"]))
    env.update({
        "PRIMER_LOCAL_DEV": "True",
        "DJANGO_SETTINGS_MODULE": "lunit_audit.settings",
        "DJANGO_DEBUG": "False",
        "DJANGO_SECRET_KEY": config["DJANGO_SECRET_KEY"],
        "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "DJANGO_CSRF_TRUSTED_ORIGINS": f"http://127.0.0.1:{django_port},http://localhost:{django_port}",
        "DJANGO_STATIC_ROOT": str(p["static"]),
        "DJANGO_MEDIA_ROOT": str(p["media"]),
        "DATABASE_ENGINE": "sqlite",
        "DATABASE_NAME": str(p["db"] / "db.sqlite3"),
        "AUDIT_DATABASE_ENGINE": "sqlite",
        "AUDIT_DATABASE_NAME": str(p["db"] / "audit.sqlite3"),
        "AUDIT_ARTIFACT_ROOT": str(p["audit_artifacts"]),
        "REPORT_V2_ROOT": str(p["reports"]),
        "REPORT_V2_SNAPSHOT_ROOT": str(p["snapshots"]),
        "EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend",
        "EMAIL_FILE_PATH": str(p["mail"]),
        "DEFAULT_FROM_EMAIL": config["DEFAULT_FROM_EMAIL"],
        "CXR_API_IP": LOOPBACK,
        "CXR_API_PORT": str(api_port),
        "API_BIND_HOST": LOOPBACK,
        "API_BIND_PORT": str(api_port),
        "API_SECRET_KEY": config["API_SECRET_KEY"],
        "API_REQUIRE_AUTH": "True",
        "FASTAPI_CORS_ORIGINS": "",
        "OLLAMA_BASE_URL": OLLAMA_BASE_URL,
        "OLLAMA_API_KEY": "ollama",
        "OLLAMA_MODEL": config["OLLAMA_MODEL"],
        "OLLAMA_MAX_WORKERS": str(config["OLLAMA_MAX_WORKERS"]),
        "LLM_DISABLE_THINKING": config["LLM_DISABLE_THINKING"],
        "LLM_ALLOW_INSECURE_TRANSPORT": "False",
        "XLSX_DECRYPT_PASSWORD": config["XLSX_DECRYPT_PASSWORD"],
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(API_DIR),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(p["mpl"]),
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    })
    return env


def ensure_dirs():
    for key, path in paths().items():
        if key != "config":
            path.mkdir(parents=True, exist_ok=True)


def secret_values(config):
    return [config[k] for k in SECRET_KEYS if config and config.get(k)]


def redact(text, config=None):
    for value in secret_values(config or {}):
        if len(value) >= 4:
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"(?i)((?:secret|password|api[_-]?key|token)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&]+", r"\1[REDACTED]", text)


# --------------------------------------------------------------------------- subprocess helpers

def manage(config, *arguments, capture=True, timeout=600, stdin=None):
    command = [sys.executable, "manage.py", *arguments]
    kwargs = dict(cwd=DJANGO_DIR, env=service_env(config), timeout=timeout)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                      text=True, encoding="utf-8", errors="replace")
    return subprocess.run(command, **kwargs)


def helper(config, *arguments, timeout=300):
    return subprocess.run([sys.executable, str(WINDOWS_DIR / "local_tasks.py"), *arguments],
                          cwd=DJANGO_DIR, env=service_env(config), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def run_step(title, completed, config):
    output = redact(completed.stdout or "", config).strip()
    if completed.returncode == 0:
        ok(title)
        return True
    fail(f"{title}（退出码 {completed.returncode}）")
    if output:
        say("\n".join("        " + line for line in output.splitlines()[-25:]))
    return False


# --------------------------------------------------------------------------- HTTP

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url, headers=None, timeout=5):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return None, b""


def port_free(port):
    # A connect probe catches listeners on 0.0.0.0, which Windows may let a
    # second socket shadow on 127.0.0.1 without a bind error.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        if probe.connect_ex((LOOPBACK, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not IS_WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((LOOPBACK, port))
            return True
        except OSError:
            return False


def port_owner_hint(port):
    if not IS_WINDOWS:
        return ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            return f"（占用进程 PID {parts[4]}，可用 tasklist /FI \"PID eq {parts[4]}\" 查看）"
    return ""


# --------------------------------------------------------------------------- process identity

def process_identity(pid):
    """(creation stamp, executable path) for a live process, or None."""
    if IS_WINDOWS:
        return _windows_identity(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return int(fields[19]), os.path.realpath(f"/proc/{pid}/exe")
    except (OSError, ValueError, IndexError):
        return None


def _windows_identity(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return created, os.path.realpath(buffer.value)
    finally:
        kernel.CloseHandle(handle)


def same_exe(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def pid_file(role):
    return paths()["run"] / f"{role}.json"


def read_pid(role):
    path = pid_file(role)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verified_pid(role):
    """Return the recorded PID only if the same process (start time + exe) is still alive."""
    record = read_pid(role)
    if not record:
        return None
    identity = process_identity(int(record["pid"]))
    if identity is None or identity[0] != record.get("created") or not same_exe(identity[1], record.get("exe", "")):
        return None
    return int(record["pid"])


def terminate(pid):
    if IS_WINDOWS:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process_identity(pid):
        time.sleep(0.2)
    if process_identity(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def rotate(path, limit=10 * 1024 * 1024):
    if path.exists() and path.stat().st_size > limit:
        backup = path.with_suffix(path.suffix + ".1")
        backup.unlink(missing_ok=True)
        path.rename(backup)


def spawn(role, command, cwd, config):
    logs = paths()["logs"]
    out_path, err_path = logs / f"{role}.out.log", logs / f"{role}.err.log"
    rotate(out_path)
    rotate(err_path)
    stamp = f"\n===== {_dt.datetime.now().isoformat(timespec='seconds')} start {role} =====\n"
    out = open(out_path, "a", encoding="utf-8")
    err = open(err_path, "a", encoding="utf-8")
    out.write(stamp)
    err.write(stamp)
    out.flush()
    err.flush()
    kwargs = dict(cwd=cwd, env=service_env(config), stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    finally:
        out.close()
        err.close()
    identity = None
    for _ in range(50):
        identity = process_identity(process.pid)
        if identity or process.poll() is not None:
            break
        time.sleep(0.05)
    if identity is None:
        raise LauncherError(f"{role} 进程启动后立即退出；请查看 {err_path}")
    pid_file(role).write_text(json.dumps({
        "pid": process.pid, "created": identity[0], "exe": identity[1], "role": role,
        "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")
    return process


def wait_http(url, process, seconds, headers=None, want=200):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        status, _ = http_get(url, headers=headers, timeout=3)
        if status == want:
            return True
        time.sleep(1)
    return False


def log_tail(role, config, lines=30):
    path = paths()["logs"] / f"{role}.err.log"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return redact("\n".join(text), config)


# --------------------------------------------------------------------------- checks used by start/doctor

def missing_core_packages():
    rows, _ = dependency_report()
    return [r["dist"] for r in rows if r["status"] != "ok"]


def migrations_applied(config):
    results = []
    for database in ("default", "audit"):
        completed = manage(config, "migrate", "--check", "--noinput", f"--database={database}", timeout=300)
        results.append((database, completed.returncode == 0, completed))
    return results


def ollama_status(model):
    status, body = http_get(OLLAMA_BASE_URL + "/models", timeout=5)
    if status != 200:
        return {"reachable": False, "status": status}
    try:
        models = [m.get("id") for m in json.loads(body).get("data", [])]
    except ValueError:
        models = []
    version_status, version_body = http_get(OLLAMA_ROOT + "/api/version", timeout=5)
    version = None
    if version_status == 200:
        try:
            version = json.loads(version_body).get("version")
        except ValueError:
            pass
    aliases = {model, model + ":latest"} if ":" not in model else {model}
    return {"reachable": True, "models": models, "model_present": bool(aliases & set(models)), "version": version}


def report_ollama(config):
    model = config["OLLAMA_MODEL"] if config else DEFAULT_MODEL
    state = ollama_status(model)
    if not state["reachable"]:
        warn(f"Ollama 未响应 {OLLAMA_BASE_URL}（请启动 Windows 版 Ollama）。上传分级会失败，其余页面可用。")
        return False
    ok(f"Ollama 可访问 {OLLAMA_BASE_URL}" + (f"，版本 {state['version']}" if state["version"] else ""))
    if not state["model_present"]:
        warn(f"未找到模型 {model}。请在命令行执行：ollama pull {model}")
        return False
    ok(f"模型 {model} 已在本机")
    if model not in ("qwen3:4b", "qwen3:8b"):
        warn(f"当前模型 {model} 未在本适配中评估；建议 qwen3:4b（先）或 qwen3:8b（后）")
    if str(config.get("OLLAMA_MAX_WORKERS", "1")) != "1":
        warn("OLLAMA_MAX_WORKERS 不是 1；8 GB 显存建议保持并发 1")
    return True


# --------------------------------------------------------------------------- commands

def cmd_setup(args):
    cmd_init(args)
    config = load_config()
    ensure_dirs()
    missing = missing_core_packages()
    if missing:
        fail("缺少核心依赖：" + ", ".join(missing) + "。请先运行 check-deps.cmd 查看最小安装方案。")
        return 1
    steps = [
        ("Django 配置检查 (manage.py check)", ("check",)),
        ("迁移主数据库 SQLite", ("migrate", "--noinput")),
        ("迁移审计数据库 SQLite", ("migrate", "--noinput", "--database=audit")),
        ("收集静态文件", ("collectstatic", "--noinput")),
        ("初始化上传格式配置", ("configure_uploads",)),
    ]
    for title, arguments in steps:
        if not run_step(title, manage(config, *arguments), config):
            return 1
    if not run_step("发布内置报表布局（overview/showcase）", helper(config, "publish-reports"), config):
        return 1
    count = helper(config, "admin-count")
    if count.returncode == 0 and count.stdout.strip().splitlines()[-1:] == ["0"]:
        warn("还没有管理员账户。请运行 create-admin.cmd 创建（密码在控制台输入，不会出现在命令行或日志）。")
    say("")
    ok(f"本地数据目录：{paths()['home']}")
    return 0


def cmd_create_admin(args):
    config = load_config()
    say("将运行 Django createsuperuser；按提示输入用户名和密码（密码不回显）。")
    completed = manage(config, "createsuperuser", capture=False, timeout=None)
    return completed.returncode


def cmd_start(args):
    config = load_config()
    ensure_dirs()
    django_port, api_port = int(config["DJANGO_PORT"]), int(config["API_PORT"])
    say("启动前检查：")
    missing = missing_core_packages()
    if missing:
        fail("缺少核心依赖：" + ", ".join(missing) + "（运行 check-deps.cmd）")
        return 1
    ok("核心依赖可导入")
    running = {role: verified_pid(role) for role in ("api", "django")}
    if all(running.values()):
        ok(f"服务已在运行：http://127.0.0.1:{django_port}/")
        return 0
    if any(running.values()):
        fail("只有一个服务在运行；请先运行 stop.cmd 再启动")
        return 1
    for name, port in (("API", api_port), ("Django", django_port)):
        if not port_free(port):
            fail(f"{name} 端口 {LOOPBACK}:{port} 已被占用{port_owner_hint(port)}")
            return 1
    ok(f"端口 {api_port}/{django_port} 可用")
    check = manage(config, "check", timeout=300)
    if not run_step("Django 配置与本地模式安全检查", check, config):
        return 1
    for database, applied, _ in migrations_applied(config):
        if not applied:
            fail(f"数据库 {database} 有未应用的迁移；请运行 setup.cmd")
            return 1
    ok("两个 SQLite 数据库迁移已是最新")
    if not (paths()["static"] / "staticfiles.json").exists():
        fail("静态文件未收集；请运行 setup.cmd")
        return 1
    report_ollama(config)

    say("")
    say("启动服务（只监听 127.0.0.1）：")
    api = spawn("api", [sys.executable, "-u", "combined_server.py", "--api-only",
                        "--host", LOOPBACK, "--port", str(api_port)], API_DIR, config)
    if not wait_http(f"http://{LOOPBACK}:{api_port}/health", api, args.timeout):
        fail("FastAPI 未在限定时间内就绪。错误日志末尾：")
        say(log_tail("api", config))
        _stop_role("api")
        return 1
    status, _ = http_get(f"http://{LOOPBACK}:{api_port}/tasks", headers={"X-API-Key": config["API_SECRET_KEY"]})
    unauth, _ = http_get(f"http://{LOOPBACK}:{api_port}/tasks")
    if status != 200 or unauth != 403:
        fail(f"内部 API 认证检查异常（带密钥 {status}，无密钥 {unauth}）")
        _stop_role("api")
        return 1
    ok(f"FastAPI  http://{LOOPBACK}:{api_port}  （内部 API 密钥校验通过，无密钥请求被拒绝）")
    web = spawn("django", [sys.executable, "-u", "manage.py", "runserver", f"{LOOPBACK}:{django_port}",
                           "--noreload"], DJANGO_DIR, config)
    if not wait_http(f"http://{LOOPBACK}:{django_port}/login/", web, args.timeout):
        fail("Django 未在限定时间内就绪。错误日志末尾：")
        say(log_tail("django", config))
        _stop_role("django")
        _stop_role("api")
        return 1
    ok(f"Django   http://{LOOPBACK}:{django_port}/")
    say("")
    say(f"打开浏览器访问 http://127.0.0.1:{django_port}/  ；停止请运行 stop.cmd")
    say(f"日志目录：{paths()['logs']}")
    return 0


def _stop_role(role):
    record = read_pid(role)
    pid = verified_pid(role)
    if pid:
        terminate(pid)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and verified_pid(role):
            time.sleep(0.2)
        if verified_pid(role):
            fail(f"{role} (PID {pid}) 未能停止")
            return False
        ok(f"已停止 {role} (PID {pid})")
    elif record:
        warn(f"{role} 记录的 PID {record.get('pid')} 已不存在或已被其他程序复用，未终止任何进程")
    pid_file(role).unlink(missing_ok=True)
    return True


def cmd_stop(args):
    config = load_config(required=False)
    if config and verified_pid("api") and not args.force:
        status, body = http_get(f"http://{LOOPBACK}:{int(config['API_PORT'])}/tasks",
                                headers={"X-API-Key": config["API_SECRET_KEY"]})
        if status == 200:
            try:
                active = [t for t in json.loads(body).get("tasks", []) if t.get("status") in ("queued", "processing")]
            except ValueError:
                active = []
            if active:
                fail(f"有 {len(active)} 个分析任务仍在运行；等待完成后再停止，或使用 stop.cmd --force")
                return 1
    results = [_stop_role("django"), _stop_role("api")]
    return 0 if all(results) else 1


def cmd_status(args):
    config = load_config(required=False)
    if config is None:
        warn("尚未初始化本地配置（运行 setup.cmd）")
        return 1
    django_port, api_port = int(config["DJANGO_PORT"]), int(config["API_PORT"])
    healthy = True
    for role, port in (("api", api_port), ("django", django_port)):
        pid = verified_pid(role)
        if pid:
            ok(f"{role} 进程运行中 PID {pid}")
        else:
            healthy = False
            warn(f"{role} 未由本启动器运行" + ("" if port_free(port) else f"，但端口 {port} 被占用{port_owner_hint(port)}"))
    status, _ = http_get(f"http://{LOOPBACK}:{api_port}/health")
    (ok if status == 200 else fail)(f"API /health -> {status}")
    status_auth, _ = http_get(f"http://{LOOPBACK}:{api_port}/tasks", headers={"X-API-Key": config["API_SECRET_KEY"]})
    (ok if status_auth == 200 else fail)(f"API 认证请求 -> {status_auth}")
    status_web, _ = http_get(f"http://{LOOPBACK}:{django_port}/login/")
    (ok if status_web == 200 else fail)(f"Django /login/ -> {status_web}")
    healthy = healthy and status == 200 and status_auth == 200 and status_web == 200
    report_ollama(config)
    for role in ("api", "django"):
        tail = log_tail(role, config, lines=200)
        errors = [l for l in tail.splitlines() if re.search(r"Traceback|ERROR|Exception", l)]
        if errors:
            warn(f"{role}.err.log 最近有 {len(errors)} 行错误；运行 collect-logs.cmd 打包查看")
    return 0 if healthy else 1


def cmd_ollama(args):
    config = load_config(required=False) or dict(EDITABLE_KEYS, OLLAMA_MODEL=DEFAULT_MODEL)
    good = report_ollama(config)
    status, body = http_get(OLLAMA_ROOT + "/api/ps")
    if status == 200:
        try:
            for model in json.loads(body).get("models", []):
                size, vram = model.get("size") or 0, model.get("size_vram") or 0
                share = f"{100 * vram / size:.0f}%" if size else "?"
                say(f"  已加载 {model.get('name')}: 显存 {vram / 2**30:.1f} GiB / 总 {size / 2**30:.1f} GiB（GPU 占比 {share}）")
        except ValueError:
            pass
    return 0 if good else 1


def cmd_doctor(args):
    say("== 环境 ==")
    ok(f"CONDA_DEFAULT_ENV={os.environ.get('CONDA_DEFAULT_ENV')}  Python {sys.version.split()[0]}")
    ok(f"sys.executable = {sys.executable}")
    ok(f"仓库 = {REPO}")
    say("== 依赖 ==")
    missing = missing_core_packages()
    if missing:
        fail("缺少/过旧：" + ", ".join(missing) + "（运行 check-deps.cmd）")
    else:
        ok("核心依赖齐全")
    _, optional = dependency_report()
    for row in optional:
        warn(f"可选 {row['dist']}: {row['version'] or '未安装'} — {row['use']}")
    say("== 配置 ==")
    p = paths()
    try:
        config = load_config()
    except LauncherError as exc:
        fail(str(exc))
        return 1
    ok(f"local.env: {p['config']}")
    for key in SECRET_KEYS:
        value = config.get(key) or ""
        say(f"  {key}: {'已设置（' + str(len(value)) + ' 字符）' if value else '未设置'}")
    say(f"  OLLAMA_MODEL={config['OLLAMA_MODEL']}  OLLAMA_MAX_WORKERS={config['OLLAMA_MAX_WORKERS']}  "
        f"LLM_DISABLE_THINKING={config['LLM_DISABLE_THINKING']}")
    say(f"  OLLAMA_BASE_URL={OLLAMA_BASE_URL}（固定）  Django 127.0.0.1:{config['DJANGO_PORT']}  API 127.0.0.1:{config['API_PORT']}")
    usage = shutil.disk_usage(p["home"] if p["home"].exists() else REPO)
    (ok if usage.free > 10 * 2**30 else warn)(f"数据目录所在磁盘剩余 {usage.free / 2**30:.1f} GiB")
    if (REPO / ".env").exists():
        warn("仓库根目录存在 .env；本地启动器不会读取它（仅 Docker Compose 使用）")
    if missing:
        return 1
    say("== Django ==")
    healthy = run_step("manage.py check（含本地模式安全检查）", manage(config, "check", timeout=300), config)
    for database, applied, _ in migrations_applied(config):
        (ok if applied else fail)(f"数据库 {database} 迁移" + ("已是最新" if applied else "未完成，运行 setup.cmd"))
        healthy = healthy and applied
    say("== 解析器自检（合成数据） ==")
    selftest = helper(config, "parser-selftest", timeout=300)
    say(redact(selftest.stdout or "", config).rstrip())
    (ok if selftest.returncode == 0 else fail)("解析器自检" + ("通过" if selftest.returncode == 0 else "失败"))
    healthy = healthy and selftest.returncode == 0
    say("== 服务 ==")
    cmd_status(args)
    say("== GPU ==")
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,memory.total,memory.used,driver_version",
                                  "--format=csv,noheader"], capture_output=True, text=True, timeout=15).stdout.strip()
            ok(out or "nvidia-smi 无输出")
        except (OSError, subprocess.SubprocessError):
            warn("nvidia-smi 调用失败")
    else:
        warn("未找到 nvidia-smi（不影响运行；Ollama 自行管理 GPU）")
    return 0 if healthy else 1


def cmd_llm_smoke(args):
    config = load_config()
    say("真实推理冒烟测试：调用本机 Ollama（不是 mock），只发送仓库内合成报告文本。")
    completed = subprocess.run([sys.executable, str(WINDOWS_DIR / "llm_smoke.py"), "--reports", str(args.reports)],
                               cwd=API_DIR, env=service_env(config), timeout=args.timeout)
    return completed.returncode


def cmd_e2e(args):
    config = load_config()
    command = [sys.executable, str(WINDOWS_DIR / "e2e_synthetic.py"),
               "--base-url", f"http://{LOOPBACK}:{int(config['DJANGO_PORT'])}",
               "--timeout", str(args.timeout)]
    env = dict(os.environ)
    env.update({"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PYTHONUTF8": "1"})
    return subprocess.run(command, cwd=REPO, env=env).returncode


def cmd_collect_logs(args):
    config = load_config(required=False)
    p = paths()
    p["logs"].mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = p["logs"] / f"primer-diagnostics-{stamp}.zip"
    diagnostics = subprocess.run([sys.executable, str(Path(__file__).resolve()), "doctor"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 env=dict(os.environ, PYTHONUTF8="1"), timeout=900)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doctor.txt", redact(diagnostics.stdout + diagnostics.stderr, config))
        for log in sorted(p["logs"].glob("*.log*")):
            data = log.read_bytes()[-2 * 1024 * 1024:].decode("utf-8", errors="replace")
            archive.writestr(f"logs/{log.name}", redact(data, config))
    ok(f"已生成（已脱敏密钥，仅含日志末尾 2 MB）：{target}")
    say("  注意：日志可能包含请求路径和合成登记号；分享前请自行检查。")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="PRIMER-LLM Windows local launcher")
    sub = parser.add_subparsers(dest="command", required=True)
    deps = sub.add_parser("check-deps")
    deps.add_argument("--json", action="store_true")
    sub.add_parser("init")
    sub.add_parser("setup")
    sub.add_parser("create-admin")
    start = sub.add_parser("start")
    start.add_argument("--timeout", type=int, default=120)
    stop = sub.add_parser("stop")
    stop.add_argument("--force", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("ollama")
    smoke = sub.add_parser("llm-smoke")
    smoke.add_argument("--reports", type=int, default=3)
    smoke.add_argument("--timeout", type=int, default=1800)
    e2e = sub.add_parser("e2e")
    e2e.add_argument("--timeout", type=int, default=1800)
    sub.add_parser("collect-logs")
    return parser


COMMANDS = {
    "check-deps": cmd_check_deps, "init": cmd_init, "setup": cmd_setup, "create-admin": cmd_create_admin,
    "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "doctor": cmd_doctor, "ollama": cmd_ollama,
    "llm-smoke": cmd_llm_smoke, "e2e": cmd_e2e, "collect-logs": cmd_collect_logs,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    require_cg()
    try:
        return COMMANDS[args.command](args)
    except LauncherError as exc:
        fail(str(exc))
        return 1
    except KeyboardInterrupt:
        say("已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
