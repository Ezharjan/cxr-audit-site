# H100 server deployment

User authorized bypassing unavailable GSD commands for this task on 2026-10-31.
The actual checkout Git root is D:/Github/cxr-audit-site, not a nested django/.

Plan: inspect deployment and inference code; verify vendor setup guidance; add an
opt-in single-H100 vLLM overlay, fresh-secret setup helper, synthetic smoke check,
regression coverage and a server runbook. Preserve default local deployment.

Scope: existing OpenAI-compatible API, Qwen3-32B BF16, private same-host inference,
offline model runtime after explicit download, existing PostgreSQL/parser services.
No patient data read, migrated, uploaded, or committed. No server access available.

Validation: deployment regression tests, Compose/YAML inspection, diff whitespace
check; GPU/container and clinical workflow acceptance are server-side steps.
