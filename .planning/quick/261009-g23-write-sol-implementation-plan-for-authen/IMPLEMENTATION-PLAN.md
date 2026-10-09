---
status: implemented
production_verification: pending_docker_nginx_postgres
executor: Sol
created: 2026-10-09
scope: authenticated routine-workflow API and admin configuration
---

# Sol implementation plan: PRIMER agent API

## Execution contract

Implement this plan through the repository GSD workflow, in the ordered slices below. This document is a handoff, not evidence of implementation. Inspect current callers before extracting shared logic; preserve manual upload, Report V2, print and HTML email behavior. Use synthetic fixtures only. Do not enable the API in an existing deployment automatically.

Repository discovery on 2026-10-09: this checkout itself is the Git root, containing django-app/, cxr-audit-api/, .git/ and .planning/. AGENTS.md references an older enclosing django/ directory. Use the actual existing repository root; do not create a nested django repository. Reconfirm git status before execution and preserve unrelated edits.

## Locked user decisions

- Authenticated upload/start processing, job status, published report list, PDF download, and email PDF only.
- Routes are /api/{project}/... with no version or literal projects segment.
- Authenticate with X-API-Key. Separate these credentials from the internal Django-to-FastAPI key.
- Keys are restricted by project, NOT by report. Do not add report ACLs or an operation-permission matrix in this release: a valid key has all five operations within its assigned projects.
- Reports always use the current published definition's defaults. No API filters, dates, cohort overrides, layout changes, saved browser preferences, or report edits. Modified reports remain manual.
- Email recipients are supplied in JSON. No recipient allowlist or mailing-list implementation in this release. A future mailing-list ID may resolve recipients internally.
- Add an admin-only page labelled API with global enable/disable and management of the signed-in user's own keys.
- Single production project today: prime. This release does not enable a second project or implement project data partitioning.

## Grounded integration map

- django-app/lunit_audit/urls.py: public route mounting; report V2 lives at /report/.
- django-app/upload/views.py: analyze_auto_sort, analyze_multiple, _get_active_task, _start_background_worker, _background_poll_and_save, _background_fetch_and_save, save_results_to_database, get_api_headers.
- django-app/upload/models.py: ProcessingTask, UploadedFile, CXRStudy. Inspect actual fields and migrations before extending.
- django-app/report_v2/views.py: _PROJECT_ID = "prime", _evaluate, widget/default evaluation and published listing.
- django-app/report_v2/data.py: list_published, load_published_layout, fetch_project_rows. Current adapter reads CXRStudy.objects.all().
- django-app/report_v2/projects/registry.py: production registry and require_project_context; currently PRIME only.
- django-app/report_v2/exports.py: snapshot creation, print view model, HTML email, recipient validation and replay handling. Current snapshot request requires browser signed widget contexts; agents must not construct those.
- django-app/report_v2/snapshots.py and templates/report_v2/print.html: reuse frozen export representation and print design; current print uses window.print(), not PDF bytes. Print charts currently become tables; preserve that output for API PDFs.
- django-app/report_v2/permissions.py: reuse current admin predicate (superuser or admins group).
- django-app/audit/: inspect emitter/model/router and use existing audit channel; never invent parallel audit storage.
- django-app/templates/base.html and existing navigation includes: add API to the appropriate admin settings group using house styling.
- django-app/requirements.txt, Dockerfile, docker-compose.yml, nginx/nginx.conf, .env.example, .env.example-prod: renderer packaging, proxy limits/timeouts and deployment documentation as necessary. Do not read real .env or datasets.

## API contract

Use the exact routes below without trailing slash redirects, especially on POST. No session-auth fallback on these endpoints. Session-auth admin configuration remains a separate surface at /settings/api/.

| Method | Route | Input | Success |
|---|---|---|---|
| POST | /api/{project}/jobs | multipart repeated files; optional supplemental_steps boolean, default false | 202: job_id, status, status_url |
| GET | /api/{project}/jobs/{job_id} | none | 200: job_id, project, status, progress_percent, safe error code when applicable |
| GET | /api/{project}/reports | none | 200: reports containing slug, title, published version, pdf_url, email_url |
| GET | /api/{project}/reports/{report_slug}/pdf | none | 200 application/pdf, attachment filename |
| POST | /api/{project}/reports/{report_slug}/email | JSON recipients: nonempty array of email addresses | 200: status accepted_by_mail_server, request_id |

