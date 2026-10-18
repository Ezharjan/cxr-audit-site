# Dedicated isolated Docker parser service

Authorized: replace mandatory production Landlock with an isolated Docker parser service.

1. Minimal non-root image, no network/capabilities/app mounts/full .env, private Unix socket transport, bounded ephemeral staging and one child per file.
2. Compose defaults to service; preserve local development subprocess and existing validation. Safe service failure response and visible upload errors.
3. Synthetic transport, parser and security regression tests; document deployment smoke checks and record results.

Actual Git and planning roots are the workspace root, unlike historical project notes. Preserve the existing .gitignore edit.
