# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Task 12 admin YAML editor: security, publish-integrity and preview-isolation guarantees.

Every filesystem touch goes through an injected scratch ``REPORT_V2_ROOT`` (never the real
``private_data`` tree, never ``~/serverfiles/downloads/db_2026-06-18.sqlite3``), and the only database
used is Django's own in-memory *test* database created by the runner. Each named test maps to one
Done-when criterion of the task.
"""
from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import AnonymousUser, Group, User
from django.core import mail
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse

from report_v2 import admin_views as av
from report_v2 import views
from report_v2.definitions.repository import DefinitionRepository, DraftNotFoundError, StaleRevisionError, default_root

# Synthetic, non-clinical fixtures reused verbatim in spirit from Task 11: a minimal valid report and a
# YAML that the strict loader (anchors) rejects. Neither carries any clinical identifier.
VALID_YAML = """\
schema_version: 1
project: prime
id: testreport
title: T
grid:
  columns: 12
  row_height_px: 64
sections:
- id: s
  title: S
  widgets:
  - id: w
    title: W
    type: value
    layout:
      width: 2
      height: 3
    query:
      measurement: record_count
      inputs: {}
    window:
      start: '2025-12-12'
      end: D
    controls:
      date_range: true
      filters: [site]
      compare_by: []
    ci:
      enabled: false
    export: full
