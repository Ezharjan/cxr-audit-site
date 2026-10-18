---
status: complete
---
# Dedicated Docker parser service

Implemented a minimal parser image and bounded private Unix-socket protocol. Compose routes ingestion through a non-root, network-disabled, read-only service with no capabilities, no application data mounts and only the optional workbook password. The shared volume contains only the socket; original uploads and parsed output stay in private temporary directories, with service staging on bounded tmpfs. A fresh child applies the existing limits to each file. No Docker socket or privileged service is used.

Django keeps existing file validation, digest and total-row limits, submission reservation and local development parser behavior. Configured service failures never fall back to local parsing. UI and routine API expose safe 503 responses; the hidden upload error box is fixed. UID/GID and socket-directory permissions are consistent across images.

Verification:
- Existing upload/API suite plus transport checks: 55 tests passed.
- After final deadline/permission changes: 6 focused tests passed, including the new API 503/idempotency regression and all 5 transport tests.
- Actual Linux Unix-socket service/subprocess: 7 tests passed, rerun after final transport changes; CSV exact identifiers, XLSX, encrypted XLSX, formula rejection, malformed protocol, timeout/staging cleanup and recovery.
- JavaScript VM regression: error box visible and correct safe text.
- Compose YAML and isolation/resource invariants, Python syntax and git diff --check passed.

Docker CLI/engine is unavailable in this workspace. Image build, Compose startup, runtime socket ownership/read-only-volume connection and PVE LXC hardening checks remain deployment smoke tests, documented in docs/upload-configuration.md. No deployment performed. Existing .gitignore changes are excluded from this task's commit.
