# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Admin-only YAML layout editor: a thin, security-gated UI over the verified backends.

This module adds *no* new persistence, validation or evaluation logic. Every durable move is
delegated to :class:`report_v2.definitions.repository.DefinitionRepository` (Task 11), and every
validation/rendering answer comes from the strict loader (Task 03), the display validators
(Task 09) and :func:`report_v2.evaluation.evaluate` (Task 10).

Security posture (the point of Task 12):

* **Every** view -- including the page itself -- first passes :func:`require_admin`, which reuses the
  one house edit predicate (``admins`` group or superuser) from :mod:`report_v2.permissions`. An
  anonymous visitor is redirected to the login page and an authenticated non-admin gets ``403``,
  *before* any model or filesystem work happens. That holds for a hand-crafted ``POST`` straight to
  a mutation URL too: the gate is the first statement inside the view, not a template nicety.
* Every mutating view additionally carries ``@require_POST`` plus ``csrf_protect``, so a
  cross-site form post (no token) is rejected by ``CsrfViewMiddleware`` before the view body runs.
* Publication changes require an explicit Publish, Unpublish, or Save on an already-published report. ``editor_preview`` never publishes and never sends mail; a rejected publish
  leaves the old pointer exactly where it was (Task 11's guarantee, relied upon, never bypassed).
* Visual transformations update submitted YAML in memory; Save and publication actions persist changes.
"""
from __future__ import annotations

from collections import Counter
import json
import re
import yaml
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings as django_settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from .field_semantics import current_settings
from . import data
from . import permissions
from . import seeding
from .definitions import visual
from .definitions.validation import DisplayValidationError
from .definitions.repository import (
    DefinitionRepository,
    DraftNotFoundError,
    PublishRejectedError,
    RepositoryError,
    StaleRevisionError,
    default_root,
)
from .definitions.loader import DefinitionError, load_report_definition, load_policy, flat_report_definition
from .views import _evaluate, _widget_frame, _validate_overrides, OverrideRejectedError

__all__ = [
    "editor",
    "editor_new",
    "editor_delete",
    "editor_preview",
    "editor_visual",
    "editor_publish",
    "editor_save_draft",
    "editor_seed",
]

#: The editor edits the definitions of the single production project (Task 04).
PROJECT_ID = "prime"

#: Starter scaffold with one working card for a brand-new report.
STARTER_YAML = """\
schema_version: 1
project: prime
id: {def_id}
title: New report
grid:
  columns: 12
  row_height_px: 64
sections:
  - id: summary
    title: Summary
    widgets:
      - id: total
        title: Record count
        type: value
        layout:
          width: 3
          height: 2
        query:
          measurement: record_count
          inputs: {{}}
        window:
          start: M
          end: D
        controls:
          date_range: true
          filters: [site]
          compare_by: [site]
        ci:
          enabled: false
        export: full
"""


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _param(request, name: str, default: Any = "") -> Any:
    """Read one body parameter from a form-encoded or JSON request body.

    Missing keys yield *default*; the CSRF field is never echoed back. The body is only decoded
    for ``application/json`` requests, so a form post never has to be parsed twice.
    """
    value = request.POST.get(name, None)
    if value is None:
        content_type = request.META.get("CONTENT_TYPE", "") or ""
        if "application/json" in content_type:
            try:
                payload = json.loads(request.body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                payload = {}
            if isinstance(payload, dict):
                value = payload.get(name, default)
    return default if value is None else value


def _expected_revision(request) -> str | None:
    """The caller's optimistic-concurrency token; an empty value means 'no assertion'."""
    raw = _param(request, "expected_revision", None)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _user_of(request):
    """The acting user, defaulting to the anonymous one when no middleware supplied it."""
    user = getattr(request, "user", None)
    if user is None:
        return AnonymousUser()
    return user


def _login_redirect(request) -> HttpResponseRedirect:
    """Mirror the house ``user_passes_test`` behaviour: send anonymous users to the login page."""
    login_url = str(getattr(django_settings, "LOGIN_URL", "login") or "login")
    next_path = getattr(request, "get_full_path", lambda: request.path)()
    return HttpResponseRedirect(f"/{login_url.strip('/')}/?next={quote(next_path, safe='/?=&')}")


def _admin_gate(request):
    """Return a denial response, or ``None`` when the request may proceed.

    The predicate is the *shared* house one (``report_v2.permissions.can_edit_catalog`` ->
    ``_is_admin``): superuser or member of the ``admins`` group. Nothing here invents a second
    authorisation scheme, and nothing touches the filesystem or the models before this returns.
    """
    user = _user_of(request)
    if not getattr(user, "is_authenticated", False):
        return _login_redirect(request)
    if not permissions.can_edit_catalog(user):
        return HttpResponseForbidden("admin only")
    return None


def require_admin(view_func):
    """Gate a view behind the shared admin predicate *before* any other work is done.

    The wrapper deliberately does not copy ``require_POST``/``csrf_protect`` markers off the inner
    view: ``functools.wraps`` would otherwise publish the mutation views' ``require_POST`` restriction
    onto the admin wrapper and reject the very ``POST`` the view exists to serve. The attributes are
    therefore set from the *wrapper's* own needs, and ``csrf_exempt`` stays absent/false so the CSRF
    middleware keeps protecting every mutating endpoint.
    """

    def _wrapped(request, *args, **kwargs):
        denied = _admin_gate(request)
        if denied is not None:
            return denied
        return view_func(request, *args, **kwargs)

    _wrapped.__name__ = getattr(view_func, "__name__", "view")
    _wrapped.__doc__ = view_func.__doc__
    _wrapped.__wrapped__ = view_func
    return _wrapped


def _repository() -> DefinitionRepository:
    """One repository per request, rooted at the configured (never public) publish root.

    ``default_root()`` is consulted per call so an operator -- or a test -- can relocate the whole
    editor by pointing ``settings.REPORT_V2_ROOT`` at a scratch directory; nothing is ever written
    under ``STATIC_ROOT``/``MEDIA_ROOT``.
    """
    return DefinitionRepository(default_root(), project_id=PROJECT_ID)


def _draft_ids(repo: DefinitionRepository) -> list[str]:
    """The def_ids that currently have a draft, read from the private drafts directory."""
    drafts = Path(repo._drafts_dir)
    if not drafts.exists():
        return []
    return sorted(p.name for p in drafts.iterdir() if p.is_file() and not repo.is_deleted(p.name))


def _report_entries(repo: DefinitionRepository) -> list[dict[str, str]]:
    """Selector entries: the union of published slugs and drafted def_ids, each with a state label.

    ``state_label`` is exactly one of ``draft only`` (draft, no pointer), ``published`` (pointer,
    no draft) or ``published + draft`` (both). A failing ``data.list_published`` read is treated as
    "no published entries" so the editor never 500s on a broken published tree.
    """
    published: set[str] = set()
    try:
        published = {entry["slug"] for entry in data.list_published(project_id=PROJECT_ID)}
    except Exception:
        published = set()
    drafts = set(_draft_ids(repo))
    entries: list[dict[str, str]] = []
    for def_id in sorted(published | drafts):
        if repo.is_deleted(def_id):
            continue
        has_draft = def_id in drafts
        has_published = def_id in published
        if has_draft and has_published:
            state_label = "published + draft"
        elif has_draft:
            state_label = "draft only"
        else:
            state_label = "published"
        text, revision = "", ""
        version = repo.get_current_version(def_id) or ""
        if has_draft:
            text, revision = repo.read_draft(def_id)
        elif version:
            text = repo._blob_path(def_id, version).read_text(encoding="utf-8")
        title = def_id
        try:
            title = load_report_definition(text).get("title", def_id)
        except DefinitionError:
            try:
                load_policy(text)
                continue  # Policies are not reports and have no report editor/list actions.
            except DefinitionError:
                pass  # Invalid report drafts must remain available for correction.
        entries.append({"def_id": def_id, "title": title, "state_label": state_label,
                        "revision": revision, "version": version})
    names = {entry["def_id"]: slugify(entry["title"]).replace("-", "_") or entry["def_id"] for entry in entries}
    counts = Counter(names.values())
    identifiers = set(names)
    used = set()
    for entry in entries:
        def_id = entry["def_id"]
        name = names[def_id]
        if counts[name] > 1:
            name += "__" + def_id
        while name in used or (name in identifiers and name != def_id):
            name += "__" + def_id
        used.add(name)
        entry["editor_slug"] = name
        entry["editor_url"] = reverse("report_editor:editor_detail", args=[name])
    return entries


# ---------------------------------------------------------------------------
# The editor page (read-only)
# ---------------------------------------------------------------------------
@require_GET
@require_admin
def editor(request, def_id: str | None = None):
    """Render the YAML layout editor (admins only; no draft is written on this path)."""
    repo = _repository()
    reports = _report_entries(repo)
    if def_id is None:
        return render(request, "report_v2/catalog.html", {"reports": reports})
    report = next((entry for entry in reports if entry["def_id"] == def_id), None)
    if report is None:
        report = next((entry for entry in reports if entry["editor_slug"] == def_id), None)
    if report is None:
        from django.http import Http404
        raise Http404("Report not found")
    if request.path != report["editor_url"]:
        return HttpResponseRedirect(report["editor_url"])
    def_id = report["def_id"]
    draft_text = ""
    revision = ""
    source_state = "none"
    published_version = repo.get_current_version(def_id)
    if def_id:
        try:
            # A draft always wins when it exists (the private working copy).
            draft_text, revision = repo.read_draft(def_id)
            source_state = "draft"
        except (DraftNotFoundError, RepositoryError):
            draft_text, revision = "", ""
            # Fall back to the PUBLISHED blob text -- read into memory only, never written back.
            try:
                published_version = repo.get_current_version(def_id)
            except (RepositoryError, Exception):
                published_version = None
            if published_version:
                source_state = "published-only"
                try:
                    draft_text = repo._blob_path(def_id, published_version).read_text(encoding="utf-8")
                except (RepositoryError, OSError):
                    draft_text = ""
    source_label = {
        "draft": "Editing private draft",
        "published-only": "Editing published YAML in memory — not saved as a draft",
        "none": "New report (no draft yet)",
    }[source_state]
    widgets = []
    try:
        draft_text = yaml.safe_dump(flat_report_definition(load_report_definition(draft_text)), sort_keys=False, allow_unicode=True)
        layout = load_report_definition(draft_text)
        widgets = [{"id": widget["id"], "title": widget.get("title", widget["id"])}
                   for section in layout.get("sections", []) for widget in section.get("widgets", [])]
    except DefinitionError:
        pass
    return render(
        request,
        "report_v2/layout.html",
        {
            "def_id": def_id or "",
            "report_title": report["title"],
            "draft_text": draft_text,
            "revision": revision,
            "reports": reports,
            "widgets": widgets,
            "project_id": PROJECT_ID,
            "source_state": source_state,
            "published_version": published_version,
            "source_label": source_label,
        },
    )


# ---------------------------------------------------------------------------
# Mutating endpoints (admin + POST + CSRF, all three)
# ---------------------------------------------------------------------------
@require_POST
@csrf_protect
@require_admin
def editor_save_draft(request):
    """Save private edits; valid edits to an already-published report update its live version."""
    def_id = str(_param(request, "def_id", "")).strip()
    yaml_text = str(_param(request, "yaml_text", ""))
    if not def_id:
        return JsonResponse({"error": "def_id is required"}, status=400)
    repo = _repository()
    try:
        yaml_text = yaml.safe_dump(flat_report_definition(load_report_definition(yaml_text)), sort_keys=False, allow_unicode=True)
    except DefinitionError:
        pass
    try:
        revision = repo.save_draft(def_id, yaml_text, expected_revision=_expected_revision(request))
    except (StaleRevisionError,):
        # Optimistic concurrency: the caller's token is out of date. The stored draft is left
        # exactly as it is (no clobber) and the client is told to reload.
        return JsonResponse(
            {"error": "version conflict: reload", "def_id": def_id, "status": "conflict"},
            status=409,
        )
    except (RepositoryError,) as exc:
        return JsonResponse({"error": str(exc), "def_id": def_id}, status=400)
    errors = repo.validate_preview(def_id, yaml_text)
    published_version = repo.get_current_version(def_id)
    if published_version and not errors:
        try:
            receipt = repo.publish(def_id, yaml_text, expected_revision=revision, expected_version=published_version)
            published_version = receipt.version
        except StaleRevisionError:
            return JsonResponse({"error": "version conflict: reload", "status": "conflict"}, status=409)
        except RepositoryError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"def_id": def_id, "revision": revision, "yaml_text": yaml_text, "status": "saved",
                         "valid": not errors, "errors": errors, "published_version": published_version,
                         "published": bool(published_version and not errors)}, status=200)


