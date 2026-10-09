# Configurable upload handling and field catalogue

**Implementation handoff for Sol — 2026-10-09 (Asia/Singapore)**

Status: Ready for implementation. Documentation only; no application changes made.
Authority: Settled user decisions below. FINAL incomplete/invalid distinction supersedes earlier discussion of rejecting incomplete studies or retaining partial studies.

## 1. Objective and boundaries

Build unified project-scoped upload handling, configurable column mappings, and semantic field metadata shared by UI, routine API, and report editor.

- Keep CXRStudy columns, types, primary key, and stored meanings unchanged. No historical rewrite or physical column rename.
- Supporting configuration/staging tables and migrations are allowed. Reuse existing tasks/reservations.
- Insert validated new studies; enrich permitted missing fields on existing studies only. Preserve populated values.
- Uploaded files NEVER supply LLM outputs via any UI/API/historical-import/alias route. Only the processor writes them.
- No new incomplete studies, retained partial-study store, deferred LLM queue, eligibility scan, or automatic existing-study reprocessing.
- Preserve grading methods, supplemental processing options, manual GT, viewer/report workflows, Docker deployment, and SQLite/PostgreSQL compatibility.
- RIS and CARPL are illustrative PRIME format seeds, not universal generic-code branches.
- The actual checkout root contains django-app/, cxr-audit-api/, .git/, and .planning/. Older AGENTS.md refers to an absent django/ parent. Use this repository; do not create another django/ tree.

## 2. Settled decisions

### D1. Identity and field catalogue

Default visible identifier name: **anon_id**. Physical destination: existing CXRStudy.accession_no (BigInteger primary key). This is naming/mapping, not anonymization. Configurable accession aliases are accepted. Distinct alphanumeric identity is out of scope.

Seed catalogue from Django model metadata and existing Report V2 sources. Physical storage type/destination are read-only. Editable display labels and aliases do not change stable IDs. Protect identifier/system-owned roles and upload permissions in code; semantic settings cannot grant write rights.

Interpretations: identifier, integer/count, continuous measurement/score, age, boolean/binary outcome, nominal category, ordered category, date/time, duration, free text. Configure units/scale, ordering, positive class where applicable. Only storage-compatible changes; no arbitrary new physical fields.

Autodetect suggestions from model/project metadata and bounded authorized profiling as needed. Never override admin choices automatically. Ten or fewer distinct values suggests categorical; it is not a limit. Numeric codes can be categorical, and small samples do not establish a closed vocabulary.

Suggest **five equal-numeric-width bins**, with editable boundaries. Do not auto-bin identifiers/binary outcomes. Age supports manually chosen meaningful boundaries. Freeze accepted boundaries across uploads. Handle empty/constant data, precise interval endpoints, out-of-range coverage, and missing separately. Zero and False are values.

### D2. File formats and matching

Format configuration contains project, stable ID, editable name, expected header set, destination map, declarative parsing/value rules, processing role, revision, deterministic order.

UI:
1. Compare header sets; order irrelevant. Implementation default: trim surrounding whitespace, preserve case, reject duplicate normalized headers.
2. Exact match means no extra/missing headers. Multiple matches select first in stable saved order (stable-ID tie-breaker). Display selected format and mappings.
3. No match: format unselected; suggest unambiguous individual aliases; unknown destinations blank.
4. Changing format **rematches all columns**, replacing previous manual allocations. Warn before losing manual choices. Apply new format mappings; unmatched destinations blank. Do not blindly clear all.
5. Blank means excluded from import, no extra Ignore action. Show exclusions in preview. A valid custom UI layout needs no saved format.
6. One destination per source, one source per destination within each file. Reject duplicate destinations. Cross-file combination is separate.

Format-specific mappings take precedence over shared/project aliases. Authorized uploaders adjust only their submission. Only admins persist shared aliases/formats/value maps/field settings. Uploads never update the shared library implicitly.

UI format selection may guide rematching a different layout. API named-format mode is stricter (D6). Filenames/extensions never determine clinical format meaning.

### D3. Parsing and categories

Use predefined parsing choices and dictionary translations, never executable transforms. Category translations are admin-defined and format-scoped. Open categories accept unseen storage-compatible values. Only explicitly closed vocabularies reject unseen values.

Missing means NULL, applicable blank/whitespace text, or configured source sentinel. Invalid supplied numbers/dates must NOT be coerced to missing and discarded as incomplete. Unallocated columns bypass destination validation but not file/resource limits.

