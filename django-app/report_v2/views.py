# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Public, login-gated read surface for *published* report-v2 layouts.

Security posture (the invariant this module is written around):

* Every forged-input gate runs **before** any row is fetched. The single ORM seam
  (:func:`report_v2.data.fetch_project_rows`) is reached only at the very last step of the data
  endpoint, so no 4xx path -- tampered context, rejected override, unknown id, stale version -- can
  touch the database or leak row data in an error body.
* Drafts are never rendered: layouts are loaded only through
  :meth:`report_v2.data.load_published_layout`, which raises :class:`~report_v2.data.PublishedNotFoundError`
  for absent / draft-only / unparseable reports (surface as :class:`~django.urls.Resolver404`, never a
  ``500``).
* The per-widget context token that rides into the browser is **server-signed** (HMAC over the salted
  ``slug|version|widget_id`` tuple) and is re-verified before use. The published *version* a widget was
  rendered under is pinned inside that signature: a client can never change it by editing the request
  body -- the version is only ever read back out of the verified token, so a stale token yields ``409``
  and a forged one yields ``403`` rather than silently re-scoping an evaluation.
* A whole-page render is fault-isolated per widget frame: any error inside one frame is captured into
  that frame's payload as an ``error`` string and the sibling frames still render -- a single bad widget
  can never turn the page into a ``500``.

