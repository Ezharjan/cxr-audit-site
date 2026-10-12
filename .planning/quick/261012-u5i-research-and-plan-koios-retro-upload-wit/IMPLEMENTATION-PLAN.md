# Extensible PRIMER uploader: PRIME-first implementation plan

Date: 2026-10-12
Status: confirmed implementation handoff; no application changes implemented
GSD quick task: 261012-u5i (original directory retained to preserve links)

## Confirmed scope

Build one project-aware backend uploader and shared page/template, implementing PRIME first. A project/study type owns its Django model; an individual study is a record, not a separate model class. Future projects may have related models and different identity/cardinality requirements. Koios is the next anticipated example, not the universal schema or an implementation requirement for this slice.

Developers define models, fixed validation rules, importable destinations, required inputs and processing steps in code. Administrators configure source formats, mappings, labels and presentation. Configuration cannot grant new storage or processing permissions.

The default page protects processed outputs. Admin-only /retro-upload uses the same mapping structure and exposes only llm_grade as an additional PRIME destination. This capability is shared across projects, with each future adapter defining its own approved outputs and completeness rules. Only PRIME is production-enabled initially.

Preserve valid supplied grades. Infer grades only for new accepted studies whose grade is missing. Derive gt_llm internally as int(llm_grade > 1); grade 1 is normal and grades 2-6 abnormal. Supplemental findings remain non-importable and run only when selected. Fully supplied grades with supplemental processing off make no LLM calls. Retain internally calculated lunit_binarised for legacy consumers; it is not importable.

Existing records: identical supplied values are a no-op; conflicting values and any attempt to fill an absent stored field hard-block the submission. No existing-record updates or LLM processing in this slice. Missing values absent from the upload do not assert a conflicting value. This policy applies to the shared normal UI, retro UI and routine upload API. It intentionally replaces current advisory stored disagreements and annotation enrichment. Filling missing existing fields and defining update/conflict policy are explicit future TODOs.

Final accepted writes are atomic across locally complete new records and new records requiring processing. Any required processing/validation/database failure means zero new study writes. Keep permission, parser, reservation, CSRF, preview retention and task protections.

## Source-grounded research

| Source | Current behavior and required reach |
|---|---|
| django-app/upload/contract.py | Field metadata, identity rules, REQUIRED and ENRICHABLE are CXRStudy-specific; require_storage supports only prime. Put these behind a minimal project adapter without changing PRIME field IDs/types. |
| upload/configuration.py | validate_mapping/validate_format use global UPLOAD_FIELDS; field_catalog and seed assume chest fields. Make project and mode explicit across saved formats, ad hoc mappings and aliases. |
| upload/validation.py | choose_layout/normalize provide strict normalization and multi-file provenance. classify/prepare query CXRStudy; existing enrichment and advisory stored disagreement must become hard blocks. Required inputs must depend on actual work. |
| upload/ingestion.py | inspect/create_preview/confirm own staging. Every new row currently dispatches; commit_studies requires chest outputs. Add all-local and mixed processing paths, with immutable work masks and adapter persistence. |
| upload/submission.py | persist_result atomically commits task/studies/preview and deduplicates via results_saved_at. Keep it shared; an unknown project must never reach chest-specific save_results_to_database fallback. |
| upload/workflow_views.py | Hardcodes prime in cookie endpoints and preview lookup. retired_import returns 410. Server-resolved route permission must govern retro capability; client flags cannot grant it. |
| upload/config_views.py; upload/models.py | Config view hardcodes prime; FieldConfiguration/UploadFormat/UploadPreview already carry project. Reuse these tables and payload staging. Separate normal versus retro saved formats so normal uploads cannot silently acquire grade mappings. |
| upload/views.py; upload/static/upload/upload.js; upload/templates/upload/upload_interface.html | Shared index, mappings, polling and progress. JSON fields populate dropdowns. Existing no-task text describes enrichment only; update it for local new inserts and unchanged records. |
| lunit_audit/urls.py; templates/base.html; nginx/nginx.conf | Add requested root route and admin navigation, supply protected POST URLs, cover retro endpoints with the current request/rate policies. |
| cxr-audit-api/canonical_processing.py; combined_server.py | PRIME canonical contract requires chest inputs and runs grading for every row. Add strict selective processing without weakening normal request validation. Preserve grade validation and exact identities/cardinality. |
| report_v2/measurements/predictions.py; report_v2/projects/prime.py; report/views.py | Report v2 recomputes policy-based Lunit prediction from scores. Legacy reports still consume stored lunit_binarised. gt_llm remains a report input. Keep both stored outputs internally derived for compatibility. |
| report_v2/projects/registry.py; agent_api/auth.py and views.py | Only PRIME production catalog/API support exists. Report catalogs do not implement storage. Routine API remains normal-mode only; shared validation must retain its strict manifest/idempotency contract. |