Preserve exact identifier tokens. Reject fractional, overflow, alphanumeric, or precision-lost IDs rather than int(float(...)). Do not silently collapse meaningful leading zeros. Do not claim to recover precision already lost in Excel numeric cells. Define explicit date formats/timezones, age units, and score scales based on actual project processing needs.

### D4. Combining, completeness, and preview — FINAL rule

Combine mapped rows across all files by validated identifier before checking completeness. Every row with mapped data needs a valid identifier. Missing/invalid identifier is blocking, not incomplete.

Identical normalized values merge; blank + valid uses valid; conflicting nonblank uploaded values for one study/destination reject the entire submission. Preserve file/row provenance. No first-file-wins, silent conflicting duplicate removal, or Cartesian joins.

| Classification | Action |
|---|---|
| New, complete source inputs | Eligible for required processing and insertion |
| New, incomplete source inputs | Exclude/discard with missing requirements; never persist or retain for later completion |
| Existing, permitted missing fields supplied | Stage fill-missing enrichment; no automatic LLM rerun |
| Existing, no eligible additions | Unchanged; count |
| Any invalid row/mapping/value, closed-vocabulary violation, or uploaded-value conflict | Reject whole submission; zero LLM dispatch and zero results writes |

Completeness means required project/workflow source inputs, not every model column. Generated LLM fields are excluded. Validate supplied mapped values even on studies otherwise destined for exclusion. Blank required value is incomplete; malformed provided value is invalid.

Preview BEFORE LLM: counts of distinct combined studies (total identifiable, new complete, new incomplete/excluded, existing split enrichable/unchanged), reasons, excluded columns, bounded blocking errors, and stored-value disagreements. Missing-ID rows have a separate invalid raw-row count. Do not double-count a study across files.

Blocked preview cannot confirm. Otherwise explicitly show discarded count. All-incomplete/no-op submissions make no LLM call. API shares classification and returns counts while remaining a routine single-request submission, not an interactive wizard.

### D5. Existing values and processing ownership

Fill only permitted NULL/applicable empty source fields. No overwrite, clearing from blank, or treating zero/False as missing. A valid uploaded value disagreeing with stored data is preserved-as-existing and reported; two conflicting uploaded values block.

Trace generated-result input dependencies. Block missing-field enrichment that would invalidate an existing derived/LLM result. Do not assume demographics are independent. Existing protected inputs stay unchanged. Manual GT retains its dedicated validated path; do not expose through generic mapping without an established existing contract.

Reject protected destination mappings. Headers resembling LLM outputs can remain unallocated, but never auto-map into outputs. Historical import must not bypass this. Existing stored results remain intact.

Any **required processing failure** means no new study or enrichment writes for that submission. Stage changes until required outputs validate; commit atomically, with no open DB transaction during LLM calls. Preserve workflow shape, not silent error fallback. Disabled supplemental steps need no outputs; enabled steps need explicit success criteria. No new per-study deferred-processing state table.

### D6. Routine API

Preserve project auth, ownership, key policy, idempotency, reservation and status URLs. Every file specifies EXACTLY ONE of format ID or explicit column mappings. Neither/both reject. No API autodetection.

For multiple files use a bounded JSON manifest tied to multipart positions/request-local IDs, not filename alone. Different files may use different modes; each file is exclusive. Project stays in existing URL context.

- Format mode: exact normalized header-set match to project format or whole-submission rejection.
- Explicit map: no format ID, no hidden alias inference; unmapped columns excluded. Validate all keys/destinations, duplicate allocations, input requirements and values.

Suggested extension: files + existing supplemental_steps + upload_manifest JSON, entries with format_id OR column_mapping. Exact serializer names are implementation details. Reject duplicate multipart metadata, unknown/duplicate manifest keys and file-count mismatch.

Bind project, file bytes/order, effective mappings/config versions and enabled steps to idempotency identity. Preserve replay of pinned accepted requests despite later config changes; never reinterpret retries silently.

Accepted response includes counts plus job/status reference. Invalid response has bounded errors/counts, no dispatch. Existing-only enrichment/no-op has a documented terminal response, no phantom LLM job. Update docs/agent-api.md and callers: old implicit-mode clients receive actionable errors, not a hidden fallback.

### D7. Report editor