Use automatic file sorting for the initial upload contract; reuse the existing processing path. Preserve manual explicitly grouped uploads. Validate missing/empty files, filenames, allowed formats and bounded file count/total bytes before forwarding; document concrete bounds selected from current deployment controls. Validate supplemental_steps strictly, not arbitrary truthy strings. Reject unknown fields and report query parameters with 400; never silently accept overrides. Paths carry project selection only.

Email is synchronous for this release: validate <=20 recipients using Django email validation, render one PDF and attach as application/pdf. Use BCC (with a configured sender/to address) to avoid revealing recipients to each other. Success means SMTP accepted, NOT delivered. Safe 502/504 on known transport failures; do not expose recipients or SMTP diagnostics in response bodies. No queue/Celery solely for this feature. Set bounded renderer and SMTP timeouts; verify proxy/worker timeouts fit. If measured render times cannot fit, record the evidence and adjust the plan rather than falsely returning queued without a durable queue.

All error responses are JSON with stable code and safe message. Missing/invalid/revoked/expired keys: 401; valid key outside project scope: 403; unknown project/report/job: 404 after authentication; disabled API: 503 api_disabled; busy/duplicate conflict: 409; invalid input: 400; oversized input: 413; bounded capacity: 429/503 with Retry-After where applicable. Never redirect API callers to login. Send Cache-Control: no-store for API responses and secret display.

## Slice 1: persistence, authentication and global switch

Create a small Django app agent_api (or reuse an existing equivalent discovered during preflight). Use normal Django models/views/JsonResponse; no DRF dependency needed.

Models:
- APIConfiguration: singleton row enforced by fixed primary key; enabled=False by default; changed_at/changed_by. Missing row means disabled. No import-time or worker-local cached enable flag.
- APIKey: owner FK, label, unique public key identifier, secret digest, created_at, required expires_at, revoked_at, last_used_at. Optional description is out of scope.
- APIKeyProject: key FK and project_id string, unique pair. Validate against production registry and explicit supported-project gate. Do not create a Project model as a substitute for the next milestone.
- APIJob association: opaque public UUID, ProcessingTask FK, project_id, owner FK, creator key FK, created_at. Do not expose old/global jobs without an explicit association. Preserve provenance after revocation.
- Durable idempotency record for POST operations, unique on key/project/operation/idempotency key, request digest, state, safe response reference and timestamps. See slice 3.

Key generation: secrets.token_urlsafe(32) or equivalent >=256-bit entropy, public ID prefix plus secret. Show the full value once on creation; persist a cryptographic digest only, compare in constant time. Never accept a public prefix alone. Avoid placing raw secrets in session, messages storage, audit, logs, URLs or hidden form fields. Use a POST response with no-store; refresh must not mint another key accidentally. List only label/prefix, project scopes, expiry, creation, last use and revocation status. Rotation is create replacement then revoke old, not recovery of the old secret.

Authenticate key ownership against an active user who still satisfies the admin predicate. Re-check owner status, revocation, expiry and global setting each request. All public API endpoints require keys even when a browser session exists. Exempt only these header-auth endpoints from CSRF as needed; never exempt the configuration page. Do not forward external keys to FastAPI.

Global disable blocks new API requests on every worker without restart; the configuration UI remains usable. Already accepted processing jobs continue through existing workers. Already-started PDF work may finish; recheck enabled/key validity immediately before email send and before upload dispatch. Document that disablement cannot retract email or external processing already dispatched. Disabling does not revoke keys; re-enabling restores still-valid keys.

Tests: default off; all five routes fail closed; malformed keys; expired/revoked keys; inactive/demoted owner; session alone rejected; cross-project denial; unknown project rejected even if registry later includes an unsupported adapter; hashes only at rest; no secret in response errors/audit; SQLite and PostgreSQL-compatible migrations.

## Slice 2: admin-only API configuration page

At /settings/api/, use house admin authorization on GET and every mutation, POST-only mutations and CSRF. Authenticated non-admin gets 403. Hide navigation for non-admins but do not rely on hiding it.

