from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import sync_playwright
from upload.models import CXRStudy


@override_settings(DEBUG=True,SECURE_SSL_REDIRECT=False,SESSION_COOKIE_SECURE=False,CSRF_COOKIE_SECURE=False)
class UploadBrowserTests(StaticLiveServerTestCase):
    databases={'default','audit'}

    def test_mapping_preview_confirmation_exclusions_and_safe_headers(self):
        user=User.objects.create_user('synthetic-browser',password='synthetic-pass')
        self.client.force_login(user)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(chromium_sandbox=True)
            try:
                context=browser.new_context(viewport={'width':1280,'height':900})
                context.add_cookies([{'name':settings.SESSION_COOKIE_NAME,'value':self.client.cookies[settings.SESSION_COOKIE_NAME].value,'url':self.live_server_url}])
                page=context.new_page()
                page.route('**/upload/api/check-*',lambda route:route.fulfill(json={'connected':True}))
                page.goto(self.live_server_url+'/upload/')
                self.assertTrue(page.locator('#sourceFiles').is_hidden())
                with page.expect_file_chooser() as chooser:
                    page.get_by_role('button', name='Select and drop multiple files together').click()
                chooser.value.set_files([{'name':'synthetic<img onerror=window.injected=1>.csv','mimeType':'text/csv','buffer':b'anon_id,ABC\n1,12\n'}, {'name':'synthetic-ris.csv','mimeType':'text/csv','buffer':b'anon_id,WORKPLACE\n1,SYNTH-A\n'}])
                page.locator('#submitBtn').click()
                page.wait_for_selector('#mapping-0-1')
                self.assertIn('1 distinct studies',page.locator('#precheckStats').inner_text())
                self.assertIn('1 incomplete',page.locator('#precheckStats').inner_text())
                self.assertEqual(page.locator('#precheckFiles section').count(),2)
                self.assertIsNone(page.evaluate('window.injected'))
                self.assertEqual(page.locator('#precheckFiles img').count(),0)
                page.locator('#mapping-0-1').select_option('patient_age')
                self.assertTrue(page.locator('#startAnalysisBtn').is_disabled())
                page.locator('#recheckMappingsBtn').click()
                page.wait_for_function("document.getElementById('startAnalysisBtn').disabled === false")
                self.assertEqual(page.locator('#mapping-0-1').input_value(),'patient_age')
                page.locator('#startAnalysisBtn').click()
                page.wait_for_function("document.getElementById('statusMessage').textContent.includes('No analysis required')")
                page.set_viewport_size({'width':390,'height':844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),391)
            finally:
                browser.close()
        self.assertEqual(CXRStudy.objects.count(),0)