Extend existing Source/Outcome/MeasurementSignature/ThresholdPolicy concepts, not a competing type system. Validate comparisons/grouping/aggregations against semantic type, units, outcome meaning, permissions. Same dtype alone is insufficient.

Saved report definitions retain bins and relevant categorical meaning via embedded snapshots or immutable revisions until explicitly edited. Defaults affect new elements. Preserve legacy definitions lacking snapshots with backward-compatible resolution. Labels never break stable source references. Upload normalization changes do not rewrite historical values; normalization and report grouping are separate.

## 3. Verified code anchors and gaps

Static reads at planning time, not runtime verification:

| Area | Finding / integration implication |
|---|---|
| django-app/upload/models.py | accession_no BigInteger PK; reuse ProcessingTask/UploadedFile/SubmissionReservation; no core model migration |
| upload/views.py precheck_files | Reads whole files, recognizes ACCESSION_NO only, skips other layouts; replace with shared classification |
| upload/views.py save_results_to_database and import_preview/import_confirm | Fixed CSV mappings and broader importer writes; close LLM import/overwrite bypasses |
| upload/submission.py | Reservation/dispatch and atomic idempotent persist_result exist; strict save currently depends on APIJob association; both new UI/API paths need strictness |
| upload/static/upload/upload.js and templates/upload/upload_interface.html | Reuse existing precheck/progress surface |
| agent_api/views.py jobs | Only supplemental_steps metadata accepted today; MAX_FILES=20 and MAX_UPLOAD_BYTES=49 MiB; auth/idempotency/content digest already exist |
| cxr-audit-api/combined_server.py sort_files_async | Assigns Excel to GE, CSV to CARPL; replace extension-based semantic routing |
| cxr-audit-api/class_process_carpl.py load_reports | Can skip unreadable GE files, keep first duplicates, coerce IDs/dates, inner-join; do not inherit silent data loss |
| Same file process_stats_accuracy | Calls grading on TEXT_REPORT and converts llm_grade > 1 to binary; validate success before conversion |
| cxr-audit-api/cxr_audit/grade_batch_async.py | Returns empty/default values after per-report failure; propagate explicit required-step failure |
| report_v2/projects/base.py, prime.py, registry.py | Typed catalogue and strict project resolution; production registry currently PRIME only |
| report_v2/data.py, evaluation.py, definitions/ and editor consumers | Trace metadata and saved-definition resolution through these during implementation |

Legacy auto-sort requires Tuberculosis alongside ten findings, while report policy enumerates ten. Loader expects ACCESSION_NO vs Accession Number and accesses timestamp fields beyond endpoint minimum checks. The complete required-input contract must be traced in Task 1; illustrative RIS/CARPL headers are not an authoritative production contract.

Koios reference reviewed: C:/Users/covie/OneDrive/Documents/Work/Research/Koios/Monitoring/Monitoring 2 temp/file_loader.py. Reuse read -> rename -> verify -> normalize separation, not a runtime dependency, permissive collisions or silent date coercion. Do not read related datasets.

## 4. Minimal architecture

Suggested supporting records; adapt names to house conventions:

- Field configuration: project + stable model field/source ID; label, interpretation, units, category/order, bins, revision. Storage/write permissions come from a code-owned adapter, not editable JSON.
- Upload format: project, stable ID, name/order, header set, mapping, bounded parsing/value rules, enabled flag and revision. Admin-only audited changes.
- Short-lived preview/staging context: owner/project, byte digest, immutable effective mappings/settings, summary, expiry/state. Reuse existing task-compatible infrastructure. Not a retained incomplete-study database or deferred queue.

One pure normalization/validation service returns canonical rows, classifications, errors and provenance; it never runs LLM or writes studies. UI/API consume it. A project adapter declares approved destinations, required input sets, generated outputs, dependency restrictions and translation to current processor inputs.

Django remains authorization/configuration authority. Pass canonical validated inputs through a narrow authenticated processor boundary instead of re-running legacy auto-sort/permissive merging. Validate internal payloads too. Use the smallest adapter/endpoint change preserving task/status flow; do not duplicate mapping engines across services.

Parse in a bounded worker/process with only required staging access. Preserve reservation and ambiguous-dispatch protections. Recheck DB classification/current values in the final short transaction; never hold it during LLM/network calls. Pipeline results and staged enrichment commit together.

### Project storage gate

