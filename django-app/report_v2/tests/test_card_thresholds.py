# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Synthetic coverage for independent report cutoffs; no deployment data or inference."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import yaml
from django.test import SimpleTestCase, TestCase, RequestFactory
from types import SimpleNamespace
from django.template.loader import render_to_string

from report_v2 import data, views, exports
from report_v2.definitions import visual
from report_v2.definitions.loader import DefinitionError, load_report_definition
from report_v2.definitions.validation import DisplayValidationError, validate_display
from report_v2.measurements.predictions import classify_prediction, InvalidPolicyConfigurationError
from report_v2.projects.prime import get_project_definition
from report_v2.seed import report_seed_text
from report_v2.thresholds import configure_policy
from upload.models import CXRStudy


def cards():
    layout = load_report_definition(report_seed_text())
    card = deepcopy(views._layout_widgets(layout)["accuracy"])
    card["query"]["lunit_thresholds"] = {"default": 10}
    single = deepcopy(card)
    single["id"] = "pneumothorax95"
    single["query"]["inputs"]["ground_truth"] = "pneumothorax_llm"
    single["query"]["lunit_thresholds"] = {"findings": ["pneumothorax"], "default": 95}
    layout["sections"] = [{"id": "canvas", "title": "Synthetic thresholds", "widgets": [card, single]}]
    return layout, card, single


class CardThresholdDefinitionTests(SimpleTestCase):
    def test_roundtrip_independent_cards_and_reset(self):
        layout, card, single = cards()
        single["type"] = "bar"
        text = yaml.safe_dump(layout)
        validate_display(load_report_definition(text))
        single["query"]["lunit_thresholds"]["overrides"] = {"pneumothorax": 96}
        text, result = visual.transform(text, {"action": "edit", "section_id": "canvas",
                                              "widget_id": single["id"], "card": single})
        self.assertEqual(views._layout_widgets(result)[card["id"]]["query"], card["query"])
        self.assertEqual(load_report_definition(text), result)
        del single["query"]["lunit_thresholds"]
        _, result = visual.transform(text, {"action": "edit", "section_id": "canvas",
                                            "widget_id": single["id"], "card": single})
        self.assertNotIn("lunit_thresholds", views._layout_widgets(result)[single["id"]]["query"])

    def test_invalid_settings_rejected(self):
        for settings in ({}, {"default": True}, {"default": -1}, {"default": 101},
                         {"default": float("nan")}, {"default": "10"}, {"findings": []},
                         {"findings": ["made_up"]}, {"findings": ["nodule", "nodule"]},
                         {"findings": ["pneumothorax"], "overrides": {"nodule": 12}},
                         {"unknown": 10}):
            with self.subTest(settings=settings):
                layout, card, _ = cards()
                card["query"]["lunit_thresholds"] = settings
                with self.assertRaises((DefinitionError, DisplayValidationError)):
                    validate_display(load_report_definition(yaml.safe_dump(layout)))
        layout, card, _ = cards()
        card["query"]["inputs"]["prediction"] = "lunit_binarised"
        with self.assertRaises(DisplayValidationError):
            validate_display(layout)
        with patch.object(CXRStudy.objects, "all", side_effect=AssertionError("must validate before DB read")):
            with self.assertRaises(data.AdapterError):
                data.fetch_project_rows(layout_widget=card)

    def test_policy_defaults_unchanged_and_override_precedence(self):
        base = get_project_definition().policy("lunit-defaults@1")
        self.assertIs(configure_policy(base), base)
        policy = configure_policy(base, {"default": 10, "overrides": {"pneumothorax": 95}})
        self.assertEqual(policy.findings["nodule"], 10)
        self.assertEqual(policy.findings["pneumothorax"], 95)
        self.assertEqual(base.findings["nodule"], 15)
        self.assertEqual(base.findings["pneumothorax"], 10)

    def test_strict_equality_and_missing_selected_scores(self):
        settings = {"findings": ["pneumothorax"], "default": 95}
        for score, expected in ((95, False), (95.1, True), (None, None)):
            prediction = classify_prediction({"pneumothorax": score}, project_id="prime",
                                             policy_ref="lunit-defaults@1", threshold_settings=settings)
            self.assertIs(prediction.positive, expected)
            self.assertEqual(set(prediction.findings), {"pneumothorax"})
        with self.assertRaises(InvalidPolicyConfigurationError):
            classify_prediction({}, project_id="prime", policy_ref="lunit-defaults@1",
                                threshold_settings={"default": False})

    def test_editor_catalog_exposes_cutoffs_and_fixed_references(self):
        catalog = visual.catalog()
        policy = next(p for p in catalog["policies"] if p["id"] == "lunit-defaults@1")
        self.assertEqual(policy["findings"]["nodule"], 15)
        self.assertEqual(policy["score_scale"], [0, 100])
        self.assertIn("pneumothorax_llm", {s["id"] for s in catalog["sources"]})


