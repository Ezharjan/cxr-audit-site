---
status: implemented
local_verification: passed
production_verification: pending
implementation_commit: a688ca1
completed: 2026-10-09
---

# Agent API implementation summary

Implemented the five agreed `/api/{project}/...` operations, X-API-Key authentication, project-only key scopes, admin-only global configuration and each administrator's own key lifecycle. The API is disabled by default. Only `prime` is supported. Reports use published defaults exclusively; server-generated PDFs reuse the frozen print representation and are attached to BCC email recipients supplied in JSON.

Implementation commit: `a688ca1` (`feat: add authenticated routine agent API and admin key management`). Application documentation: `docs/agent-api.md`.

## Changes and integration

- Added API configuration/key/project/job/idempotency/PDF-reservation models and additive migrations; secrets are hashed and shown once. Duplicate key creation form submission is guarded with a signed one-use identifier.
- Added the API page/navigation, enable/disable confirmations, expiry/last-use metadata, revocation and rotation instructions. Existing house admin rules, sessions and CSRF protect every configuration mutation.
- Extracted shared upload submission and transactional persistence. API/manual uploads/backfills share a database reservation. Completion follows saved results; stale progress/status polling cannot erase completed results. Local cancellation cannot free the reservation while the backend still runs. Terminal worker cleanup does not block a newly submitted job.
- Added strict default snapshot construction, bounded sandboxed Chromium PDF generation, network/JavaScript blocking, private temporary profile cleanup, PDF attachment email and durable retry outcomes. SMTP ambiguity never automatically resends.
- Added PHI-safe audit events, sanitized error responses, separate internal/public credentials, Nginx rate/size responses and query-free access logs. Production Chromium packaging, the official pinned sandbox seccomp profile, init/shared-memory settings and synthetic smoke/reconciliation commands are included.
- Preserved existing manual print, HTML email and report behavior. No second project, mailing list, filters, report-level permissions, MCP or queue framework added.

## Verification evidence

Commands ran from `django-app` unless noted:

1. `python manage.py test agent_api upload.tests report_v2.tests.test_print report_v2.tests.test_email report_v2.tests.test_snapshots report_v2.tests.test_navigation report_v2.tests.test_routes lunit_audit.tests --settings=agent_api.test_settings --noinput --verbosity=0` — **98 tests passed** in 45.309 seconds.
2. After final key-format/page styling changes: `python manage.py test agent_api.tests.APITests.test_key_expiration_revocation_owner_demotion_and_hash agent_api.tests.APITests.test_project_scope_and_no_overrides agent_api.test_browser --settings=agent_api.test_settings --noinput --verbosity=0` — **3 focused tests passed** in 5.670 seconds.
3. After the terminal worker-slot fix: `python manage.py test agent_api.tests.APITests.test_finished_worker_slot_does_not_block_next_job_monitor --settings=agent_api.test_settings --noinput --verbosity=0` — **1 focused test passed** in 1.293 seconds.
4. `python manage.py makemigrations --check --dry-run --settings=agent_api.test_settings` — no changes detected.
5. `python manage.py check --settings=agent_api.test_settings` — no issues.
6. `python manage.py check_api_pdf --settings=agent_api.test_settings` — sandboxed synthetic PDF rendered successfully (18,478 bytes).
7. Python compilation, Compose YAML and seccomp JSON parsing/assertions, Bash entrypoint syntax and `git diff --check` passed.

Coverage includes the complete synthetic admin create/enable -> upload -> saved completion -> report list -> real PDF download -> real PDF email -> revoke/disable journey. Mail was sent only to the test memory backend. Cross-user job/key access, revoked/expired/demoted/inactive owners, HTTPS, overrides, concurrent claims using separate SQLite connections, retries/ambiguous effects, audit failure, timeouts/cleanup and stale completion races were tested. Browser checks covered key lifecycle, both themes and a 390px mobile viewport. Synthetic screenshots in the OS temporary directory were visually inspected; they contain no real keys/patient data.

The Django test runner temporarily sets DEBUG=False and produced the existing LLM transport warning during suite startup. Standalone synthetic configuration checks were clean. An early existing report email test failed because its relative static path requires the django-app working directory; rerunning from that directory passed. No application workaround was added for that test harness issue.

## Remaining rollout checks and limitations

Docker/container and PostgreSQL runtime verification could not run: no usable Docker CLI/daemon, Nginx binary or PostgreSQL server was available in this Windows/WSL execution environment. Compose/seccomp parsing is not a substitute for a real image build and non-root Linux sandbox test. Run the documented deployment-host build/PDF/Nginx checks and PostgreSQL migration/concurrency suite before enabling the API. No deployment, live migrations, API enablement or real external email occurred.

Django minimum was raised from 5.0 to 5.1 for the `CheckConstraint(condition=...)` API; local verification used Django 5.2.15, Python 3.13 and Playwright 1.62.0. Production Python 3.11 image execution remains the pending host check.

The current project registry does not partition CXRStudy data, so the API rejects all projects except prime. Existing in-process workers and the processor's memory-only task store retain their restart limitations. PDF charts follow existing print output as data tables, and separate requests may reflect different dataset/publication times. Idempotency cannot guarantee exactly-once external dispatch; unknown outcomes require operator reconciliation. Host user-namespace policy must support the included non-root Chromium sandbox; never silently disable it.
