# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Task 13 published-report page + per-widget data endpoint: security & rendering guarantees.

One named test per Task-13 Done-when criterion. Every filesystem touch runs through an injected
scratch ``REPORT_V2_ROOT`` and the only rows ever evaluated are the synthetic mappings returned by the
single ORM seam :func:`report_v2.data.fetch_project_rows`, monkeypatched here (``views`` calls it as
``data.fetch_project_rows`` so patching the ``data`` module attribute is what binds). No real clinical
data is constructed; the layout, the row shapes and the sites are synthetic demonstration values.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core import signing
from django.test import Client, TestCase, override_settings

from report_v2 import data, views

# A two-section, 12-column layout: section 1 a value widget; section 2 a wide table widget plus a
# never-matching value widget (made empty by the seam, so it exercises the server empty state).
LAYOUT = """\
schema_version: 1
project: prime
id: t13report
title: Task 13 Report
grid:
  columns: 12
  row_height_px: 64
sections:
- id: s1
  title: Section One
  widgets:
  - id: v1
    title: Value One
    type: value
    layout:
      width: 4
      height: 1
    query:
      measurement: record_count
      inputs: {}
    window:
      start: D-30
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
- id: s2
  title: Section Two
  widgets:
  - id: t1
    title: Table One
    type: table
    layout:
      width: 12
      height: 6
    query:
      measurement: record_count
      inputs: {}
    window:
      start: D-30
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
  - id: v2
    title: Never Matching
    type: value
    layout:
      width: 4
      height: 1
    query:
      measurement: record_count
      inputs: {}
    window:
      start: D-30
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
"""

_SETTINGS = dict(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    SECURE_SSL_REDIRECT=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)

_EVENT_DATE = date(2026, 9, 1)
_ACCESSION_BASE = 9_000_000
_DATA_ERROR_MESSAGE = "synthetic adapter fault (task 13)"


def _rows(n: int, *, eligible: bool = True, foreign: bool = False) -> list[dict]:
    """Synthetic evaluator input rows (never clinical): accession / event_date / site / eligible."""
    out: list[dict] = []
    for i in range(n):
        row = {
            "accession": _ACCESSION_BASE + i,
            "event_date": _EVENT_DATE,
            "site": "SYNTH-SITE-A",
            "eligible": eligible,
        }
        if foreign:
            row["cross_project"] = True
        out.append(row)
    return out


class _Seam:
    """A ``fetch_project_rows`` stand-in keyed on the requested widget id, with its own call ledger.

    The row decision is taken from ``layout_widget['id']`` so a single patch can make one frame empty,
    fault, foreign, or fully hydrated while its siblings stay live -- exactly the fault-isolation the
    page is asserted to provide. ``calls`` records the widget id of every read so the tests can prove
    that every 4xx rejection reads zero rows.
    """

    def __init__(self, *, good=None, empty_ids=(), error_ids=(), foreign_ids=()):
        self.good = _rows(3) if good is None else list(good)
        self.empty_ids = frozenset(empty_ids)
        self.error_ids = frozenset(error_ids)
        self.foreign_ids = frozenset(foreign_ids)
        self.calls: list[str] = []

    def reset(self) -> None:
        self.calls.clear()

    def __call__(self, project_id: str = "prime", *, layout_widget: dict | None = None, limit: int | None = None):
        widget_id = (layout_widget or {}).get("id")
        self.calls.append(widget_id)
        if widget_id in self.error_ids:
            raise data.AdapterError(_DATA_ERROR_MESSAGE + ": " + str(widget_id))
        if widget_id in self.foreign_ids:
            return _rows(3, foreign=True)
        if widget_id in self.empty_ids:
            return _rows(3, eligible=False)
        return list(self.good)


def _make_seam(*, good=None, empty_ids=(), error_ids=(), foreign_ids=()) -> _Seam:
    return _Seam(good=good, empty_ids=empty_ids, error_ids=error_ids, foreign_ids=foreign_ids)


