from django.contrib.auth.models import AnonymousUser, User
from django.core.checks import run_checks
from django.template import engines
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from django.urls import resolve

from . import settings


class TransportSecurityCheckTests(SimpleTestCase):
    def test_http_llm_transport_warns_in_production_without_allowance(self):
        with override_settings(
            DEBUG=False,
            LLM_BASE_URL="http://llm.internal:11434/v1",
            LLM_ALLOW_INSECURE_TRANSPORT=False,
        ):
            warning_ids = {message.id for message in run_checks()}

        self.assertIn("lunit_audit.W002", warning_ids)

    def test_https_llm_transport_does_not_warn(self):
        with override_settings(
            DEBUG=False,
            LLM_BASE_URL="https://llm.internal/v1",
            LLM_ALLOW_INSECURE_TRANSPORT=False,
        ):
            warning_ids = {message.id for message in run_checks()}

        self.assertNotIn("lunit_audit.W001", warning_ids)
        self.assertNotIn("lunit_audit.W002", warning_ids)


class DatabaseSettingsTests(SimpleTestCase):
    def test_database_config_defaults_to_sqlite(self):
        config = settings.database_config()

        self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")
        self.assertTrue(str(config["NAME"]).endswith("db.sqlite3"))

    @override_settings()
    def test_database_config_supports_postgres_env(self):
        env = {
            "DATABASE_ENGINE": "postgres",
            "DATABASE_NAME": "primer_app",
            "DATABASE_USER": "primer_user",
            "DATABASE_PASSWORD": "placeholder",
            "DATABASE_HOST": "postgres",
            "DATABASE_PORT": "5432",
            "DATABASE_SSLMODE": "require",
            "DATABASE_CONN_MAX_AGE": "60",
            "DATABASE_CONN_HEALTH_CHECKS": "True",
        }
        with override_environ(env):
            config = settings.database_config()

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "primer_app")
        self.assertEqual(config["USER"], "primer_user")
        self.assertEqual(config["PASSWORD"], "placeholder")
        self.assertEqual(config["HOST"], "postgres")
        self.assertEqual(config["PORT"], "5432")
        self.assertEqual(config["OPTIONS"], {"sslmode": "require"})
        self.assertEqual(config["CONN_MAX_AGE"], 60)
        self.assertTrue(config["CONN_HEALTH_CHECKS"])

    def test_audit_database_config_supports_separate_postgres_database(self):
        env = {
            "AUDIT_DATABASE_ENGINE": "postgres",
            "AUDIT_DATABASE_NAME": "primer_audit",
            "AUDIT_DATABASE_USER": "audit_user",
            "AUDIT_DATABASE_PASSWORD": "placeholder",
            "AUDIT_DATABASE_HOST": "postgres",
            "AUDIT_DATABASE_PORT": "5432",
        }
        with override_environ(env):
            config = settings.audit_database_config()

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "primer_audit")
        self.assertEqual(config["USER"], "audit_user")
        self.assertEqual(config["HOST"], "postgres")


