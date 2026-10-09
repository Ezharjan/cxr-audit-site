# API settings UI and optional expiry

Scope: configuration template/CSS/JS, key model/migration, authentication, creation validation, docs and regression tests.

1. Reproduce layout and lifecycle in Chromium; fix fieldset spacing, action styling and narrow table layout.
2. Add explicit Never expiry; null means no expiry, while revocation and owner checks remain required. Reject invalid dated requests.
3. Test actual copy, expiry switching, dated and nonexpiring keys, toggle and revocation; check desktop/mobile light/dark. Restart isolated preview and document results.
