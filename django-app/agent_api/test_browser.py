import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import sync_playwright


@override_settings(DEBUG=True, SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class APIConfigurationBrowserTests(StaticLiveServerTestCase):
    databases = {"default", "audit"}

    def test_admin_key_lifecycle_and_responsive_theme(self):
        user = User.objects.create_superuser("browser-admin", "synthetic@example.invalid", "synthetic")
        self.client.force_login(user)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 1000})
                context.add_cookies([{"name": settings.SESSION_COOKIE_NAME,
                    "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value, "url": self.live_server_url}])
                page = context.new_page()
                page.goto(self.live_server_url + "/settings/api/")
                self.assertEqual(page.get_by_role("heading", name="API access: disabled").count(), 1)
                page.get_by_label("Key label").fill("Synthetic browser agent")
                page.get_by_label("Key expiry").select_option("never")
                self.assertFalse(page.get_by_label("Expires on").is_visible())
                page.get_by_label("Key expiry").select_option("date")
                self.assertTrue(page.get_by_label("Expires on").is_visible())
                page.get_by_label("Key expiry").select_option("never")
                page.get_by_role("button", name="Create key", exact=True).click()
                key = page.get_by_label("New API key").input_value()
                self.assertTrue(key.startswith("primer_"))
                context.grant_permissions(["clipboard-read", "clipboard-write"])
                page.get_by_role("button", name="Copy key", exact=True).click()
                page.wait_for_function("document.getElementById('copyStatus').textContent === 'Copied.'")
                self.assertEqual(page.evaluate("navigator.clipboard.readText()"), key)
                page.get_by_role("link", name="Done", exact=True).click()
                self.assertEqual(page.get_by_label("New API key").count(), 0)
                self.assertIn("Synthetic browser agent", page.inner_text("body"))
                self.assertIn("Never", page.locator('td[data-label="Expires"]').inner_text())
                page.get_by_label("I understand these effects.").check()
                page.get_by_role("button", name="Enable API", exact=True).click()
                self.assertEqual(page.get_by_role("heading", name="API access: enabled").count(), 1)
                response = context.request.get(self.live_server_url + "/api/prime/reports", headers={"X-API-Key": key})
                self.assertEqual(response.status, 200)
                page.screenshot(path=str(Path(tempfile.gettempdir()) / "primer-api-config-light.png"), full_page=True)
                page.locator("#themeToggle").click()
                page.wait_for_timeout(350)  # Let the shared theme transition finish before screenshots.
                page.screenshot(path=str(Path(tempfile.gettempdir()) / "primer-api-config-dark.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(350)  # Let the shared sidebar transition finish after resizing.
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
                self.assertEqual(page.locator(".api-table-wrap").evaluate("el => el.scrollWidth <= el.clientWidth"), True)
                page.screenshot(path=str(Path(tempfile.gettempdir()) / "primer-api-config-mobile.png"), full_page=True)
                page.get_by_label("Confirm revocation").check()
                page.get_by_role("button", name="Revoke Synthetic browser agent").click()
                response = context.request.get(self.live_server_url + "/api/prime/reports", headers={"X-API-Key": key})
                self.assertEqual(response.status, 401)
            finally:
                browser.close()