@require_POST
@csrf_protect
@require_admin
def editor_preview(request):
    """Validate and render a preview. Read-only by contract: no draft, no publish, no mail.

    ``repo.validate_preview`` (loader 03 + display validators 09) produces the typed error list;
    only when that list is empty do we evaluate the selected widget with temporary, validated
    control overrides and render the same card as the published report. An evaluation failure
    is reported as preview information -- it can never turn into a publication.
    """
    def_id = str(_param(request, "def_id", "")).strip()
    yaml_text = str(_param(request, "yaml_text", ""))
    repo = _repository()
    probe_id = def_id or "__preview__"
    errors = repo.validate_preview(probe_id, yaml_text)
    payload: dict[str, Any] = {"def_id": def_id, "errors": errors, "valid": not errors, "preview": None}
    if errors:
        return JsonResponse(payload, status=200)
    layout = visual.project_document(yaml_text)
    widgets = [widget for section in layout.get("sections", []) for widget in section.get("widgets", [])]
    payload["widgets"] = [{"id": widget["id"], "title": widget.get("title", widget["id"])} for widget in widgets]
    if str(_param(request, "list_only", "")) == "1":
        return JsonResponse(payload)
    widget_id = str(_param(request, "widget_id", "")).strip()
    widget = next((widget for widget in widgets if widget["id"] == widget_id), None) if widget_id else next((card for card in widgets if card["type"] != "heading"), next(iter(widgets), None))
    if widget is None:
        payload["preview_error"] = "Select a card from this report to preview." if widget_id else "Add a card to preview this report."
        return JsonResponse(payload, status=400 if widget_id else 200)
    try:
        overrides = json.loads(str(_param(request, "overrides", "{}")))
        if not isinstance(overrides, dict):
            raise OverrideRejectedError("preview overrides must be an object")
        date, filters, comparison, grouping, page = _validate_overrides(widget, overrides)
    except (ValueError, TypeError, OverrideRejectedError) as exc:
        return JsonResponse({"error": str(exc), "status": "rejected"}, status=400)
    try:
        payload["preview"] = _evaluate(
            layout, widget, date_override=date, filters=filters, comparison=comparison,
            grouping=grouping, page=page, time_grouping=overrides.get("time_grouping"),
        )
        payload["card_html"] = render_to_string("report_v2/_widget_card.html", {
            "f": _widget_frame(def_id, "", widget, payload["preview"], preview=True),
        }, request=request)
    except Exception:
        # Do not expose clinical values from adapter or database exceptions.
        payload["preview_error"] = "Unable to evaluate this widget. Check its configuration and data connection."
    payload["row_height_px"] = layout["grid"]["row_height_px"]
    payload["widget_id"] = widget["id"]
    payload["widget"] = widget
    return JsonResponse(payload, status=200)


