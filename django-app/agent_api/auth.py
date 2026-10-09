import hashlib
import hmac
import secrets
import re
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from report_v2.permissions import can_edit_catalog
from report_v2.projects.registry import require_project_context

from .models import APIConfiguration, APIKey

SUPPORTED_PROJECTS = ("prime",)


class APIError(Exception):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)


def error(code, status):
    return JsonResponse({"error": {"code": code, "message": code.replace("_", " ")}}, status=status)


def key_is_current(key):
    return (key.revoked_at is None and key.expires_at > timezone.now()
            and key.owner.is_active and can_edit_catalog(key.owner))


def authenticate(request, project):
    if not APIConfiguration.objects.filter(pk=1, enabled=True).exists():
        raise APIError("api_disabled", 503)
    if not settings.DEBUG and not request.is_secure():
        raise APIError("https_required", 403)
    raw = request.headers.get("X-API-Key", "")
    if not re.fullmatch(r"primer_[0-9a-f]{24}\.[A-Za-z0-9_-]{43}", raw):
        raise APIError("invalid_api_key", 401)
    public_id, separator, secret = raw[7:].partition(".")
    key = APIKey.objects.select_related("owner").filter(public_id=public_id).first() if separator else None
    if key is None or not hmac.compare_digest(key.secret_digest, hashlib.sha256(secret.encode()).hexdigest()) or not key_is_current(key):
        raise APIError("invalid_api_key", 401)
    request.api_key = key
    request.user = key.owner
    # shortcut: PRIME has the only isolated adapter; enable other projects after data isolation exists.
    if project not in SUPPORTED_PROJECTS:
        raise APIError("project_not_found", 404)
    require_project_context(project)
    if not key.projects.filter(project_id=project).exists():
        raise APIError("project_forbidden", 403)
    request.api_key = key
    request.user = key.owner
    APIKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key


def create_key(owner, label, expires_at, projects, *, public_id=None):
    public_id = public_id or secrets.token_hex(12)
    secret = secrets.token_urlsafe(32)
    key = APIKey.objects.create(owner=owner, label=label, public_id=public_id,
                               secret_digest=hashlib.sha256(secret.encode()).hexdigest(), expires_at=expires_at)
    key.projects.bulk_create([key.projects.model(key=key, project_id=p) for p in projects])
    return key, f"primer_{public_id}.{secret}"


def api_endpoint(method):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, project, **kwargs):
            request.api_request_id = secrets.token_hex(12)
            try:
                authenticate(request, project)
                if method is not None and request.method != method:
                    raise APIError("method_not_allowed", 405)
                if request.GET:
                    raise APIError("query_parameters_not_supported")
                if method == "GET" and request.body:
                    raise APIError("request_body_not_supported")
                response = view(request, project, **kwargs)
            except APIError as exc:
                response = error(exc.code, exc.status)
            except RequestDataTooBig:
                response = error("request_too_large", 413)
            except DatabaseError:
                response = error("service_unavailable", 503)
            except Exception:
                response = error("internal_error", 500)
            response["Cache-Control"] = "no-store"
            response["X-Request-ID"] = request.api_request_id
            if response.status_code == 401:
                response["WWW-Authenticate"] = "ApiKey"
            if response.status_code in (409, 429, 503):
                response["Retry-After"] = "5"
            try:
                from .audit import record
                record(request, "api_request", project=project, status=response.status_code)
            except DatabaseError:
                response = error("audit_unavailable", 503)
                response["Cache-Control"] = "no-store"
                response["X-Request-ID"] = request.api_request_id
            return response
        return wrapped
    return decorate