The index view keeps the exact behaviour and HTML markers the routing tests pin (it renders no
clinical data; it reads only user preferences); the two new views add the published report page and its
per-widget JSON data endpoint.
"""
from __future__ import annotations

import csv
import hashlib
import json
import pickle
import re
from typing import Mapping
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.cache.backends.locmem import LocMemCache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import Resolver404, reverse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from . import data
from .dates import DateRangeError, parse_date_literal
from .models import ReportPreference
from .definitions.repository import InvalidDefinitionIdError, validate_definition_id
from .evaluation import (EvaluationError, PAIRED_MEASUREMENTS, RequestContract, WidgetSpec, evaluate, _bucket_rows,
                         _catalog_score_columns, _highest_score_cell)
from .measurements import fn_fp_cases
from .permissions import can_edit_catalog
from .projects.prime import get_project_definition

__all__ = ["index", "report_page", "widget_data", "report_csv"]

#: The single production project every published report is scoped to (Task 04).
_PROJECT_ID = "prime"

#: Domain-separated salt for the per-widget context signature (never reused elsewhere).
_CTX_SALT = "report_v2.page_context.v1"

#: Rows a table widget renders/serves per page (well inside the evaluator's hard
#: :data:`~report_v2.evaluation.MAX_PAGE_SIZE` cap) and the last page a client may ask for.
_SERVER_PAGE_SIZE = 50
_MAX_PAGE = 100
# ponytail: per-worker paging cache; shared protected cache only if multi-worker misses matter.
_TABLE_PAGES = LocMemCache("report-v2-table-pages", {"TIMEOUT": 60, "OPTIONS": {"MAX_ENTRIES": 16}})
_TABLE_CACHE_MAX_BYTES = 2 * 1024 * 1024


def _table_page(payload: dict, page: int) -> dict:
    """Slice evaluated detail rows without changing full-population statistics."""
    payload = dict(payload)
    if payload.get("pagination") is not None:
        rows = payload.get("rows") or []
        start = (page - 1) * _SERVER_PAGE_SIZE
        payload["rows"] = rows[start:start + _SERVER_PAGE_SIZE]
        payload["pagination"] = {
            "page": page, "page_size": _SERVER_PAGE_SIZE,
            "returned": len(payload["rows"]),
            "truncated": page < _MAX_PAGE and start + _SERVER_PAGE_SIZE < len(rows),
        }
    if payload.get("time_groups"):
        payload["time_groups"] = [{**group, "payload": _table_page(group["payload"], page)}
                                  for group in payload["time_groups"]]
    return payload

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELATIVE_RE = re.compile(r"^(?:D|W|M|Y)(?:-\d+)?$")
_WINDOW_START_RE = re.compile(r"^(?:D|W|M|Y)(?:-\d+)?$")

#: The only top-level keys a widget-data body may carry. Anything else (version / anchor /
#: measurement(_override) / policy_version_override / ci_override / layout_override / include_rows /
#: rows / widget_id / bucket / ...) is a disallowed override attempt and is rejected up-front.
_ALLOWED_TOP_KEYS = frozenset({"context", "date", "filters", "comparison", "page", "request_seq", "time_grouping"})

#: The scalar types an override filter value may be (or a list/tuple of these, bounded in length).
_SCALAR_TYPES = (str, int, float, bool)
_MAX_FILTER_LIST = 50

# ---------------------------------------------------------------------------
# Scoped CSV export (Task 16): the widget's own measurement + filters are the single scope definition.
# There is deliberately no DEFAULT_FROM_DATE-style global default anywhere here -- the export population
# is exactly the widget window/filters that the evaluator already resolved (LEGACY-MAP: preserve the
# selected cohort, no implicit global date range).
# ---------------------------------------------------------------------------
_CSV_KINDS = frozenset({"full", "false_negatives", "false_positives"})
_CSV_DISCREPANCY_MEASUREMENTS = frozenset({
    "false_negatives", "false_positives", "classification_summary",
    "reference_agreement", "paired_reference_comparison",
})
_CSV_COLUMNS = {
    "full": ("accession", "site", "study_date", "gt_label", "pred_label"),
    "false_negatives": ("accession", "site", "study_date", "gt_label", "pred_label",
                        "highest_finding", "highest_score"),
    "false_positives": ("accession", "site", "study_date", "gt_label", "pred_label",
                        "highest_finding", "highest_score"),
}
_CSV_MAX_ROWS = _SERVER_PAGE_SIZE * _MAX_PAGE
_CSV_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


class TamperedContextError(Exception):
    """The signed per-widget context token failed to verify (forge / expiry / wrong salt / malformed)."""


class OverrideRejectedError(Exception):
    """A request override is outside the per-widget allow-list, or is malformed."""


# ---------------------------------------------------------------------------
# Signed per-widget context token (server-authoritative slug/version/widget binding)
# ---------------------------------------------------------------------------
def _widget_context_token(slug: str, version: str, widget_id: str) -> str:
    """Sign the ``slug|version|widget_id`` tuple the frame was rendered under."""
    return signing.dumps(f"{slug}|{version}|{widget_id}", salt=_CTX_SALT)


def _parse_widget_context(token: object) -> tuple[str, str, str]:
    """Verify ``token`` and split it back into ``(slug, version, widget_id)``.

    Any failure to unsalt-verify -- a bad signature, an expired timeframe, a wrong salt, a
    non-string payload or a malformed split -- is a :class:`TamperedContextError`; the caller turns
    that into a ``403`` *before* anything is read. This is deliberately fail-closed.
    """
    try:
        value = signing.loads(token, salt=_CTX_SALT)
    except Exception as exc:  # BadSignature / SignatureExpired / ValueError / TypeError / ...
        raise TamperedContextError("context token failed to verify") from exc
    if not isinstance(value, str):
        raise TamperedContextError("context token payload is malformed")
    parts = value.split("|")
    if len(parts) != 3:
        raise TamperedContextError("context token payload is malformed")
    return parts[0], parts[1], parts[2]


# ---------------------------------------------------------------------------
# Layout-derived catalog (decision D3): the published widgets *are* the evaluator's allow-list
# ---------------------------------------------------------------------------
def _layout_widgets(layout: dict) -> dict[str, dict]:
    """Map ``widget_id -> widget dict`` walking sections (and widgets) in declared render order."""
    widgets: dict[str, dict] = {}
    for section in layout.get("sections") or []:
        for widget in section.get("widgets") or []:
            widget_id = widget.get("id")
            if widget_id is not None:
                widgets[widget_id] = widget
    return widgets


def _build_specs(layout: dict) -> dict[str, WidgetSpec]:
    """Derive a frozen :class:`~report_v2.evaluation.WidgetSpec` per widget from the *layout*.

    The layout is the published allow-list: a widget's declared ``measurement``/``type`` fix what it
    may compute and ``aggregate`` is ``True`` for everything except a ``table`` (only a non-aggregate
    widget may publish raw case-level rows). ``window`` falls back to ``D-30`` unless the widget's own
    ``window.start`` is already a relative token.
    """
    specs: dict[str, WidgetSpec] = {}
    for widget_id, widget in _layout_widgets(layout).items():
        display = widget.get("type")
        measurement = (widget.get("query") or {}).get("measurement")
        controls = widget.get("controls") or {}
        window_start = (widget.get("window") or {}).get("start") or ""
        window = window_start if _WINDOW_START_RE.match(window_start) else "D-30"
        specs[widget_id] = WidgetSpec(
            widget_id=widget_id,
            display=display,
            measurement=measurement,
            aggregate=(display != "table"),
            paired=measurement in PAIRED_MEASUREMENTS,
            window=window,
            filters=frozenset(controls.get("filters") or ()),
            dimensions=frozenset(controls.get("compare_by") or ()),
        )
    return specs


def _allowed_controls(widget: dict) -> dict:
    """The client-visible control surface for a widget (a copy of its controls + a few read-only hints)."""
    if widget.get("type") in {"text", "divider", "heading"}:
        return {"enabled": False}
    controls = widget.get("controls") or {}
    query = widget.get("query") or {}
    window = {"start": "D-30", "end": "D", **(widget.get("window") or {})}
    native_dates = {key: value if _ISO_RE.fullmatch(value) else "" for key, value in window.items()}
    date_inputs = {key: parse_date_literal(value).strftime("%d/%m/%Y") if native_dates[key] else value
                   for key, value in window.items()}
    return {
        "enabled": True,
        "date_range": bool(controls.get("date_range")),
        "filters": list(controls.get("filters") or ()),
        "compare_by": [
            {"id": name, "label": get_project_definition().dimension(name).label or name}
            for name in controls.get("compare_by") or ()
        ],
        "measurement": query.get("measurement"),
        "inputs": dict(query.get("inputs") or {}),
        "window": window,
        "date_inputs": date_inputs,
        "native_dates": native_dates,
        "default_compare_by": widget.get("default_compare_by"),
        "time_grouping": widget.get("bucket") or ("week" if widget.get("type") == "line" else None),
        "type": widget.get("type"),
    }


# ---------------------------------------------------------------------------
# Override validation -- every gate here runs against the layout only; nothing reads a row
# ---------------------------------------------------------------------------
def _check_top_keys(body: dict) -> None:
    """Reject any top-level key outside the allow-list before it is ever interpreted."""
    for key in body:
        if key not in _ALLOWED_TOP_KEYS:
            raise OverrideRejectedError(f"unexpected top-level key {key!r}")


def _validate_date(raw: object) -> dict | None:
    """Validate a ``date`` override into the shape the evaluator's ``date_override`` expects."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OverrideRejectedError("date override must be an object")
    if "relative" in raw:
        if set(raw) - {"relative"}:
            raise OverrideRejectedError("relative date override carries unexpected keys")
        token = raw.get("relative")
        if not isinstance(token, str) or not _RELATIVE_RE.match(token):
            raise OverrideRejectedError("relative window token is invalid")
        return {"relative": token}
    start = raw.get("start")
    end = raw.get("end")
    if set(raw) - {"start", "end"}:
        raise OverrideRejectedError("date override carries unexpected keys")
    for name, value in (("start", start), ("end", end)):
        if not isinstance(value, str) or not (_ISO_RE.fullmatch(value) or _RELATIVE_RE.fullmatch(value)):
            raise OverrideRejectedError(f"date {name} must be YYYY-MM-DD or D/W/M/Y with an optional minus offset")
        if _ISO_RE.fullmatch(value):
            try:
                parse_date_literal(value)
            except DateRangeError as exc:
                raise OverrideRejectedError(f"date {name} is not a valid calendar date") from exc
    return {"start": start, "end": end}


