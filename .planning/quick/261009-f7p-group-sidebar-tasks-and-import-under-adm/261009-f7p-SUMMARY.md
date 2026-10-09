---
status: complete
---

Added admin-only native collapsible categories: Admin contains Tasks and Import; More tools contains Report editor and Database. The current page opens its category automatically; links keep existing URLs, template blocks and active markers. Added category styling and keyboard focus indication.

Validation: all 5 VisualShellTemplateTests passed, including grouping, active-category expansion and ordinary-user exclusion; git diff --check passed. Restarted identified synthetic preview helper on port 8766; HTTP 200 and both category disclosures confirmed. Existing LLM HTTP transport warning is unrelated. No clinical datasets read. Browser interaction and production deployment were not checked.

Implementation commit: eab5f1f.
