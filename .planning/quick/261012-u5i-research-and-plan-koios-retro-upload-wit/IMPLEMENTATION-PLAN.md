# Koios retro-upload implementation plan

Date: 2026-10-12
Status: implementation handoff; application changes not implemented
GSD quick task: 261012-u5i

## Agreed scope

One backend uploader and one shared page/template. Default uploads protect LLM output destinations. The admin-only /retro-upload variation exposes approved Koios LLM output destinations in the same per-file mapping structure. Supplied valid predictions are preserved. Complete output columns require no corresponding LLM work; incomplete columns cause processing only for rows missing outputs. Apply this to Koios first; PRIME/Lunit behavior stays unchanged. Use atomic final persistence and existing preview/conflict controls. This supersedes the earlier blanket prohibition on importing LLM outputs only for authorized Koios retro uploads.

This is not an unrestricted database importer. Identity, types, project isolation, server-managed metadata, CSRF, upload limits and output validation remain mandatory. Manual GT remains on its established path unless a reviewed Koios contract explicitly includes it. No second independent importer, generic workflow engine, new queue or inference backend replacement.

## Source-grounded findings

| Source | Current behavior and implication |
|---|---|
| upload/contract.py | FIELDS comes from CXRStudy; GENERATED excludes LLM, manual GT, timing and system fields from UPLOAD_FIELDS. REQUIRED is chest-specific. require_storage rejects every project except prime. Cannot enable Koios by adding a dropdown option. |
| upload/configuration.py | validate_mapping and validate_format enforce global UPLOAD_FIELDS; field_catalog and seed use global chest fields. Stored formats and explicit mappings both need project/mode-aware checks. |
| upload/validation.py | choose_layout/normalize use existing strict mapping and provenance; classify/prepare query CXRStudy and use global REQUIRED/ENRICHABLE. Conflicting uploaded values block; disagreements with stored values currently preserve storage and are advisory. Keep normal behavior; explicitly block conflicting retro writes. |
| upload/ingestion.py | inspect, create_preview, confirm and commit_studies own private staging and dispatch. Every new PRIME row goes to analyze-canonical. commit_studies requires PRIME grades and inserts CXRStudy. Existing-only submissions already demonstrate local completion without a task. |
| upload/submission.py | persist_result wraps task, study and preview writes in transaction.atomic; results_saved_at prevents repeated writes. Legacy fallback calls save_results_to_database. Koios results must never reach that chest-specific fallback. |
| upload/workflow_views.py | Cookie precheck/submit hardcode prime. retired_import returns 410. Permission filtering in JSON alone cannot secure a retro mode. |
| upload/config_views.py and models.py | Configuration UI hardcodes prime; FieldConfiguration, UploadFormat and UploadPreview already carry project. Preview payload can hold immutable mode, work plan and revisions; no new generic staging table is needed. |
| upload/static/upload/upload.js and templates/upload/upload_interface.html | Mapping options come from precheck fields. Reuse this UI with route-specific URLs/context. Current no-task completion text only describes existing enrichment, so must include locally inserted retro records. |
| lunit_audit/urls.py and templates/base.html | Existing upload routes are under /upload/. Add requested /retro-upload route explicitly at root; admin navigation must link to it. |
| cxr-audit-api/canonical_processing.py and combined_server.py | Canonical endpoint accepts only prime, chest inputs and supplemental flag. Processor runs grading over all rows; not a Koios processor. Keep existing PRIME payload contract unchanged. |
| report_v2/projects/registry.py and agent_api/auth.py | Only PRIME is production-registered/supported. A synthetic test adapter is not Koios support. Report project definitions are metadata, not a storage adapter. Do not silently enable Koios reporting or routine API keys. |

External Koios source root: C:/Users/covie/OneDrive/Documents/Work/Research/Koios/Monitoring/Monitoring 2 temp. Source files only were read; no data, checkpoint, credentials or external service was accessed.

- llm_extract.py: user-confirmed processing reference. llm_extract validates JSON through the supplied Pydantic response_model; run_extraction batches text_column with caller-supplied schema/prompt. Output is shaped through helpers.shape_extraction/model_dump. Failure default can silently produce None columns; integration must use max_failures=0 and explicit successful identities.
- report_extractor.py: ThyroidReportExtraction has five top-level outputs: isthmus_size_cm (nullable float), right_lobe_size_cm and left_lobe_size_cm (nullable lists of nullable floats), nodules (nullable list of structured Nodule), other_findings (nullable string). Nested nodule fields include location, size_cm, characteristics and manual_tr. All five come from one report extraction call, not five independent calls.
- pipeline.py: deterministic Koios/PACS processing precedes extraction on study_times, then session/nodule/ROI merges. run_pipeline unconditionally extracts and defaults to filesystem checkpoints. Do not call it unchanged for retro uploads. Missing extraction inputs differ from missing inputs for downstream deterministic analysis.
- file_loader.py: useful source aliases and source-type definitions, but permissive datetime coercion and renaming are not substitutes for current bounded parser/strict normalization.
- nodule_merger_v2_llm.py: separate optional accession-level LLM matching fallback exists. It is not invoked in the reviewed run_pipeline. Keep it disabled in this slice; if later enabled, give it its own inputs, outputs, success and completeness rules.

