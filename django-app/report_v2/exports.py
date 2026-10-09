# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Snapshot-based export surface for report v2 (Task 17: print; Task 18 will add email).

Two endpoints, both login-gated, both built on the Task-13 forged-input discipline
(every rejection returns *before* a row is read):

* ``POST /report/<slug>/snapshot/`` -- freeze the *server-side* evaluation of every
  published widget under the caller's current overrides and hand back an opaque,
  scoped, bounded-lifetime snapshot token plus the print URL. The request must
  carry one entry per published widget (signed context token + that widget's
  overrides) and an explicit ``settled: true`` acknowledgement: while any widget
  update is still pending the client must NOT request an export, and a request
  without the acknowledgement is rejected with ``409 pending`` (the "block export
  while widget updates are pending" gate).
* ``GET /report/<slug>/print/<token>/`` -- render print-ready HTML from the frozen
  document ONLY. No re-evaluation, no ORM access, no cache of definitions: a page
  already rendered in the user's browser stays exportable even after the dataset
  or the published version moved on, and an expired/foreign/tampered snapshot fails
  with an explicit status + regenerate instruction instead of silently re-freezing
  under new definitions.

The print page is a standalone light-theme document (no app chrome, no widget
controls, no page-state contract): a snapshot is export state, never restored user
preferences. Charts render as accessible data tables straight from the frozen
payloads, so offscreen widgets and long tables print deterministically.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timezone as _tz
from email.mime.image import MIMEImage
from email.utils import formatdate, make_msgid
from typing import Any, Mapping

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.core.mail.message import SafeMIMEMultipart, SafeMIMEText
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import Resolver404, reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from . import data, snapshots
from .definitions.repository import InvalidDefinitionIdError, validate_definition_id
from .evaluation import EvaluationError
from .views import (
    _PROJECT_ID,
    TamperedContextError,
    OverrideRejectedError,
    _evaluate,
    _display_dates,
    _layout_widgets,
    _parse_widget_context,
    _summary_text,
    _validate_overrides,
)

__all__ = ["create_snapshot", "print_view", "email_report"]

#: The only top-level keys a snapshot-creation body may carry.
_SNAPSHOT_BODY_KEYS = frozenset({"settled", "widgets"})

#: The only keys one per-widget entry may carry (mirrors the widget-data allow-list:
#: date / filters / comparison / page are the sole user-controllable state).
_ENTRY_KEYS = frozenset({"context", "date", "filters", "comparison", "page", "time_grouping"})

#: Document discriminator + view contract version frozen into every snapshot.
_SNAPSHOT_KIND = "report_v2_print_v1"

#: The only top-level keys an email body may carry.
_EMAIL_BODY_KEYS = frozenset({"snapshot", "recipients", "note", "images"})

#: Recipient/note bounds (legacy UX parity: many addresses in one field, optional note).
_MAX_RECIPIENTS = 20
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_NOTE_CHARS = 2000

#: Chart-image contract: only chart-typed widgets of the snapshot, PNG-only MIME
#: (echarts getDataURL), a per-image byte bound and a total count bound.
_CHART_DISPLAYS = frozenset({"line", "bar", "pie", "boxplot"})
_PNG_DATAURL_PREFIX = "data:image/png;base64,"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_BYTES = 512 * 1024
_MAX_IMAGES = 32

#: Case rows in the HTML email: shown only for widgets frozen with ``export: full``,
#: and capped so an oversized table cannot blow up the MIME payload.
_EMAIL_ROWS_CAP = 25

#: Duplicate-submission guard lifetime (seconds); obeys the store's TTL floor.
_EMAIL_REPLAY_TTL = 120


class _Rejected(Exception):
    """Internal carrier -> JsonResponse(rejected, 400)."""


# ---------------------------------------------------------------------------
# POST /report/<slug>/snapshot/ -- freeze the evaluated report server-side
# ---------------------------------------------------------------------------
def _entry_state(widget: dict, entry: dict) -> tuple[dict | None, dict, str | None, list | None, int]:
    """Validate one entry against one widget (delegates to the Task-13 validators)."""
    for key in entry:
        if key not in _ENTRY_KEYS:
            raise _Rejected(f"widget entry carries unexpected key {key!r}")
    if not isinstance(entry.get("context"), str) or not entry["context"]:
        raise _Rejected("widget entry is missing its signed context token")
    return _validate_overrides(widget, entry)


def _freeze_widgets(layout, plan, project_id):
    # The evaluation pass: one ORM read per widget, fault-isolated per frame (a bad
    # widget freezes its error string exactly like the page render does).
    frozen_widgets: list[dict[str, Any]] = []
    for widget, entry, date_override, filters, comparison, grouping, page in plan:
        try:
            rows = [] if widget.get("type") in {"text", "divider", "heading"} else data.fetch_project_rows(project_id, layout_widget=widget)
            payload = _evaluate(
                layout, widget,
                date_override=date_override, filters=filters,
                comparison=comparison, grouping=grouping, page=page, rows=rows,
                time_grouping=entry.get("time_grouping"),
            )
        except (EvaluationError, data.AdapterError) as exc:
            payload = {"widget_id": widget.get("id"), "error": str(exc)}
        except Exception as exc:  # fault isolation: one frame must not kill the export
            payload = {"widget_id": widget.get("id"), "error": str(exc)}
        frozen_widgets.append(
            {
                "widget_id": widget.get("id"),
                "title": widget.get("title"),
                "type": widget.get("type"),
                "section_id": None,
                "export": widget.get("export"),
                "applied": {
                    "date": entry.get("date"),
                    "filters": dict(filters or {}),
                    "comparison": comparison,
                    "time_grouping": entry.get("time_grouping"),
                    "page": page,
                },
                "payload": payload,
            }
        )
    return frozen_widgets


def default_document(project_id, slug):
    """Freeze published defaults without browser preferences or request overrides."""
    version, layout = data.load_published_layout(slug, project_id)
    plan = [(widget, {}, None, {}, None, None, 1) for widget in _layout_widgets(layout).values()]
    frozen = _freeze_widgets(layout, plan, project_id)
    if any(widget["payload"].get("error") or widget["payload"].get("errors") for widget in frozen):
        raise EvaluationError("default export evaluation failed")
    return {"kind": _SNAPSHOT_KIND, "project_id": project_id, "slug": slug, "version": version,
            "title": layout.get("title"), "generated": datetime.now(_tz.utc).isoformat(), "widgets": frozen}


@login_required
@require_POST
@csrf_protect
def create_snapshot(request, slug: str):
    """Freeze every published widget under the caller's current state; return the print URL.

    Order of gates (mirrors :func:`report_v2.views.widget_data`; each returns before a
    row is read): body shape -> ``settled`` acknowledgement -> well-formed slug ->
    published layout -> full, duplicate-free widget coverage -> per-entry context
    verification + override validation -> the single evaluation pass per widget.
    """
    try:
        body = json.loads(request.body or b"{}")
    except Exception:
        return JsonResponse({"error": "request body is not valid json", "status": "rejected"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "request body must be a json object", "status": "rejected"}, status=400)
    for key in body:
        if key not in _SNAPSHOT_BODY_KEYS:
            return JsonResponse({"error": f"unexpected top-level key {key!r}", "status": "rejected"}, status=400)

    # The pending gate: the client must confirm every widget update has settled.
    if body.get("settled") is not True:
        return JsonResponse(
            {"error": "widget updates are still pending; retry once they have settled",
             "status": "pending"},
            status=409,
        )

    try:
        validate_definition_id(slug)
    except InvalidDefinitionIdError as exc:
        raise Resolver404(f"unknown report {slug!r}") from exc
    try:
        version, layout = data.load_published_layout(slug, _PROJECT_ID)
    except data.PublishedNotFoundError:
        return JsonResponse({"error": "report is not published", "status": "not_found"}, status=404)

    layout_widgets = _layout_widgets(layout)
    entries = body.get("widgets")
    if not isinstance(entries, list) or not entries:
        return JsonResponse({"error": "widgets must be a non-empty list", "status": "rejected"}, status=400)
    if len(entries) > snapshots.MAX_SNAPSHOT_WIDGETS:
        return JsonResponse(
            {"error": f"too many widget entries (bound {snapshots.MAX_SNAPSHOT_WIDGETS})",
             "status": "rejected"},
            status=400,
        )
    if len(entries) != len(layout_widgets):
        return JsonResponse(
            {"error": "snapshot must cover every published widget exactly once",
             "status": "rejected"},
            status=400,
        )

    # Verify every entry (context + overrides) before ANY row is read.
    plan: list[tuple[dict, dict, dict | None, dict, str | None, list | None, int]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return JsonResponse({"error": "widget entry must be an object", "status": "rejected"}, status=400)
        try:
            token_slug, token_version, widget_id = _parse_widget_context(entry.get("context"))
        except TamperedContextError:
            return JsonResponse({"error": "context token failed to verify", "status": "tampered"}, status=403)
        # The widget id is read back out of the verified token only -- an unsigned
        # body field can never re-scope an entry.
        if (token_slug, token_version) != (slug, version):
            if token_version != version:
                return JsonResponse({"error": "report version has changed; reload",
                                     "status": "stale"}, status=409)
            return JsonResponse({"error": "context does not match this report", "status": "tampered"}, status=403)
        if widget_id in seen:
            return JsonResponse({"error": f"widget {widget_id!r} appears twice",
                                 "status": "rejected"}, status=400)
        widget = layout_widgets.get(widget_id)
        if widget is None:
            return JsonResponse({"error": "widget is not on this layout", "status": "tampered"}, status=403)
        seen.add(widget_id)
        try:
            date_override, filters, comparison, grouping, page = _entry_state(widget, entry)
        except (_Rejected, OverrideRejectedError) as exc:
            return JsonResponse({"error": str(exc), "status": "rejected"}, status=400)
        plan.append((widget, entry, date_override, filters, comparison, grouping, page))

    if seen != set(layout_widgets):
        return JsonResponse({"error": "snapshot must cover every published widget exactly once",
                             "status": "rejected"}, status=400)

    frozen_widgets = _freeze_widgets(layout, plan, _PROJECT_ID)

    document = {
        "kind": _SNAPSHOT_KIND,
        "project_id": _PROJECT_ID,
        "slug": slug,
        "version": version,
        "title": layout.get("title"),
        "generated": datetime.now(_tz.utc).isoformat(),
        "widgets": frozen_widgets,
    }
    token = snapshots.create_snapshot(
        user_id=request.user.pk,
        project_id=_PROJECT_ID,
        slug=slug,
        document=document,
        ttl=snapshots.DEFAULT_SNAPSHOT_TTL,
    )
    return JsonResponse(
        {
            "status": "ok",
            "token": token,
            "print_url": reverse("report_v2:print", args=[slug, token]),
            "expires_in": snapshots.DEFAULT_SNAPSHOT_TTL,
        },
        status=201,
    )


# ---------------------------------------------------------------------------
# GET /report/<slug>/print/<token>/ -- render the frozen document, nothing else
# ---------------------------------------------------------------------------
@login_required
@require_GET
def print_view(request, slug: str, snap_token: str):
    """Render print-ready HTML from the frozen snapshot (no re-evaluation, no ORM)."""
    try:
        document = snapshots.load_snapshot(
            snap_token, user_id=request.user.pk, project_id=_PROJECT_ID, slug=slug
        )
    except snapshots.SnapshotExpiredError as exc:
        return HttpResponse(str(exc), status=410, content_type="text/plain")
    except snapshots.SnapshotForeignError as exc:
        return HttpResponse(f"{exc} It cannot be viewed from this report page.", status=403,
                            content_type="text/plain")
    except snapshots.SnapshotTamperedError as exc:
        return HttpResponse(str(exc), status=403, content_type="text/plain")

    if document.get("kind") != _SNAPSHOT_KIND:
        return HttpResponse("snapshot token failed to verify", status=403, content_type="text/plain")

    return render(
        request,
        "report_v2/print.html",
        {"print": _print_view_model(document)},
    )


# ---------------------------------------------------------------------------
# Print view-model (pure; consumes the frozen document only)
# ---------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    """One textContent-safe cell value: ``None`` -> em dash, containers -> readable text."""
    if value is None:
        return "—"
    if isinstance(value, Mapping):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " | ".join(_fmt(item) for item in value)
    return _display_dates(value)


def _table(caption: str, columns: list[str], rows: list[list[str]], kind: str = "meta") -> dict[str, Any]:
    return {"caption": caption, "columns": columns, "rows": rows, "kind": kind}


def _applied_line(applied: Mapping[str, Any]) -> str:
    """One line describing the widget state that was frozen (dates/filters/grouping/page)."""
    parts: list[str] = []
    date = applied.get("date")
    if isinstance(date, Mapping):
        if "relative" in date:
            parts.append(f"window {date['relative']}")
        else:
            parts.append(f"dates {date.get('start')}..{date.get('end')}")
    if applied.get("time_grouping"):
        parts.append(f"grouping {applied['time_grouping']}")
    filters = applied.get("filters") or {}
    for key in sorted(filters):
        parts.append(f"{key}={_fmt(filters[key])}")
    if applied.get("comparison"):
        parts.append(f"group by {applied['comparison']}")
    if applied.get("page") and int(applied["page"] or 1) > 1:
        parts.append(f"page {applied['page']}")
    return _display_dates("; ".join(parts)) if parts else "default window and filters"


def _counts_line(payload: Mapping[str, Any]) -> str:
    counts = payload.get("counts") or {}
    if not counts:
        return ""
    text = (
        f"matching {counts.get('matching')} of {counts.get('incoming')}"
        f" · {counts.get('eligible')} eligible for measurement"
    )
    reasons = counts.get("excluded_by_reason") or {}
    if reasons:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        text += f" · excluded: {detail}"
    return text


def _bucket_label(payload: Mapping[str, Any], index: Any) -> str:
    buckets = payload.get("buckets") or []
    try:
        bucket = buckets[int(index)]
    except (TypeError, ValueError, IndexError):
        return _fmt(index)
    if not isinstance(bucket, Mapping):
        return _fmt(index)
    label = bucket.get("label")
    if label:
        return _display_dates(label)
    start, end = bucket.get("start"), bucket.get("end")
    if start and end:
        return f"{start}..{end}"
    return _fmt(index)


def _widget_tables(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Render the frozen payload as print tables: aggregates, rows, and chart contracts."""
    tables: list[dict[str, Any]] = []
    if payload.get("time_groups"):
        for group in payload["time_groups"]:
            for table in _widget_tables(group["payload"]):
                tables.append({**table, "caption": _display_dates(f"{group['label']} — {table['caption']}")})
        return tables
    chart = payload.get("chart") or payload

    aggregates = payload.get("aggregates") or {}
    matrix = aggregates.get("matrix")
    if isinstance(matrix, Mapping) and chart.get("cells"):
        classes = [str(c) for c in chart.get("classes") or []]
        cells = chart["cells"]
        totals = chart.get("row_totals") or [None] * len(cells)
        rows = []
        for i, gt_class in enumerate(classes):
            row = [gt_class] + [_fmt(cells[i][j]) for j in range(len(classes))]
            row.append(_fmt(totals[i]))
            rows.append(row)
        tables.append(_table(
            str(chart.get("caption") or "Ground truth (rows) vs prediction (columns)"),
            ["GT \\ Pred"] + classes + ["Total"], rows,
        ))
    elif payload.get("grouped_values"):
        grouped = payload["grouped_values"]
        columns = list(grouped[0].get("aggregates") or {})
        tables.append(_table(
            "Grouped values", ["Group"] + columns,
            [[group["name"]] + [_fmt(group["aggregates"].get(key)) for key in columns] for group in grouped],
        ))
    elif aggregates:
        tables.append(_table(
            "Measured values",
            ["Measure", "Value"],
            [[str(k), _fmt(v)] for k, v in aggregates.items()],
        ))

    if isinstance(payload.get("rows"), list) and payload["rows"]:
        columns: list[str] = []
        for row in payload["rows"]:
            for key in row:
                if key not in columns:
                    columns.append(key)
        tables.append(_table(
            "Case rows",
            columns,
            [[_fmt(row.get(col)) for col in columns] for row in payload["rows"]],
            kind="cases",
        ))
        pagination = payload.get("pagination") or {}
        if pagination.get("truncated"):
            tables.append(_table(
                "Table pagination",
                ["Property", "Value"],
                [["shown", str(pagination.get("returned"))],
                 ["page", str(pagination.get("page"))],
                 ["page size", str(pagination.get("page_size"))],
                 ["more rows", "yes — regenerate the snapshot on a later page to print them"]],
            ))

    series = chart.get("series")
    if isinstance(series, list) and series:
        rows = []
        for cell in series:
            label = (_bucket_label(payload, cell.get("bucket_index"))
                     if cell.get("bucket_index") is not None else _fmt(cell.get("category")))
            rows.append([_fmt(cell.get("group")), label, _fmt(cell.get("value"))])
        tables.append(_table("Chart data (series)", ["Series", "Period", "Value"], rows, kind="chart"))

    categories = chart.get("categories")
    if isinstance(categories, list) and categories:
        tables.append(_table(
            "Chart data (categories)",
            ["Category", "Count"],
            [[_fmt(c.get("label")), _fmt(c.get("count"))] for c in categories],
            kind="chart",
        ))

    summaries = chart.get("summaries")
    if isinstance(summaries, list) and summaries:
        rows = []
        for item in summaries:
            summary = item.get("summary") or {}
            for key, value in summary.items():
                rows.append([_fmt(item.get("name")), str(key), _fmt(value)])
        if rows:
            tables.append(_table("Chart data (distribution summary)",
                                 ["Series", "Statistic", "Value"], rows, kind="chart"))

    return tables


def _print_view_model(document: Mapping[str, Any]) -> dict[str, Any]:
    """Template context for ``print.html`` -- everything derives from the frozen document."""
    widgets: list[dict[str, Any]] = []
    for widget in document.get("widgets") or []:
        payload = widget.get("payload") or {}
        if not isinstance(payload, Mapping):
            payload = {}
        error = payload.get("error")
        dates = payload.get("dates") or {}
        versions = payload.get("versions") or {}
        widgets.append(
            {
                "id": widget.get("widget_id"),
                "title": widget.get("title"),
                "type": widget.get("type"),
                "text": payload.get("text", ""),
                "export": widget.get("export"),
                "error": str(error) if error else "",
                "summary": _summary_text(payload) if not error else str(error),
                "applied_line": _applied_line(widget.get("applied") or {}),
                "counts_line": _counts_line(payload) if not error else "",
                "window": _display_dates(f"{dates.get('window_start') or '?'} .. {dates.get('window_end') or '?'}"),
                "anchor": _display_dates(dates.get("anchor_date") or "—"),
                "timezone": dates.get("timezone") or "",
                "coverage": _display_dates(dates.get("coverage_note") or ""),
                "measurement": versions.get("measurement_id") or "",
                "policy": versions.get("policy_ref") or "—",
                "tables": [] if error else _widget_tables(payload),
            }
        )
    return {
        "slug": document.get("slug"),
        "version": document.get("version"),
        "title": document.get("title"),
        "project_id": document.get("project_id"),
        "generated": document.get("generated"),
        "widgets": widgets,
    }


# ---------------------------------------------------------------------------
# POST /report/<slug>/email/ -- legacy-style HTML email off the frozen snapshot
# ---------------------------------------------------------------------------
def _validate_recipients(raw: object) -> list[str]:
    """Accept only a JSON list of already-split address strings (client splits like legacy)."""
    if not isinstance(raw, list) or not raw:
        raise _Rejected("recipients must be a non-empty list of email addresses")
    if len(raw) > _MAX_RECIPIENTS:
        raise _Rejected(f"at most {_MAX_RECIPIENTS} recipients are allowed")
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise _Rejected("each recipient must be a string")
        address = item.strip()
        if not address or len(address) > 254 or not _EMAIL_RE.match(address):
            raise _Rejected(f"invalid email address {item!r}")
        if address not in seen:  # de-duplicate, preserving order
            seen.append(address)
    if not seen:
        raise _Rejected("no valid recipients")
    return seen


def _validate_note(raw: object) -> str:
    """Optional free-text note; stripped and length-bounded (the template escapes it)."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise _Rejected("note must be a string")
    note = raw.strip()
    if len(note) > _MAX_NOTE_CHARS:
        raise _Rejected(f"note is longer than {_MAX_NOTE_CHARS} characters")
    return note


def _validate_images(raw: object, document: Mapping[str, Any]) -> dict[str, bytes]:
    """Validate client-captured chart PNGs against the frozen snapshot.

    Names must be chart-typed widgets of THIS snapshot; the payload must be a PNG
    data URL whose bytes carry the real PNG signature and stay within the bound.
    Nothing is silently dropped -- every invalid entry is an explicit rejection.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _Rejected("images must be an object keyed by widget id")
    if len(raw) > _MAX_IMAGES:
        raise _Rejected(f"at most {_MAX_IMAGES} chart images are allowed")
    by_id = {widget.get("widget_id"): widget for widget in document.get("widgets") or []}
    out: dict[str, bytes] = {}
    for name, value in raw.items():
        widget = by_id.get(name)
        if widget is None or widget.get("type") not in _CHART_DISPLAYS:
            raise _Rejected(f"chart image {name!r} is not an allowed widget of this snapshot")
        if not isinstance(value, str) or not value.startswith(_PNG_DATAURL_PREFIX):
            raise _Rejected(f"chart image {name!r} must be a PNG data URL")
        try:
            img_bytes = base64.b64decode(value[len(_PNG_DATAURL_PREFIX):], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _Rejected(f"chart image {name!r} is not valid base64") from exc
        if len(img_bytes) > _MAX_IMAGE_BYTES:
            raise _Rejected(f"chart image {name!r} exceeds {_MAX_IMAGE_BYTES} bytes")
        if not img_bytes.startswith(_PNG_SIGNATURE):
            raise _Rejected(f"chart image {name!r} is not PNG content")
        out[name] = img_bytes
    return out


def _email_view_model(document: Mapping[str, Any], cid_map: Mapping[str, str]) -> dict[str, Any]:
    """The email body model: the print model plus email-specific disclosure rules.

    Discrepancy case tables stay summary-only (the widget's frozen ``export`` flag
    must be ``full`` for rows to appear at all, and even then rows are capped);
    chart-data tables are dropped when a captured chart image carries the widget.
    """
    vm = _print_view_model(document)
    for widget in vm["widgets"]:
        widget["image_cid"] = cid_map.get(widget["id"])
        widget["rows_note"] = ""
        if widget["error"]:
            continue
        tables = widget["tables"]
        if widget["image_cid"]:
            tables = [t for t in tables if t.get("kind") != "chart"]
        if widget["type"] == "table":
            cases = [t for t in tables if t.get("kind") == "cases"]
            if widget.get("export") != "full":
                tables = [t for t in tables if t.get("kind") != "cases"]
                if cases:
                    widget["rows_note"] = (
                        f"{len(cases[0]['rows'])} case rows captured; this table is "
                        "summary-only in email. Open the report page for the full table."
                    )
            elif cases and len(cases[0]["rows"]) > _EMAIL_ROWS_CAP:
                full = cases[0]["rows"]
                capped = [t for t in tables if t.get("kind") != "cases"]
                head = dict(cases[0], rows=full[:_EMAIL_ROWS_CAP])
                capped.append(head)
                tables = capped
                widget["rows_note"] = (
                    f"Showing the first {_EMAIL_ROWS_CAP} of {len(full)} captured rows."
                )
        widget["tables"] = tables
    return vm


def _email_text(vm: Mapping[str, Any], note: str, sender: str) -> str:
    """Readable prose fallback -- explicitly NOT a monospaced data dump."""
    lines: list[str] = [
        f"PRIMER-LLM Report - {vm['title']} ({vm['version']})",
        f"Project {vm['project_id']}, report {vm['slug']}, generated {vm['generated']}.",
        "",
    ]
    if note:
        lines.extend(["Note from the sender:", note, ""])
    for widget in vm["widgets"]:
        lines.append(f"{widget['title']} ({widget['type']})")
        if widget["type"] in {"text", "divider", "heading"}:
            lines.append(widget.get("text", "") if widget["type"] == "text" else widget["title"] if widget["type"] == "heading" else "--------------------")
            continue
        if widget["error"]:
            lines.append(f"  Could not be evaluated when captured: {widget['error']}")
        else:
            lines.append(f"  Window {widget['window']}, anchor {widget['anchor']}.")
            lines.append(f"  State: {widget['applied_line']}.")
            if widget["counts_line"]:
                lines.append(f"  Counts: {widget['counts_line']}.")
            if widget["measurement"]:
                lines.append(f"  Measurement {widget['measurement']}, policy {widget['policy']}.")
            if widget["image_cid"]:
                lines.append("  The chart is attached as an image.")
            if widget["rows_note"]:
                lines.append(f"  {widget['rows_note']}")
            for table in widget["tables"]:
                lines.append(f"  {table['caption']}:")
                for row in table["rows"][:8]:
                    lines.append("    " + " - ".join(str(cell) for cell in row))
                if len(table["rows"]) > 8:
                    lines.append(f"    ... {len(table['rows']) - 8} more rows in the HTML version.")
        lines.append("")
    lines.append(f"Sent via PRIMER-LLM by {sender}.")
    return "\n".join(lines)


@login_required
@require_POST
@csrf_protect
def email_report(request, slug: str):
    """Send the frozen snapshot as a legacy-style HTML email (explicit user action only).

    Every value in the body is read from the snapshot -- never from posted browser
    metrics; the only client-supplied presentation input is the validated set of chart
    PNG captures. Recipients/note/image validation failures are explicit 400s; a
    duplicate submission of the same snapshot to the same recipients with the same note
    is rejected 409 without re-sending (the duplicate-click guard).
    """
    try:
        body = json.loads(request.body or b"{}")
    except Exception:
        return JsonResponse({"error": "request body is not valid json", "status": "rejected"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "request body must be a json object", "status": "rejected"}, status=400)
    for key in body:
        if key not in _EMAIL_BODY_KEYS:
            return JsonResponse({"error": f"unexpected top-level key {key!r}", "status": "rejected"}, status=400)

    token = body.get("snapshot")
    if not isinstance(token, str) or not token:
        return JsonResponse({"error": "a snapshot token is required", "status": "rejected"}, status=400)
    try:
        document = snapshots.load_snapshot(
            token, user_id=request.user.pk, project_id=_PROJECT_ID, slug=slug
        )
    except snapshots.SnapshotExpiredError as exc:
        return JsonResponse({"error": str(exc), "status": "expired"}, status=410)
    except snapshots.SnapshotForeignError:
        return JsonResponse({"error": "snapshot is scoped to another user or report",
                             "status": "foreign"}, status=403)
    except snapshots.SnapshotTamperedError:
        return JsonResponse({"error": "snapshot token failed to verify", "status": "tampered"}, status=403)
    if document.get("kind") != _SNAPSHOT_KIND:
        return JsonResponse({"error": "snapshot token failed to verify", "status": "tampered"}, status=403)

    try:
        recipients = _validate_recipients(body.get("recipients"))
        note = _validate_note(body.get("note"))
        images = _validate_images(body.get("images"), document)
    except _Rejected as exc:
        return JsonResponse({"error": str(exc), "status": "rejected"}, status=400)

    # Duplicate-click guard: the same snapshot + recipients + note will not re-send
    # within the guard window (the token is single-snapshot, so the key is stable).
    replay_key = "email-sent:{}:{}".format(
        hashlib.sha256(token.encode("utf-8")).hexdigest()[:24],
        hashlib.sha256(("\x00".join(recipients) + "\x00" + note).encode("utf-8")).hexdigest()[:16],
    )
    if snapshots.transient_get(replay_key) is not None:
        return JsonResponse({"error": "this report was already sent to these recipients "
                                      "from this snapshot", "status": "duplicate"}, status=409)

    cid_map = {name: f"cid:{name}@primer-llm" for name in images}
    vm = _email_view_model(document, cid_map)
    subject = f"PRIMER-LLM Report \u2014 {document.get('title')} ({document.get('version')})"
    html_content = render_to_string(
        "report_v2/email.html", {"email": vm, "note": note, "subject": subject}
    )
    text_content = _email_text(vm, note, request.user.get_username())

    try:
        related = SafeMIMEMultipart("related")
        related["Subject"] = subject
        related["From"] = settings.DEFAULT_FROM_EMAIL
        related["To"] = ", ".join(recipients)
        related["Date"] = formatdate(localtime=True)
        related["Message-ID"] = make_msgid(domain="primer-llm")

        alt = SafeMIMEMultipart("alternative")
        alt.attach(SafeMIMEText(text_content, "plain", "utf-8"))
        alt.attach(SafeMIMEText(html_content, "html", "utf-8"))
        related.attach(alt)
        for name, img_bytes in images.items():
            part = MIMEImage(img_bytes, _subtype="png")
            part.add_header("Content-ID", f"<{name}@primer-llm>")
            part.add_header("Content-Disposition", "inline", filename=f"{name}.png")
            related.attach(part)

        class _CIDEmail(EmailMessage):
            def message(self_inner):
                return related

        _CIDEmail(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        ).send(fail_silently=False)
    except Exception as exc:
        return JsonResponse({"error": f"email send failed: {exc}", "status": "error"}, status=500)

    snapshots.transient_set(replay_key, "1", ttl=_EMAIL_REPLAY_TTL)
    return JsonResponse({"status": "sent", "sent_to": recipients})
