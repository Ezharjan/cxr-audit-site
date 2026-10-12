---
status: complete
---
# Research and planning summary

Created IMPLEMENTATION-PLAN.md after reading shared ingestion/mapping/storage/task/UI/API/processor boundaries and external Koios source only. Koios has no production model/adapter here. The plan adds isolated Koios storage, shares upload controls, preserves PRIME, groups missing outputs for one thyroid extraction call and handles successful nulls separately from failures. User-confirmed llm_extract.py is the processing reference.

Verification: static source walkthrough and plan review; no datasets, tests, runtime services or application code changed. Implementation and deployment gates remain in the handoff.
