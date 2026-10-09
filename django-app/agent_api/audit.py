from audit.models import AuditEvent


def record(request, action, **metadata):
    key = getattr(request, "api_key", None)
    user = request.user
    AuditEvent.objects.create(
        username=user.get_username() if user.is_authenticated else "anonymous",
        user_id=user.pk if user.is_authenticated else None,
        category=AuditEvent.Category.USER_SELECTED,
        action=action,
        method=request.method,
        metadata={"key_id": key.public_id if key else None,
                  "request_id": getattr(request, "api_request_id", None), **metadata},
    )
