---
status: complete
---
# API canonical runtime packaging

Root cause: the API Dockerfile selectively copied modules but omitted canonical_processing.py. /analyze-canonical imports it before creating a task, causing HTTP 500 without dispatch. Django conservatively reports submission_outcome_unknown and retains its reservation.

Added the module COPY and a runtime-stage build-time import check for both validate_rows and process. Six existing canonical tests passed; git diff --check passed. Docker build/deployment not run because Docker is unavailable locally. Rebuild the UAT api service after updating the checkout; reconcile the taskless reservation before retrying. Remote push requires explicit approval after earlier automatic-review rejection.