Two sections:
1. Global API status with enable/disable button, persistent current state, and a confirmation displaying effects.
2. Your API keys: create label/project scopes/expiry; show-once copyable secret; list metadata; revoke with explicit confirmation; replacement creation for rotation. Scope selection currently offers only prime. No viewing/managing another user's keys by guessing IDs, including another admin's keys. Server derives owner from request.user, never posted owner_id. Permit creating keys while disabled to prepare setup, showing that they will not work yet.

Warnings (plain language, visible before confirmation):
- Enable: "API keys allow automated uploads and access to all published reports in their selected projects, including emailing PDFs to addresses supplied by the caller. Reports may contain patient information. Only share keys with trusted systems over HTTPS."
- Disable: "New API requests will stop immediately. Processing already submitted will continue. Emails already sent cannot be recalled. Existing keys remain valid when the API is enabled again."
- Secret: "Copy this key now. It will not be shown again. Anyone holding it can use its selected projects."
- Revoke: "This key will stop working. Update any automation using it."

Do not include a filter/report selector or operation scopes. Default expiry UI to 90 days and permit selecting a future date; document this as an implementation default, not a user-specified requirement. API setting changes and key create/revoke emit sanitized audit events.

Tests: anonymous/nonadmin access and direct POST; CSRF; owner isolation; scope tampering; expiry validation; show-once behavior; enable/disable across separate requests; keyboard labels, focus and existing dark/light layouts. Check navigation and controls in browser using synthetic data.

## Slice 3: upload jobs and status

Extract only the minimum shared upload submission helper required from upload/views.py. Existing manual views keep their contracts and error handling. API authenticates and validates before calling it; do not call decorated views with fabricated requests or bypass decorators via __wrapped__.

Create the APIJob association and establish retry state around submission; avoid reporting success if tracking cannot persist. Preserve current single-active-task restriction across manual and API submissions and close the check/create race with a database-backed reservation; a Python lock is insufficient across Gunicorn workers. Do not hold a database transaction open during network upload. Record ambiguous upstream outcomes instead of blindly retrying. Inspect existing task states/recovery and add only the necessary coordination.

GET status must prove local job association and project before contacting the backend. Keys may access project jobs created by the same owner, including through replacement keys; another owner's jobs return 404. Document this job ownership rule separately from project-wide report access. Never accept arbitrary upstream task IDs. Map processing state to queued/processing/saving/completed/failed. completed means Django result persistence succeeded, not merely FastAPI finished. Do not return raw progress text, reports, filenames or backend exceptions. Preserve existing background save exactly once and expose safe failure if backend task state was lost on restart.

Require Idempotency-Key on upload and email POSTs; document length/character bounds. Atomically claim by key/project/operation/value; hash canonical validated inputs (stream file hashes for upload, not PHI payload storage). Matching retries return prior outcome without repeating effects; same key with changed payload returns 409; concurrent in-progress returns 409 with retry guidance. Retain records for a documented 7-day retry window, using existing maintenance mechanisms where available. An ambiguous dispatch/SMTP result remains unknown and must not automatically resend; exactly-once external delivery cannot be promised. Keep records free of raw file contents, PDF bytes and recipient lists.

Tests: synthetic multipart success; malformed/missing files; size bounds; backend failure and timeout; duplicate/concurrent submission across separate DB connections; manual/API active-job collision; owner/project mismatch; completed only after save; worker failure; idempotency replay/conflict/unknown outcome. Mock inference, never send clinical data.

## Slice 4: default-only PDF and email

Share the existing report default evaluation/snapshot construction with a small internal function accepting explicit authenticated owner/project/report, no override parameters. Pin the published version for the whole render and resolve relative defaults once per request. Browser signed contexts remain required on existing browser mutation paths; do not weaken them to support the API. API authorization precedes definition lookup and study reads.

Evaluate configured widget defaults, freeze into the existing snapshot representation, and reuse _print_view_model plus print.html. Respect export visibility, default pagination and existing data-table chart representation. Ignore ReportPreference/browser state for export content. Unknown/unpublished reports fail before data evaluation. If any widget fails, fail the API export with a sanitized error; do not email a partial success-looking report. Leave existing manual fault isolation unchanged.