@override_settings(
    STORAGES={
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        }
    }
)
class VisualShellTemplateTests(TestCase):
    def _render_shell(self, user, is_admin=False, path="/report/"):
        template = engines["django"].from_string(
            "{% extends 'base.html' %}{% block nav_report %}active{% endblock %}"
            "{% block body %}<p>Shell probe</p>{% endblock %}"
        )
        request = RequestFactory().get(path)
        request.user = user
        request.resolver_match = resolve(path)
        return template.render(
            {"user": user, "is_admin": is_admin}, request=request
        )

    def test_authenticated_shell_has_sidebar_landmarks_and_active_page(self):
        html = self._render_shell(User.objects.create_user(username="reviewer"))

        self.assertIn('id="primaryNavigation"', html)
        self.assertIn('id="mainContent"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("Shell probe", html)
        self.assertIn('id="themeToggle"', html)
        self.assertIn('role="switch"', html)
        self.assertIn("Sign out", html)
        self.assertNotIn("Logout ↗", html)

    def test_admin_shell_retains_admin_only_destinations(self):
        html = self._render_shell(
            User.objects.create_user(username="admin", is_superuser=True), is_admin=True
        )

        for label in ("Tasks", "Import", "Database", "Report editor"):
            self.assertIn(label, html)

    def test_admin_categories_group_links_and_open_active_category(self):
        user = User.objects.create_user(username="category-admin", is_superuser=True)
        for path, active_title in (("/upload/tasks/", "Admin"), ("/upload/import/", "Admin"),
                                   ("/editor/", "More tools"), ("/view/", "More tools"), ("/gt/", "More tools")):
            with self.subTest(path=path):
                html = self._render_shell(user, path=path)
                categories = html.split('<details class="nav-category"')[1:]
                self.assertEqual(len(categories), 2)
                for category, title, labels in zip(categories, ("More tools", "Admin"),
                                                   (("Report editor", "Database", "Manual GT"), ("Tasks", "Import"))):
                    opening, content = category.split(">", 1)
                    content = content.split("</details>", 1)[0]
                    self.assertEqual("open" in opening, title == active_title)
                    self.assertIn(f'<summary class="nav-category-title">{title}</summary>', content)
                    for label in labels:
                        self.assertIn(f"<span>{label}</span>", content)

    def test_regular_user_has_no_admin_categories(self):
        html = self._render_shell(User.objects.create_user(username="category-reviewer"))
        self.assertEqual(html.count('<details class="nav-category"'), 1)
        self.assertIn('<summary class="nav-category-title">More tools</summary>', html)
        self.assertIn("<span>Manual GT</span>", html)
        self.assertNotIn('<summary class="nav-category-title">Admin</summary>', html)
        for label in ("Tasks", "Import", "Report editor", "Database"):
            self.assertNotIn(f"<span>{label}</span>", html)

    def test_unauthenticated_shell_omits_application_navigation(self):
        html = self._render_shell(AnonymousUser())

        self.assertNotIn('id="primaryNavigation"', html)
        self.assertIn('id="mainContent"', html)

@override_settings(
    SECURE_SSL_REDIRECT=False,
    MIDDLEWARE=[
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
    ],
    STORAGES={
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        }
    },
)
class VisualPageRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer = User.objects.create_user(
            username="visual-reviewer", password="test-password"
        )
        cls.admin = User.objects.create_superuser(
            username="visual-admin",
            email="visual-admin@example.invalid",
            password="test-password",
        )

    def assert_page_contains(self, url, markers, user=None):
        if user is not None:
            self.client.force_login(user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        for marker in markers:
            self.assertContains(response, marker, html=False)
        self.client.logout()

    def test_login_page_renders_current_form_copy(self):
        self.assert_page_contains(
            "/login/",
            ("PRIMER-LLM", "Sign in to continue", 'id="id_username"'),
        )

    def test_upload_page_keeps_interaction_targets(self):
        self.assert_page_contains(
            "/upload/",
            ("Upload &amp; Analyze", 'id="uploadForm"', 'id="statusContainer"'),
            self.reviewer,
        )

    def test_tasks_page_keeps_interaction_targets(self):
        self.assert_page_contains(
            "/upload/tasks/",
            ("Processing Tasks", 'id="backfillForm"', 'id="deleteModal"'),
            self.admin,
        )

    def test_import_page_keeps_interaction_targets(self):
        self.assert_page_contains(
            "/upload/import/",
            ("Import Historical Data", 'id="uploadZone"', 'id="previewCard"'),
            self.admin,
        )

    def test_viewer_page_keeps_interaction_targets(self):
        self.assert_page_contains(
            "/view/",
            ('id="filterForm"', 'class="table-wrap"', 'id="detailModal"'),
            self.admin,
        )

    def test_report_page_keeps_dashboard_targets(self):
        self.assert_page_contains(
            "/report-old/",
            ('id="generateBtn"', 'id="resultsSection"', 'id="emailModal"'),
            self.reviewer,
        )

    def test_document_reports_use_light_primer_design(self):
        context = {
            "data": {},
            "date_from": "2026-08-01",
            "date_to": "2026-08-07",
            "chart_images": {},
            "ci_status": None,
            "note": "",
        }

        for template_name in (
            "report/email_report.html",
            "report/print_report.html",
        ):
            with self.subTest(template=template_name):
                html = engines["django"].get_template(template_name).render(context)
                self.assertIn('content="light only"', html)
                self.assertIn("color-scheme: only light", html)
                self.assertIn("#0a5a62", html)
                self.assertNotIn('data-theme="dark"', html)

    def test_manual_gt_page_keeps_interaction_targets(self):
        self.assert_page_contains(
            "/gt/",
            ('id="downloadBtn"', 'id="gtFileDrop"', 'id="resultPanel"'),
            self.reviewer,
        )

class override_environ:
    def __init__(self, values):
        self.values = values
        self.original = {}

    def __enter__(self):
        import os

        for key, value in self.values.items():
            self.original[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, exc_type, exc, tb):
        import os

        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
