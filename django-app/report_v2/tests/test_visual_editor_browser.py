# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Real Chromium checks with the existing synthetic-only throwaway server."""
import unittest
import yaml
from report_v2.definitions.loader import load_report_definition
from report_v2.tests import test_browser_layout as harness


@unittest.skipUnless(harness._PW_LAUNCH_OK, 'Playwright Chromium unavailable')
class VisualEditorBrowserTests(unittest.TestCase):
    setUpClass = classmethod(harness.BrowserLayoutTests.setUpClass.__func__)
    tearDownClass = classmethod(harness.BrowserLayoutTests.tearDownClass.__func__)

    def ready(self):
        self.page.wait_for_function("document.querySelector('[data-role=visual-status]').textContent === ''")

    def draft(self):
        return load_report_definition(self.page.locator('#id_yaml_text').input_value())

    def test_per_card_lunit_thresholds_roundtrip_and_reset(self):
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        original = self.page.locator('#id_yaml_text').input_value()
        card = self.page.locator('[data-widget-id=bl]')
        card.hover(); card.locator('[data-visual-action=edit]').click()
        form = self.page.locator('[data-role=card-options-form]')
        form.locator('[name=measurement]').select_option('accuracy')
        form.locator('select[data-role=ground_truth]').select_option('pneumothorax_llm')
        form.locator('select[data-role=prediction]').select_option('lunit_findings')
        form.locator('details > summary').click()
        form.locator('[name=policy]').select_option('lunit-defaults@1')
        self.assertTrue(form.locator('[data-role=lunit-threshold-options]').is_visible())
        form.locator('[name=lunit_default]').fill('95')
        for checkbox in form.locator('[name=lunit_findings]').all():
            checkbox.set_checked(checkbox.get_attribute('value') == 'pneumothorax')
        form.locator('[data-finding=pneumothorax]').fill('96')
        form.locator('[type=submit]').click()
        self.page.locator('[data-role=card-options]').wait_for(state='hidden'); self.ready()
        query = next(w for w in self.draft()['sections'][0]['widgets'] if w['id'] == 'bl')['query']
        self.assertEqual(query['lunit_thresholds'], {'findings': ['pneumothorax'], 'default': 95,
                                                   'overrides': {'pneumothorax': 96}})
        card = self.page.locator('[data-widget-id=bl]')
        card.hover(); card.locator('[data-visual-action=edit]').click()
        self.assertEqual(form.locator('[name=lunit_default]').input_value(), '95')
        self.assertEqual(form.locator('[data-finding=pneumothorax]').input_value(), '96')
        if not form.locator('details').evaluate('(node) => node.open'):
            form.locator('details > summary').click()
        form.locator('[data-visual-action=reset-thresholds]').click()
        form.locator('[type=submit]').click()
        self.page.locator('[data-role=card-options]').wait_for(state='hidden'); self.ready()
        query = next(w for w in self.draft()['sections'][0]['widgets'] if w['id'] == 'bl')['query']
        self.assertNotIn('lunit_thresholds', query)
        self.page.locator('[data-visual-action=undo]').click(); self.ready()
        self.page.locator('[data-visual-action=undo]').click(); self.ready()
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), original)

    def test_card_deletion_requires_confirmation(self):
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        original = self.page.locator('#id_yaml_text').input_value()
        card = self.page.locator('[data-widget-id=bv]')
        title = next(w['title'] for w in self.draft()['sections'][0]['widgets'] if w['id'] == 'bv')
        card.hover()
        card.locator('[data-visual-action=edit]').click()
        form = self.page.locator('[data-role=card-options-form]')
        prompts = []

        def cancel(dialog):
            prompts.append((dialog.type, dialog.message))
            dialog.dismiss()

        self.page.once('dialog', cancel)
        form.locator('[data-visual-action=delete-card]').click()
        self.assertEqual(prompts, [('confirm', f'Are you sure you want to delete "{title}"?')])
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), original)
        self.assertEqual(card.count(), 1)
        self.assertTrue(form.is_visible())

        self.page.once('dialog', lambda dialog: dialog.accept())
        form.locator('[data-visual-action=delete-card]').click()
        self.page.locator('[data-role=card-options]').wait_for(state='hidden')
        self.ready()
        self.assertEqual(card.count(), 0)
        self.assertFalse(any(w['id'] == 'bv' for w in self.draft()['sections'][0]['widgets']))
        self.page.locator('[data-visual-action=undo]').click()
        self.ready()
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), original)

    def test_complete_visual_workflow(self):
        errors = []
        self.page.on('pageerror', lambda error: errors.append(str(error)))
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        self.assertEqual(self.page.locator('.visual-card').count(), 5)
        original = self.page.locator('#id_yaml_text').input_value()
        self.page.locator('[data-visual-action=add-card]').click()
        form = self.page.locator('[data-role=card-options-form]')
        form.locator('[name=type]').select_option('text')
        form.locator('[name=title]').fill('Synthetic note')
        form.locator('[name=text]').fill('Safe <script>text</script>\nSecond line')
        form.locator('[type=submit]').click()
        self.page.locator('[data-role=card-options]').wait_for(state='hidden')
        self.ready()
        self.assertEqual(self.page.locator('.visual-card').count(), 6)
        self.assertEqual(self.draft()['sections'][0]['widgets'][-1]['type'], 'text')
        note = self.page.locator('.visual-card').last
        note.hover(); note.locator('[data-visual-action=preview]').click()
        self.page.locator('[data-role=preview-dialog]').wait_for(state='visible')
        self.page.locator('[data-role=preview-output] .widget-static-text').wait_for(state='visible')
        self.assertIn('Safe <script>text</script>', self.page.locator('[data-role=preview-output]').inner_text())
        self.assertEqual(self.page.locator('[data-role=preview-output] script').count(), 0)
        self.page.locator('[data-action=close-preview]').click()
        self.page.locator('[data-visual-action=undo]').click(); self.ready()
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), original)

        # Guided edits and options Preview do not commit until Apply.
        first = self.page.locator('[data-widget-id=bv]')
        first.hover(); first.locator('[data-visual-action=edit]').click()
        form.locator('[name=title]').fill('Unsaved preview title')
        form.locator('[name=width]').fill('6')
        form.locator('[data-visual-action=preview-options]').click()
        self.page.locator('[data-role=preview-output] .widget-frame').wait_for(state='visible')
        self.assertIn('Unsaved preview title', self.page.locator('[data-role=preview-output]').inner_text())
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), original)
        self.page.locator('[data-action=close-preview]').click()
        self.assertTrue(form.evaluate('(form) => form.checkValidity()'), form.evaluate('(form) => Array.from(form.elements).filter(el => el.validity && !el.validity.valid).map(el => [el.name, el.validationMessage])'))
        form.locator('[type=submit]').click()
        self.page.wait_for_function("!document.querySelector('[data-role=card-options]').open || document.querySelector('[data-role=card-form-error]').textContent")
        self.assertEqual(form.locator('[data-role=card-form-error]').inner_text(), '')
        self.page.locator('[data-role=card-options]').wait_for(state='hidden'); self.ready()
        self.assertEqual(next(w for w in self.draft()['sections'][0]['widgets'] if w['id'] == 'bv')['layout']['width'], 6)
        self.page.locator('[data-visual-action=undo]').click(); self.ready()

        # Snapped resize commits one change, then undo restores it.
        node = self.page.locator('[data-widget-id=bv]')
        handle = node.locator('.visual-resize').bounding_box()
        grid = self.page.locator('.visual-grid').bounding_box()
        self.page.mouse.move(handle['x'] + 5, handle['y'] + 5)
        self.page.mouse.down(); self.page.mouse.move(handle['x'] + 5 + grid['width'] / 12, handle['y'] + 5, steps=5); self.page.mouse.up()
        self.ready()
        self.assertEqual(next(w for w in self.draft()['sections'][0]['widgets'] if w['id'] == 'bv')['layout']['width'], 4)
        self.page.locator('[data-visual-action=undo]').click(); self.ready()

        # Moving via a handle reorders within the section without changing identity.
        source = self.page.locator('[data-widget-id=bv]').locator('.visual-move').bounding_box()
        target = self.page.locator('[data-widget-id=bt]').bounding_box()
        self.page.mouse.move(source['x'] + 5, source['y'] + 5); self.page.mouse.down()
        self.page.mouse.move(target['x'] + target['width'] - 20, target['y'] + 40, steps=8); self.page.mouse.up()
        self.ready()
        self.assertEqual(self.draft()['sections'][0]['widgets'][2]['id'], 'bv')
        self.page.locator('[data-visual-action=undo]').click(); self.ready()

        # Invalid manual YAML pauses the canvas; undo retains the original draft.
        self.page.locator('#yamlEditorTab').click()
        self.page.locator('#id_yaml_text').fill('invalid: [')
        self.page.wait_for_function("document.querySelector('[data-role=visual-status]').textContent.includes('paused')")
        self.page.locator('#visualEditorTab').click()
        self.assertTrue(self.page.locator('[data-role=visual-canvas]').evaluate('(node) => node.inert'))
        self.page.locator('[data-visual-action=undo]').click(); self.ready()
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), original)
        self.assertFalse(errors, errors)
        self.page.wait_for_function("document.querySelector('[data-role=validation-summary]').textContent === 'No errors'")
        self.page.screenshot(path=str(harness.DJANGO_APP.parent / '.planning/report-v2/visual-editor-execution/visual-editor.png'))

    def test_narrow_canvas_preserves_width_and_options_work(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        before = self.page.locator('#id_yaml_text').input_value()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 391)
        self.page.locator('[data-widget-id=bv]').click()
        self.page.locator('[data-widget-id=bv]').locator('[data-visual-action=edit]').click()
        self.assertEqual(self.page.locator('[name=width]').input_value(), '3')
        self.page.locator('[data-visual-action=cancel]').click()
        self.assertEqual(self.page.locator('#id_yaml_text').input_value(), before)
        self.page.set_viewport_size({'width': 1440, 'height': 900})

    def test_toolbar_add_card_height_and_publication_cycle(self):
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        self.page.locator('#yamlEditorTab').click()
        yaml_input = self.page.locator('#id_yaml_text')
        yaml_input.fill(yaml_input.input_value().replace('row_height_px: 64', 'row_height_px: 80'))
        self.page.locator('#visualEditorTab').click(); self.ready()
        first = self.page.locator('[data-widget-id=bv]')
        first.click()
        self.page.locator('[data-visual-action=add-card]').click()
        form = self.page.locator('[data-role=card-options-form]')
        form.locator('[name=type]').select_option('text')
        form.locator('[name=title]').fill('Row-end synthetic card')
        form.locator('[name=text]').fill('Synthetic height check')
        form.locator('[name=height]').fill('5')
        form.locator('[type=submit]').click()
        self.page.locator('[data-role=card-options]').wait_for(state='hidden')
        self.ready()
        cards = self.draft()['sections'][0]['widgets']
        self.assertEqual(cards[2]['title'], 'Row-end synthetic card')
        node = self.page.locator('.visual-card').nth(2)
        node.locator('[data-visual-action=preview]').click()
        frame = self.page.locator('[data-role=preview-output] .widget-frame')
        frame.wait_for(state='visible')
        self.assertAlmostEqual(frame.bounding_box()['height'], 5 * 80 + 4 * 16, delta=1)
        self.page.locator('[data-action=close-preview]').click()
        publish = self.page.locator('[data-action=publish]')
        if publish.inner_text() == 'Unpublish':
            publish.click(); self.page.wait_for_function("document.querySelector('[data-action=publish]').textContent === 'Publish'")
        self.assertTrue(self.page.locator('[data-role=view-report]').is_disabled())
        self.page.locator('[data-action=publish]').click()
        self.page.wait_for_url('**/report/browser14a/')
        report = self.page.locator('.widget-frame').filter(has_text='Row-end synthetic card')
        self.assertAlmostEqual(report.bounding_box()['height'], 5 * 80 + 4 * 16, delta=1)
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        self.assertEqual(self.page.locator('[data-action=publish]').inner_text(), 'Unpublish')
        self.assertEqual(self.page.locator('[data-role=view-report]').get_attribute('href'), '/report/browser14a/')
        self.page.locator('[data-action=publish]').click()
        self.page.wait_for_function("document.querySelector('[data-action=publish]').textContent === 'Publish'")
        self.assertEqual(self.page.locator('[data-action=publish]').inner_text(), 'Publish')

    def test_heading_divider_defaults_and_toolbar(self):
        self.page.goto(self.server.base + '/editor/browser_layout_14a/')
        self.ready()
        undo = self.page.locator('[data-visual-action=undo]').bounding_box()
        add = self.page.locator('[data-visual-action=add-card]').bounding_box()
        self.assertAlmostEqual(add['x'] - undo['x'] - undo['width'], 8, delta=1)
        self.assertAlmostEqual(add['y'], undo['y'], delta=1)
        self.assertNotIn('Visual edits update YAML', self.page.locator('[data-editor]').inner_text())
        self.assertEqual(self.page.locator('.visual-grid').count(), 1)
        form = self.page.locator('[data-role=card-options-form]')
        for kind in ('heading', 'divider'):
            self.page.locator('[data-visual-action=add-card]').click()
            form.locator('[name=type]').select_option(kind)
            self.assertEqual(form.locator('[name=width]').input_value(), '12')
            self.assertEqual(form.locator('[name=height]').input_value(), '1')
            form.locator('[name=title]').fill('Synthetic ' + kind)
            form.locator('[type=submit]').click()
            self.page.locator('[data-role=card-options]').wait_for(state='hidden'); self.ready()
        divider = self.page.locator('.visual-card[data-type=divider]').last
        self.assertEqual(divider.locator('hr').count(), 1)
        self.assertEqual(divider.evaluate('(node) => getComputedStyle(node).backgroundColor'), 'rgba(0, 0, 0, 0)')
        self.assertEqual(divider.evaluate('(node) => getComputedStyle(node).borderTopColor'), 'rgba(0, 0, 0, 0)')
        self.assertNotIn('sections:', self.page.locator('#id_yaml_text').input_value())