External source only reviewed at C:/Users/covie/OneDrive/Documents/Work/Research/Koios/Monitoring/Monitoring 2 temp:

- User-confirmed llm_extract.py is the Koios processing reference. It accepts caller-defined schema/prompt, uses OpenAI-compatible calls and run_extraction; required errors must not silently become successful null results.
- report_extractor.py produces five nullable outputs in one call, with structured nodules and measurements. This establishes the need for grouped processing outputs and valid completed-null semantics, not a five-field universal contract.
- pipeline.py combines deterministic source processing, extraction and study/nodule/session/ROI merges. Its unconditional extraction and default filesystem checkpointing must not become the shared uploader behavior.
- file_loader.py has project-specific aliases/inputs; retain current bounded parsing and strict validation rather than copying permissive date coercion.
- nodule_merger_v2_llm.py demonstrates a separate optional LLM step with different dependencies. No Koios implementation or optional matching is included here.

No external datasets/checkpoints/credentials were read; no runtime service was accessed.

## Architecture and behavior contract

### Minimal project adapter

Introduce one explicit lookup for supported upload projects. An adapter owns model/query/insert operations, exact identity normalization, fields and types, normal/retro writable sets, archive requirements, processing input/output dependencies, value completeness and result consistency checks. Keep existing layers and small functions; no generic scheduling engine, schema designer or arbitrary transformations.

Implement only PRIME. Demonstrate a second project in synthetic tests with a different identity type and a structured/nullable output group; test-only registration cannot enter production. Future adapters can persist related records within the same final transaction. Never assume all identities are accession integers or all processing units are single flat study rows. Define stable project-local work identity for future multi-record groups; do not implement a speculative Koios schema now.

Developer-defined steps may produce multiple outputs. Union missing-output masks per processing unit, call once for that unit, and merge only planned missing outputs. Preserve supplied outputs. Reject contradictions under project rules rather than silently replacing values. Completeness must distinguish unknown from valid zero/false/empty/explicit-null outcomes when the project's schema allows them; no universal null-as-missing policy. PRIME grade completeness is simply a valid integer 1-6; blank is missing, zero/null is not a completed grade.

### Permission and formats

GET /retro-upload and its precheck/confirm endpoints require existing superuser/admins-group authorization. Default endpoint is normal mode even for administrators. The server resolves mode/project from the approved endpoint/context; it does not trust upload metadata, referrer or a frontend switch. Initial page is PRIME; future selection resolves only registered production adapters.

Bind preview to owner, project, mode, effective configuration revisions, source rows, supplied outputs and exact work plan. Recheck administrator permission at confirmation, including after demotion. Reject cross-mode/project/owner previews. Confirm accepts only its ID. Routine API keys cannot exercise retro mode.

Normal formats remain unchanged. Add a minimal format mode/capability discriminator only if needed for persisted retro-grade mappings; existing formats default to normal. Reuse headers, mapping and parsing rules. Validate every format against the adapter and its intended mode, and filter matching/selection accordingly. Default page/API reject retro-only formats and grade destinations rather than silently importing protected fields. Retain current exact header matching/rematch and blank-excludes-source-column behavior.

### PRIME required inputs and work

