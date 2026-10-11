# Synthetic uploader browser verification

1. Generate fictional CSV/XLSX CARPL and RIS fixtures matching PRIME seed headers, plus incomplete, conflicting, invalid-ID and invalid-score cases.
2. Start an isolated localhost Django preview with synthetic storage and seed configuration. Preserve all existing databases.
3. Drive browser upload/precheck/confirmation paths; record observed outcomes and backend limitations. Commit fixtures and GSD evidence only.
