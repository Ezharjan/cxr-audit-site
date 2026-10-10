# PRIMER routine workflow API

The API is disabled by default. An active administrator manages global API access and their own keys at **Admin → API** (`/settings/api/`). Each key grants the five routine operations for its selected projects, including access to every published report and email to caller-supplied recipients. Only `prime` is supported today. Project paths do not yet provide multi-project study isolation.

## Credentials and setup

Create a named key, select `prime`, and choose an expiry date (the form defaults to 90 days) or Never. Keys with Never expiry remain valid until revoked or their owner loses access. Copy the secret from the creation response; it cannot be recovered. Refreshing/resubmitting the same creation form does not create another key or reveal the secret again. Rotate by creating a replacement, updating automation and revoking the old key. Other administrators cannot view/manage your keys. Revocation, expiry, owner deactivation or removal of admin authority stops access.

Send `X-API-Key` on **every request** over HTTPS. Browser sessions do not authenticate API requests. Never put the key in URLs, source control or application logs. Internal Django/FastAPI credentials remain separate.

Global disablement stops new API calls without a restart. Existing processing continues; an email already dispatched cannot be recalled. Still-valid keys work when re-enabled. The API remains off until an administrator explicitly enables it.

## Routes

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/prime/jobs` | Validate explicit mappings, submit complete new studies or finish enrichment/no-op |
| GET | `/api/prime/jobs/{job_id}` | Status of a job owned by the key's user |
| GET | `/api/prime/reports` | Published report slugs, titles, versions and URLs |
| GET | `/api/prime/reports/{report_slug}/pdf` | Real PDF using current published defaults |
| POST | `/api/prime/reports/{report_slug}/email` | Email that default report as a PDF attachment |

Do not append trailing slashes. GET requests have no body or query parameters. Report APIs reject filters and overrides. User/browser report preferences never affect these exports. A published report may include study-level content if configured to export it.

Upload is multipart: repeated `files` fields, required `upload_manifest` JSON, and optional `supplemental_steps` (`true` or `false`, default `false`). The manifest is an ordered array: entry 0 describes multipart file 0, regardless of filename. Each entry contains **exactly one** of `format_id` or `column_mapping`. Duplicate JSON keys, duplicate metadata fields, unknown destinations and duplicate destination allocations reject the whole request. The API never infers aliases or autodetects a format.

Format IDs refer to enabled PRIME formats saved under **Admin → Upload settings**. Header sets must match exactly after surrounding whitespace is trimmed; order does not matter, case does. Explicit mapping objects map source headers to permitted stable field IDs; absent or empty destinations exclude columns. Mixed modes across files are supported:

```json
[{"format_id":"my-carpl"},{"column_mapping":{"anon_id":"accession_no","RIS_REPORT":"text_report","SITE":"workplace"}}]
```

Files combine by exact `anon_id`/`accession_no` before completeness checks. New studies require identifier, report text, workplace and the ten primary finding scores. Missing required inputs exclude/discard new studies; malformed supplied values, missing/invalid identifiers and conflicting uploaded values reject everything before dispatch. Raw Tuberculosis is optional; enabled supplemental extraction includes TB. Exact integer identifiers only: unsupported alphanumeric IDs, leading-zero tokens, fractional or overflow IDs and precision-lost spreadsheet numeric IDs reject. Format rules can declare date formats/timezones, value dictionaries, missing sentinels and score scale; no executable transformations.

Existing studies can fill approved independent missing annotations only. Populated values, zero and false remain unchanged; processing-dependent inputs and generated fields cannot be enriched through upload. Existing studies never automatically rerun LLM grading. Uploaded LLM/manual-GT outputs are forbidden destinations. Manual GT retains its dedicated workflow; historical import now returns 410 with migration guidance.

Limits: 1–20 nonempty UTF-8 CSV/XLS/XLSX files, 49 MiB combined, 50,000 total rows, 128 columns, 65,536 characters per cell, one worksheet, 128 MiB expanded/decrypted workbook size. Formulas/macros/external workbook links are unsupported. Parsing is bounded to 25 seconds per file and 60 seconds per submission, 20 CPU seconds and 768 MiB per worker. Nginx caps multipart requests at 50 MiB. Busy parsing/processing returns 409 and shares the manual/backfill reservation. See [upload rollout and platform gates](upload-configuration.md) before enabling this release.

Email JSON contains **only** `recipients`: 1–20 valid addresses. Addresses are sent as BCC, with `DEFAULT_FROM_EMAIL` as sender/to. The endpoint sends synchronously with a 20-second SMTP timeout. `accepted_by_mail_server` means accepted by the configured mail backend, not confirmed delivery. The HTTP caller must have a timeout sufficient for PDF rendering plus SMTP.

Each PDF pins the published definition and freezes its configured defaults and results. Existing print styles and chart data tables are reused; these are actual PDF bytes, not a browser print dialog. Case tables keep configured/default pagination and export policy. Widget evaluation errors block API export/email. Separate download/email requests use current data at their respective generation times and need not be byte-identical. Changed reports are exported manually.

## Synthetic examples

The examples use placeholder credentials and synthetic filenames. Save `my-carpl` and `my-ris` with the exact synthetic header sets before using the named-format upload example. Replace the base URL and load the key from your secret store. Do not disable TLS verification in production.

```sh
# Set PRIMER_API_KEY in the calling environment without printing it.
curl --fail-with-body 'https://primer.example/api/prime/jobs' \
  -H "X-API-Key: $PRIMER_API_KEY" \
  -H 'Idempotency-Key: routine-upload-2026-10-09' \
  -F 'files=@synthetic-carpl.csv' -F 'files=@synthetic-ris.xlsx' \
  -F 'upload_manifest=[{"format_id":"my-carpl"},{"format_id":"my-ris"}]' \
  -F 'supplemental_steps=false'

