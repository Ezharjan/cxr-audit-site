# Fix API runtime packaging

Copy canonical_processing.py into the API runtime image. Add a build-time import check to prevent images with a missing canonical pipeline. Run existing synthetic canonical tests. The reported import fails before task creation; explain reconciliation before retry.
