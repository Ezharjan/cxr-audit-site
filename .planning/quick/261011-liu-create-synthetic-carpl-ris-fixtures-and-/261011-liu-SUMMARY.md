---
status: complete
---
# Synthetic upload verification

Created reusable fictional CARPL/RIS CSV and RIS XLSX files, incomplete/conflict/invalid cases and existing-record annotations under docs/test-data/uploader. Generator includes basic assertions. Browser verification and exact expected scenarios are documented in README.md.

Used separate blank temporary SQLite databases at primer-uploader-261011 and primer-uploader-validation-261011. No deployment datasets were read or cleared. First database retained the ambiguous dispatch reservation for evidence. Validation database contains exactly two deliberately seeded fictional records; one missing comment was filled and the other preserved. No processing tasks exist in either database.

Passed: exact seed autodetection, six-study cross-file join, mixed incomplete, all-incomplete confirmation without analysis, conflicting values, fractional identifier, malformed and out-of-range scores, fill-missing preservation, repeat no-op and mapping invalidation/recheck/exclusion.

Findings: unavailable API confirmation returns 502 submission_outcome_unknown with an unbound reservation and no task-administration reconciliation entry; subsequent precheck returns 409 processing_busy. showError leaves the initially hidden statusContainer hidden. Recorded, not fixed in this task.

Not verified: full real inference/persistence, encrypted workbook, production kernel/resource limits, PostgreSQL, deployment. The localhost preview remains available with isolated synthetic data.