def _validate_filters(widget: dict, raw: object) -> dict:
    """Validate a ``filters`` override against the widget's declared filter allow-list."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise OverrideRejectedError("filters override must be an object")
    allowed = frozenset((widget.get("controls") or {}).get("filters") or ())
    out: dict = {}
    for key, value in raw.items():
        if key not in allowed:
            raise OverrideRejectedError(f"filter {key!r} is not permitted on this widget")
        if isinstance(value, _SCALAR_TYPES):
            out[key] = value
        elif isinstance(value, (list, tuple)):
            if len(value) > _MAX_FILTER_LIST:
                raise OverrideRejectedError(f"filter {key!r} has too many values")
            for item in value:
                if not isinstance(item, _SCALAR_TYPES):
                    raise OverrideRejectedError(f"filter {key!r} has a non-scalar value")
            out[key] = list(value)
        else:
            # A nested container (dict / object) is refused: filters are scalar or a flat list of them.
            raise OverrideRejectedError(f"filter {key!r} must be a scalar or a list of scalars")
    return out


def _validate_comparison(widget: dict, raw: object) -> str | None:
    """Validate a single ``comparison`` selection, falling back to the widget's default."""
    allowed = frozenset((widget.get("controls") or {}).get("compare_by") or ())
    if raw is None:
        default = widget.get("default_compare_by")
        if default is None:
            return None
        if default not in allowed:
            raise OverrideRejectedError("default comparison is not permitted on this widget")
        return default
    if raw == "":
        return ""  # Explicit None differs from an omitted server default.
    if not isinstance(raw, str) or raw not in allowed:
        raise OverrideRejectedError("comparison is not permitted on this widget")
    return raw