Project-scoped config is not study isolation. The global accession key cannot independently represent the same accession for two projects. Keep the current results adapter restricted to PRIME and reject production uploads for projects lacking an approved storage adapter. Test generic config with a synthetic second project, without registering it in production. Do not enable another production project against this table merely because a format exists. Supporting ownership or separate storage requires a separately verified design; no silent cross-project enrichment or core schema change.

## 5. Security requirements — release blockers

1. Central destination allowlist and typed validation; no arbitrary client names passed to setattr/model kwargs. Admin metadata cannot expand protected write permissions.
2. Declarative configuration only: no eval, SQL/Python snippets, arbitrary regex transformations, URL imports, paths or templates.
3. Project/owner/auth checks for inspect, preview, confirm, errors, status and config; CSRF on cookie-auth mutations. Do not copy existing csrf_exempt importer patterns.
4. Business-required formats only (CSV/XLS/XLSX as needed); actual content validation and maintained parsers. No macro execution/formula evaluation/external workbook-link retrieval. Handle unsupported features explicitly. Preserve encrypted workbook needs without exposing passwords.
5. Consistent UI/API file count/byte limits; additionally bound rows, columns, cell length, decompressed size, memory, CPU/time and concurrency. Bound decrypted input too. Content-Type and worksheet dimensions are untrusted. Document values and parser isolation; post-parse checks alone are insufficient.
6. Generated filenames outside public webroot/media; owner-scoped staging with expiry and cleanup on success/rejection/timeout. Audit actor/project/config revision/counts/outcome. No report content, raw values, passwords or parser exception payloads in logs.
7. Escaped text for previews/headers, no untrusted innerHTML. Formula-injection-safe CSV downloads. Bounded authenticated diagnostics.
8. Bind confirmation to bytes/project/owner/config version. Changes require re-preview or consistently pinned settings. No replacing files after validation.
9. Idempotent dispatch/commit; compare-and-fill concurrency. If study classification changed after preview, request refresh/conflict instead of silently changing operations. Failure may retain task/error records, but not partial study changes.
10. Admission/rate/token limits before expensive work. LLM has no tools or database-write authority. Validate output schemas, IDs, cardinality and explicit success; malformed output is not a successful normal label.