## Data and execution contract

### Project and permission

Resolve an explicit server-approved Koios context; never fall back to PRIME. Reuse a minimal project contract containing storage/model identity, field descriptors, normal/retro writable destinations, required persistence fields and the single extraction step. Avoid creating a scheduler framework. Retain PRIME constants behind its adapter with unchanged semantics.

GET /retro-upload and its POST precheck/confirm endpoints require the existing superuser/admins-group rule. Resolve retro mode from the protected endpoint, not a client boolean. Store project, mode, owner, field/format revisions and planned missing outputs in the preview. Recheck admin permission on confirm, even after demotion. Confirmation accepts the preview ID only and must reject previews from another owner/project/mode. Default UI and routine API cannot confirm a retro preview or submit protected destinations.

### Koios storage prerequisite

There is no Koios model or production upload adapter here. First define an isolated Koios study/result model, with project-local identity preserving exact source tokens and typed scalar/JSON extraction fields. Do not repurpose CXRStudy or cast Koios identity through accession_no's integer parser. Determine source identity and study/nodule cardinality from the source processors before writing a migration. Child nodule records are needed only if the actual persistence/consumer contract needs them; the extraction nodules list can remain validated JSON for this slice.

Use existing project-scoped field/format/preview tables. Store minimal extraction completion/provenance needed to distinguish unknown output from successful explicit null and retain imported versus generated origins after preview purge. No raw prompts/responses in audit logs. Existing report pages must not expose Koios records through PRIME queries.

### Missing and complete values

Evaluate after normalization and multi-file combination, per distinct study, not per raw file row. Blank/unmapped means unknown and eligible for generation. Valid false, zero and empty lists are present values. Strict JSON literal null is an explicit known-null result in retro mappings; a blank cell is unknown. Field rules must not silently collapse these into one state. Nested nullable members are valid under the extraction schema.

A completely filled mapped column has an empty missing-row set. Because all five outputs share one call, union missing-row sets across the enabled extraction outputs, call once per affected study, and copy only the missing outputs. Do not send complete rows or overwrite supplied cells. Validate both imported and generated values against fixed storage/schema rules; administrator display settings cannot relax them.

Absent output mappings mean missing outputs for the project's enabled extraction step, not a silent way to disable required processing. When all required outputs are known, including valid explicit nulls, skip extraction completely. Persist successful nullable outputs as completed so retries do not keep rerunning them. No fake normal/zero result on errors.

### Existing studies and final write

For retro mode, combine stored and uploaded values without overwriting populated fields. Identical reimports are no-ops; missing approved output fields may be filled. Conflicting source or prediction values block the submission with bounded location/field errors. Baseline must include processing input dependencies and affected output fields, not only uploaded columns, so stale work cannot attach to changed reports.

Processing missing outputs on existing Koios records is authorized only for records participating in this submission. No global eligibility scan. Required report text can come from the validated stored record or upload; missing required inputs block the affected work before dispatch. Fully predicted new records require storage identity/mandatory storage fields, not fresh-extraction source inputs. Deterministic downstream computations retain their own required inputs; do not require audit files merely to archive a valid prediction.

Keep local-only new rows, existing enrichment and remote-required work in one preview. With zero work, commit locally using the existing reservation and transaction pattern, creating no remote job. With work, dispatch only the planned subset, validate exact expected IDs/cardinality/schema, fill only planned missing values, then commit all accepted changes together. Any required extraction/validation/insert failure produces zero study mutations. Hold no DB transaction across inference. Repeated polling/confirmation must not duplicate writes; cancelled/failed tasks cannot commit. Recheck baselines under locks in the final transaction on both SQLite and PostgreSQL.

## Ordered implementation tasks

