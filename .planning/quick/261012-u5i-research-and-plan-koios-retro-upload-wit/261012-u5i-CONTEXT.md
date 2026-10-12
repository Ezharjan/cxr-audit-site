# Confirmed design decisions — 2026-10-12

User confirmed shared understanding after grill-me/grilling rounds.

1. PRIME first; architecture supports project-owned models and more complex projects. Koios is illustrative and deferred.
2. One model per project/study type; studies are records; related models supported by adapter persistence.
3. Developer-owned model/step/validation contracts; admin-configurable formats/mappings/presentation.
4. Retro capability applies across supported projects, approved outputs defined per adapter.
5. Block conflicts and all attempted existing-record enrichment; identical supplied records skipped. Missing-field filling is a future TODO.
6. PRIME retro additional destination: llm_grade only. Derive gt_llm; preserve internal Lunit prediction for legacy consumers.
7. Preserve supplied outputs; grouped processing fills only missing outputs and rejects inconsistent combinations.
8. Skip grade inference for supplied grades; optional selected supplemental inference still runs.
9. Project-specific completeness including valid null/empty results for future projects.
10. Report text required only for rows needing selected/mandatory LLM work; retain PRIME identity/workplace/Lunit score requirements.
11. Final accepted writes atomic; same shared uploader and mapping UI with protected /retro-upload route.

These decisions supersede the original Koios-first handoff and its existing-output enrichment/production-storage tasks. No Koios five-column schema is prescribed for the shared architecture.
