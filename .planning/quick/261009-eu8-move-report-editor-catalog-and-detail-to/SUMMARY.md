---
status: complete
---

Catalog is now /editor/; detail is /editor/<report>/. Named navigation, creation responses and action URLs follow the new namespace. /layout/ redirects to the catalog; legacy detail and action URLs remain compatible. No stored report identities or data changed.

Validation: 56 Django editor, page and visual-editor tests and 6 JavaScript navigation tests passed; git diff --check passed. Browser fixtures updated, browser suites not run.