@override_settings(**_SETTINGS)
class PublishedReportPageTests(TestCase):
    """One named test per Task-13 Done-when item; state under a per-test scratch root."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="t13-admin", email="t13-admin@example.invalid", password="pw-strong-1"
        )
        cls.normal = User.objects.create_user(
            username="t13-normal", email="t13-normal@example.invalid", password="pw-strong-1"
        )

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="t13-pages-"))
        overrider = override_settings(REPORT_V2_ROOT=str(self.root))
        overrider.enable()
        self.addCleanup(overrider.disable)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    # -- helpers -----------------------------------------------------------
    def _publish(self, def_id: str, yaml_text: str = LAYOUT) -> str:
        client = Client(SERVER_NAME="localhost")  # CSRF not enforced for the trusted admin publish
        client.force_login(self.admin)
        response = client.post(
            "/report/layout/editor/publish/", {"def_id": def_id, "yaml_text": yaml_text}
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()["version"]

    def _login(self, user, *, enforce_csrf: bool = True) -> Client:
        client = Client(SERVER_NAME="localhost", enforce_csrf_checks=enforce_csrf)
        client.force_login(user)
        return client

    def _page(self, client: Client, slug: str) -> str:
        response = client.get(f"/report/{slug}/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _prime_csrf(self, client: Client, slug: str) -> None:
        # Rendering the page primes the csrftoken cookie (page.html carries {% csrf_token %}).
        client.get(f"/report/{slug}/")

    def _post_data(self, client: Client, slug: str, widget_id: str, body: dict, *, csrf: bool = True):
        url = f"/report/{slug}/widget/{widget_id}/data/"
        headers = {}
        if csrf:
            headers["X-CSRFToken"] = client.cookies["csrftoken"].value
        return client.post(
            url, json.dumps(body), content_type="application/json", headers=headers or None
        )

    def _initial_payload(self, html_text: str, widget_id: str) -> dict:
        pattern = r'data-widget-id="' + re.escape(widget_id) + r'".*?data-initial-payload>(.*?)</pre>'
        match = re.search(pattern, html_text, re.S)
        self.assertIsNotNone(match, f"no initial payload for {widget_id}")
        return json.loads(html.unescape(match.group(1)))

    def _frame(self, html_text: str, widget_id: str) -> str:
        match = re.search(
            r'data-widget-id="' + re.escape(widget_id) + r'".*?</article>', html_text, re.S
        )
        self.assertIsNotNone(match, f"no frame for {widget_id}")
        return match.group(0)

    # -- 1. index lists published reports / shows an empty state, touches no rows ----------
    def test_index_lists_published_and_empty_state(self):
        empty_client = self._login(self.normal, enforce_csrf=False)
        with mock.patch(
            "report_v2.data.fetch_project_rows", side_effect=AssertionError("index must not read rows")
        ):
            blank = empty_client.get("/report/")
            self.assertEqual(blank.status_code, 200)
            blank_html = blank.content.decode("utf-8")
            self.assertIn("No reports are published yet", blank_html)
            self.assertIn('id="noPublished"', blank_html)
            self.assertNotIn("/report/t13report/", blank_html)

        version = self._publish("t13report")
        self.assertEqual(version, "t13report@r1")
        with mock.patch(
            "report_v2.data.fetch_project_rows", side_effect=AssertionError("index must not read rows")
        ):
            listed = empty_client.get("/report/")
            self.assertEqual(listed.status_code, 200)
            listed_html = listed.content.decode("utf-8")
            self.assertIn("/report/t13report/", listed_html)
            self.assertIn("Task 13 Report", listed_html)
            self.assertIn(version, listed_html)

    # -- 2. the report page renders in YAML order on a 12-column grid ----------------------
    def test_report_shell_does_not_read_studies_or_render_raw_tables(self):
        self._publish("t13report")
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=AssertionError("shell must not read studies")) as fetch:
            page = self._page(self._login(self.normal), "t13report")
        fetch.assert_not_called()
        self.assertEqual(page.count('class="widget-skeleton"'), 3)
        self.assertNotIn('<table', page)
        for widget_id in ("v1", "v2", "t1"):
            self.assertTrue(self._initial_payload(page, widget_id)["loading"])

    def test_report_page_renders_in_yaml_order_on_12col_grid(self):
        self._publish("t13report")
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=_make_seam(empty_ids={"v2"})):
            html_text = self._page(self._login(self.normal, enforce_csrf=False), "t13report")

        self.assertIn("data-report-page", html_text)
        self.assertIn('data-slug="t13report"', html_text)
        self.assertIn('data-version="t13report@r1"', html_text)

        # Sections and widgets appear in declared (YAML) order.
        self.assertLess(
            html_text.index("data-widget-id=\"heading_s1\""), html_text.index("data-widget-id=\"heading_s2\"")
        )
        positions = [html_text.index(f'data-widget-id="{wid}"') for wid in ("v1", "t1", "v2")]
        self.assertEqual(positions, sorted(positions))

        # The grid spans carry the declared widths; the inline spans only format because the
        # container rule below exists in the shipped stylesheet.
        css = (
            Path(views.__file__).resolve().parent / "static" / "report_v2" / "report.css"
        ).read_text(encoding="utf-8")
        self.assertRegex(css, r"\.report-grid\s*\{[^}]*display\s*:\s*grid")
        self.assertRegex(css, r"\.report-grid\s*\{[^}]*grid-template-columns\s*:\s*repeat\(\s*12\s*,")
        self.assertRegex(css, r"@media[^{]*\{[\s\S]*\.report-grid\s*>\s*\.widget-frame\s*\{[^}]*grid-column\s*:\s*span\s*12\s*!important")
        v1 = self._frame(html_text, "v1")
        t1 = self._frame(html_text, "t1")
        self.assertRegex(v1, r'grid-column:\s*span\s*4')
        self.assertRegex(t1, r'grid-column:\s*span\s*12')

        # Every frame exposes the full client data contract.
        for widget_id, kind in (("v1", "value"), ("t1", "table"), ("v2", "value")):
            frame = self._frame(html_text, widget_id)
            with self.subTest(widget=widget_id):
                self.assertIn(f'data-widget-id="{widget_id}"', frame)
                self.assertIn(f'data-type="{kind}"', frame)
                self.assertIn(f'data-data-url="/report/t13report/widget/{widget_id}/data/"', frame)
                self.assertRegex(frame, r'data-context-token="[^"]+"')
                self.assertIn("data-initial-payload", frame)

    # -- 3. ordinary users see no draft / configuration affordances --------------------------
    def test_ordinary_user_sees_no_draft_or_configuration_controls(self):
        self._publish("t13report")
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=_make_seam(empty_ids={"v2"})):
            ordinary = self._page(self._login(self.normal, enforce_csrf=False), "t13report")
            admin = self._page(self._login(self.admin, enforce_csrf=False), "t13report")

        for forbidden in ("Edit report", "/report/layout/", "draft"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, ordinary)
        toolbar = re.search(r'<p class="report-v2-exportbar">(.*?)</p>', admin, re.S).group(1)
        self.assertRegex(toolbar, r'(?s)data-action="email".*?</button>\s*<a class="btn btn-secondary"[^>]*data-role="edit-layout">Edit report</a>')
        self.assertEqual(admin.count('data-role="edit-layout"'), 1)

    # -- 4. the server-issued context token pins the version, and tampering is refused -----
    def test_server_issued_context_pins_version(self):
        version = self._publish("t13report")
        seam = _make_seam(empty_ids={"v2"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            html_text = self._page(client, "t13report")
            token = re.search(r'data-widget-id="t1".*?data-context-token="([^"]+)"', html_text, re.S).group(1)
            token2 = re.search(r'data-widget-id="v1".*?data-context-token="([^"]+)"', html_text, re.S).group(1)

            # The shipped token verifies under the view's own salt and carries the pinned triple.
            self.assertEqual(signing.loads(token, salt=views._CTX_SALT), f"t13report|{version}|t1")

            # CSRF is enforced: the same well-formed request without a token is a 4xx.
            no_token = self._post_data(client, "t13report", "t1", {"context": token, "request_seq": 1}, csrf=False)
            self.assertGreaterEqual(no_token.status_code, 400)
            self.assertLess(no_token.status_code, 500)

            # A token signed under a foreign salt cannot be made to verify.
            forged = signing.dumps(f"t13report|{version}|t1", salt="some-other-salt")
            tampered = self._post_data(client, "t13report", "t1", {"context": forged, "request_seq": 1})
            self.assertEqual(tampered.status_code, 403)
            self.assertEqual(tampered.json()["status"], "tampered")

            # A missing context is refused; a token bound to another widget is refused as tampered.
            missing = self._post_data(client, "t13report", "t1", {"request_seq": 1})
            self.assertEqual(missing.status_code, 403)
            wrong_widget = self._post_data(client, "t13report", "t1", {"context": token2, "request_seq": 1})
            self.assertEqual(wrong_widget.status_code, 403)
            self.assertEqual(wrong_widget.json()["status"], "tampered")

    # -- 5. version cannot be forged; a stale (republished) token yields 409 ----------------
    def test_version_cannot_be_forged_and_stale_yields_409(self):
        self._publish("t13report")
        seam = _make_seam(empty_ids={"v2"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            html_text = self._page(client, "t13report")
            token = re.search(r'data-widget-id="t1".*?data-context-token="([^"]+)"', html_text, re.S).group(1)

            # Any disallowed top-level key is rejected up-front, before a single row is read.
            for smuggled in ("version", "anchor", "measurement_override", "rows", "widget_id", "include_rows"):
                seam.reset()
                with self.subTest(key=smuggled):
                    rejected = self._post_data(
                        client, "t13report", "t1", {"context": token, "request_seq": 1, smuggled: "x"}
                    )
                    self.assertEqual(rejected.status_code, 400)
                    self.assertEqual(rejected.json()["status"], "rejected")
                    self.assertEqual(len(seam.calls), 0)

            # After a republish the old token is pinned to a stale version.
            stale_client = self._login(self.normal)
            stale_html = self._page(stale_client, "t13report")
            stale_token = re.search(r'data-widget-id="t1".*?data-context-token="([^"]+)"', stale_html, re.S).group(1)
            self._publish("t13report")  # -> r2; the r1 token is now stale
            stale = self._post_data(stale_client, "t13report", "t1", {"context": stale_token, "request_seq": 1})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["status"], "stale")

    # -- 6. the override allow-list admits only what the widget declared --------------------
    def test_allowlist_overrides(self):
        self._publish("t13report")
        seam = _make_seam(good=_rows(120))
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            self._page(client, "t13report")

            def rejected(body, widget="t1"):
                seam.reset()
                response = self._post_data(client, "t13report", widget, body)
                self.assertEqual(response.status_code, 400, response.content[:200])
                self.assertEqual(response.json()["status"], "rejected")
                self.assertEqual(len(seam.calls), 0, "a 400 must not read any rows")

            def accepted(body, widget="t1"):
                seam.reset()
                response = self._post_data(client, "t13report", widget, body)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["status"], "ok")
                self.assertGreater(len(seam.calls), 0)
                return response.json()

            # Date overrides.
            accepted({"context": self._token(client, "t1"), "date": {"start": "2026-08-01", "end": "2026-09-01"}})
            accepted({"context": self._token(client, "t1"), "date": {"relative": "D-7"}})
            rejected({"context": self._token(client, "t1"), "date": {"relative": "Z-7"}})
            rejected({"context": self._token(client, "t1"), "date": {"start": "2026-08-01"}})

            # Filter allow-list + value shape.
            rejected({"context": self._token(client, "t1"), "filters": {"region": "north"}})
            rejected({"context": self._token(client, "t1"), "filters": {"site": {"nested": 1}}})
            accepted({"context": self._token(client, "t1"), "filters": {"site": ["A"] * 5}})
            rejected({"context": self._token(client, "t1"), "filters": {"site": ["A"] * 51}})

            # Comparison must be one of the widget's declared compare-by axes.
            accepted({"context": self._token(client, "t1"), "comparison": "site"})
            rejected({"context": self._token(client, "t1"), "comparison": "region"})

            # Page selector bounds.
            for bad_page in (0, "x", 10**6, True):
                rejected({"context": self._token(client, "t1"), "page": bad_page})

            # Page 2 slices rows 50..100.
            payload = accepted({"context": self._token(client, "t1"), "page": 2})
            self.assertEqual(payload["pagination"]["page"], 2)
            self.assertEqual(len(payload["rows"]), 50)
            self.assertEqual(payload["rows"][0]["accession"], _ACCESSION_BASE + 50)

    def _token(self, client: Client, widget_id: str) -> str:
        html_text = self._page(client, "t13report")
        return re.search(
            r'data-widget-id="' + re.escape(widget_id) + r'".*?data-context-token="([^"]+)"', html_text, re.S
        ).group(1)

    # -- 7. the ok response echoes request_seq and the reducer keeps its ledger ------------
    def test_ok_response_echoes_request_seq(self):
        self._publish("t13report")
        seam = _make_seam(empty_ids={"v2"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            self._page(client, "t13report")
            token = self._token(client, "t1")
            echoed = self._post_data(client, "t13report", "t1", {"context": token, "request_seq": 7})
            self.assertEqual(echoed.status_code, 200)
            self.assertEqual(echoed.json()["request_seq"], 7)
            self.assertIsInstance(echoed.json()["request_seq"], int)
            non_int = self._post_data(client, "t13report", "t1", {"context": token, "request_seq": "x"})
            self.assertIsNone(non_int.json()["request_seq"])

        state_source = (
            Path(views.__file__).resolve().parent / "static" / "report_v2" / "page_state.mjs"
        ).read_text(encoding="utf-8")
        for token_name in ("pendingSeq", "lastAppliedSeq", "droppedStale"):
            with self.subTest(token=token_name):
                self.assertIn(token_name, state_source)

    # -- 8. the error domain is data (200), never a server fault (500) ----------------------
    def test_error_domain_returns_200_not_500(self):
        self._publish("t13report")
        seam = _make_seam(error_ids={"t1"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            self._page(client, "t13report")
            token = self._token(client, "t1")
            errored = self._post_data(client, "t13report", "t1", {"context": token, "request_seq": 1})
            self.assertEqual(errored.status_code, 200)
            self.assertEqual(errored.json()["status"], "error")
            self.assertIn(_DATA_ERROR_MESSAGE, errored.json()["error"])
            self.assertNotIn("rows", errored.json())

            # Page render still 200: the failing frame reports the fault, siblings still hydrate.
            page_html = self._page(client, "t13report")
            self.assertTrue(self._initial_payload(page_html, "t1")["loading"])
            sibling = self._initial_payload(page_html, "v1")
            self.assertTrue(sibling["loading"])

    # -- 9. an empty widget explains itself --------------------------------------------------
    def test_empty_widget_explains_itself(self):
        self._publish("t13report")
        seam = _make_seam(empty_ids={"v2"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            html_text = self._page(self._login(self.normal, enforce_csrf=False), "t13report")

        empty_message = views._EMPTY_MESSAGE
        v2 = self._frame(html_text, "v2")
        self.assertIn(empty_message, v2)
        self.assertTrue(self._initial_payload(html_text, "v2")["loading"])
        self.assertTrue(self._initial_payload(html_text, "v1")["loading"])

        # Every frame body carries the hidden server empty-message element report.js reads.
        frames = re.findall(r'data-widget-id="[^"]+".*?</article>', html_text, re.S)
        frames = [frame for frame in frames if "data-decoration" not in frame]
        self.assertEqual(len(frames), 3)
        for frame in frames:
            self.assertIn('data-role="empty-message"', frame)
            self.assertIn(empty_message, frame)

    # -- 10. a long table paginates instead of clipping ------------------------------------
    def test_long_table_paginates_without_clipping(self):
        self._publish("t13report")
        seam = _make_seam(good=_rows(120))
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            self._page(client, "t13report")
            token = self._token(client, "t1")
            response = self._post_data(client, "t13report", "t1", {"context": token, "request_seq": 1})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(
                set(payload["pagination"]) >= {"page", "page_size", "returned", "truncated"}, True
            )
            self.assertEqual(payload["pagination"]["page_size"], 50)
            self.assertEqual(payload["pagination"]["returned"], 50)
            self.assertTrue(payload["pagination"]["truncated"])

        css = (
            Path(views.__file__).resolve().parent / "static" / "report_v2" / "report.css"
        ).read_text(encoding="utf-8")
        self.assertRegex(css, r"\.widget-body\s*\{[^}]*overflow\s*:\s*auto")

    def test_table_navigation_reuses_scope_and_apply_refreshes(self):
        views._TABLE_PAGES.clear()
        self._publish("t13report")
        client = self._login(self.normal)
        self._page(client, "t13report")
        token = self._token(client, "t1")
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=_make_seam(good=_rows(120))) as fetch:
            first = self._post_data(client, "t13report", "t1", {"context": token}).json()
            second = self._post_data(client, "t13report", "t1", {"context": token, "page": 2}).json()
            third = self._post_data(client, "t13report", "t1", {"context": token, "page": 3}).json()
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual([first["pagination"]["returned"], second["pagination"]["returned"], third["pagination"]["returned"]], [50, 50, 20])
            self.assertFalse(third["pagination"]["truncated"])
            self.assertEqual(first["counts"], second["counts"])
            self.assertNotEqual(first["rows"], second["rows"])
            self._post_data(client, "t13report", "t1", {"context": token})
            self.assertEqual(fetch.call_count, 2)
            other = self._login(self.admin)
            self._page(other, "t13report")
            self._post_data(other, "t13report", "t1", {"context": token, "page": 2})
            self.assertEqual(fetch.call_count, 3)
            views._TABLE_PAGES.clear()
            self._post_data(client, "t13report", "t1", {"context": token, "page": 2})
            self.assertEqual(fetch.call_count, 4)

    # -- 11. unknown paths resolve to 404; cross-report tokens are refused ------------------
    def test_unknown_paths_404(self):
        self._publish("t13report")
        self._publish("t13other")
        seam = _make_seam(empty_ids={"v2"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal, enforce_csrf=False)
            self.assertEqual(client.get("/report/no-such-report/").status_code, 404)
            # The defid converter regex rejects mixed case, so this matches no route at all.
            self.assertEqual(client.get("/report/T13Report/").status_code, 404)

            token = self._token(client, "t1")  # a valid token for report t13report
            cross = self._post_data(client, "t13other", "t1", {"context": token, "request_seq": 1})
            # The token's version is pinned inside its signature and belongs to another report, so it
            # can never match the target report's current version -> a stale rejection, never a serve.
            self.assertEqual(cross.status_code, 409)
            self.assertNotEqual(cross.status_code, 200)
            self.assertNotIn("rows", cross.json())

    # -- 12. no 4xx body ever carries row or count data ------------------------------------
    def test_no_row_data_in_rejection_bodies(self):
        self._publish("t13report")
        seam = _make_seam(empty_ids={"v2"})
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=seam):
            client = self._login(self.normal)
            self._page(client, "t13report")
            token = self._token(client, "t1")
            forged = signing.dumps("t13report|t13report@r1|t1", salt="foreign-salt")

            cases = {
                "bad_json_top_key": {"context": token, "rows": []},
                "tampered": {"context": forged, "request_seq": 1},
                "missing_context": {"request_seq": 1},
                "wrong_widget": {"context": self._token(client, "v1"), "request_seq": 1},
                "bad_filter": {"context": token, "filters": {"nope": "x"}},
            }
            for label, body in cases.items():
                with self.subTest(case=label):
                    response = self._post_data(client, "t13report", "t1", body)
                    self.assertGreaterEqual(response.status_code, 400)
                    self.assertLess(response.status_code, 500)
                    payload = response.json()
                    self.assertNotIn("rows", payload)
                    self.assertNotIn("counts", payload)

    # -- 13. the client summary wording is byte-identical to the server's ---------------------
    def test_client_summary_wording_matches_server(self):
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skip("node is not available")
        source = (
            Path(views.__file__).resolve().parent / "static" / "report_v2" / "report.js"
        ).read_text(encoding="utf-8")
        empty_decl = re.search(r"const DEFAULT_EMPTY = '([^']*)';", source)
        self.assertIsNotNone(empty_decl, "report.js must keep the DEFAULT_EMPTY constant")
        self.assertEqual(empty_decl.group(1), views._EMPTY_MESSAGE)
        summary_fn = re.search(r"(function summaryText\(payload\) \{[\s\S]*?\n    \})", source)
        self.assertIsNotNone(summary_fn, "report.js must keep function summaryText")

        cases = [
            {"counts": {"incoming": 6, "matching": 6, "eligible": 5},
             "dates": {"coverage_note": "coverage 2026-08-14..2026-09-12"}},
            {"counts": {"incoming": 6, "matching": 3, "eligible": 2}},
            {"counts": {"incoming": 6, "matching": 0, "eligible": 0}},
            {"error": "unexpected top-level key"},
        ]
        script = (
            "const cases = " + json.dumps(cases) + ";\n"
            "const DEFAULT_EMPTY = " + json.dumps(views._EMPTY_MESSAGE) + ";\n"
            + re.search(r"const displayDates = [^\n]+", source).group(0) + "\n"
            + summary_fn.group(1) + "\n"
            + "process.stdout.write(JSON.stringify(cases.map(summaryText)));\n"
        )
        completed = subprocess.run(
            [node, "-e", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout), [views._summary_text(case) for case in cases]
        )

    # -- 14A. the admin Edit-layout link is report-specific, ordinary users get no affordance ----- #
    def test_report_edit_link_matches_catalog_with_duplicate_titles(self):
        self._publish("first")
        self._publish("second")
        client = self._login(self.admin, enforce_csrf=False)
        catalog = client.get("/editor/").content.decode()
        for slug in ("first", "second"):
            url = f"/editor/task_13_report__{slug}/"
            self.assertIn(f'href="{url}"', catalog)
            self.assertIn(f'href="{url}"', self._page(client, slug))
            self.assertEqual(client.get(url).status_code, 200)

    def test_admin_edit_layout_links_to_report_specific_editor(self):
        self._publish("t13report")
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=_make_seam(empty_ids={"v2"})):
            admin = self._page(self._login(self.admin, enforce_csrf=False), "t13report")
            ordinary = self._page(self._login(self.normal, enforce_csrf=False), "t13report")
        self.assertIn('href="/editor/task_13_report/"', admin)
        self.assertIn('>Edit report</a>', admin)
        response = self._login(self.admin, enforce_csrf=False).get("/editor/task_13_report/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-role="edit-layout"', admin)
        for forbidden in ("Edit report", "/report/layout/"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, ordinary)

    # -- 14A. grid rows grow with content and the css contract stays pinned ---------------------- #
    def test_grid_rows_grow_with_content(self):
        template = (
            Path(views.__file__).resolve().parent / "templates" / "report_v2" / "page.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("grid-auto-rows: minmax(", template)
        self.assertNotIn("grid-row: span", template)
        self.assertNotIn("px, auto)", template)
        css = (
            Path(views.__file__).resolve().parent / "static" / "report_v2" / "report.css"
        ).read_text(encoding="utf-8")
        self.assertRegex(css, r"\.report-grid\s*\{[^}]*display\s*:\s*grid")
        self.assertRegex(css, r"\.report-grid\s*\{[^}]*grid-template-columns\s*:\s*repeat\(\s*12\s*,")
        self.assertRegex(
            css,
            r"@media[^{]*\{[\s\S]*\.report-grid\s*>\s*\.widget-frame\s*\{[^}]*grid-column\s*:\s*span\s*12\s*!important",
        )

    # -- 14A. the registry boot module is wired into the page and shipped on disk ---------------- #
    def test_registry_boot_script_is_wired(self):
        self._publish("t13report")
        with mock.patch("report_v2.data.fetch_project_rows", side_effect=_make_seam(empty_ids={"v2"})):
            admin = self._page(self._login(self.admin, enforce_csrf=False), "t13report")
        self.assertIn('type="module"', admin)
        self.assertIn('type="module" src="/static/report_v2/report.js"', admin)
        source = (Path(views.__file__).resolve().parent / "static" / "report_v2" / "report.js").read_text(encoding="utf-8")
        self.assertIn("import './widgets/boot.mjs'", source)
        self.assertIn("from './page_state.mjs'", source)
        self.assertNotIn("widgets/test-synthetic", admin)
        boot = (
            Path(views.__file__).resolve().parent / "static" / "report_v2" / "widgets" / "boot.mjs"
        )
        self.assertTrue(boot.exists(), f"missing deliverable: {boot}")
        boot_text = boot.read_text(encoding="utf-8")
        for kind in ("value", "table", "line", "bar"):
            with self.subTest(kind=kind):
                self.assertRegex(boot_text, r"register\(\s*[\"']" + kind + r"[\"']")
