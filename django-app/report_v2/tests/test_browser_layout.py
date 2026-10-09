# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Task 14A browser/layout suite: structural stabilisation + registry renderers under real Chromium.

The suite boots ONE throwaway dev server (report_v2.tests.browser_settings, a tmp SQLite, a tmp
REPORT_V2_ROOT) over loopback, publishes a synthetic four-widget layout, seeds the study table with
SYNTHETIC rows only (never clinical, never the production ``db/`` tree), then drives it with Playwright
Chromium. Every class is skipped cleanly when Playwright / Chromium tooling is unavailable; when the
tooling is present the assertions must pass. Screenshots land in ``/tmp/rv2-14a-evidence`` for review.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import time
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Playwright import + probe (byte-for-byte import line, guarded).
try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:  # pragma: no cover - tooling absent in some environments
    sync_playwright = None
    HAVE_PW = False

_PW_LAUNCH_OK = False
if HAVE_PW:
    try:
        with sync_playwright() as _probe:
            _probe_browser = _probe.chromium.launch(headless=True)
            _probe_browser.close()
        _PW_LAUNCH_OK = True
    except Exception:  # pragma: no cover
        _PW_LAUNCH_OK = False

HERE = Path(__file__).resolve().parent
DJANGO_APP = HERE.parent.parent                       # .../django-app
EVIDENCE = Path("/tmp/rv2-14a-evidence")

DEF_ID = "browser14a"

# A synthetic, non-clinical four-widget layout (value width 3, table width 12, line + bar width 6).
# Every widget measures ``record_count`` over D-30..D with the date/site/compare-by controls.
LAYOUT = """\
schema_version: 1
project: prime
id: browser14a
title: Browser Layout 14A
grid:
  columns: 12
  row_height_px: 64
sections:
- id: s1
  title: Widgets
  widgets:
  - id: bv
    title: Value Widget
    type: value
    layout:
      width: 3
      height: 2
    query:
      measurement: record_count
      inputs: {}
    window:
      start: 'D-30'
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
  - id: bt
    title: Table Widget
    type: table
    layout:
      width: 12
      height: 6
    query:
      measurement: record_count
      inputs: {}
    window:
      start: 'D-30'
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
  - id: bl
    title: Line Widget
    type: line
    layout:
      width: 6
      height: 4
    query:
      measurement: record_count
      inputs: {}
    window:
      start: 'D-30'
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
  - id: br
    title: Bar Widget
    type: bar
    layout:
      width: 6
      height: 4
    query:
      measurement: record_count
      inputs: {}
    window:
      start: 'D-30'
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: [site]
    ci:
      enabled: false
    export: full
"""

