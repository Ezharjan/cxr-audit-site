# Completed — configurable upload handling

Implemented the 261009-vmg plan across Django upload/configuration/report integration, authenticated jobs and the existing FastAPI processor. Supporting migration only; no deployment, production data read or inference backend change.

Evidence: Django full regression run exercised 592 tests (one pre-existing skip). Two obsolete fixture expectations were corrected and their affected test classes rerun successfully. FastAPI 13 tests passed; report JavaScript 90 tests passed. New upload tests include bounded/encrypted workbook parsing, validation/merge, exclusive API manifests, ownership/CSRF, atomic rollback/replay, report semantic snapshots, migration preservation and real Chromium interaction. Linux synthetic isolation self-check passed filesystem denial, network denial and exact identifier preservation. makemigrations --check and JS syntax/diff checks passed.

Contract and handoff: ../261009-vmg-write-sol-implementation-plan-for-config/FIELD-CONTRACT.md and IMPLEMENTATION-SUMMARY.md. Administration/rollout/rollback: docs/upload-configuration.md. API caller migration: docs/agent-api.md.

Unverified deployment gates: PostgreSQL concurrency, Docker/Nginx and production-kernel limits, real vLLM synthetic end-to-end run. Windows parser isolation is development-only. Remote cleanup on network failure requires task administration. No automatic deployment/migrations/seeds/restarts.