@require_POST
@csrf_protect
@require_admin
def editor_publish(request):
    """The single publish path, reached only by an explicit Publish button press.

    ``PublishRejectedError`` (invalid YAML / failed display validation) surfaces the validation
    errors and -- because Task 11 validates *before* the pointer flip -- leaves the current pointer
    untouched; that is asserted by ``test_invalid_yaml_does_not_replace_published``.
    """
    def_id = str(_param(request, "def_id", "")).strip()
    yaml_text = str(_param(request, "yaml_text", ""))
    if not def_id:
        return JsonResponse({"error": "def_id is required"}, status=400)
    repo = _repository()
    try:
        if not yaml_text:
            yaml_text, _ = repo.read_draft(def_id)
        receipt = repo.publish(def_id, yaml_text, expected_revision=_expected_revision(request))
    except (PublishRejectedError,) as exc:
        return JsonResponse(
            {"error": "publish rejected", "errors": [str(exc)], "def_id": def_id, "status": "rejected"},
            status=422,
        )
    except (StaleRevisionError,):
        return JsonResponse(
            {"error": "version conflict: reload", "def_id": def_id, "status": "conflict"},
            status=409,
        )
    except (RepositoryError,) as exc:
        return JsonResponse({"error": str(exc), "def_id": def_id}, status=400)
    return JsonResponse(
        {
            "def_id": def_id,
            "version": receipt.version,
            "pointer": repo._read_pointer(def_id),
            "status": "published",
            "url": reverse("report_v2:page", args=[def_id]),
        },
        status=200,
    )


