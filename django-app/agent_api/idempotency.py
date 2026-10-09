import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from .auth import APIError
from .models import IdempotencyRecord


def claim(request, project, operation, digest):
    value = request.headers.get("Idempotency-Key", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise APIError("idempotency_key_required")
    lookup = dict(key=request.api_key, project_id=project, operation=operation, value=value)
    try:
        with transaction.atomic():
            entry = IdempotencyRecord.objects.create(**lookup, request_digest=digest)
        return entry, None
    except IntegrityError:
        entry = IdempotencyRecord.objects.get(**lookup)
    if entry.created_at < timezone.now() - timedelta(days=7):
        raise APIError("idempotency_key_expired", 409)
    if entry.request_digest != digest:
        raise APIError("idempotency_conflict", 409)
    if entry.state == "complete":
        return entry, JsonResponse(entry.response, status=entry.status_code)
    raise APIError("operation_outcome_unknown" if entry.state == "unknown" else "operation_in_progress", 409)


def finish(entry, body, status=200):
    entry.state, entry.response, entry.status_code = "complete", body, status
    entry.save(update_fields=["state", "response", "status_code"])
    return JsonResponse(body, status=status)


def uncertain(entry):
    IdempotencyRecord.objects.filter(pk=entry.pk).update(state="unknown")
