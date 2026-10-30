"""Project-level deployment and transport checks."""

import ipaddress
import os
from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, Warning, register

# Values shipped in examples/defaults that must never protect a running service.
_PLACEHOLDER_SECRETS = {
    "",
    "change-me",
    "change-me-to-a-random-secret",
    "change-me-to-a-real-secret-key",
    "changeme",
    "dummy",
}
_MIN_LOCAL_SECRET_LENGTH = 32


def is_loopback_host(host):
    """True only for loopback names/addresses (no wildcard, no LAN address)."""

    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url_is_loopback(url):
    try:
        return is_loopback_host(urlparse(url).hostname)
    except ValueError:
        return False


def _weak_secret(value):
    value = value or ""
    return value.lower() in _PLACEHOLDER_SECRETS or value.startswith("django-insecure") or len(value) < _MIN_LOCAL_SECRET_LENGTH


@register()
def check_local_dev_profile(app_configs, **kwargs):
    """Fail closed when the explicit local development profile is not loopback-only.

    PRIMER_LOCAL_DEV relaxes HTTPS-only cookies/redirects, so it is only allowed
    for a single workstation: loopback hosts, loopback API and LLM endpoints,
    SQLite, a non-default Django secret and authenticated service API calls.
    """

    if not getattr(settings, "PRIMER_LOCAL_DEV", False):
        return []

    errors = []
    bad_hosts = [h for h in settings.ALLOWED_HOSTS if not is_loopback_host(h)]
    if bad_hosts:
        errors.append(Error(
            "PRIMER_LOCAL_DEV allows only loopback ALLOWED_HOSTS.",
            hint="Use DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost.",
            id="lunit_audit.E101",
        ))
    for alias, config in settings.DATABASES.items():
        if config.get("ENGINE") != "django.db.backends.sqlite3":
            errors.append(Error(
                f"PRIMER_LOCAL_DEV requires SQLite for the '{alias}' database.",
                hint="Unset DATABASE_ENGINE/AUDIT_DATABASE_ENGINE or set them to sqlite.",
                id="lunit_audit.E102",
            ))
    if not is_loopback_host(str(settings.CXR_API_CONFIG.get("API_IP", ""))):
        errors.append(Error(
            "PRIMER_LOCAL_DEV requires the analysis API on loopback.",
            hint="Use CXR_API_IP=127.0.0.1.",
            id="lunit_audit.E103",
        ))
    if not _url_is_loopback(settings.LLM_BASE_URL):
        errors.append(Error(
            "PRIMER_LOCAL_DEV requires a loopback LLM endpoint.",
            hint="Use OLLAMA_BASE_URL=http://127.0.0.1:11434/v1.",
            id="lunit_audit.E104",
        ))
    if _weak_secret(os.environ.get("API_SECRET_KEY", "")):
        errors.append(Error(
            "PRIMER_LOCAL_DEV requires a generated API_SECRET_KEY (32+ characters).",
            hint="Run the Windows launcher 'init' command to generate local secrets.",
            id="lunit_audit.E105",
        ))
    if _weak_secret(settings.SECRET_KEY):
        errors.append(Error(
            "PRIMER_LOCAL_DEV requires a generated DJANGO_SECRET_KEY.",
            hint="Run the Windows launcher 'init' command to generate local secrets.",
            id="lunit_audit.E106",
        ))
    if os.environ.get("API_REQUIRE_AUTH", "").lower() not in ("true", "1", "yes"):
        errors.append(Error(
            "PRIMER_LOCAL_DEV requires API_REQUIRE_AUTH=True.",
            id="lunit_audit.E107",
        ))
    if settings.DEBUG:
        errors.append(Warning(
            "DEBUG is enabled in the local development profile.",
            hint="Error pages may echo request values; keep DJANGO_DEBUG=False with synthetic data.",
            id="lunit_audit.W110",
        ))
    return errors


@register()
def check_llm_transport(app_configs, **kwargs):
    """Warn when production uses plaintext LLM transport without an explicit allow flag."""

    if settings.DEBUG:
        return []

    parsed = urlparse(settings.LLM_BASE_URL)
    if parsed.scheme != "http":
        return []

    if getattr(settings, "PRIMER_LOCAL_DEV", False) and _url_is_loopback(settings.LLM_BASE_URL):
        # Same-host loopback inference; the local profile check enforces loopback.
        return []

    if settings.LLM_ALLOW_INSECURE_TRANSPORT:
        return [
            Warning(
                "LLM_BASE_URL uses HTTP in production with LLM_ALLOW_INSECURE_TRANSPORT enabled.",
                hint=(
                    "Use this only for isolated private transport. Prefer HTTPS or a "
                    "controlled private inference network for production ePHI workflows."
                ),
                id="lunit_audit.W001",
            )
        ]

    return [
        Warning(
            "LLM_BASE_URL uses HTTP while DEBUG=False.",
            hint=(
                "Use an HTTPS OpenAI-compatible endpoint, or set "
                "LLM_ALLOW_INSECURE_TRANSPORT=True only after documenting private "
                "network isolation and endpoint authentication."
            ),
            id="lunit_audit.W002",
        )
    ]
