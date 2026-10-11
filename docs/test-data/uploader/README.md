# Synthetic uploader fixtures

All identifiers, names, reports and sites are fictional. Generated outputs are not clinical benchmarks. The files match the current illustrative PRIME format seeds; administrator-edited formats may need custom mappings.

Start with `carpl-valid.csv` plus either `ris-valid.csv` or `ris-valid.xlsx`, selected together. On a blank database, precheck should detect PRIME CARPL/RIS and show six distinct complete studies. IDs are 991011000001 through 991011000006. Successful insertion needs the running analysis service and OpenAI-compatible inference endpoint.

| Files | Expected on blank database |
|---|---|
| carpl-valid.csv + ris-valid.csv or ris-valid.xlsx | 6 complete |
| carpl-valid.csv + ris-one-incomplete.csv | 5 complete, 1 discarded (missing report) |
| carpl-valid.csv + ris-all-incomplete.csv | 6 discarded; confirmation completes without analysis |
| carpl-valid.csv + ris-conflict.csv | Blocked: conflicting uploaded report |
| ris-invalid-id.csv | Blocked: fractional identifier |
| carpl-invalid-score.csv | Blocked: malformed score, even though other required fields are missing |
| carpl-out-of-range.csv | Blocked: score 101 exceeds 0–100 |
| existing-annotations.csv | Custom alias mapping; behavior depends on existing records |

For existing-record testing in an isolated synthetic database, create ID 991011000001 with Comments `Existing synthetic comment` and ID 991011000002 with empty Comments. Upload existing-annotations.csv: one enrichable, one unchanged, one stored-value disagreement. Confirm: first comment preserved, second filled with `New synthetic comment`. Re-upload: two unchanged. Without those records, both rows are incomplete and discarded.

Regenerate with `python docs/test-data/uploader/generate.py` from the checkout root. Requires installed Django and openpyxl. The generator uses synthetic test settings and never reads or writes a database. Do not clear a deployment database to try these examples.

Browser verification on 2026-10-11 passed CSV/XLSX autodetection, cross-file combination, incomplete exclusion/no-analysis confirmation, conflict/invalid rejection, existing fill-only preservation, no-op re-upload and mapping recheck. Read-only checks verified no new study/task writes from rejected or incomplete cases.

Observed issues: dispatch to the deliberately unavailable preview API returned 502 submission_outcome_unknown and retained a reservation without a task; Tasks showed zero tasks and later precheck returned processing_busy. The error status panel stayed hidden because showError does not display it. No application fixes were made during fixture verification. Full inference, encrypted XLSX, resource limits and production/PostgreSQL deployment were not tested here.
