---
status: complete
---

Report-page Edit report links now use the existing catalog canonical name mapping directly, including duplicate-name disambiguation. Ordinary users receive no editor link. Stored IDs and report data unchanged.

Validation: 49 page/editor tests passed, including direct canonical link, duplicate titles matching catalog, 200 editor response and ordinary-user exclusion. Browser fixture updated; live browser suite not run. git diff --check passed.
