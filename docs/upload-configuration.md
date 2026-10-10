# Configurable PRIME upload rollout

This implementation uses existing CXRStudy storage and the existing processing reservation. Only supporting FieldConfiguration, UploadFormat and UploadPreview tables are added. No study columns or historical results are rewritten. Only PRIME has an approved storage adapter.

## Rollout

1. Drain/reconcile active tasks before upgrading; preserve deployment backups through the existing backup procedure.
2. Build and upgrade Django **and** the cxr-audit-api images together. Django now submits validated canonical JSON to authenticated `/analyze-canonical`; legacy processor endpoints remain for existing internal consumers but do not authorize study imports.
3. Run `python manage.py migrate` using deployment settings, then `python manage.py configure_uploads` to seed missing fields and illustrative formats. Seeds preserve administrator changes. Validate real format **headers/settings only**, without committing datasets.
4. Under Admin → Upload settings, review exact format signatures, date formats/timezones, source sentinels, units, mappings and vocabulary rules. Blank mapping means excluded. Identity is named anon_id by default and stored as accession_no; this label does not anonymize data.
5. Update routine clients per [agent-api.md](agent-api.md). Test synthetic CSV, XLSX and encrypted inputs, invalid rejection, incomplete exclusions, no-op/enrichment, required-step failure and result persistence in the deployment.
6. Upgrade/reload Nginx and validate configuration. Its existing request limits/rate policy now also cover `/upload/api/precheck`.
7. Schedule `python manage.py configure_uploads --cleanup-only` at least every 30 minutes through the existing operational scheduler. This is private preview maintenance, not a deferred study/LLM queue.

No automatic deployment, production migration or second-project enablement was performed.

## Isolation and retention

Django authorizes inspection and owns the immutable effective settings/canonical rows. Uploaded originals use generated names in mode-0700 temporary directories outside public media; mode-0600 files are deleted after parsing, including rejection/timeout. Owner-scoped previews expire after 30 minutes. Confirmation accepts only a preview ID, never replacement bytes or settings. Terminal previews retain only counts; required in-flight payloads remain until completion/failure. Ambiguous dispatch keeps the existing reservation for operator reconciliation. Source payloads are purged on failure/cancellation and expired unbound previews are removed by maintenance.

One parser runs at a time across Django workers through the shared database reservation. Limits are 20 files / 49 MiB, 50,000 total rows, 128 columns, 65,536 characters per cell, one worksheet, 128 MiB expanded/decrypted input, 25 seconds per file / 60 seconds per submission, 20 CPU seconds and 768 MiB per parser. Linux workers use unprivileged Landlock to restrict reads to Python libraries and staging, and seccomp to deny network sockets/exec. Unsupported Linux kernels/architectures fail closed. Use Linux-native staging (Docker tmpfs/local /tmp); WSL drvfs staging was denied in verification. Windows development workers have Job Object memory/CPU limits; Windows is not the production filesystem/network isolation boundary.

Required grading steps must return valid grades/findings and exact output identities/cardinality. Grades 1–6 preserve the existing prompt's stated range; zero/failure is never normal. No intermediate grading CSV is written. Results and approved existing annotation enrichment commit in one short transaction; compare-and-fill checks reject stale previews. No database transaction spans an LLM call. Repeated polling cannot duplicate writes. Canonical task CSV uses stable field IDs and escapes spreadsheet formula prefixes; viewer/report exports remain on their existing paths. Successfully committed remote results are deleted; failed remote cleanup must be reconciled in task administration.

## Administration and reports

Storage destinations/types and write permissions are fixed in code. Shared field labels, aliases, interpretations, units, categories/ordering/positive class, bin boundaries and format rules are admin-only, revision-checked and audited. Profiling reads at most 1,000 authorized rows and returns advisory category/bin suggestions. Five equal-width numeric bins cover outer ranges; empty/constant data are handled separately. No defaults are silently changed.

New/explicitly edited visual report cards embed the relevant field meaning/bins/categories in `query.field_semantics`. Saved snapshots survive later configuration changes; legacy cards without snapshots retain the original catalogue. Stable source IDs remain unchanged. Identifiers cannot be numerically aggregated, classification requires compatible binary meanings/score scale, and duration summaries require seconds. Upload normalization does not rewrite stored values or report groupings.

Historical import endpoints are retired (410). Source files now use the shared mapped upload path; uploaded grading results cannot bypass the processor. Dedicated manual GT remains available.

## Deployment gates and rollback

Verify the actual Docker kernel/seccomp profile allows Landlock setup and parser limits. Verify encrypted workbook parsing and resource limits in that image. Exercise PostgreSQL concurrent preview/commit and supporting migration SQL on an isolated synthetic database; local executable coverage uses SQLite. Run `nginx -t` in the deployment image. Verify the real OpenAI-compatible endpoint with synthetic reports only. These are deployment gates, not claims established by local tests.

Rollback requires draining/reconciling tasks and reverting Django/API/Nginx together. Keep supporting tables and pinned report definitions while rolling back; older report code cannot interpret new field_semantics snapshots. Preserve a pre-upgrade report-definition backup. Do not reverse supporting migrations while jobs/previews are active or rewrite CXRStudy data as rollback. Historical-import resurrection is not a safe migration path.

Reference behavior: [Django atomic transactions](https://docs.djangoproject.com/en/5.2/topics/db/transactions/), [Python resource limits](https://docs.python.org/3/library/resource.html), [openpyxl read-only worksheets](https://openpyxl.readthedocs.io/en/stable/optimized.html).