def _validate_time_grouping(widget: dict, raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in {"", "day", "week", "month", "year"}:
        raise OverrideRejectedError("time grouping must be day, week, month or year")
    return raw


def _validate_page(raw: object) -> int:
    """Validate a ``page`` selector: absent -> 1, else a positive int within the bound."""
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise OverrideRejectedError("page must be an integer")
    if not (1 <= raw <= _MAX_PAGE):
        raise OverrideRejectedError("page is out of range")
    return raw


def _validate_overrides(widget: dict, body: dict) -> tuple[dict | None, dict, str | None, list | None, int]:
    """Validate a request body against one widget and return the evaluator inputs.

    Order: the top-level key gate, then each permitted override. The returned ``grouping`` is derived
    from the single validated ``comparison`` selector so the caller cannot smuggle a second axis.
    """
    _check_top_keys(body)
    _validate_time_grouping(widget, body.get("time_grouping"))
    date_override = _validate_date(body.get("date"))
    filters = _validate_filters(widget, body.get("filters"))
    comparison = _validate_comparison(widget, body.get("comparison"))
    grouping = [comparison] if comparison else ([] if comparison == "" else None)
    page = _validate_page(body.get("page"))
    return date_override, filters, comparison, grouping, page


# ---------------------------------------------------------------------------
# Evaluation (the only place a row is ever read; callers trap domain errors -> frame error dict)
# ---------------------------------------------------------------------------
def _evaluate(
    layout: dict,
    widget: dict,
    *,
    date_override: dict | None = None,
    filters: dict | None = None,
    comparison: str | None = None,
    grouping: list | None = None,
    page: int = 1,
    rows: list | None = None,
    selected_rows: list | None = None,
    time_grouping: str | None = None,
) -> dict:
    """Evaluate one widget into a JSON-safe payload dict. NEVER raises for a domain failure."""
    if widget.get("type") in {"text", "divider", "heading"}:
        return {"widget_id": widget["id"], "static_type": widget["type"], "text": widget.get("text", ""), "title": widget.get("title", "")}
    if rows is None:
        rows = data.fetch_project_rows(_PROJECT_ID, layout_widget=widget)
    project = get_project_definition()
    rows = [dict(row) for row in rows]
    dimensions = project.dimensions
    for row in rows:
        for name, dimension in dimensions.items():
            row[name] = dimension.group_value(row.get(name, row.get(dimension.field)))
    group_options = {}
    for name in (widget.get("controls") or {}).get("compare_by") or ():
        dimension = project.dimension(name)
        values = {row[name] for row in rows if not row.get("cross_project") and row.get("eligible", True)}
        if len(values) > 100:
            raise OverrideRejectedError("group selection exceeds the 100-group limit")
        group_options[name] = ([label for label, _, _ in dimension.bands] + ["Unknown"]
                               if dimension.bands else sorted(values))
    specs = _build_specs(layout)
    if date_override is None:
        default_window = dict(widget.get("window") or {})
        if default_window.get("start"):
            date_override = default_window
    request = RequestContract(
        widget_id=widget["id"],
        date_override=date_override,
        filter_overrides=dict(filters or {}),
        comparison=comparison or None,
    )
    if grouping is None:
        comparison = comparison or widget.get("default_compare_by")
        grouping = [comparison] if comparison else None
    display = widget.get("type")
    buckets = (widget.get("bucket") or ("week" if display == "line" else None)) if time_grouping is None else (time_grouping or None)
    ci_registry = {"positive_predictive_value"} if (widget.get("ci") or {}).get("enabled") else None
    policy = (widget.get("query") or {}).get("threshold_policy")
    is_table = display == "table"
    is_summary_table = is_table and (widget.get("query") or {}).get("measurement") in {
        "classification_summary", "reference_agreement", "paired_reference_comparison", "duration_summary"
    } or (is_table and bool(grouping) and (widget.get("query") or {}).get("measurement") in {"record_count", "label_count"})
    result = evaluate(
        request=request,
        project=project,
        catalog=None,
        rows=rows,
        ci_registry=ci_registry,
        policy=policy,
        selected_rows=selected_rows,
        grouping=grouping,
        buckets=buckets,
        page_size=_SERVER_PAGE_SIZE if is_table and not is_summary_table else None,
        max_groups=100,
        published_widget_ids=frozenset(specs),
        widgets=specs,
        include_rows=is_table,
        widget_inputs=dict((widget.get("query") or {}).get("inputs") or {}),
    )
    payload = result.to_dict()
    from .thresholds import threshold_metadata, threshold_caption
    thresholds = threshold_metadata(widget.get("query") or {}, project)
    if thresholds:
        payload["lunit_thresholds"] = thresholds
        payload["caption"] = " ".join(filter(None, [payload.get("caption"), threshold_caption(thresholds)]))
    payload["group_options"] = group_options
    if is_summary_table:
        # Group cardinality is already bounded to 100; show every summary group together.
        payload["pagination"] = None
    elif is_table:
        all_rows = list(result.rows)
        # Page zero is internal: retain rows for the bounded navigation cache.
        page_rows = all_rows if page == 0 else all_rows[(page - 1) * _SERVER_PAGE_SIZE : page * _SERVER_PAGE_SIZE]
        payload["rows"] = page_rows
        payload["pagination"] = {
            "page": page,
            "page_size": _SERVER_PAGE_SIZE,
            "returned": len(page_rows),
            "truncated": page < _MAX_PAGE and page * _SERVER_PAGE_SIZE < len(all_rows),
        }
    # A synthesized Overall row must not make an empty selection look populated.
    counts = payload.get("counts") or {}
    payload["empty"] = not counts.get("matching")
    if buckets and display != "line" and not payload["empty"]:
        # ponytail: bounded per-period evaluation; partition once if 100 groups become too slow.
        periods = [bucket for bucket in payload["buckets"] if _bucket_rows(rows, bucket)]
        if len(periods) > 100:
            raise OverrideRejectedError("calendar grouping exceeds the 100-period limit; choose a coarser grouping")
        payload["time_groups"] = []
        for bucket in periods:
            period_payload = _evaluate(
                layout, {**widget, "bucket": None}, rows=rows, selected_rows=selected_rows,
                date_override={"start": bucket["start_date"], "end": bucket["end_date"]},
                filters=filters, comparison=comparison, grouping=grouping, page=page,
            )
            if not period_payload["empty"]:
                payload["time_groups"].append({"label": bucket["label"], "payload": period_payload})
    return payload


_EMPTY_MESSAGE = (
    "No matching records in this window -- the dates or filters may exclude every "
    "record. Adjust the controls or reset them."
)


def _fmt_aggregate_value(value: object) -> str:
    """Render one aggregates-group value as compact, textContent-safe display text."""
    if isinstance(value, Mapping):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " | ".join(str(item) for item in value)
    return str(value)


def _primary_value(widget: dict, payload: dict) -> str:
    key = (widget.get("query") or {}).get("measurement")
    if key == "record_count":
        key = "n"
    value = (payload.get("aggregates") or {}).get(key)
    if value is None:
        return "—"
    unit = (payload.get("units") or {}).get(key, "")
    if "rate[0,1]" in unit or "ratio" in unit or "probability" in unit:
        return f"{value * 100:.1f}%"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _display_dates(value: object) -> str:
    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", r"\3/\2/\1", str(value))


def _summary_text(payload: dict) -> str:
    """A one-line, human-readable account of what the payload measured (or why it is empty)."""
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    counts = (payload or {}).get("counts")
    if not counts:
        return _EMPTY_MESSAGE
    matching = counts.get("matching")
    if not matching:
        return _EMPTY_MESSAGE
    text = (
        f"matching {matching} of {counts.get('incoming')} "
        f"\u00b7 {counts.get('eligible')} eligible for measurement"
    )
    coverage = ((payload or {}).get("dates") or {}).get("coverage_note")
    if coverage:
        text = f"{text} ({_display_dates(coverage)})"
    return text


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def _csv_links(slug, version, widget, payload):
    if payload.get("error") or not (payload.get("dates") or {}).get("window_start"):
        return []
    dates = payload["dates"]
    params = urlencode({"widget": widget["id"], "context": _widget_context_token(slug, version, widget["id"]),
                        "date_from": dates["window_start"], "date_to": dates["window_end"]})
    kinds = [("full", "Download CSV")]
    measurement = (widget.get("query") or {}).get("measurement")
    if measurement in ("false_negatives", "false_positives"):
        kinds = [(measurement, "Download cases CSV")]
    return [{"url": reverse("report_v2:csv", args=[slug, kind]) + "?" + params, "label": label}
            for kind, label in kinds]


def _preferred_report(user):
    return ReportPreference.objects.filter(user=user).values_list("default_slug", flat=True).first()


@login_required
@require_GET
def index(request):
    """Published report list, or the current user's chosen default; never reads study rows."""
    published_reports = data.list_published(project_id=_PROJECT_ID)
    default_slug = _preferred_report(request.user) if published_reports else None
    available = {entry["slug"] for entry in published_reports}
    if default_slug not in available:
        default_slug = None
    if default_slug and request.GET.get("list") != "1":
        return redirect("report_v2:page", slug=default_slug)
    return render(request, "report_v2/index.html", {
        "published_reports": published_reports,
        "default_slug": default_slug,
    })


@login_required
@require_POST
@csrf_protect
def set_default_report(request):
    slug = request.POST.get("slug", "")
    if slug and slug not in {entry["slug"] for entry in data.list_published(project_id=_PROJECT_ID)}:
        return HttpResponse("Choose an available published report.", status=400)
    if slug:
        ReportPreference.objects.update_or_create(user=request.user, defaults={"default_slug": slug})
    else:
        ReportPreference.objects.filter(user=request.user).delete()
    return redirect(reverse("report_v2:index") + "?list=1")



def _widget_frame(slug, version, widget, payload, *, preview=False):
    """Shared server-hydrated card for published reports and read-only previews."""
    widget_id = widget.get("id")
    row_columns = (list(payload["rows"][0].keys())
                  if isinstance(payload, dict) and payload.get("rows") else [])
    row_cells = ([[row.get(key) for key in row_columns] for row in payload.get("rows") or []]
                if row_columns else [])
    return {
        "id": widget_id,
        "title": widget.get("title"),
        "type": widget.get("type"),
        "measurement": (widget.get("query") or {}).get("measurement"),
        "primary_value": _primary_value(widget, payload),
        "width": (widget.get("layout") or {}).get("width"),
        "height": (widget.get("layout") or {}).get("height"),
        "controls": _allowed_controls(widget),
        "context_token": "" if preview else _widget_context_token(slug, version, widget_id),
        "data_url": reverse("report_editor:editor_preview") if preview else reverse("report_v2:widget_data", args=[slug, widget_id]),
        "csv_links": [] if preview else _csv_links(slug, version, widget, payload),
        "payload": payload,
        "summary": _summary_text(payload),
        "empty_message": _EMPTY_MESSAGE,
        # Pre-formatted aggregate rows for the server-side rendering: name/value pairs as
        # plain strings so the template never iterates dict internals or emits raw markup.
        "aggregates_view": [
            {"name": str(key), "value": _fmt_aggregate_value(value)}
            for key, value in (payload.get("aggregates") or {}).items()
        ] if isinstance(payload, dict) and not payload.get("error") else [],
        "has_rows": bool(isinstance(payload, dict) and payload.get("rows")),
        "row_columns": row_columns,
        "row_cells": row_cells,
        # The view applies escape() itself so the template can mark this |safe; the
        # embedded JSON can then never break out of the <pre> as markup.
        "initial_json": escape(json.dumps(payload, default=str)),
    }

@login_required
@require_GET
def report_page(request, slug: str):
    """Render one published report layout as a grid of server-hydrated, fault-isolated widget frames."""
    try:
        validate_definition_id(slug)
    except InvalidDefinitionIdError as exc:
        raise Resolver404(f"unknown report {slug!r}") from exc
    try:
        version, layout = data.load_published_layout(slug, _PROJECT_ID)
    except data.PublishedNotFoundError as exc:
        raise Resolver404(f"unknown report {slug!r}") from exc

    from .definitions.loader import flat_report_definition
    canvas = flat_report_definition(layout)
    sections_ctx = []
    # Generated legacy headings are decorative until the definition is saved in flat form.
    legacy_cards = _layout_widgets(layout)
    for section in [{"id": "canvas", "title": "", "widgets": canvas["widgets"]}]:
        frames = []
        for widget in section.get("widgets") or []:
            widget_id = widget.get("id")
            try:
                payload = (_evaluate(layout, widget) if widget.get("type") in {"text", "divider", "heading"}
                           else {"widget_id": widget_id, "loading": True})
            except Exception as exc:  # a single bad frame must not 500 the page or its siblings
                payload = {"widget_id": widget_id, "error": str(exc)}
            frame = _widget_frame(slug, version, widget, payload)
            if widget_id not in legacy_cards:
                frame["context_token"] = ""
                frame["decoration"] = True
            frames.append(frame)
        sections_ctx.append(
            {
                "id": section.get("id"),
                "title": section.get("title"),
                "widgets": frames,
            }
        )

    context = {
        "slug": slug,
        "version": version,
        "title": layout.get("title"),
        "grid": layout.get("grid") or {},
        "sections_ctx": sections_ctx,
        "is_admin": can_edit_catalog(request.user),
        "legacy_report_url": reverse("report:index"),
    }
    if context["is_admin"]:
        from .admin_views import _report_entries, _repository
        context["editor_url"] = next(
            entry["editor_url"] for entry in _report_entries(_repository()) if entry["def_id"] == slug
        )
    return render(request, "report_v2/page.html", context)


@login_required
@require_POST
@csrf_protect
def widget_data(request, slug: str, widget_id: str):
    """Return one widget's evaluated data as JSON, after every forged-input gate.

    Strict order; every rejection returns *before* a row is read, and no error body carries row data:

    1. body must be a JSON object;
    2. only allow-listed top-level keys;
    3. ids must be well-formed (else ``404``);
    4. the signed context token must verify and be bound to this ``(slug, current version, widget_id)``
       (tamper -> ``403``, pointer moved past the pinned version -> ``409``, absent report -> ``404``);
    5. the widget must exist in the current layout;
    6. every override must be permitted (else ``400``);
    7. the evaluation runs -- a domain error is reported as a ``200`` ``error`` status, never a ``500``;
    8. the payload is returned with the ``request_seq`` echoed back.
    """
    try:
        body = json.loads(request.body or b"{}")
    except Exception:
        return JsonResponse({"error": "request body is not valid json", "status": "rejected"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "request body must be a json object", "status": "rejected"}, status=400)

    # (2) top-level allow-list on the raw body, before any interpretation or lookup.
    try:
        _check_top_keys(body)
    except OverrideRejectedError as exc:
        return JsonResponse({"error": str(exc), "status": "rejected"}, status=400)

    # (3) identifiers must be well-formed; a malformed id is simply "no such route" -> 404.
    try:
        validate_definition_id(slug)
        validate_definition_id(widget_id)
    except InvalidDefinitionIdError as exc:
        raise Resolver404(f"unknown report widget {slug!r}/{widget_id!r}") from exc

    # (4) verify the signed context token, then bind it to this request + the CURRENT pointer.
    try:
        token_slug, token_version, token_widget = _parse_widget_context(body.get("context"))
    except TamperedContextError:
        return JsonResponse({"error": "context token failed to verify", "status": "tampered"}, status=403)

    try:
        current_version, layout = data.load_published_layout(slug, _PROJECT_ID)
    except data.PublishedNotFoundError:
        return JsonResponse({"error": "report is not published", "status": "not_found"}, status=404)

    if (token_slug, token_version, token_widget) != (slug, current_version, widget_id):
        if token_version != current_version:
            # The version is pinned inside the signature; it cannot be changed by the request body.
            return JsonResponse({"error": "report version has changed; reload", "status": "stale"}, status=409)
        return JsonResponse({"error": "context does not match this widget", "status": "tampered"}, status=403)

    # (5) the widget must actually be on the current published layout.
    widget = _layout_widgets(layout).get(widget_id)
    if widget is None:
        return JsonResponse({"error": "widget is not on this layout", "status": "tampered"}, status=403)

    # (6) every permitted override, validated against the widget (no row is read here).
    try:
        date_override, filters, comparison, grouping, page = _validate_overrides(widget, body)
    except OverrideRejectedError as exc:
        return JsonResponse({"error": str(exc), "status": "rejected"}, status=400)

    # (7) the ONLY row read on this path; a domain error is data (200), not a server fault.
    try:
        is_table = widget["type"] == "table"
        # Gates above run on every request, including cache hits. No PHI in cache keys.
        scope = {key: value for key, value in body.items() if key not in {"page", "request_seq"}}
        scope["widget_definition"] = widget
        scope["user"] = request.user.pk
        scope["session"] = request.session.session_key
        key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        payload = _TABLE_PAGES.get(key) if is_table and page > 1 else None
        if payload is None:
            rows = data.fetch_project_rows(_PROJECT_ID, layout_widget=widget)
            payload = _evaluate(
                layout, widget, date_override=date_override, filters=filters,
                comparison=comparison, grouping=grouping, page=0 if is_table else page,
                time_grouping=body.get("time_grouping"), rows=rows,
            )
            if is_table and payload.get("pagination") is not None:
                # Bound retained detail rows to the existing navigation limit.
                payload["rows"] = payload["rows"][:_SERVER_PAGE_SIZE * _MAX_PAGE + 1]
                if len(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)) <= _TABLE_CACHE_MAX_BYTES:
                    _TABLE_PAGES.set(key, payload)
        if is_table:
            payload = _table_page(payload, page)
    except (EvaluationError, data.AdapterError, DateRangeError, OverrideRejectedError) as exc:
        return JsonResponse({"status": "error", "error": str(exc), "widget_id": widget_id}, status=200)

    # (8) echo the client's sequencing token (int-coerced when it is one) and return the payload.
    request_seq = body.get("request_seq")
    if request_seq is not None:
        try:
            request_seq = int(request_seq)
        except (TypeError, ValueError):
            request_seq = None
    return JsonResponse({"status": "ok", "request_seq": request_seq, **payload}, status=200)