class CardThresholdAdapterTests(TestCase):
    def setUp(self):
        self.layout, self.all_findings, self.single = cards()
        scores = {f: 5 for f in get_project_definition().policy("lunit-defaults@1").findings}
        scores.update(pneumothorax=95, nodule=11)
        self.study = CXRStudy.objects.create(accession_no=990000001, gt_llm=1,
            pneumothorax_llm=0, lunit_binarised=0,
            procedure_start_date=datetime(2026, 10, 1, tzinfo=timezone.utc), **scores)

    def test_independent_classification_and_unchanged_stored_labels(self):
        first = data.fetch_project_rows(layout_widget=self.all_findings)[0]
        second = data.fetch_project_rows(layout_widget=self.single)[0]
        self.assertEqual((first["gt_label"], first["pred_label"]), (1, 1))
        self.assertEqual((second["gt_label"], second["pred_label"]), (0, 0))
        self.study.refresh_from_db()
        self.assertEqual((self.study.gt_llm, self.study.pneumothorax_llm, self.study.lunit_binarised), (1, 0, 0))
        self.study.nodule = None
        self.study.save(update_fields=["nodule"])
        self.assertIsNone(data.fetch_project_rows(layout_widget=self.all_findings)[0]["pred_label"])
        self.assertEqual(data.fetch_project_rows(layout_widget=self.single)[0]["pred_label"], 0)

    def test_evaluation_and_frozen_exports_carry_effective_thresholds(self):
        payload = views._evaluate(self.layout, self.single)
        self.assertNotIn("error", payload)
        self.assertEqual(payload["aggregates"]["accuracy"], 1)
        self.assertEqual(payload["lunit_thresholds"]["findings"], {"pneumothorax": 95})
        self.assertIn("pneumothorax > 95", payload["caption"])
        document = {"widgets": [{"widget_id": self.single["id"], "title": "Pneumothorax",
                                 "type": "bar", "payload": payload}]}
        vm = exports._print_view_model(document)
        self.assertIn("pneumothorax > 95", vm["widgets"][0]["policy"])
        for template, context in (("print", {"print": vm}),
                                  ("email", {"email": exports._email_view_model(document, {})})):
            self.assertIn("pneumothorax &gt; 95", render_to_string("report_v2/" + template + ".html", context))

    def test_csv_uses_same_predictions_and_records_cutoffs(self):
        token = views._widget_context_token("synthetic_thresholds", "v1", self.single["id"])
        request = RequestFactory().get("/report/synthetic_thresholds/csv/full/",
                                      {"widget": self.single["id"], "context": token})
        request.user = SimpleNamespace(is_authenticated=True)
        with patch.object(data, "load_published_layout", return_value=("v1", self.layout)):
            response = views.report_csv(request, "synthetic_thresholds", "full")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("# lunit_thresholds=", content)
        self.assertIn('pneumothorax"": 95', content)
        self.assertIn("990000001", content)
