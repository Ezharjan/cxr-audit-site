---
status: complete
---
# Unified uploader selector

Replaced CARPL and RIS selectors with one sourceFiles multiple CSV/XLS/XLSX input, selected-file list and shared drop zone. Precheck keeps detecting format from headers; no backend changes. Updated change/clear, task locking, multipart collection and existing browser regression caller. Upload settings now reuses icon-upload.

Verification: node --check passed; 14 focused tests passed (upload.test_ingestion_browser, VisualShellTemplateTests, VisualPageRenderTests). Browser selected real synthetic CARPL CSV + RIS XLSX together, previewed six combined studies (four new complete plus two intentionally seeded existing rows), cleared selection/preview, then reversed file order and verified RIS/CARPL autodetection. Fresh localhost browser console: zero errors. Existing browser regression now uploads two files and confirms a single combined incomplete study, mapping recheck, safe headers, no-analysis completion and mobile width.

Preview template required restart; previously cached JavaScript on 127.0.0.1 needed cache refresh. Fresh localhost origin verified current assets. No deployment performed. Analysis remains offline in the synthetic preview. Earlier hidden error and ambiguous reservation findings remain open in the fixture README.

Implementation commit: 6fdb50b (see git log for feat(upload): unify source file selection and settings icon).