# ---------------------------------------------------------------------------
# Scoped CSV export (the read-only compatibility surface for the legacy full / FN / FP downloads)
# ---------------------------------------------------------------------------
def _csv_cell(value: object) -> str:
    """Render one cell for the csv writer: ``None``/missing becomes ``''`` (never a fabricated value)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


@login_required
@require_GET
def report_csv(request, slug: str, kind: str):
    """Return one scoped CSV export off a single published widget's resolved scope.

    Mirrors :func:`widget_data`'s discipline: every forged-input gate returns *before* a single row is
    read, and no error body carries row data. The widget's own measurement + validated filters are the
    only scope definition -- there is no implicit global date range and no re-derived statistic.
    """
    # (a) an unknown kind is simply "no such route" -> 404 (mirrors the malformed-id handling above).
    if kind not in _CSV_KINDS:
        raise Resolver404(f"unknown csv kind {kind!r}")

    # (b) identifiers must be well-formed; a malformed slug is "no such report" -> 404.
    try:
        validate_definition_id(slug)
    except InvalidDefinitionIdError as exc:
        raise Resolver404(f"unknown report {slug!r}") from exc

    # (c) a widget id and a signed context are both mandatory: the export is always widget-scoped.
    widget = request.GET.get("widget")
    context = request.GET.get("context")
    if not widget or not context:
        return JsonResponse({"error": "widget and signed context are required", "status": "rejected"}, status=400)

    # (d) verify the signed token first; a forged/expired/garbage token is fail-closed tampering -> 403.
    try:
        token_slug, token_version, token_widget = _parse_widget_context(context)
    except TamperedContextError:
        return JsonResponse({"error": "context token failed to verify", "status": "tampered"}, status=403)

    # (e) the report must be published (drafts are never exported) -> 404 otherwise.
    try:
        version, layout = data.load_published_layout(slug, _PROJECT_ID)
    except data.PublishedNotFoundError:
        return JsonResponse({"error": "report is not published", "status": "not_found"}, status=404)

    # (f) the token must be bound to this (slug, current version, widget); a moved pointer is stale -> 409.
    if (token_slug, token_version, token_widget) != (slug, version, widget):
        if token_version != version:
            return JsonResponse({"error": "report version has changed; reload", "status": "stale"}, status=409)
        return JsonResponse({"error": "context does not match this widget", "status": "tampered"}, status=403)

    # (g) the widget must actually be on the current published layout.
    widget_def = _layout_widgets(layout).get(widget)
    if widget_def is None:
        return JsonResponse({"error": "widget is not on this layout", "status": "tampered"}, status=403)

    # (h) the two discrepancy kinds require a widget whose measurement can actually yield FN/FP cases;
    #     "full" is permitted on any widget. A non-supporting measurement is a semantic rejection -> 422.
    measurement = (widget_def.get("query") or {}).get("measurement")
    if kind in ("false_negatives", "false_positives") and measurement not in _CSV_DISCREPANCY_MEASUREMENTS:
        return JsonResponse(
            {"error": f"{kind} exports are not available for the {measurement!r} widget",
             "status": "rejected"},
            status=422,
        )

    # (i) query-string overrides are allowed ONLY through the existing validators; nothing else is accepted
    #     (no implicit global date range, ever). Any unknown key -> 400 before a row is read.
    for key in request.GET.keys():
        if key not in frozenset({"widget", "context", "date_from", "date_to", "site"}):
            return JsonResponse({"error": f"unexpected query parameter {key!r}", "status": "rejected"}, status=400)
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    try:
        date_override = None if (date_from is None and date_to is None) else _validate_date(
            {"start": date_from, "end": date_to}
        )
        site_values = request.GET.getlist("site")
        # Only the widget's own declared filter dimensions may be narrowed; the value dict is built from
        # those names alone so an arbitrary key can never reach the evaluator (else _validate_filters rejects).
        filters = _validate_filters(widget_def, {"site": site_values} if site_values else {})
    except OverrideRejectedError as exc:
        return JsonResponse({"error": str(exc), "status": "rejected"}, status=400)

    # (j)+(k) the single ORM seam + the one evaluation; a domain error is data (200), never a 500.
    try:
        rows = data.fetch_project_rows(_PROJECT_ID, layout_widget=widget_def)
        scoped_rows = []
        payload = _evaluate(
            layout,
            widget_def,
            date_override=date_override,
            filters=filters,
            comparison=None,
            grouping=None,
            page=1,
            rows=rows,
            selected_rows=scoped_rows,
        )
    except (EvaluationError, data.AdapterError, DateRangeError, OverrideRejectedError) as exc:
        return JsonResponse({"status": "error", "error": str(exc), "widget_id": widget}, status=200)

    # (l) select the rows to emit, reading every statistic off the already-computed payload.
    columns = _CSV_COLUMNS[kind]
    selected_rows = [dict(row, study_date=row.get("event_date")) for row in scoped_rows]
    if kind != "full":
        result = fn_fp_cases(
            [row.get("gt_label") for row in selected_rows],
            [row.get("pred_label") for row in selected_rows],
            [row.get("accession") for row in selected_rows],
            reference="manual", prediction="llm",
        )
        selected_ids = set(result.false_negative_ids if kind == "false_negatives" else result.false_positive_ids)
        selected_rows = [row for row in selected_rows if row.get("accession") in selected_ids]
        score_columns = _catalog_score_columns({"project": get_project_definition()})
        for row in selected_rows:
            row["highest_finding"], row["highest_score"] = _highest_score_cell(row, score_columns)

    # (m) render to CSV over the stdlib writer, then the provenance trailer. Truncation is flagged in-body
    #     and via a header so a consumer can detect a capped export.
    truncated = len(selected_rows) > _CSV_MAX_ROWS
    if truncated:
        selected_rows = selected_rows[:_CSV_MAX_ROWS]

    response = HttpResponse(content_type="text/csv")
    writer = csv.writer(response)
    writer.writerow(list(columns))
    for row in selected_rows:
        writer.writerow([_csv_cell(row.get(col)) for col in columns])
    if truncated:
        writer.writerow(["# truncated"])
        response["X-Report-V2-Truncated"] = "1"

    dates = payload.get("dates") or {}
    inputs = (widget_def.get("query") or {}).get("inputs") or {}
    trailer = [
        f"# widget={widget}",
        f"# measurement={measurement}",
        f"# reference={inputs.get('ground_truth') or '-'}",
        f"# prediction={inputs.get('prediction') or '-'}",
        f"# window={dates.get('window_start')}..{dates.get('window_end')}",
        f"# anchor={dates.get('anchor_date') or '-'}",
        f"# filters={json.dumps(filters, sort_keys=True, default=str)}",
        f"# version={version}",
        f"# policy={(widget_def.get('query') or {}).get('threshold_policy') or '-'}",
        f"# lunit_thresholds={json.dumps(payload.get('lunit_thresholds'), sort_keys=True)}",
        f"# kind={kind}",
    ]
    for line in trailer:
        writer.writerow([line])

    safe_slug = _CSV_FILENAME_SAFE_RE.sub("", slug)
    safe_widget = _CSV_FILENAME_SAFE_RE.sub("", widget)
    filename = f"{safe_slug}_{safe_widget}_{kind}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
