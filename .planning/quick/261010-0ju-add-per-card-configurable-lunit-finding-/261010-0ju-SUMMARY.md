---
status: complete
---
# Configurable Lunit thresholds per report card

Completed: 2026-10-10 (Asia/Singapore)
Source commit: 3dc74a5

Implemented optional query.lunit_thresholds with selected findings, a default cutoff and per-finding overrides. Existing definitions retain reviewed policy defaults. The card editor provides cutoff inputs and reset; saved settings are part of immutable published definitions.

The shared prediction engine applies strict > comparisons to original scores under a policy resolved once per card. Missing selected scores remain ineligible. LLM and stored Lunit labels are never rewritten. Existing per-finding LLM labels are now selectable references. Tables/charts, CSV, frozen print/PDF and email retain effective cutoff metadata. Paging cache identity includes the card definition.

Validation rejects unknown/duplicate/empty findings, unrelated sources, absent policies, invalid bounds, booleans and non-finite values. Generic YAML loader remains vocabulary-free; project/display validation owns the clinical rules.

Verification:
- Broad reporting and authenticated API suite: 237 tests, OK; 1 host-specific symlink test skipped.
- Chromium editor suite plus new cutoff cases: all six browser cases passed (14-test run had a CSV test naming error, fixed and passed in the broad suite).
- After resolving the policy once per card: 88 affected tests passed; final eight focused tests passed after import/check cleanup.
- Node syntax check and git diff --check passed.
- Synthetic 10,000-study pure-classification benchmark with policy resolved once: defaults 0.396 seconds; custom all-findings >10 0.422 seconds. This excludes database and report rendering and is not a production timing guarantee.

No dependency, migration, inference request or deployment was added. No PHI-bearing database or uploaded dataset was inspected.
Usage: docs/report-card-thresholds.md.

Repository-boundary deviation: the supplied AGENTS.md refers to a removed django/ nesting. User explicitly approved using the actual root Git repository and its existing .planning/ directory.