For accepted new records, retain PRIME identity, workplace and the ten existing Lunit finding scores. A valid supplied grade permits report text to be absent only when no selected processing step needs it. Missing grade requires report text for grade inference. Selected supplemental extraction requires report text for every participating new record, including records with supplied grades. Preserve normal mode's current incomplete-row exclusion behavior and preview counts; a required input missing for the planned processing makes the new row incomplete/excluded before dispatch. Invalid supplied values and existing-record conflicts hard-block all confirmation.

Retro additional import destination is exactly llm_grade. gt_llm, lunit_binarised, supplemental findings, derived durations, manual GT and system metadata remain protected. Derive gt_llm and Lunit prediction deterministically from valid inputs, using the existing rules; supplied grade never changes score policy. No removal of stored columns or rewriting historical values.

An unmapped grade is missing and follows grading behavior. Optional supplemental checkbox enables only supplemental extraction. Partition/merge work so a supplied grade is never rerun merely because supplemental extraction is selected. Keep both steps strict. Derive timing outputs under existing rules, independently of whether any inference ran. No DB transaction across network calls.

### Preview, processing and persistence

Normalize/merge before computing completeness. Exclude incomplete new rows with bounded reasons. Skip unchanged existing rows, block stored disagreements and attempted fills, and never dispatch existing IDs. Preview reports new accepted, excluded, unchanged existing, blocked rows, rows needing grade inference, rows needing supplemental inference, and accepted rows requiring no inference. Counts are distinct studies; step counts may overlap and must be labeled clearly.

All-local accepted submissions use the existing reservation/short atomic commit path with no phantom remote task. Mixed submissions retain imported grades/private rows in Django and dispatch only necessary source inputs/work masks. Add a versioned strict selective PRIME request contract or a separately validated extension; old normal contracts stay strict. The analysis service does not gain authority to import arbitrary generated columns.

Validate returned exact work IDs, no duplicates/missing/unexpected results, step success, grade range and supplemental schema. Merge only expected outputs and ensure supplied grades are unchanged. Assemble local deterministic outputs and returned outputs for all accepted new studies before the final transaction. Recheck all accepted/excluded/unchanged participating identities and configuration/input baselines to detect stale previews. With concurrent insertion/update, refresh is required; never overwrite.

Persist studies/task/preview atomically through adapter code, preserving repeated confirmation/polling safeguards, cancelled/failed protections and cleanup. An insert/output/required-step failure rolls back local-only rows too. No enrichment writes. Completed preview retains counts only; audit minimal actor/project/mode/counts, and retain imported/generated grade provenance without logging clinical text. Use an existing safe processing metadata mechanism or the smallest necessary supporting field if provenance would otherwise vanish when payload is purged.

## Ordered implementation tasks

1. **Extract PRIME upload contract.** Inventory callers in configuration, validation, ingestion, canonical processing and API. Implement minimal adapter lookup and move CXR-specific identity/model/fields/requirements/steps behind PRIME ownership. Preserve existing interfaces where practical. Add synthetic second-adapter checks for different identity and grouped nullable outputs; no Koios production registration.
2. **Make mappings and settings project/mode-aware.** Parameterize field_catalog, settings validation, mapping/format validation, seeds and configuration page context. Add retro format discrimination if needed, default old formats to normal, and expose only llm_grade additionally for PRIME. Permissions stay code-owned. Test saved formats/aliases/ad hoc mappings and unsupported projects.
3. **Implement shared classification and immutable preview.** Replace existing-record enrichment/advisory conflicts with hard blocking in UI/API. Classify new rows against conditional requirements, calculate grade/supplemental work masks and preview counts, bind mode/revisions and unchanged/excluded baselines. Retain parser limits and payload cleanup. Routine API has the same existing-record policy but no retro capability.
4. **Add selective PRIME processing.** Separate grade and supplemental execution in canonical_processing.py, with exact work/result validation and existing batch helpers. Support all-local, grade-only, supplemental-only and mixed masks. Preserve normal endpoint compatibility and strict errors; no dummy grade or silent fallback. Calculate gt_llm, Lunit prediction and timing locally or through shared pure project functions without requiring an inference request.
5. **Complete shared atomic persistence.** Assemble local/remote results, preserve supplied grades, insert only new accepted studies via adapter, and recheck baselines under locks. Ensure local-only creates correct completion counts, mixed failures write nothing, retries cannot duplicate and unknown projects never reach the legacy chest fallback. Carry minimal grade provenance through cleanup.
6. **Expose shared retro page and controls.** Add root /retro-upload with consistent trailing-slash behavior, protected precheck/confirm URLs, shared template/JS route context and admin sidebar entry. Preserve mapping workflow; show step counts and direct-import completion. Default page keeps protected options hidden and server-rejected. Update nginx coverage. Keep retired historical importer closed with accurate guidance.
7. **Verify/document/roll out.** Run focused synthetic regressions and affected upload/API/report/canonical suites. Update upload-configuration.md, relevant API behavior docs and explicitly supersede the older plan's blanket no-import rule and existing annotation enrichment for this slice. Record existing-record filling as TODO. No production deployment or historical rewrite during implementation.

