---
status: complete
---
# Restored custom upload picker

Native multiple-file input is explicitly hidden. The existing file-drop surface is a type=button with the same stylesheet and existing picker/drop handlers, supporting keyboard activation without submitting the form. Unified source selection and precheck are preserved.

Verification: focused UploadBrowserTests passed. It now asserts the native input is hidden, activates the custom button via a real file chooser, selects two files and completes mapping/precheck/no-analysis flow. Browser accessibility state and rendered screenshot confirmed the stock Choose Files/No file chosen row is absent and the existing dashed picker styling remains. git diff --check passed.

No backend or production database changes. Real LLM processing not exercised.
