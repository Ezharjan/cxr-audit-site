---
status: complete
---
# Restore standard CARPL/RIS export compatibility

Default field rules now handle CARPL dash score sentinels and deterministic RIS timestamps independently of exact saved-header matches. The same rules apply when mappings are resubmitted, while saved per-column rules override defaults. ISO offsets, timezone/DST validation, strict identifiers, protected fields and exact named API format signatures remain enforced. No database/configuration mutation or reseeding required.

Aggregate-only verification against the user-authorized files: 54 valid complete studies, 8,206 incomplete studies, 0 invalid identifiable studies, and 5 rows rejected for invalid identifiers; blocked=False. No PHI-bearing values or data files were committed. Counts assume default settings and no existing studies; deployment saved overrides and existing studies can affect counts.

Synthetic regression coverage includes 12-hour month-first timestamps, 24-hour day-first timestamps, midnight, ISO offsets, malformed dates, dash versus zero scores, recheck mappings, saved ISO overrides, named API header checks and combined valid/incomplete source exports. Full upload/API suite: 56 tests passed. git diff --check passed. No deployment performed.
