# Windows native local development mode

GSD slash commands were not available in the executing session; this quick task follows the existing `.planning/quick/` PLAN/SUMMARY convention manually.

Goal: run upload -> parse -> LLM grade -> save -> viewer -> report -> CSV natively on Windows 11 inside the existing Conda env `cg`, with SQLite, Django runserver, FastAPI/Uvicorn and local Ollama (`qwen3:4b`, concurrency 1). No WSL, Docker, VM, PostgreSQL, Redis, Node.js or new system software. Docker/Linux deployment behaviour stays unchanged unless explicitly opted in.

Scope:
1. `PRIMER_LOCAL_DEV` (explicit opt-in) in Django settings: HTTP-on-loopback cookie/redirect settings, path overrides outside the checkout, system checks that fail closed on non-loopback hosts/API/LLM, non-SQLite DBs, default Django secret, missing/weak API secret.
2. FastAPI: configurable bind host/port (default unchanged `0.0.0.0:1221`), local-dev startup guard (loopback + auth required + strong secret), platform temp dir instead of `/tmp`.
3. LLM structured output: SDK-version-neutral parse call, `<think>` stripping, schema-validated fallback (fix missing `re` import), optional Qwen3 `/no_think`.
4. Parser on Windows: keep subprocess + Job Object (memory, CPU) and add one-process limit; document missing filesystem/network isolation.
5. `windows/` launcher (stdlib only): cg guard, deps check (no auto install), config outside Git, setup/migrate, start/stop (PID + creation-time identity), health, doctor, Ollama check, real LLM smoke, log collection; `.cmd` wrappers.
6. Tests: unit tests for each changed control; cloud E2E with clearly labelled mock OpenAI-compatible server.
7. `WINDOWS_LOCAL.md` (Chinese) with verified / unverified / needs-local-verification table.

Risks: Windows-only code (Job Object, ctypes process identity, taskkill) cannot run in the Linux cloud; it is exercised by the local doctor/self-test. PDF via Playwright Chromium stays optional and unverified.