@require_POST
@csrf_protect
@require_admin
def editor_unpublish(request):
    def_id = str(_param(request, "def_id", "")).strip()
    try:
        _repository().unpublish(def_id, expected_version=str(_param(request, "expected_version", "")))
    except StaleRevisionError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except RepositoryError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"status": "unpublished", "def_id": def_id})


@require_POST
@csrf_protect
@require_admin
def editor_new(request):
    """Create a second, independent report: a fresh ``def_id`` plus a starter scaffold draft."""
    name = str(_param(request, "name", "")).strip()
    def_id = str(_param(request, "def_id", "")).strip() or slugify(name).replace("-", "_")
    if not def_id or len(def_id) > 64 or def_id in {"layout", "preferences"}:
        return JsonResponse({"error": "Choose a report name with letters or numbers (up to 64 characters)."}, status=400)
    template = str(_param(request, "template", "blank"))
    if template not in {"blank", "prime"}:
        return JsonResponse({"error": "Choose a supported starter template."}, status=400)
    if name and not def_id[0].isalpha():
        def_id = "report_" + def_id
    title = name or "New report"
    if len(title) > 200:
        return JsonResponse({"error": "Report names must be 200 characters or fewer."}, status=400)
    repo = _repository()
    scaffold = STARTER_YAML.format(def_id=def_id).replace("title: New report", "title: " + json.dumps(title))
    if template == "prime":
        try:
            scaffold = seeding.seed_draft_texts(def_id=def_id)["report"]
            scaffold = re.sub(r"^title:.*$", lambda match: "title: " + json.dumps(title), scaffold, count=1, flags=re.MULTILINE)
        except (seeding.SeedError, RepositoryError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    scaffold = yaml.safe_dump(flat_report_definition(load_report_definition(scaffold)), sort_keys=False, allow_unicode=True)
    try:
        revision = repo.save_draft(def_id, scaffold, expected_revision=None, create_only=True)
    except StaleRevisionError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except RepositoryError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"def_id": def_id, "revision": revision, "yaml_text": scaffold,
                         "status": "created", "url": next(entry["editor_url"] for entry in _report_entries(repo) if entry["def_id"] == def_id)})