## Acceptance and regression coverage

- Anonymous/nonadmin page/precheck/confirm denied; forged mode and protected mappings rejected. Demoted admin, cross-owner/project/mode, expired/reused preview protections work.
- Normal UI/API cannot select/import llm_grade through any alias, format or explicit mapping. Only PRIME supported in production; second adapter isolated to synthetic tests.
- Retro grades 1-6 imported exactly; invalid/zero/fractional/nonfinite grades block. gt_llm correctly derived and not importable. Other generated/system/manual fields stay protected.
- All grades present plus supplemental off: zero inference calls/service dispatch and local atomic insert. Missing grades: only those new rows inferred. Supplied grades plus supplemental on: supplemental-only calls, grades preserved. Mixed rows dispatch exact subsets once per step.
- Complete grades without report text accepted when supplemental off; report-dependent incomplete rows excluded before dispatch when work requires text. Required source score/workplace/identity validation remains. Normal incomplete exclusions unchanged.
- Existing identical supplied values skipped; disagreement and missing-field fill attempts block the whole submission, including normal/API annotation enrichment. No existing-row inference or mutation. Multi-file disagreements block before inference.
- Bad output/identity/cardinality, required processing failure and mid-insert failure leave zero new rows, including local-ready rows. Concurrent inserts or changed baselines invalidate preview. Retry/poll/cancel behavior remains idempotent.
- Dynamic v2 Lunit reports unaffected; legacy stored prediction remains populated for accepted new rows. Ground truth and timing rules unchanged. Existing studies/data unaffected by migrations.
- Browser checks shared mapping/rematch, admin navigation, step counts, optional selection, local completion, accessible controls and mode endpoint binding.
- Regression suites: upload/test_ingestion.py, upload/test_ingestion_browser.py, upload/tests.py, agent_api tests, report-v2 threshold/semantic tests and cxr-audit-api/tests/test_canonical.py. Update enrichment expectations deliberately. Test supported migrations on isolated SQLite/PostgreSQL and concurrent commit on PostgreSQL; container parser/transport and nginx checks remain deployment gates.

## Explicit deferred work and deployment

TODO: existing-record missing-field filling, overwrites and update conflict resolution. TODO: real Koios models, adapter, prompts/schema packaging, source processing, report integration and successful-null representation. TODO: additional routine API project support. No new arbitrary fields/steps UI, global reprocessing scan, queue, checkpoint store or stored-Lunit cleanup.

Deploy coordinated Django/API changes after draining/reconciling active tasks, apply only required supporting migrations, seed/review retro formats and test synthetic uploads. Preserve backups, records and schema during rollback; disable the route/drain jobs rather than deleting imported studies. Existing previews from the old contract must expire/drain or be invalidated explicitly during rollout.

Confirmed by user on 2026-10-12 after grill-me interview. Planning verification is static source review only; no tests, database/data inspection, inference or deployment performed.
