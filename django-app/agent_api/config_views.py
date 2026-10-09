from datetime import datetime, time, timedelta
import secrets

from django.core import signing

from django.contrib.auth.decorators import login_required
from django.db import transaction, IntegrityError
from django.http import HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from report_v2.permissions import can_edit_catalog

from .audit import record
from .auth import SUPPORTED_PROJECTS, create_key
from .models import APIConfiguration, APIKey


@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def configuration(request):
    if not request.user.is_active or not can_edit_catalog(request.user):
        return HttpResponseForbidden("Admin access required")
    secret = None
    message = None
    if request.method == "POST":
        action = request.POST.get("action")
        with transaction.atomic():
            if action == "toggle" and request.POST.get("confirmed") == "yes" and request.POST.get("enabled") in ("true", "false"):
                enabled = request.POST.get("enabled") == "true"
                APIConfiguration.objects.update_or_create(pk=1, defaults={"enabled": enabled, "changed_by": request.user})
                record(request, "api_enabled" if enabled else "api_disabled")
                return redirect("agent_api:configuration")
            elif action == "revoke" and request.POST.get("confirmed") == "yes":
                key_id = request.POST.get("key_id", "")
                if not key_id.isascii() or not key_id.isdigit() or len(key_id) > 18:
                    return HttpResponseForbidden("Invalid key identifier")
                key = get_object_or_404(APIKey, pk=key_id, owner=request.user)
                if key.revoked_at is None:
                    key.revoked_at = timezone.now()
                    key.save(update_fields=["revoked_at"])
                    record(request, "api_key_revoked", key_id=key.public_id)
                return redirect("agent_api:configuration")
            elif action == "create":
                try:
                    owner_id, public_id = signing.loads(request.POST.get("creation_token", ""), salt="api-key-creation", max_age=1800)
                    if owner_id != request.user.pk:
                        raise signing.BadSignature("owner")
                except (signing.BadSignature, ValueError, TypeError):
                    return HttpResponseForbidden("Reload this page before creating a key.")
                if APIKey.objects.filter(public_id=public_id).exists():
                    return redirect("agent_api:configuration")
                label = request.POST.get("label", "").strip()
                projects = sorted(set(request.POST.getlist("projects")))
                expiry_mode = request.POST.get("expiry_mode", "date")
                expires = None
                if expiry_mode == "date":
                    try:
                        expires = timezone.make_aware(datetime.combine(datetime.strptime(request.POST.get("expires", ""), "%Y-%m-%d").date(), time.max))
                    except ValueError:
                        pass
                valid_expiry = expiry_mode == "never" or (expiry_mode == "date" and expires is not None and expires > timezone.now())
                if not label or len(label) > 100 or not projects or not set(projects) <= set(SUPPORTED_PROJECTS) or not valid_expiry:
                    message = "Enter a label, a supported project, and a future expiry date or select Never."
                else:
                    try:
                        with transaction.atomic():
                            key, secret = create_key(request.user, label, expires, projects, public_id=public_id)
                    except IntegrityError:
                        return redirect("agent_api:configuration")
                    record(request, "api_key_created", key_id=key.public_id, projects=projects)
            else:
                message = "Confirm the requested action."
    response = render(request, "agent_api/configuration.html", {
        "now": timezone.now(),
        "enabled": APIConfiguration.objects.filter(pk=1, enabled=True).exists(),
        "keys": APIKey.objects.filter(owner=request.user).prefetch_related("projects").order_by("-created_at"),
        "creation_token": signing.dumps([request.user.pk, secrets.token_hex(12)], salt="api-key-creation"),
        "projects": SUPPORTED_PROJECTS, "secret": secret, "error": message,
        "default_expiry": (timezone.localdate() + timedelta(days=90)).isoformat(),
    }, status=400 if message else 200)
    response["Cache-Control"] = "no-store"
    return response