"""

INVALID_YAML = """\
schema_version: 1
project: prime
id: bad
title: [&anchor "x"]
sections: []
"""

_MUTATION_PATHS = {
    "save": "/editor/actions/save/",
    "preview": "/editor/actions/preview/",
    "publish": "/editor/actions/publish/",
    "unpublish": "/editor/actions/unpublish/",
    "new": "/editor/actions/new/",
}

_SETTINGS = dict(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    SECURE_SSL_REDIRECT=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)


@override_settings(**_SETTINGS)
class EditorAdminSecurityTests(TestCase):
    """One named test per Task-12 Done-when item; all state under a scratch root."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="editor-admin", email="editor-admin@example.invalid", password="pw-strong-1"
        )
        cls.normal = User.objects.create_user(
            username="editor-normal", email="editor-normal@example.invalid", password="pw-strong-1"
        )

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="t12-editor-"))
        overrider = override_settings(REPORT_V2_ROOT=str(self.root))
        overrider.enable()
        self.addCleanup(overrider.disable)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        mail.outbox.clear()
        self.addCleanup(mail.outbox.clear)

    # -- helpers -----------------------------------------------------------
    def _repo(self) -> DefinitionRepository:
        # Fresh instance every call so assertions read the on-disk truth, not a cache.
        return DefinitionRepository(default_root(), project_id="prime")

    def _client(self, *, enforce_csrf=False):
        return Client(SERVER_NAME="localhost", enforce_csrf_checks=enforce_csrf)

    def _as_admin(self, *, enforce_csrf=False):
        client = self._client(enforce_csrf=enforce_csrf)
        client.force_login(self.admin)
        return client

    # -- 1. the editor + every mutation endpoint refuse non-admins -------
    def test_non_admin_cannot_access_editor(self):
        """Anonymous + normal users get 403/redirect (never 200) for the page AND every direct POST."""
        # Anonymous through the full HTTP stack.
        anon = self._client(enforce_csrf=True)
        page = anon.get("/editor/")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login", page.url)
        for label, url in _MUTATION_PATHS.items():
            with self.subTest(user="anonymous", endpoint=label):
                response = anon.post(url, {"def_id": "forbidden", "yaml_text": VALID_YAML})
                self.assertNotEqual(response.status_code, 200)
                self.assertIn(response.status_code, (302, 403))

        # Authenticated non-admin through the full HTTP stack.
        normal = self._as_user_client(self.normal, enforce_csrf=True)
        page = normal.get("/editor/")
        self.assertEqual(page.status_code, 403)
        for label, url in _MUTATION_PATHS.items():
            with self.subTest(user="normal", endpoint=label):
                response = normal.post(url, {"def_id": "forbidden", "yaml_text": VALID_YAML})
                self.assertEqual(response.status_code, 403)

        # A hand-crafted direct request (bypassing the client) is rejected by the gate itself,
        # BEFORE any model/filesystem work -- proven by the scratch root staying empty for that def_id.
        rf = RequestFactory(SERVER_NAME="localhost")
        for user in (AnonymousUser(), self.normal):
            for label, view in (
                ("save", av.editor_save_draft),
                ("preview", av.editor_preview),
                ("publish", av.editor_publish),
                ("new", av.editor_new),
            ):
                with self.subTest(user=type(user).__name__, endpoint=label):
                    request = rf.post(_MUTATION_PATHS[label], {"def_id": "forbidden", "yaml_text": VALID_YAML})
                    request.user = user
                    response = view(request)
                    self.assertNotEqual(response.status_code, 200)
                    self.assertIn(response.status_code, (302, 403))
        # No draft/blob/pointer was ever produced for the rejected def_id.
        self.assertFalse(any(self.root.rglob("*forbidden*")))

    def _as_user_client(self, user, *, enforce_csrf=False):
        client = self._client(enforce_csrf=enforce_csrf)
        client.force_login(user)
        return client

    # -- 2. CSRF is enforced on the mutators -----------------------------
    def test_missing_csrf_rejected(self):
        """A logged-in admin POST without a CSRF token is rejected (4xx) by the CSRF layer."""
        for label in ("save", "preview", "publish"):
            client = self._as_admin(enforce_csrf=True)
            with self.subTest(endpoint=label):
                response = client.post(_MUTATION_PATHS[label], {"def_id": "csrf", "yaml_text": VALID_YAML})
                self.assertGreaterEqual(response.status_code, 400)
                self.assertLess(response.status_code, 500)

    def test_bad_csrf_token_rejected(self):
        """Even a supplied-but-wrong token cannot bypass the CSRF protection."""
        client = self._as_admin(enforce_csrf=True)
        client.get("/editor/")  # establish a csrftoken cookie
        token = client.cookies.get("csrftoken")
        # Sanity: the editor page handed the admin a CSRF cookie at all.
        self.assertIsNotNone(token)
        response = client.post(
            _MUTATION_PATHS["save"],
            {"def_id": "csrf", "yaml_text": VALID_YAML, "csrfmiddlewaretoken": "x" * 64},
        )
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)

    # -- 3. invalid YAML can never replace published content -------------
    def test_invalid_yaml_does_not_replace_published(self):
        """Publish valid content, then a rejected publish leaves the pointer byte-for-byte unchanged."""
        repo = self._repo()
        good = self._as_admin()
        first = good.post(_MUTATION_PATHS["publish"], {"def_id": "guarded", "yaml_text": VALID_YAML})
        self.assertEqual(first.status_code, 200)
        pointer_before = self._repo()._read_pointer("guarded")
        self.assertIsNotNone(pointer_before)

        bad = self._as_admin()
        rejected = bad.post(_MUTATION_PATHS["publish"], {"def_id": "guarded", "yaml_text": INVALID_YAML})
        self.assertEqual(rejected.status_code, 422)
        self.assertIn("errors", rejected.json())
        self.assertTrue(rejected.json()["errors"])

        # Read the pointer back from a fresh repository instance -- unchanged.
        self.assertEqual(self._repo()._read_pointer("guarded"), pointer_before)

    # -- 4. preview neither publishes nor mails --------------------------
    def test_preview_does_not_publish_or_send_mail(self):
        """Preview of VALID and INVALID YAML: no pointer, no draft, and mail.outbox stays empty."""
        self.assertIsNone(self._repo()._read_pointer("previewer"))
        for label, yaml_text in (("valid", VALID_YAML), ("invalid", INVALID_YAML)):
            client = self._as_admin()
            with self.subTest(case=label):
                response = client.post(
                    _MUTATION_PATHS["preview"], {"def_id": "previewer", "yaml_text": yaml_text}
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertIn("errors", payload)
                if label == "invalid":
                    self.assertFalse(payload["valid"])
                    self.assertTrue(payload["errors"])
                # Nothing was published and nothing was mailed.
                self.assertIsNone(self._repo()._read_pointer("previewer"))
                with self.assertRaises(DraftNotFoundError):
                    self._repo().read_draft("previewer")
                self.assertEqual(len(mail.outbox), 0)

    # -- 5. draft round-trip + optimistic-conflict without clobber -------
    def test_save_draft_and_stale_conflict(self):
        """A stale expected_revision yields 409 and the stored draft is not clobbered."""
        client = self._as_admin()
        saved = client.post(
            _MUTATION_PATHS["save"], {"def_id": "concurrent", "yaml_text": VALID_YAML}
        )
        self.assertEqual(saved.status_code, 200)
        rev1 = saved.json()["revision"]
        self.assertEqual(self._repo().read_draft("concurrent")[0], saved.json()["yaml_text"])

        # A concurrent writer advances the draft behind the client's back.
        advanced = VALID_YAML.replace("title: T", "title: T-advanced")
        self._repo().save_draft("concurrent", advanced, expected_revision=None)

        # The original client now saves with its now-STALE revision: conflict, no clobber.
        stale = self._as_admin()
        conflict = stale.post(
            _MUTATION_PATHS["save"],
            {"def_id": "concurrent", "yaml_text": "attempted-clobber: yes", "expected_revision": rev1},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["status"], "conflict")
        # The on-disk draft is the concurrent writer's text, not the attempted clobber.
        self.assertEqual(self._repo().read_draft("concurrent")[0], advanced)

    # -- 6. admins can create independent reports ------------------------
    def test_admin_can_create_second_report(self):
        """editor_new yields independent reports whose publication pointers never collide."""
        one = self._as_admin()
        created_a = one.post(_MUTATION_PATHS["new"], {"def_id": "report-a"})
        self.assertEqual(created_a.status_code, 200)
        two = self._as_admin()
        created_b = two.post(_MUTATION_PATHS["new"], {"def_id": "report-b"})
        self.assertEqual(created_b.status_code, 200)

        # Neither started published.
        self.assertIsNone(self._repo()._read_pointer("report-a"))
        self.assertIsNone(self._repo()._read_pointer("report-b"))

        # Publish A only; B stays independent.
        pub = self._as_admin()
        published = pub.post(_MUTATION_PATHS["publish"], {"def_id": "report-a", "yaml_text": VALID_YAML})
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["version"], "report-a@r1")
        self.assertEqual(self._repo()._read_pointer("report-a"), "report-a@r1")
        self.assertIsNone(self._repo()._read_pointer("report-b"))

        # Republishing A does not touch B's (still-absent) pointer.
        again = self._as_admin()
        republished = again.post(_MUTATION_PATHS["publish"], {"def_id": "report-a", "yaml_text": VALID_YAML})
        self.assertEqual(republished.json()["version"], "report-a@r2")
        self.assertEqual(self._repo()._read_pointer("report-a"), "report-a@r2")
        self.assertIsNone(self._repo()._read_pointer("report-b"))

    def test_legacy_layout_bookmarks_redirect_to_editor(self):
        client = self._as_admin()
        self.assertRedirects(client.get("/layout/"), "/editor/")
        self._repo().save_draft("testreport", VALID_YAML, expected_revision=None)
        self.assertRedirects(client.get("/layout/editor/testreport/"), "/editor/t/")
        self.assertEqual(reverse("report_editor:editor_detail", args=["t"]), "/editor/t/")
        self.assertEqual(client.get("/editor/missing/").status_code, 404)

    # -- 7. the reserved route wins before any slug ----------------------
    def test_editor_route_before_slug(self):
        """'/editor/' resolves to the editor with no slug kwargs; legacy routes are intact."""
        match = resolve("/editor/")
        self.assertIs(match.func, av.editor)
        self.assertEqual(match.kwargs, {})  # not captured as a slug/def_id pattern

        # The report index still routes to the legacy v2 index view, and every named editor route
        # reverses inside the reserved /editor/ block (distinct names, none shadowed).
        self.assertIs(resolve("/report/").func, views.index)
        self.assertEqual(resolve("/editor/").url_name, "editor")
        for name in ("editor_save_draft", "editor_preview", "editor_publish", "editor_new"):
            with self.subTest(name=name):
                self.assertTrue(reverse(f"report_editor:{name}").startswith("/editor/"))

        # A stray slug path is NOT swallowed by the reserved block (still 404, i.e. no catch-all
        # was added and the legacy report lives on its own separate /report-old/ mount).
        with self.assertRaises(Resolver404):
            resolve("/report/invalid.slug/")

        # The admin page itself renders 200 through the full stack (proves the base-template
        # inheritance and the static assets actually resolve).
        page = self._as_admin().get("/editor/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Report editor", page.content.decode())

    def test_visual_editor_updates_yaml_without_saving(self):
        client = self._as_admin()
        response = client.post("/editor/actions/visual/", {
            "yaml_text": VALID_YAML,
            "operation": '{"action":"resize","section_id":"s","widget_id":"w","layout":{"width":6,"height":4}}',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("width: 6", response.json()["yaml_text"])
        self.assertFalse((self.root / "prime" / "drafts").exists())
        client = self._as_admin(enforce_csrf=True)
        self.assertEqual(client.post("/editor/actions/visual/", {"yaml_text": VALID_YAML}).status_code, 403)

    # -- 14A.4 selector lists published + drafts with a state label ------------------- #
    def test_editor_selector_lists_published_and_drafts_with_state(self):
        admin = self._as_admin()
        published = admin.post(_MUTATION_PATHS["publish"], {"def_id": "listed", "yaml_text": VALID_YAML})
        self.assertEqual(published.status_code, 200)
        created = self._as_admin().post(_MUTATION_PATHS["new"], {"def_id": "drafted"})
        self.assertEqual(created.status_code, 200)

        body = self._as_admin().get("/editor/").content.decode("utf-8")
        self.assertIn('href="/editor/t/"', body)
        self.assertIn('href="/editor/new_report/"', body)
        self.assertIn('data-state="published"', body)
        self.assertIn('data-state="draft only"', body)
        self.assertIn("listed — published", body)
        self.assertIn("drafted — draft only", body)

        # Saving a draft for the published def flips its label to the combined state.
        self._as_admin().post(_MUTATION_PATHS["save"], {"def_id": "listed", "yaml_text": VALID_YAML})
        again = self._as_admin().get("/editor/").content.decode("utf-8")
        self.assertIn('data-state="published + draft"', again)

    # -- 14A.5 opening a published-only report is read-only in memory ------------------ #
    def test_opening_published_only_report_is_read_only_in_memory(self):
        published = self._as_admin().post(
            _MUTATION_PATHS["publish"], {"def_id": "listed", "yaml_text": VALID_YAML}
        )
        self.assertEqual(published.status_code, 200)
        response = self._as_admin().get("/editor/listed/", follow=True)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("title: T", body)
        self.assertNotIn('data-role="report-select"', body)
        self.assertIn('data-source-state="published-only"', body)
        self.assertIn("Revision:", body)
        self.assertIn("(Published)", body)
        self.assertIn('data-role="revision-indicator"', body)
        self.assertFalse((self.root / "drafts" / "listed").exists())

    # -- 14A.6 saving a draft from the published view creates the private draft -------- #
    def test_save_draft_from_published_view_creates_private_draft(self):
        published = self._as_admin().post(
            _MUTATION_PATHS["publish"], {"def_id": "listed", "yaml_text": VALID_YAML}
        )
        self.assertEqual(published.status_code, 200)
        published_text = self._repo()._blob_path("listed", published.json()["version"]).read_text(encoding="utf-8")
        self.assertFalse((self.root / "drafts" / "listed").exists())

        saved = self._as_admin().post(
            _MUTATION_PATHS["save"], {"def_id": "listed", "yaml_text": published_text}
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["revision"])
        self.assertTrue((self.root / "drafts" / "listed").exists())
        self.assertEqual(self._repo().read_draft("listed")[0], saved.json()["yaml_text"])

        again = self._as_admin().get("/editor/listed/", follow=True).content.decode("utf-8")
        self.assertIn('data-source-state="draft"', again)

    # -- 14A.7 previewing from the published view still writes nothing ------------------- #
    def test_preview_from_published_view_still_writes_nothing(self):
        published = self._as_admin().post(
            _MUTATION_PATHS["publish"], {"def_id": "listed", "yaml_text": VALID_YAML}
        )
        self.assertEqual(published.status_code, 200)
        pointer_before = self._repo()._read_pointer("listed")
        published_text = self._repo()._blob_path("listed", published.json()["version"]).read_text(encoding="utf-8")

        preview = self._as_admin().post(
            _MUTATION_PATHS["preview"], {"def_id": "listed", "yaml_text": published_text}
        )
        self.assertEqual(preview.status_code, 200)
        self.assertFalse((self.root / "drafts" / "listed").exists())
        self.assertEqual(self._repo()._read_pointer("listed"), pointer_before)
        self.assertEqual(pointer_before, published.json()["version"])

    def test_editor_navigation_is_admin_only(self):
        from django.template.loader import render_to_string

        group_admin = User.objects.create_user(username="editor-group-admin")
        group_admin.groups.add(Group.objects.get_or_create(name="admins")[0])
        for user, visible in ((self.admin, True), (group_admin, True), (self.normal, False), (AnonymousUser(), False)):
            with self.subTest(user=str(user)):
                request = RequestFactory().get(reverse("report_editor:editor"))
                request.user = user
                request.resolver_match = resolve(request.path)
                body = render_to_string("base.html", request=request)
                self.assertEqual('href="/editor/"' in body, visible)
                if visible:
                    self.assertIn('aria-current="page"', body)
        client = Client()
        client.force_login(group_admin)
        self.assertEqual(client.get(reverse("report_editor:editor")).status_code, 200)

    def test_preview_evaluates_submitted_draft_with_data_adapter(self):
        from unittest.mock import patch

        draft = VALID_YAML.replace("id: w", "id: draft_widget")
        with patch("report_v2.data.fetch_project_rows", return_value=[]) as fetch:
            response = self._as_admin().post(_MUTATION_PATHS["preview"], {
                "def_id": "drafttest", "yaml_text": draft,
            })
        payload = response.json()
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["widget_id"], "draft_widget")
        self.assertEqual(payload["widget"]["query"]["measurement"], "record_count")
        fetch.assert_called_once_with("prime", layout_widget=payload["widget"])
        self.assertIsNotNone(payload["preview"])
        self.assertIsNone(self._repo()._read_pointer("drafttest"))

    def test_editor_copy_and_site_styles(self):
        self._as_admin().post(_MUTATION_PATHS["new"], {"def_id": "copytest"})
        body = self._as_admin().get("/editor/copytest/", follow=True).content.decode()
        self.assertNotIn("Admin-only. Drafts are saved privately", body)
        self.assertNotIn("Preview renders from synthetic data only", body)
        self.assertNotIn("editor-seeds", body)
        self.assertIn("editor-columns", body)

    def test_preview_data_failure_does_not_expose_exception(self):
        from unittest.mock import patch

        with patch("report_v2.data.fetch_project_rows", side_effect=RuntimeError("private-data-sentinel")):
            response = self._as_admin().post(_MUTATION_PATHS["preview"], {
                "def_id": "failed", "yaml_text": VALID_YAML,
            })
        self.assertNotIn("private-data-sentinel", response.content.decode())
        self.assertIn("preview_error", response.json())

    def test_named_template_creation_and_duplicate_protection(self):
        created = self._as_admin().post(_MUTATION_PATHS["new"], {"name": "My Report", "template": "prime"})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["url"], "/editor/my_report/")
        from report_v2.definitions.loader import load_report_definition
        layout = load_report_definition(self._repo().read_draft("my_report")[0])
        self.assertEqual(layout["title"], "My Report")
        self.assertTrue(layout["sections"])
        original = self._repo().read_draft("my_report")
        duplicate = self._as_admin().post(_MUTATION_PATHS["new"], {"name": "My Report"})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self._repo().read_draft("my_report"), original)
        self.assertEqual(self._as_admin().post(_MUTATION_PATHS["new"], {"name": "Bad", "template": "unknown"}).status_code, 400)

    def test_select_card_and_list_cards_without_fetching_data(self):
        draft = VALID_YAML.replace("  - id: w", "  - id: first")
        draft += draft[draft.index("  - id: first"):].replace("id: first", "id: second").replace("title: W", "title: Second")
        from unittest.mock import patch
        with patch("report_v2.data.fetch_project_rows", return_value=[]) as fetch:
            cards = self._as_admin().post(_MUTATION_PATHS["preview"], {"def_id": "cards", "yaml_text": draft, "list_only": "1"})
            self.assertEqual([card["id"] for card in cards.json()["widgets"]], ["heading_s", "first", "second"])
            fetch.assert_not_called()
            result = self._as_admin().post(_MUTATION_PATHS["preview"], {"def_id": "cards", "yaml_text": draft, "widget_id": "second"})
            self.assertEqual(result.json()["widget_id"], "second")
            fetch.assert_called_once()
        with patch("report_v2.data.fetch_project_rows") as fetch:
            result = self._as_admin().post(_MUTATION_PATHS["preview"], {"def_id": "cards", "yaml_text": draft, "widget_id": "foreign"})
            self.assertEqual(result.status_code, 400)
            fetch.assert_not_called()

    def test_delete_admin_csrf_conflict_and_retained_history(self):
        delete_url = reverse("report_editor:editor_delete")
        repo = self._repo()
        revision = repo.save_draft("remove_me", VALID_YAML, expected_revision=None)
        receipt = repo.publish("remove_me", VALID_YAML)
        body = {"def_id": "remove_me", "expected_revision": revision, "expected_version": receipt.version, "confirmation": "delete this report"}
        normal = Client()
        normal.force_login(self.normal)
        self.assertEqual(normal.post(delete_url, body).status_code, 403)
        self.assertEqual(self._as_admin(enforce_csrf=True).post(delete_url, body).status_code, 403)
        self.assertEqual(self._as_admin().get(delete_url).status_code, 405)
        self.assertEqual(self._as_admin().post(delete_url, {**body, "expected_revision": "stale"}).status_code, 409)
        self.assertEqual(repo.get_current_version("remove_me"), receipt.version)
        for confirmation in ("", "delete", "Delete this report", "delete this report "):
            self.assertEqual(self._as_admin().post(delete_url, {**body, "confirmation": confirmation}).status_code, 400)
            self.assertEqual(repo.get_current_version("remove_me"), receipt.version)
        self.assertEqual(self._as_admin().post(delete_url, body).status_code, 200)
        self.assertIsNone(repo.get_current_version("remove_me"))
        self.assertTrue(receipt.blob_path.exists())
        self.assertNotIn("remove_me", self._as_admin().get(reverse("report_editor:editor")).content.decode())
        self.assertEqual(self._as_admin().get(reverse("report_editor:editor_detail", args=["remove_me"])).status_code, 404)
        self.assertEqual(self._as_admin().post(_MUTATION_PATHS["save"], {"def_id": "remove_me", "yaml_text": VALID_YAML}).status_code, 400)
        self.assertEqual(self._as_admin().post(_MUTATION_PATHS["publish"], {"def_id": "remove_me", "yaml_text": VALID_YAML}).status_code, 400)
        self.assertEqual(self._as_admin().post(delete_url, {"def_id": "../escape"}).status_code, 400)
        self.assertEqual(self._as_admin().post(_MUTATION_PATHS["new"], {"def_id": "remove_me"}).status_code, 400)

    def test_catalog_hides_policy_drafts(self):
        from report_v2.seed import policy_seed_text
        self._repo().save_draft("private-policy", policy_seed_text(), expected_revision=None)
        page = self._as_admin().get(reverse("report_editor:editor"))
        self.assertNotIn("private-policy", page.content.decode())
        self.assertIn('data-role="starter-template"', page.content.decode())

    def test_basic_template_has_valid_working_boilerplate(self):
        from report_v2.definitions.loader import load_report_definition
        response = self._as_admin().post(_MUTATION_PATHS["new"], {"name": "Basic Example", "template": "blank"})
        self.assertEqual(response.status_code, 200)
        text, _ = self._repo().read_draft("basic_example")
        self.assertEqual(self._repo().validate_preview("basic_example", text), [])
        layout = load_report_definition(text)
        self.assertEqual(next(w for w in layout["sections"][0]["widgets"] if w["type"] == "value")["query"]["measurement"], "record_count")

    def test_catalog_create_follows_reports(self):
        self._as_admin().post(_MUTATION_PATHS["new"], {"name": "Placement check"})
        body = self._as_admin().get(reverse("report_editor:editor")).content.decode()
        self.assertGreater(body.index('data-action="create"'), body.index('href="/editor/placement_check/"'))

    def test_name_based_editor_heading_redirect_and_identity(self):
        repo = self._repo()
        text = VALID_YAML.replace("title: T", "title: Analysis Report")
        repo.save_draft("overview", text, expected_revision=None)
        client = self._as_admin()
        response = client.get("/editor/overview/")
        self.assertRedirects(response, "/editor/analysis_report/")
        body = client.get("/editor/analysis_report/").content.decode()
        self.assertIn('class="page-title">Editing: Analysis Report</h1>', body)
        self.assertIn('data-def-id="overview"', body)
        self.assertNotIn('class="page-title">Report layout', body)
        self.assertIn('href="/editor/analysis_report/"', client.get("/editor/").content.decode())
        normal = Client()
        normal.force_login(self.normal)
        self.assertEqual(normal.get("/editor/analysis_report/").status_code, 403)

    def test_duplicate_report_names_have_distinct_safe_editor_urls(self):
        from report_v2.admin_views import _report_entries
        repo = self._repo()
        for identifier in ("first", "second", "t__first"):
            repo.save_draft(identifier, VALID_YAML, expected_revision=None)
        entries = _report_entries(repo)
        self.assertEqual(len({entry["editor_url"] for entry in entries}), 3)
        for entry in entries:
            response = self._as_admin().get(entry["editor_url"])
            self.assertEqual(response.status_code, 200)
            self.assertIn('data-def-id="'+entry["def_id"]+'"', response.content.decode())

    def test_empty_height_and_ci_enabled_are_reported_after_save_and_preview(self):
        invalid = VALID_YAML.replace("height: 3", "height:").replace("enabled: false", "enabled:")
        client = self._as_admin()
        for action in ("save", "preview"):
            response = client.post(_MUTATION_PATHS[action], {"def_id": "invalid_values", "yaml_text": invalid})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["valid"])
            errors = " ".join(response.json()["errors"])
            self.assertIn("layout.height", errors)
            self.assertIn("ci.enabled", errors)
        self.assertEqual(self._repo().read_draft("invalid_values")[0], invalid)
        response = client.post(_MUTATION_PATHS["publish"], {"def_id": "invalid_values", "yaml_text": invalid})
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(self._repo().get_current_version("invalid_values"))

    def test_full_card_preview_controls_are_temporary_and_validated(self):
        import json
        from unittest.mock import patch

        draft = VALID_YAML
        with patch("report_v2.data.fetch_project_rows", return_value=[]):
            response = self._as_admin().post(_MUTATION_PATHS["preview"], {
                "def_id": "testreport", "yaml_text": draft,
                "overrides": json.dumps({"date": {"start": "2025-12-20", "end": "2025-12-21"}}),
            })
        card = response.json()["card_html"]
        self.assertIn('class="card widget-frame"', card)
        self.assertIn('class="widget-settings"', card)
        self.assertIn('name="time_grouping"', card)
        self.assertIn('data-action="apply"', card)
        self.assertIn('data-action="reset"', card)
        self.assertIn('data-initial-payload', card)
        self.assertIsNone(self._repo()._read_pointer("testreport"))
        with self.assertRaises(DraftNotFoundError):
            self._repo().read_draft("testreport")
        with patch("report_v2.data.fetch_project_rows") as fetch:
            rejected = self._as_admin().post(_MUTATION_PATHS["preview"], {
                "def_id": "testreport", "yaml_text": draft,
                "overrides": json.dumps({"comparison": "unknown_dimension"}),
            })
        self.assertEqual(rejected.status_code, 400)
        fetch.assert_not_called()

    def test_publication_controls_and_republish_preserve_history(self):
        client = self._as_admin()
        repo = self._repo()
        revision = repo.save_draft("cycle", VALID_YAML, expected_revision=None)
        page = client.get("/editor/t/").content.decode()
        self.assertIn('data-role="view-report" disabled', page)
        result = client.post(_MUTATION_PATHS["publish"], {"def_id": "cycle", "expected_revision": revision})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["url"], "/report/cycle/")
        page = client.get("/editor/t/").content.decode()
        self.assertIn('>Unpublish</button>', page)
        self.assertIn('href="/report/cycle/">View report', page)
        stale = client.post(_MUTATION_PATHS["unpublish"], {"def_id": "cycle", "expected_version": "cycle@r0"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(repo.get_current_version("cycle"), "cycle@r1")
        result = client.post(_MUTATION_PATHS["unpublish"], {"def_id": "cycle", "expected_version": "cycle@r1"})
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(repo.get_current_version("cycle"))
        self.assertEqual(repo.read_draft("cycle")[0], VALID_YAML)
        self.assertEqual(client.get("/report/cycle/").status_code, 404)
        result = client.post(_MUTATION_PATHS["publish"], {"def_id": "cycle", "yaml_text": VALID_YAML.replace("height: 3", "height: 5")})
        self.assertEqual(result.json()["version"], "cycle@r2")
        self.assertEqual(repo._blob_path("cycle", "cycle@r1").read_text(), VALID_YAML)
        repo._draft_path("cycle").unlink()
        repo.unpublish("cycle", expected_version="cycle@r2")
        self.assertIn("height: 5", repo.read_draft("cycle")[0])

    def test_save_updates_live_report_only_when_valid(self):
        client = self._as_admin()
        repo = self._repo()
        first = client.post(_MUTATION_PATHS["publish"], {"def_id": "save_live", "yaml_text": VALID_YAML})
        saved = client.post(_MUTATION_PATHS["save"], {"def_id": "save_live", "yaml_text": VALID_YAML.replace("height: 3", "height: 5")})
        self.assertTrue(saved.json()["published"])
        self.assertEqual(repo.get_current_version("save_live"), "save_live@r2")
        self.assertIn("height: 5", repo._blob_path("save_live", "save_live@r2").read_text())
        invalid = client.post(_MUTATION_PATHS["save"], {"def_id": "save_live", "yaml_text": INVALID_YAML, "expected_revision": saved.json()["revision"]})
        self.assertFalse(invalid.json()["published"])
        self.assertEqual(repo.get_current_version("save_live"), "save_live@r2")
        self.assertEqual(repo.read_draft("save_live")[0], INVALID_YAML)
        repo.unpublish("save_live", expected_version="save_live@r2")
        with self.assertRaises(StaleRevisionError):
            repo.publish("save_live", VALID_YAML, expected_version="save_live@r2")
        self.assertIsNone(repo.get_current_version("save_live"))
