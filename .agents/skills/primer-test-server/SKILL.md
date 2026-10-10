---
name: primer-test-server
description: Start, verify, or restart the local PRIMER Django test server on port 8766, including the older report preview helper, on this Windows workspace.
---

# PRIMER test server (8766)

Use PowerShell from the Git checkout containing `django-app/` and `.planning/`. This workspace is `\\truenas\ServerFiles\git\cxr-audit-site`; older planning references call it `django/`, but do not append that directory when it does not exist.

## Identify the server before restarting

```powershell
$listeners = @(Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)
$listeners | Select-Object LocalAddress, OwningProcess
$listeners.OwningProcess | Sort-Object -Unique | ForEach-Object {
    Get-CimInstance Win32_Process -Filter "ProcessId = $_" |
        Select-Object ProcessId, ExecutablePath, CommandLine
}
```

Two existing launchers live outside Git in `$env:TEMP`:

- `primer-full-preview.py`: full Django development server; use this for login, navigation, and application workflow checks.
- `primer-prime-preview.py`: limited report/layout HTTP harness with a mocked `synthetic-reviewer` administrator and CSRF bypass. Preserve this launcher when specifically restarting the report preview. It is not a complete application server or an authentication/security test.

Only stop a listener after confirming its executable and command line identify the intended helper at the expected path. If the owner is unrelated or cannot be identified, stop and report the conflict. Never kill all Python processes. No listener means startup is needed, not a kill.

## Start the full Django test server

The existing helper uses `agent_api.test_settings`, binds only `127.0.0.1:8766`, disables autoreload, migrates temporary databases, and creates or updates the `preview-admin` superuser. It takes the password from `PRIMER_PREVIEW_PASSWORD` and resets it each time it starts. Email stays in memory and the API target is localhost port 9; external analysis is unavailable.

```powershell
$repoRoot = (Get-Location).Path
$pythonExe = (Get-Command python -ErrorAction Stop).Source
$previewScript = Join-Path $env:TEMP 'primer-full-preview.py'
if (!(Test-Path -LiteralPath (Join-Path $repoRoot 'django-app/manage.py'))) { throw 'Run from the PRIMER checkout root.' }
if (!(Test-Path -LiteralPath $previewScript)) { throw 'Temporary preview launcher is missing; recover it before starting.' }
if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8766 is occupied; inspect its owner first.' }
& $pythonExe -c 'import django; print(django.get_version())'
if ($LASTEXITCODE -ne 0) { throw 'Choose a Python environment with the project dependencies installed.' }
$password = Read-Host 'Set the local preview-admin password' -AsSecureString
$env:PRIMER_PREVIEW_PASSWORD = [System.Net.NetworkCredential]::new('', $password).Password
try {
    $previewProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-u', ('"{0}"' -f $previewScript)) -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $env:TEMP 'primer-full-preview.out.log') -RedirectStandardError (Join-Path $env:TEMP 'primer-full-preview.err.log') -PassThru
} finally {
    Remove-Item Env:PRIMER_PREVIEW_PASSWORD -ErrorAction SilentlyContinue
}
$previewProcess.Id
```

The observed Python executable is `C:\ProgramData\anaconda3\python.exe`; resolve the current environment rather than assuming that path exists. For agent-driven startup, obtain the preview password from the user or use a newly generated local-only password and provide it privately; do not put it in command arguments, logs, commits, or this skill.

Temporary state lives under `$env:TEMP/primer-full-preview/`: `default.sqlite3`, `audit.sqlite3`, media, audit artifacts, and reports. Restart preserves that state. Use synthetic data only. Do not substitute deployment databases, copy clinical data into this directory, or delete it as part of a restart. The launcher is temporary and not versioned; if missing, recover or recreate it with these isolation properties before proceeding, rather than falling back to ordinary `manage.py runserver` against deployment settings.

## Restart and verify

After verifying the owner above, stop only its recorded process ID:

```powershell
Stop-Process -Id $verifiedPreviewProcessId -ErrorAction Stop
```

Confirm no listener remains on 8766, then repeat startup with the same helper and storage. The full Django launcher has no autoreload, so Python changes require a restart. Allow up to 30 seconds for startup, checking at one-second intervals; finish early on a successful HTTP response or process exit. Do not retry indefinitely.

```powershell
Invoke-WebRequest 'http://127.0.0.1:8766/accounts/login/' -UseBasicParsing | Select-Object StatusCode
```

Expect HTTP 200, then log in as `preview-admin` using the password set at startup and visit `/report/`. A successful login page alone does not verify report data or external analysis. If startup fails, inspect `$env:TEMP/primer-full-preview.err.log` and `.out.log`, and check the listener owner again. Do not publish this development server or bind it to `0.0.0.0`.

## Restart the older report preview when requested

Inspect `$env:TEMP/primer-prime-preview.py` before using it: the current helper sets a read-only URI for `django-app/db/db.sqlite3`. Its mocked identity does **not** guarantee synthetic report data. Do not use it with a PHI-bearing database; use the isolated full Django preview above for general test-server requests.

After confirming this helper is appropriate and stopping only its verified listener, use the startup block with `$previewScript` set to that helper and distinct `primer-prime-preview.out.log` / `.err.log` files. Omit the password steps; this harness has no real login. Check HTTP 200 at `/report/overview/`, `/report/showcase/`, or `/layout/editor/showcase/`. Its editor drafts persist separately at `$env:TEMP/primer-preview-editor-drafts/`; retain them on restart. Read its supported routes before assuming upload, viewer, or API workflows work.