1. **Freeze Koios contract and synthetic examples.** Record canonical identity, mandatory archive fields, the five output types, source aliases, strict JSON representations, completion state and downstream dependencies in FIELD-CONTRACT.md beside this plan. Inspect Koios/PACS processors for identity/cardinality; ask only for unresolved business semantics. Capture successful all-null versus failed extraction explicitly. Verify source licensing before copying required modules into the repo; production must never import an absolute OneDrive path.
2. **Add isolated storage and minimal project-aware contract.** Implement Koios models/migration and adapter, parameterize field_catalog/validate_settings/validate_mapping/validate_format/seed and validation lookups. Support JSON normalization bounded by existing parser/cell limits. Preserve PRIME behavior and existing field IDs; no CXRStudy historical migration. Add project-specific format selection/configuration rather than encoding Koios outputs in PRIME formats.
3. **Implement shared retro inspection and immutable work plan.** Extend inspect/prepare/classify/create_preview with server-resolved project/mode; store rows already complete, missing-output masks, input dependency baselines, semantics revisions and per-step counts. Reject invalid cells and retro conflicts before inference. Include existing-row missing-output work and explicit null success semantics.
4. **Integrate Koios selective extraction.** Package the smallest approved schema/prompt/extraction code from llm_extract.py/report_extractor.py and needed helper pieces in the analysis service. Use deployment-configured authenticated OpenAI-compatible transport and current security settings, not copied HTTP defaults or dummy credentials. Give Koios its own strict canonical request validation within the existing service; preserve PRIME endpoint contract. Run grouped extraction only on planned IDs, fail on any required failure, disable filesystem checkpoints/intermediate files, return successful IDs and validated outputs. Do not enable optional LLM matching or port the full monitoring server/viewer.
5. **Finish atomic project-aware persistence.** Route persist_result/commit_studies by the stored preview project; implement Koios local-only and mixed import/generation commits. Check exact IDs, planned masks, imported equality, schema and baselines. Retain per-output provenance/completion, task idempotence, payload cleanup and remote deletion. Unknown project/result shape fails closed, never chest fallback.
6. **Add route variation and shared UI.** Implement /retro-upload (with consistent trailing-slash redirect) plus protected precheck/confirm routes, reusing the uploader template/JS and configuration UI. Template context supplies URLs/project/mode; mappings retain the existing format/rematch workflow. Only approved LLM destinations are additionally selectable. Preview shows direct imports, rows needing extraction, existing fills/no-ops and blocked conflicts. Local completion includes inserted counts; mixed progress uses the extraction subset. Add admin sidebar link; leave retired old import endpoints closed with accurate guidance.
7. **Verify and document.** Add focused synthetic tests below, update docs/upload-configuration.md and affected API docs, annotate the superseded restriction in the old implementation plan. Record migration/rollout/rollback and deployment checks. Do not enable routine Koios API/reporting merely because upload storage exists; preserve their current supported-project restrictions and test shared-call compatibility.

## Required regression matrix

- Anonymous/nonadmin denied at page, precheck and confirm; direct forged metadata/mappings fail. Admin demoted after preview cannot confirm. Cross-owner, cross-mode, cross-project and expired previews fail.
- Default UI/API still reject LLM destinations for explicit mappings, saved formats and alias matching. PRIME behavior and canonical request remain unchanged.
- All five Koios outputs known: zero extractor/service calls, no phantom task, local atomic insert. Complete one column plus incomplete another: one call per missing row, complete values preserved.
- Multiple files merge by exact identity; conflicting predictions block. JSON object/list/null parsing validates structure and bounds; malformed/nonfinite/wrong-type values block. False/zero/[]/explicit null never become blanks.
- Successful all-null extraction commits as completed; failure/refusal/malformed output is not successful null. Missing report text blocks only required processing, not complete historical imports.
- Existing equal rows no-op; missing outputs fill; conflicting stored values block. Existing participating rows process only missing fields. No unrelated record is scanned or updated.
- Mixed local-only/remote rows and enrichment roll back together on required processing, output or DB failure. Reject duplicate/unexpected/missing IDs and changed supplied values. Concurrent report/output updates invalidate preview. Retry/poll/cancel protections remain intact.
- UI synthetic browser checks for added mapping options, format rematch, route permission, direct-import completion, mixed counts and accessible controls.
- Run existing upload ingestion/browser, agent API, report semantic snapshot and canonical processor suites; add Koios unit/integration coverage. Validate migrations on isolated SQLite and PostgreSQL, concurrent confirm/commit on PostgreSQL, parser limits in actual containers, nginx rate limits for retro POST routes, and synthetic real-endpoint transport separately before deployment.

## Rollout and remaining decisions

Implementation is gated on the Koios storage/identity contract, not on a new dependency. Nullable schema handling above is a proposed concrete representation that must be confirmed against historical export conventions during task 1; no dataset inspection is needed to define synthetic examples. Treat derived matching/changelog output import as outside the five-field extraction slice unless explicitly requested and contracted.

Deploy Django/API together after draining tasks; apply isolated Koios migration, seed Koios formats, then enable the admin route. Existing PRIME data must remain unchanged. Rollback drains/reconciles Koios jobs and disables the route while retaining imported records/supporting schema; never delete historical results to reverse this feature.

Planning verification: source-grounded walkthrough only. No application tests, deployment, database inspection or inference calls performed.