curl --fail-with-body 'https://primer.example/api/prime/jobs/JOB_UUID' \
  -H "X-API-Key: $PRIMER_API_KEY"

curl --fail-with-body 'https://primer.example/api/prime/reports' \
  -H "X-API-Key: $PRIMER_API_KEY"

curl --fail-with-body 'https://primer.example/api/prime/reports/REPORT_SLUG/pdf' \
  -H "X-API-Key: $PRIMER_API_KEY" -o routine-report.pdf

curl --fail-with-body 'https://primer.example/api/prime/reports/REPORT_SLUG/email' \
  -H "X-API-Key: $PRIMER_API_KEY" \
  -H 'Idempotency-Key: routine-email-2026-10-09' \
  -H 'Content-Type: application/json' \
  --data '{"recipients":["auditor@example.invalid"]}'
```

Upload with new complete studies returns 202 and `{job_id,status,status_url,counts}`. Enrichment-only, all-incomplete or unchanged submissions return 200 with `{status:"completed",counts}` and no job ID or LLM call. Invalid values return 400 with bounded errors/counts. Counts refer to distinct combined studies, including `total`, `new_complete`, `new_incomplete`, `existing_enrichable`, `existing_unchanged` and separate `invalid_raw_rows`. Poll `status_url`; states are `queued`, `processing`, `saving`, `completed`, `failed`. **Completed means the results were saved into Django.** Job IDs are opaque UUIDs; legacy backend IDs are not accepted. Replacement keys owned by the same user can access that user's jobs within the project. Another user's jobs return 404. Report access is project-wide.

## Retries, errors and capacity

Upload and email require `Idempotency-Key` (1–128 ASCII letters/digits or `_ . : -`). Reuse the same key only for retries of the same request, with the same API credential. Matching completed retries return the original result; changed input returns 409. Concurrent processing returns `operation_in_progress`. An ambiguous upload/SMTP dispatch returns a safe error; subsequent matching requests return `operation_outcome_unknown` instead of repeating an effect. Local retry tracking cannot promise exactly-once external delivery. Do not switch keys or generate a fresh retry ID after a timeout without reconciling the earlier operation.

The retry window is seven days. Run `python manage.py prune_api_idempotency` through your existing maintenance scheduler; it removes completed records older than seven days, while retaining unknown/in-progress records for investigation. After the window, clients must reconcile before retrying, especially if completed records have been pruned.

Errors are JSON: `{"error":{"code":"...","message":"..."}}`. API responses are `no-store` and include `X-Request-ID`; credential errors include `WWW-Authenticate: ApiKey`. Common status codes:

- 400 invalid input, unsupported fields/filters, missing idempotency key.
- 401 missing/invalid/expired/revoked key or inactive/non-admin owner.
- 403 valid key outside project scope, or insecure production transport.
- 404 unsupported project, unknown/unpublished report, foreign/unknown job.
- 409 processing busy, input conflict, in-progress or unknown retry outcome.
- 413 upload/PDF size limit; 422 report evaluation failed.
- 429 PDF capacity or proxy rate limit; honor `Retry-After`.
- 502/504 processor, renderer or SMTP failure/unknown outcome.
- 503 globally disabled, processor unavailable, or database/audit unavailable.

There is one concurrent submission/active-processing reservation shared with manual workflows and one PDF render slot across workers. Nginx allows 10 API requests/second per client IP with a burst of 20. Poll conservatively (e.g. every 5 seconds). API proxy read timeout is 110 seconds; Gunicorn's default remains 120 seconds. Upload upstream timeouts are 10 seconds connect / 75 seconds read. PDF subprocess timeout is 40 seconds with sandboxed Chromium, network/JavaScript blocked, private temporary profiles and cleanup. Safe errors contain no clinical text, filenames, recipient lists or raw credentials. API audit events record user, public key ID, project/report/job, request ID, outcomes and recipient count, not email contents.

## Deployment and verification

Django 5.1+ is required for the new database constraint API (verified locally on 5.2.15). Build the Django image with the pinned Playwright package/browser; Dockerfile installs Chromium runtime dependencies. Compose adds an init process, private 256 MiB shared memory and the pinned Playwright sandbox seccomp profile alongside `no-new-privileges`. PDF rendering runs as `appuser` with Chromium sandbox enabled. Host user namespaces/security policy must permit this; do not bypass the sandbox or add broad privileged capabilities to hide failures.

The vendored `django-app/chromium-seccomp.json` is from [Playwright v1.62.0](https://github.com/microsoft/playwright/blob/v1.62.0/utils/docker/seccomp_profile.json), SHA256 `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`, Apache-2.0 license in `chromium-seccomp.LICENSE`. It allows user-namespace creation on top of its Docker default syscall allowlist, following [Playwright's non-root sandbox guidance](https://playwright.dev/python/docs/docker). Keep this profile/browser pairing reviewed on upgrades. The profile still denies unspecified syscalls.

Before enabling the API on a deployment:

```sh
docker compose build django
docker compose run --rm --no-deps --user appuser --entrypoint python django manage.py check_api_pdf
docker compose run --rm --no-deps --entrypoint sh nginx -c 'nginx -t'
```

Perform the normal controlled deployment/migration process, confirm SMTP sender/settings, then enable in the API page. The API setting is in the database, not an environment toggle; a deployment update must not silently enable it. Additive migrations create API models and processing reservation/results-saved fields. Nginx API access logs and Gunicorn access logs omit query strings and headers.

For local development, install `django-app/requirements.txt` and `python -m playwright install chromium`. HTTP is permitted only with Django `DEBUG=True`. Run synthetic verification from `django-app`:

```sh
python -m pip install -r agent_api/test-requirements.txt
python manage.py test agent_api upload.tests report_v2.tests.test_print report_v2.tests.test_email report_v2.tests.test_snapshots report_v2.tests.test_navigation lunit_audit.tests --settings=agent_api.test_settings --noinput
python manage.py makemigrations --check --dry-run --settings=agent_api.test_settings
python manage.py check_api_pdf --settings=agent_api.test_settings
```

Synthetic settings isolate both databases and use a test mail backend. Real renderer tests require the pinned browser. PostgreSQL concurrency and non-root production-image smoke tests must also pass on the deployment host before rollout; never point test commands at real patient data.

## Operator reconciliation

The existing polling workers are in-process threads. They can stop on a Django restart; authorized job polling can restart monitoring, and a missing backend task is surfaced as failed. The internal processor's in-memory tasks still cannot survive its own restart. Manual local cancellation stops monitoring; the shared submission reservation stays occupied until the backend confirms the earlier task has stopped.

An ambiguous upload keeps the reservation blocked. First inspect/reconcile the backend and local task records using authorized admin tools; only when no processing remains, run:

```sh
python manage.py reset_submission_reservation --confirmed-backend-idle
```

This does not resend the upload or clear unknown retry outcomes. Keep unknown idempotency records for reconciliation. Disablement/revocation cannot undo effects already dispatched. Do not activate a second project until ingestion/storage partitioning and authorization tests exist.


### Upload client migration

Older clients without `upload_manifest` receive `upload_manifest_required` (400); there is no implicit legacy fallback. Add the ordered per-file manifest, handle both 202 jobs and terminal 200 counts, and show incomplete discard counts. Request digests cover project, file order/name/bytes, mapping modes and supplemental steps. First acceptance privately pins effective format/field revisions; accepted same-key retries replay the original response even after defaults change. They never reinterpret existing requests under new settings.
