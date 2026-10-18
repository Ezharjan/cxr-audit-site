---
status: complete
---
# Valid upload studies and concise skip summaries

Shared normalization/classification now excludes malformed or conflicting studies and permits valid complete studies to proceed. All contributions for an affected study are excluded, preserving strict interpretation rather than silently dropping invalid fields. Missing/invalid identifiers skip only their unidentifiable source rows. Structural mapping/file errors remain fatal. Counts are independent of the diagnostic sample cap; UI replaces per-cell error walls with aggregate counts and plain skip reasons. Public API counts include invalid studies and invalid source rows.

Verification: 53 synthetic upload/API tests passed, including mixed valid/invalid/conflicting studies, uncapped warning counts, and no-dispatch all-invalid API submissions. Aggregate-only verification against the two user-authorized files found 0 valid studies, 6,536 incomplete studies, 1,724 invalid studies, 1,736 invalid source rows and 5 missing/invalid identifier rows under default mappings. The old 54 complete count ignored invalid optional date values; RIS timestamp format configuration remains necessary to make those studies valid. No patient values were printed or committed. No deployment performed.