Sources: [OWASP uploads](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) and [mass assignment](https://cheatsheetseries.owasp.org/cheatsheets/Mass_Assignment_Cheat_Sheet.html). This plan is not a completed security audit. Absence of PHI does not remove integrity/availability risk.

## 6. Ordered execution tasks for Sol

### Task 1 — Establish actual PRIME contract

Read nested AGENTS.md and all callers of touched functions. Trace upload/views/submission, agent_api auth/idempotency/jobs, combined_server, class_process_carpl, grade_batch_async/classifier, report metadata/data/editor, manual GT, viewer/export.

Write a compact contract table in this directory: physical field, semantic type/unit, upload permission, aliases, required per enabled workflow, generated dependency and enrichment permission. Resolve header inconsistencies and ten-findings/Tuberculosis explicitly. Seed missing optional columns safely rather than requiring every historical field. Derive facts from source; ask only for genuinely unresolved business rules.

Acceptance: synthetic complete PRIME fixture traverses required calculations without missing-column errors; every generated field protected; exact source requirements and output success criteria documented.

### Task 2 — Catalogue, formats, admin page

Add supporting migrations and idempotent seeds from models/project definitions/current aliases. No result-data mutation. Build admin-only options in shared shell: read-only storage, compatible semantic meaning/labels/units/categories/bins, aliases/formats/order/revision/audit. Profiling bounded and advisory. Reuse existing permissions.

Acceptance: forged/nonadmin/protected-permission edits rejected; seeds preserve admin changes; project references isolated; result schema unchanged.

### Task 3 — Shared parser, validation, merge and preview

Implement D2-D4 once, with bounded parsing/isolation and provenance. Securely stage exact preview context. No LLM call or study write during inspection/preview. Add synthetic pure-logic tests.

Acceptance: incomplete exclusions work, malformed values within them still block everything; duplicate values merge without multiplying studies; IDs never disappear through coercion; summary matches canonical rows.

### Task 4 — Processor/persistence integration

Feed only complete-new canonical rows through existing grading. Remove silent skip/first-duplicate/coerced-invalid behaviour on this path. Return explicit required-step failures, validate output IDs/cardinality/schema before writing. Reuse polling/reservation/results_saved_at. Existing and excluded studies never automatically process. Stage enrichment and commit accepted changes atomically after required processing succeeds.

Acceptance: required LLM/output/insert failure causes zero study mutations; retries/poll repetition cannot duplicate; concurrency cannot overwrite newly populated values; enrichment-only makes no LLM call; UI/API strictness identical.

### Task 5 — Unified UI/API

Extend upload/precheck/progress: per-file format selector/mappings, anon_id label, rematch notice, blank exclusions, errors, unique-study summary and confirmation. Preserve cancellation/task ownership semantics. Route historical import through restrictions or retire its mutation route with clear migration guidance; no LLM import bypass.

Extend jobs endpoint with exclusive per-file mapping modes, strict header signatures, shared validation, counts, digest coverage, and terminal no-op/enrichment responses. Update docs/agent-api.md, callers, examples and tests together. No admin configuration API or interactive API wizard. Preserve key/status/auth controls.

Acceptance: keyboard-accessible controls, safe rendering, UI/API parity, exact-format mismatch before dispatch, neither/both API modes reject, different modes across files work.

### Task 6 — Report integration

Extend existing source/type resolution, editor options, server comparison validation and grouping. Implement bins/category settings with snapshots or immutable revisions. Preserve legacy definitions and stable IDs. Separate normalization from grouping.

Acceptance: labels do not break references; changed defaults do not alter saved reports; identifiers cannot be averaged; unit/outcome compatibility enforced; age/numeric boundary counts correct.

### Task 7 — Verification and handoff

Run focused Python/API/JS tests then relevant existing upload/report regressions. Verify SQLite/PostgreSQL transaction behaviour where available; explicitly record deployment-only checks. Inspect migration SQL for zero CXRStudy alterations/rewrites. Document limits, isolation, cleanup, administration, API migration, exclusion semantics, rollout and rollback. Write IMPLEMENTATION-SUMMARY.md here with evidence. No automatic deployment or additional production project enablement.

## 7. Required regression matrix

| Area | Evidence |
|---|---|
| Schema | Supporting migrations only; historical fixture unchanged by migrations/seeds |
| Identity | anon_id aliases, exact large integers; reject missing/fractional/overflow/unsupported IDs |
| Detection | Order independent; extra/missing headers; stable first match; rematch replaces manual allocations |
| Allocation | ABC -> age, blank exclusion, duplicate destinations, protected destinations via every route |
| API | Strict mismatch, neither/both, manifest/file mismatch, duplicate metadata, project scope |
| Categories/bins | Open/closed, normalization/order, equal-width, empty/constant data, boundaries/missing/out-of-range |
| Completeness | Files combine, incomplete excluded, malformed blocks, missing ID separate error/count |
| Merge | Identical/blank accepted, uploaded conflicts reject all, unique output studies |
| Existing | Fill missing, preserve zero/False, stored conflict reported, dependency protection, no rerun |
| Preview | Unique counts, all-incomplete no-op, no early dispatch, owner/bytes/settings tampering |
| Processing | Required failure explicit, missing/extra/duplicate/malformed outputs, no failure-to-normal |
| Persistence | Atomic rollback including enrichment, idempotency/poll replay, concurrent compare-and-fill |
| Security | Config/project/preview auth, CSRF, path/HTML/formula payloads, workbook expansion/time/memory limits, cleanup |
| Reports | Saved meaning invariant after defaults change, stable IDs, units/outcomes |
| Compatibility | Grading options, encrypted input, manual GT, viewer/export, auth/status, SQLite/PostgreSQL |

Synthetic fixtures only, including supported spreadsheets. No datasets, real reports, PHI or secrets committed. Prove behaviour rather than mirror private implementation.

## 8. Non-goals and release gates

No schema designer, arbitrary transforms, inference-backend replacement, grading redesign, historical rewrite, uploaded LLM results, retained partial studies, deferred processing, cross-project identity redesign or new report engine. No dependency for trivial helpers.

Release only when all supported upload routes share validation; invalid input dispatches/writes nothing; incomplete exclusions are visible; required failures cannot masquerade as success; saved report meaning is stable; resource/auth boundaries have regression coverage. Roll out to PRIME only. Unverified parser isolation, deployment limits and PostgreSQL concurrency remain explicit deployment gates.

Planning limits: static inspection only. Task 1 resolves exact source requirements because legacy checks disagree with downstream accesses. Current atomicity, parser isolation, project storage safety and LLM failure handling are not asserted to meet this plan already.
