---
status: complete-pending-local-verification
---
# Windows native local development mode

Added an explicit `PRIMER_LOCAL_DEV` profile and a stdlib-only launcher under `windows/` so the upload -> parse -> grade -> save -> viewer -> report -> CSV flow runs natively on Windows 11 in the existing Conda env `cg` with SQLite, Django runserver, FastAPI and local Ollama (`qwen3:4b`, concurrency 1). No new system software. Docker/Linux defaults are unchanged and asserted by tests. User guide: `WINDOWS_LOCAL.md` (Chinese).

Changes:
- Django settings: opt-in local profile (HTTP-on-loopback cookie/redirect/HSTS defaults), optional static/media/report/snapshot/email paths. New fail-closed system checks E101-E107 (loopback hosts/API/LLM, SQLite only, non-default Django secret, strong API secret, API auth required) and W110 (DEBUG).
- FastAPI: `--host/--port` (default `0.0.0.0:1221` unchanged), local startup guard, constant-time API key comparison, platform temp dir instead of `/tmp`.
- LLM `getLLMJSON`: SDK-neutral parse (openai>=2 removed `beta...parse`, which made structured output always fail), `<think>`/fence stripping, schema-validated fallback plus one repair (previously unvalidated and referenced un-imported `re`), opt-in Qwen3 `/no_think`.
- Windows parser Job Object: added one-active-process limit and no crash dialog.
- `windows/`: cg-guarded `.cmd` wrappers; check-deps (no installs, minimal pip plan), init (secrets outside Git), setup, create-admin, start/stop (PID + creation time + exe identity), status, doctor (parser self-test incl. memory/spawn probes), ollama, llm-smoke (real inference, labels mocks), e2e, collect-logs (redacted).

Verification (cloud Linux, not the user's machine):
- Django suite 617 tests: same 1 failure + 5 errors as the pre-change baseline (Playwright/Chromium absent x4; Landlock denies /etc/mime.types for Linux XLSX parsing on this CI kernel x2).
- FastAPI 29 and launcher 11 tests pass on Python 3.13 (openai 3.41, FastAPI 0.162, Django 6.1) and Python 3.11 (openai 1.40, FastAPI 0.104.1, Django 5.1).
- `windows/tests/cloud_e2e_mock.py` with the MOCK LLM: full launcher lifecycle and e2e 25/25 (re-run 21/21) in structured and think-leak modes on both stacks; no secrets in logs or diagnostics zip. Mock results are not real inference.

Not verified: any Windows execution (cmd, Job Object, taskkill, process identity), real Ollama/qwen3 inference and VRAM, XLSX end-to-end in cloud (Landlock), encrypted XLSX/XLS, Agent API PDF (Playwright Chromium not installed by design), browser print.

Found, not changed: `docs/test-data/uploader/README.md` still describes invalid rows as "Blocked"; current code excludes them and continues (07cc3b0). Linux parser Landlock blocks `/etc/mime.types` on hosts that have it (affects this CI kernel; Docker slim image likely unaffected).