_SEED_ONE_LINER = (
    "from datetime import datetime, timedelta\n"
    "from upload.models import CXRStudy\n"
    "N = int(__import__('os').environ.get('SEED_ROWS', '40'))\n"
    "now = datetime.now()\n"
    "CXRStudy.objects.all().delete()\n"
    "CXRStudy.objects.bulk_create([\n"
    "    CXRStudy(accession_no=9300000+k, workplace=('SYNTH-SITE-A' if k % 2 else 'SYNTH-SITE-B'),\n"
    "             procedure_start_date=now - timedelta(days=(k % 24) + 1), gt_manual=(k % 2),\n"
    "             atelectasis=float(k % 100), consolidation=float((k * 7) % 100))\n"
    "    for k in range(N)\n"
    "])\n"
    "print('ROWS', CXRStudy.objects.count())\n"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _intersects(a, b, *, tol: float = 0.5) -> bool:
    """True when two {x,y,width,height} rects overlap by more than a hair (touching edges are ok)."""
    ax2, ay2 = a["x"] + a["width"], a["y"] + a["height"]
    bx2, by2 = b["x"] + b["width"], b["y"] + b["height"]
    if ax2 <= b["x"] + tol or bx2 <= a["x"] + tol:
        return False
    if ay2 <= b["y"] + tol or by2 <= a["y"] + tol:
        return False
    return True


class _Server:
    """A single throwaway runserver with a migrated tmp database and a synthetic seed."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.root = tmp / "definitions"
        self.env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "report_v2.tests.browser_settings",
            "DATABASE_NAME": str(tmp / "browser.sqlite3"),
            "AUDIT_DATABASE_NAME": str(tmp / "audit.sqlite3"),
            "REPORT_V2_ROOT": str(self.root),
            "PYTHONUNBUFFERED": "1",
            "SEED_ROWS": os.environ.get("SEED_ROWS", "40"),
        }
        self.proc = None

    def _manage(self, *args: str, command: str | None = None) -> subprocess.completedprocess:
        argv = [sys.executable, "manage.py", *args]
        return subprocess.run(argv, cwd=str(DJANGO_APP), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def _manage_shell(self, liner: str) -> subprocess.completedprocess:
        return subprocess.run([sys.executable, "manage.py", "shell", "--command", liner],
                             cwd=str(DJANGO_APP), env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def migrate(self) -> None:
        done = self._manage("migrate", "--noinput")
        assert done.returncode == 0, f"migrate failed:\n{done.stdout}\n{done.stderr}"

    def create_users(self) -> None:
        liner = (
            "from django.contrib.auth.models import User\n"
            "User.objects.filter(username='browser-admin').exists() or User.objects.create_superuser("
            "username='browser-admin', email='browser-admin@example.invalid', password='pw-strong-1')\n"
            "User.objects.filter(username='browser-normal').exists() or User.objects.create_user("
            "username='browser-normal', email='browser-normal@example.invalid', password='pw-strong-1')\n"
            "print('USERS', User.objects.count())\n"
        )
        done = self._manage_shell(liner)
        assert done.returncode == 0, f"user seed failed:\n{done.stdout}\n{done.stderr}"

    def seed_rows(self, n: int) -> None:
        self.env["SEED_ROWS"] = str(n)
        done = self._manage_shell(_SEED_ONE_LINER)
        assert done.returncode == 0, f"row seed failed:\n{done.stdout}\n{done.stderr}"

    def start(self) -> None:
        # A file keeps an unread PIPE from blocking the server during longer browser suites.
        with (self.tmp / "server.log").open("w", encoding="utf-8") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "manage.py", "runserver", f"127.0.0.1:{self.port}",
                 "--noreload", "--insecure"],
                cwd=str(DJANGO_APP), env=self.env,
                stdout=log, stderr=subprocess.STDOUT, text=True,
            )
        self._wait_ready()

    def _wait_ready(self, *, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last: object = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:                     # the server died on boot
                break
            try:
                with urllib.request.urlopen(f"{self.base}/login/", timeout=2) as response:
                    if getattr(response, "status", response.code) == 200:
                        return
            except Exception as exc:                              # not up yet, keep polling
                last = exc
            time.sleep(0.3)
        raise AssertionError(f"dev server did not become ready on {self.base} (last={last!r})")

    def _open(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPRedirectHandler(),
            urllib.request.HTTPDefaultErrorHandler(),
        )
        return jar, opener

    def get(self, path: str, *, jar=None, opener=None):
        if opener is None:
            jar, opener = self._open()
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            response = opener.open(request)
            body = response.read().decode("utf-8", "replace")
            return getattr(response, "status", response.code), body, jar
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), jar

    def _csrf_from_cookie(self, jar) -> str:
        for cookie in jar:
            if cookie.name == "csrftoken":
                return cookie.value
        return ""

    def login(self, username: str, password: str):
        """Log in over plain http with a cookie jar; return (jar, opener).

        Success is judged by the sessionid cookie the server sets on the post-login redirect, not the
        (already-redirect-followed, hence always 2xx) final status."""
        jar, opener = self._open()
        self.get("/login/", jar=jar, opener=opener)             # prime the csrftoken cookie
        token = self._csrf_from_cookie(jar)
        data = urllib.parse.urlencode(
            {"csrfmiddlewaretoken": token, "username": username, "password": password}
        ).encode()
        request = urllib.request.Request(self.base + "/login/", data=data, method="POST")
        try:
            opener.open(request).read()
        except urllib.error.HTTPError:
            pass
        return jar, opener

    def publish(self, jar, opener) -> dict:
        token = self._csrf_from_cookie(jar)
        data = urllib.parse.urlencode(
            {"csrfmiddlewaretoken": token, "def_id": DEF_ID, "yaml_text": LAYOUT}
        ).encode()
        request = urllib.request.Request(
            self.base + "/report/layout/editor/publish/", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        response = opener.open(request)
        body = response.read().decode("utf-8", "replace")
        return json.loads(body)

    def session_cookie(self, jar) -> str:
        for cookie in jar:
            if cookie.name == "sessionid":
                return cookie.value
        return ""

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@unittest.skipUnless(HAVE_PW and _PW_LAUNCH_OK, "playwright/chromium unavailable")
class BrowserLayoutTests(unittest.TestCase):
    """One shared browser + server across the width/registry/editor checks."""

    def test_email_uses_native_modal_focus_and_escape(self):
        self.page.goto(self.report_url, wait_until="networkidle")
        self.page.locator('[data-action="email"]').click()
        dialog = self.page.locator('dialog[data-role="email-modal"]')
        self.assertTrue(dialog.evaluate("node => node.open && node.matches(':modal')"))
        self.assertTrue(self.page.locator('[data-role="email-recipients"]').evaluate("node => node === document.activeElement"))
        self.page.keyboard.press("Escape")
        self.assertFalse(dialog.evaluate("node => node.open"))
        self.page.locator('[data-action="email"]').click()
        self.page.locator('[data-action="email-cancel"]').click()
        self.assertFalse(dialog.evaluate("node => node.open"))

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rv2-14a-browser-"))
        cls.server = _Server(cls.tmp)
        cls.server.migrate()
        cls.server.create_users()
        cls.server.seed_rows(40)
        cls.server.start()
        jar, opener = cls.server.login("browser-admin", "pw-strong-1")
        cls.admin_session = cls.server.session_cookie(jar)
        if not cls.admin_session:
            raise AssertionError("admin login did not establish a session cookie")
        cls.server.publish(jar, opener)
        cls.report_url = f"{cls.server.base}/report/{DEF_ID}/"

        cls.pw = sync_playwright()
        cls.pw = cls.pw.start()                     # start() returns the live Playwright instance
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.context = cls.browser.new_context(viewport={"width": 1440, "height": 900})
        cls.context.add_cookies([{
            "name": "sessionid", "value": cls.admin_session,
            "url": cls.server.base,
        }])
        cls.page = cls.context.new_page()
        EVIDENCE.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        for attr in ("page", "context", "browser"):
            obj = getattr(cls, attr, None)
            try:
                if attr == "browser":
                    obj.close()
                else:
                    obj.close() if obj is not None else None
            except Exception:
                pass
        try:
            cls.pw.stop()
        except Exception:
            pass
        if getattr(cls, "server", None) is not None:
            cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------------ #
    def _goto(self, url: str, *, width: int, height: int) -> None:
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.goto(url, wait_until="networkidle")
        self.page.wait_for_timeout(250)
        for toggle in self.page.locator(".widget-settings > summary").all():
            toggle.click()  # Verify the controls after opening their native disclosures.

    def _rects(self, selector: str) -> list[dict]:
        return self.page.evaluate(
            "() => Array.from(document.querySelectorAll(%r)).map((n) => { const r = n.getBoundingClientRect(); "
            "return {x: r.left, y: r.top, width: r.right - r.left, height: r.bottom - r.top}; })" % selector
        )

    def _assert_no_frame_overlap(self) -> None:
        rects = self._rects(".report-grid > .widget-frame")
        self.assertTrue(len(rects) >= 4, "expected four widget frames")
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                self.assertFalse(
                    _intersects(rects[i], rects[j]),
                    f"frames {i} and {j} overlap: {rects[i]} vs {rects[j]}",
                )

    def _assert_within_frame(self, frame_index: int) -> None:
        frames = self.page.locator(".report-grid > .widget-frame")
        frame = frames.nth(frame_index)
        fr = frame.bounding_box()
        form = frame.locator(".widget-controls").first
        if not form.count():
            return
        region = form.bounding_box()
        self.assertGreaterEqual(region["x"], fr["x"] - 1, "control row left outside frame")
        self.assertLessEqual(region["x"] + region["width"], fr["x"] + fr["width"] + 1,
                             "scrollable control row right outside frame")
        scroll = form.evaluate("n => ({width: n.scrollWidth, left: n.scrollLeft, overflow: getComputedStyle(n).overflowX})")
        self.assertEqual(scroll["overflow"], "auto")
        for sel in ("input", "select", "button"):
            for control in frame.locator(f".widget-controls {sel}").element_handles():
                if not control.is_visible():
                    continue
                box = control.bounding_box()
                self.assertGreaterEqual(box["x"], region["x"] - scroll["left"] - 1)
                self.assertLessEqual(box["x"] + box["width"], region["x"] - scroll["left"] + scroll["width"] + 1)

    def _assert_inner_regions_no_overlap(self) -> None:
        for index in range(self.page.locator(".report-grid > .widget-frame").count()):
            frame = self.page.locator(".report-grid > .widget-frame").nth(index)
            regions = {}
            for cls_name in ("widget-controls", "widget-summary", "widget-body"):
                node = frame.locator(f".{cls_name}").first
                if node.count() and node.is_visible():
                    regions[cls_name] = node.bounding_box()
            keys = list(regions)
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    self.assertFalse(
                        _intersects(regions[keys[i]], regions[keys[j]]),
                        f"frame {index}: {keys[i]} overlaps {keys[j]}",
                    )

    # -- 1. no page overflow / overlap at all widths + keyboard reachability ---------------- #
    def test_no_page_overflow_or_overlap_at_all_widths(self):
        for width, height in ((1440, 900), (1024, 768), (390, 844)):
            with self.subTest(width=width, height=height):
                self._goto(self.report_url, width=width, height=height)
                metrics = self.page.evaluate(
                    "() => { const d = document.documentElement; "
                    "return {sw: d.scrollWidth, cw: d.clientWidth, bsw: document.body.scrollWidth, bcw: document.body.clientWidth}; }"
                )
                self.assertLessEqual(metrics["sw"], metrics["cw"] + 1, "horizontal page overflow")
                self.assertLessEqual(metrics["bsw"], metrics["bcw"] + 1, "horizontal body overflow")
                self._assert_no_frame_overlap()
                self._assert_inner_regions_no_overlap()
                for index in range(self.page.locator(".report-grid > .widget-frame").count()):
                    self._assert_within_frame(index)
                self._assert_keyboard_reaches_an_apply()
                self._shot(f"page-{DEF_ID}-{width}.png", full_page=True)

    def test_day_first_dates_and_native_picker_update_one_card(self):
        self._goto(self.report_url, width=1440, height=900)
        frame = self.page.locator('.widget-frame').nth(1)
        with self.page.expect_response(lambda response: response.url == self.server.base + frame.get_attribute('data-data-url')) as loaded:
            frame.locator('[data-action="apply"]').click()
        anchor = loaded.value.json()['dates']['anchor_date']
        picker = frame.locator('[data-date-picker="end"]')
        picker.evaluate("n => { const native=n.showPicker.bind(n); n.showPicker=() => { n.dataset.opened='yes'; native(); }; }")
        picker.click()
        self.assertEqual(picker.get_attribute('data-opened'), 'yes')
        self.page.keyboard.press('Escape')
        picker.fill(anchor)
        self.assertEqual(frame.locator('[data-date="end"]').input_value(), '/'.join(reversed(anchor.split('-'))))
        frame.locator('[data-date="start"]').fill('01/01/2026')
        with self.page.expect_response(lambda response: '/widget/' in response.url and response.url.endswith('/data/')) as result:
            frame.locator('[data-action="apply"]').click()
        payload = result.value.json()
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['dates']['window_start'], '2026-01-01')
        self.assertEqual(payload['dates']['window_end'], anchor)
        self._shot('date-controls-picker-1440.png', full_page=True)

    def test_charts_fit_available_height(self):
        self._goto(self.report_url, width=1440, height=900)
        for frame in self.page.locator('.widget-frame:has(.widget-chart-box)').all():
            chart = frame.locator('.widget-chart-box').first
            self.page.wait_for_function("Array.from(document.querySelectorAll('.widget-chart-box')).every(n => n.clientHeight > 0)")
            body = frame.locator('.widget-body').bounding_box()
            box = chart.bounding_box()
            self.assertGreater(box['height'], 0)
            self.assertLessEqual(box['height'], body['height'] + 1)
            self.assertLessEqual(box['y'] + box['height'], body['y'] + body['height'] + 1)
            before = box['height']
            frame.locator('.widget-settings > summary').click()
            self.page.wait_for_timeout(100)
            self.assertGreater(chart.bounding_box()['height'], before)

    def test_confusion_matrix_scales_to_body_height(self):
        self._goto(self.report_url, width=1440, height=900)
        self.page.evaluate("""() => {
          const frame = document.createElement('article'); frame.className = 'card widget-frame';
          frame.dataset.type = 'confusion_matrix'; frame.style.cssText = 'grid-column:span 6;grid-row:span 4;--widget-rows:4';
          frame.innerHTML = '<h3>Synthetic matrix</h3><div class="widget-body"><div class="widget-live"><div class="widget-mount"></div></div></div>';
          document.querySelector('.report-grid').append(frame);
          window.__rv2widgets.registry.render('confusion_matrix', frame.querySelector('.widget-mount'), {classes:[0,1], cells:[[20,2],[3,25]], row_totals:[22,28], accuracy:{value:.9}});
        }""")
        frame = self.page.locator('.widget-frame[data-type=confusion_matrix]')
        for height in (4, 7):
            frame.evaluate('(node, rows) => { node.style.setProperty("--widget-rows", rows); node.style.gridRow = "span " + rows; }', height)
            self.page.wait_for_timeout(100)
            body = frame.locator('.widget-body').bounding_box()
            matrix = frame.locator('.widget-confusion').bounding_box()
            self.assertLessEqual(matrix['y'] + matrix['height'], body['y'] + body['height'] + 1)
            self.assertGreater(matrix['height'], 0)

    def test_metadata_fonts_match_and_group_dropdown_does_not_grow_card(self):
        self._goto(self.report_url, width=1024, height=768)
        frame = self.page.locator('.widget-frame:not([data-decoration])').first
        frame.locator('[data-comparison]').select_option('site')
        frame.locator('[data-action="apply"]').click()
        self.page.wait_for_timeout(300)
        before = frame.bounding_box()["height"]
        frame.locator('[data-group-trigger]').click()
        menu = frame.locator('[data-group-menu]')
        self.assertTrue(menu.is_visible())
        self.assertLessEqual(menu.bounding_box()["height"], 224)
        self.assertAlmostEqual(frame.bounding_box()["height"], before, delta=1)
        styles = frame.evaluate("n => ['.widget-summary', '.widget-reference'].map(s => { const c=getComputedStyle(n.querySelector(s)); return [c.fontFamily,c.fontSize,c.fontWeight,c.lineHeight]; })")
        self.assertEqual(styles[0], styles[1])
        self.page.keyboard.press('Escape')
        self.assertFalse(menu.is_visible())

    def _assert_keyboard_reaches_an_apply(self) -> None:
        self.page.evaluate("() => { document.body.focus(); }")
        for _ in range(60):
            self.page.keyboard.press("Tab")
            state = self.page.evaluate(
                "() => { const a = document.activeElement; if (!a) { return null; } "
                "return {action: a.getAttribute('data-action'), inFrame: !!(a.closest && a.closest('.widget-frame'))}; }"
            )
            if state and state.get("action") == "apply" and state.get("inFrame"):
                return
        self.fail("no Apply button reachable via Tab within 60 presses")

    # -- 2. a long table grows without clipping and re-renders cleanly ----------------------- #
    def test_table_grows_without_clipping(self):
        self.server.seed_rows(120)
        try:
            self._goto(self.report_url, width=1440, height=900)
            frame = self.page.locator('.widget-frame[data-widget-id="bt"]')
            body = frame.locator(".widget-body").first
            self.assertGreaterEqual(body.evaluate("(n) => n.scrollHeight"), 1)
            section_bottom = self.page.locator(".report-section").first.bounding_box()
            frame_box = frame.bounding_box()
            self.assertLessEqual(
                frame_box["y"] + frame_box["height"],
                section_bottom["y"] + section_bottom["height"] + 2,
                "the table frame must not grow past its section bottom",
            )
            with self.page.expect_response(
                lambda r: f"/widget/bt/data/" in r.url and r.status == 200, timeout=15000
            ):
                frame.locator('[data-action="apply"]').first.click()
            self.page.wait_for_timeout(300)
            self.assertTrue(frame.locator(".widget-live").first.count() >= 1, "live region missing")
            pager = frame.locator('.widget-pagination')
            self.assertTrue(pager.is_visible())
            previous = pager.locator('[data-action="previous-page"]')
            next_page = pager.locator('[data-action="next-page"]')
            self.assertTrue(previous.is_disabled())
            self.assertEqual(pager.locator('[data-page-info]').inner_text(), 'Rows 1–50')
            body.evaluate('(node) => { node.scrollTop = node.scrollHeight; }')
            pager_box = pager.bounding_box()
            self.assertLessEqual(pager_box['y'] + pager_box['height'], frame.bounding_box()['y'] + frame.bounding_box()['height'])
            for expected in ('Rows 51–100', 'Rows 101–120'):
                with self.page.expect_response(lambda r: '/widget/bt/data/' in r.url and r.status == 200):
                    next_page.click()
                self.page.wait_for_function("document.querySelector('[data-widget-id=bt] [data-page-info]').textContent === " + json.dumps(expected))
            self.assertTrue(next_page.is_disabled())
            self.assertTrue(previous.is_enabled())
            with self.page.expect_response(lambda r: '/widget/bt/data/' in r.url and r.status == 200):
                previous.click()
            self.page.wait_for_function("document.querySelector('[data-widget-id=bt] [data-page-info]').textContent === 'Rows 51–100'")
            with self.page.expect_response(lambda r: '/widget/bt/data/' in r.url and r.status == 200):
                frame.locator('[data-action="apply"]').first.click()
            self.page.wait_for_function("document.querySelector('[data-widget-id=bt] [data-page-info]').textContent === 'Rows 1–50'")
            self.assertEqual(frame.locator('[data-action="more"]').count(), 0)
            self.assertEqual(frame.locator('.widget-pageinfo').count(), 0)
            self._assert_no_frame_overlap()
            self._shot(f"page-longtable-1440.png", full_page=True)
        finally:
            self.server.seed_rows(40)

    # -- 3. the registry renderers paint line + bar ------------------------------------------ #
    def test_registry_renderers_paint(self):
        self._goto(self.report_url, width=1440, height=900)
        booted = self.page.evaluate("() => !!(window.__rv2widgets && window.__rv2widgets.bootReady)")
        self.assertTrue(booted, "boot.mjs did not flag window.__rv2widgets.bootReady")
        for widget_id in ("bl", "br"):
            with self.subTest(widget=widget_id):
                frame = self.page.locator(f'.widget-frame[data-widget-id="{widget_id}"]')
                with self.page.expect_response(
                    lambda r: f"/widget/{widget_id}/data/" in r.url and r.status == 200, timeout=15000
                ):
                    frame.locator('[data-action="apply"]').first.click()
                self.page.wait_for_timeout(500)
                canvases = frame.locator("canvas").count()
                self.assertGreaterEqual(canvases, 1, "echarts must paint at least one canvas")
                self.assertGreaterEqual(frame.locator(".widget-a11y").count(), 1, "a11y table missing")
                body_text = frame.locator(".widget-live").first.inner_text()
                for forbidden in ("<", "undefined", "NaN"):
                    self.assertNotIn(forbidden, body_text)

    # -- 4. the admin page links to the report-specific editor ------------------------------- #
    def test_editor_navigation_from_admin_page(self):
        self._goto(self.report_url, width=1440, height=900)
        link = self.page.locator('[data-role="edit-layout"]').first
        self.assertEqual(link.get_attribute("href"), f"/editor/{DEF_ID}/")
        editor_url = self.server.base + link.get_attribute("href")
        link.click()
        self.page.wait_for_load_state("networkidle")
        self.assertTrue(self.page.url.endswith("/editor/browser_layout_14a/"), self.page.url)
        self.assertEqual(self.page.locator("h1").inner_text(), "Editing: Browser Layout 14A")
        self.assertEqual(self.page.locator("[data-editor]").get_attribute("data-def-id"), DEF_ID)
        self.assertEqual(self.page.locator('[data-role="report-select"]').count(), 0)
        textarea = self.page.locator("#id_yaml_text").first
        self.assertIn("Browser Layout 14A", textarea.input_value())
        self.assertFalse((self.server.root / "drafts" / DEF_ID).exists(), "the editor GET must not write a draft")
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollHeight"), 901)
        preview = self.page.locator('[data-role="preview-dialog"]')
        self.assertFalse(preview.is_visible())
        self.page.locator('.editor-workspace [data-action="preview"]').click()
        preview.wait_for(state="visible")
        self.assertGreater(preview.bounding_box()["width"], 1200)
        self.page.locator('[data-action="close-preview"]').click()
        self.assertFalse(preview.is_visible())
        self._shot(f"editor-1440.png", full_page=True)

        # An ordinary user gets no edit-layout affordance at all.
        jar, opener = self.server.login("browser-normal", "pw-strong-1")
        self.assertTrue(self.server.session_cookie(jar), "normal login must establish a session")
        ordinary_code, ordinary_body, _ = self.server.get(f"/report/{DEF_ID}/", jar=jar, opener=opener)
        self.assertEqual(ordinary_code, 200)
        self.assertNotIn('data-role="edit-layout"', ordinary_body)
        self.assertNotIn("Edit layout", ordinary_body)

    # -- 5. the legacy report route is untouched --------------------------------------------- #
    def test_legacy_report_untouched(self):
        # Reuse the admin session (a fresh login keeps the legacy redirect chain independent of the
        # browser's injected session).
        jar, opener = self.server.login("browser-admin", "pw-strong-1")
        legacy_code, _body, _ = self.server.get("/report-old/", jar=jar, opener=opener)
        self.assertLess(legacy_code, 500, "legacy report must not 5xx")

    # -- screenshot helper ------------------------------------------------------------------- #
    def _shot(self, name: str, *, full_page: bool = True) -> None:
        path = EVIDENCE / name
        self.page.screenshot(path=str(path), full_page=full_page)
        print(f"SCREENSHOT {path.resolve()}")