Renderer choice: prefer server-side Chromium printing of the existing print HTML via Playwright, unless an existing installed PDF renderer is found that faithfully handles this template. Verify current official renderer documentation and template assets before adding dependency. Pin a tested compatible renderer/browser pair and install runtime/browser dependencies in the Django image; run non-root. Render trusted template HTML from the frozen model with autoescaped data, embed/local-resolve required styles/fonts, and block arbitrary outbound requests and filesystem reads. Never navigate a user-provided URL or pass an API key into a page. Disable print dialog invocation in automated rendering context. Set print media, explicit paper/margins, timeout and bounded concurrent renders; clean browser processes and temporary files on success/failure. Do not disable browser sandbox merely to make deployment work without investigating supported non-root operation.

Download returns actual %PDF bytes with safe Content-Disposition; email calls the same PDF service and attaches those bytes. Separate calls intentionally use defaults/current data at each call; byte identity between a prior download and later email is not promised. No public snapshot parameter in this release. Prevent PDFs from entering static/media public storage or ordinary logs; use memory or private cleaned temporary storage. Avoid PDF byte retention beyond operation need.

Tests: published list and draft exclusion; strict no-overrides; configured defaults and relative-date pinning; preferences ignored; report version stable during render; widget failure blocks send; real synthetic PDF signature/page count/text and multipage layout; no arbitrary renderer network access; timeout/cleanup; mail backend attachment equals renderer output and recipients use BCC; retries do not resend; disable/revoke before dispatch blocks email. Preserve existing print and manual HTML email tests.

## Slice 5: deployment, documentation and final verification

- Mount routes and ensure reverse-generated URLs exactly match contracts. No CORS expansion unless a concrete client requires it.
- Align Nginx/Django/body and time limits; enforce HTTPS in production and document local development allowance. Keep FastAPI private and its key unchanged. Sanitize new API access/error/audit logs; no raw key, body, recipient or report content.
- Audit API requests/actions with owner, key public ID, project, report/job IDs, request ID, outcome, and recipient count for email. Use existing PHI-safe audit facility. Inspect middleware to avoid logging bodies automatically.
- Add a concise API guide with synthetic curl examples for all five routes, header authentication, idempotency, status meanings, published-default behavior, error codes, key lifecycle, disable semantics and SMTP acceptance vs delivery. No OpenAPI framework or MCP required; use a static schema only if useful within existing tooling.
- Run focused new tests plus existing upload, report export/snapshot, audit and navigation tests affected by extraction. Run Django checks/migration checks and synthetic SQLite/PostgreSQL concurrency coverage. Build the production image and generate one PDF within it as the non-root runtime user; verify health/startup.
- Test complete synthetic journey: API disabled -> admin creates scoped key -> enables -> upload -> poll through saved completion -> list reports -> download PDF -> email through a test mail sink -> revoke -> denied -> disable -> all five denied. Test with a second admin and a non-admin too. Do not send real external email.
- Produce execution summary listing commits, actual test commands/results, rendering limitations and unresolved risks. Keep API disabled for rollout; operator enables through UI after configuration.

## Suggested commit order and completion gate

1. API models/auth/config page with security tests.
2. Shared upload service, job association/status/idempotency with regression tests.
3. Default snapshot/PDF renderer and packaging with synthetic render tests.
4. Report listing/PDF email endpoints with retry/audit tests.
5. Documentation and full journey verification.

Use separate commits where slices are too large, but never mark an endpoint complete without its auth and negative tests. No destructive migrations or new project activation. All five endpoints, own-key admin page, global switch, actual PDF attachments, and focused regression evidence are required for completion.

## Explicit exclusions and risks

No MCP, schedules, mailing lists, report overrides, report-specific keys, cross-user key administration, extra project implementation, Celery, or redesign of manual reports. The current project registry does not isolate CXRStudy rows: hard reject every project except prime until the later project milestone implements storage and ingestion isolation. API enablement creates an ePHI export path to caller-supplied recipients; warnings do not replace authentication/audit. External dispatch cannot be made exactly-once by a local idempotency table. Existing in-process background worker restart limitations must be surfaced honestly rather than hidden behind success responses.