@require_POST
@csrf_protect
@require_admin
def editor_delete(request):
    if _param(request, "confirmation", "") != "delete this report":
        return JsonResponse({"error": 'Type "delete this report" to confirm deletion.'}, status=400)
    def_id = str(_param(request, "def_id", "")).strip()
    repo = _repository()
    try:
        repo.delete(def_id, expected_revision=_expected_revision(request),
                    expected_version=str(_param(request, "expected_version", "")).strip() or None)
    except DraftNotFoundError:
        return JsonResponse({"error": "Report not found."}, status=404)
    except StaleRevisionError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except RepositoryError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"status": "deleted", "def_id": def_id})


@require_POST
@csrf_protect
@require_admin
def editor_seed(request):
    """Load the reviewed report-v2 seed into admin-editable DRAFTS only. Never validates-to-publish,
    never publishes, never mails."""
    def_id = str(_param(request, "def_id", "")).strip() or seeding.SEED_DEF_ID
    dry_run = str(_param(request, "dry_run", "")).lower() in ("1", "true", "yes", "on")
    if dry_run:
        # A dry run is a pure read of the packaged seed bytes: no repository write happens below.
        violations = seeding.validate_seeds() + seeding.validate_seed_bindings()
        if violations:
            return JsonResponse(
                {"error": "seed rejected", "errors": violations, "def_id": def_id,
                 "status": "rejected"},
                status=422,
            )
        return JsonResponse(
            {"def_id": def_id, "errors": [], "valid": True, "dry_run": True, "drafts_only": True,
             "published": False, "status": "checked"},
            status=200,
        )
    expected = _expected_revision(request)
    try:
        receipt = seeding.install_drafts(
            default_root(),
            def_id=def_id,
            expected_revisions=({"report": expected} if expected else None),
        )
    except seeding.SeedValidationError as exc:
        return JsonResponse(
            {"error": "seed rejected", "errors": exc.violations, "def_id": def_id,
             "status": "rejected"},
            status=422,
        )
    except StaleRevisionError:
        return JsonResponse(
            {"error": "version conflict: reload", "def_id": def_id, "status": "conflict"},
            status=409,
        )
    except RepositoryError as exc:
        return JsonResponse({"error": str(exc), "def_id": def_id}, status=400)
    except seeding.SeedError as exc:
        return JsonResponse(
            {"error": "seed rejected", "errors": [str(exc)], "def_id": def_id,
             "status": "rejected"},
            status=422,
        )
    return JsonResponse({**receipt, "status": "seeded", "drafts_only": True, "published": False}, status=200)


@require_POST
@csrf_protect
@require_admin
def editor_visual(request):
    """Project or transform the submitted draft in memory; never evaluate clinical data."""
    text = str(_param(request, "yaml_text", ""))
    try:
        operation = _param(request, "operation", "")
        if operation:
            if isinstance(operation, str):
                operation = json.loads(operation)
            text, layout = visual.transform(text, operation, settings=current_settings())
        else:
            layout = visual.project_document(text)
        return JsonResponse({"yaml_text": text, "layout": layout, "catalog": visual.catalog(current_settings())})
    except (DefinitionError, ValueError, DisplayValidationError) as exc:
        errors = getattr(exc, "errors", None)
        messages = [f"{error.get('path') or '$'}: {error['message']}" for error in errors] if errors else [str(exc)]
        return JsonResponse({"errors": messages}, status=422)
