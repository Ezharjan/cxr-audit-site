# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Adapter-owned catalog for the single configured PRIME reporting project.

Where :mod:`report_v2.projects.base` is deliberately free of clinical vocabulary, this module is exactly
the place that is *allowed* to know it. It binds the generic metadata structures to the real
``upload.CXRStudy`` physical columns, mirrors the reviewed read-only seeds (``lunit-defaults.v1.yaml`` and
the ``prime-overview.yaml`` measurement registry) and exposes one frozen :class:`ProjectDefinition`.

Everything here is plain metadata -- nothing queries the database and nothing imports Django. The column
``field`` strings intentionally keep the source-system names so the future adapter layer can build ORM
queries straight from them; the generic layer never inspects them. The two synthetic site codes below are
demonstration values only; real site choices are produced by the PRIME adapter at query time.
"""

from __future__ import annotations

from .base import (
    Cohort,
    Dimension,
    MeasurementSignature,
    Outcome,
    ProjectDefinition,
    Source,
    ThresholdPolicy,
)

__all__ = [
    "PROJECT_ID",
    "POLICY_ID",
    "POLICY_VERSION",
    "SOURCES",
    "OUTCOMES",
    "COHORTS",
    "DIMENSIONS",
    "POLICIES",
    "MEASUREMENTS",
    "PROJECT",
    "get_project_definition",
]

PROJECT_ID = "prime"
MODEL_LABEL = "upload.cxrstudy"
POLICY_ID = "lunit-defaults"
POLICY_VERSION = 1

# The ten binarised Lunit findings carried by the threshold policy (order mirrors the legacy DEFAULTS).
_POLICY_FINDINGS = (
    "atelectasis",
    "calcification",
    "cardiomegaly",
    "consolidation",
    "fibrosis",
    "mediastinal_widening",
    "nodule",
    "pleural_effusion",
    "pneumoperitoneum",
    "pneumothorax",
)
#: nodule thresholds higher than the rest, exactly as the reviewed ``lunit-defaults.v1`` seed does.
_FINDING_THRESHOLDS = {f: (15.0 if f == "nodule" else 10.0) for f in _POLICY_FINDINGS}


def _build_sources():
    sources = [
        Source("record_id", "record_id", "accession_no", label="Record id"),
        Source("site", "identifier", "workplace", label="Site"),
        Source("procedure_date", "timestamp", "procedure_start_date", label="Procedure start"),
        Source("report_text", "text", "text_report", label="Report text"),
        Source("manual_abnormal", "label", "gt_manual", label="Manual ground truth"),
        Source("llm_abnormal", "label", "gt_llm", label="LLM ground truth"),
        Source("lunit_binarised", "label", "lunit_binarised", label="Lunit binarised"),
        Source(
            "lunit_findings",
            "label",
            "lunit_binarised",
            label="Lunit findings (policy-derived)",
            description="Aggregate recomputed from the ten raw finding scores using the widget threshold policy.",
        ),
        Source(
            "time_to_clinical_decision",
            "duration",
            "time_to_clinical_decision_seconds",
            unit="seconds",
            label="Time to clinical decision",
        ),
        Source(
            "time_end_to_end",
            "duration",
            "time_end_to_end_seconds",
            unit="seconds",
            label="End-to-end time",
        ),
    ]
    # One numeric score source per policy finding, bound to its physical column, on the 0-100 scale.
    for finding in _POLICY_FINDINGS:
        sources.append(
            Source(
                f"score_{finding}",
                "score",
                finding,
                unit="0-100",
                label=finding,
                description="Numeric Lunit score on the policy score scale.",
            )
        )
    sources.extend(Source(f"{finding}_llm", "label", f"{finding}_llm",
                          label=f"LLM {finding.replace('_', ' ')}") for finding in _POLICY_FINDINGS)
    return {source.source_id: source for source in sources}


def _build_outcomes():
    # Binary vocabularies are declared as (normal, abnormal); the positive class is always "abnormal".
    return {
        outcome.outcome_id: outcome
        for outcome in (
            Outcome("abnormal_manual", ("normal", "abnormal"), "abnormal", meaning="Manually graded abnormality"),
            Outcome("abnormal_llm", ("normal", "abnormal"), "abnormal", meaning="LLM-graded abnormality"),
            Outcome("abnormal_lunit", ("normal", "abnormal"), "abnormal", meaning="Binarised Lunit abnormality"),
        )
    }


def _build_cohorts():
    return {
        cohort.cohort_id: cohort
        for cohort in (
            Cohort(
                "manual_label_present",
                description="The legacy manually annotated subset: rows that carry a manual ground-truth label.",
                predicate={"gt_manual__isnull": False},
                requires_fields=("gt_manual",),
            ),
            Cohort(
                "manual_gt_subset",
                description="Canonical name of the manually annotated subset: rows carrying a manual ground-truth label.",
                predicate={"gt_manual__isnull": False},
                requires_fields=("gt_manual",),
            ),
            Cohort("all", description="The whole project population (no restriction)."),
        )
    }


def _build_dimensions():
    return {
        dimension.dimension_id: dimension
        for dimension in (
            Dimension(
                "site",
                "workplace",
                label="Site",
            ),
            Dimension(
                "age", "patient_age", label="Age",
                bands=(("<18", 0, 18), ("18–39", 18, 40), ("40–59", 40, 60),
                       ("60–79", 60, 80), ("80+", 80, None)),
            ),
        )
    }


def _build_policies():
    policy = ThresholdPolicy(
        policy_id=POLICY_ID,
        version=POLICY_VERSION,
        score_scale=(0.0, 100.0),
        operator="gt",
        aggregate="any_positive",
        require_all_scores=True,
        findings=dict(_FINDING_THRESHOLDS),
        description="Uniform per-finding thresholds copied from the legacy DEFAULTS (mirrors lunit-defaults.v1).",
    )
    return {policy.policy_id: policy}


def _build_measurements():
    # ``requires_threshold_policy`` means the prediction role may be a numeric score source that needs a
    # policy to binarise; a label-valued prediction satisfies it without any thresholding.
    measurement_set = [
        MeasurementSignature(
            "record_count",
            inputs={},
            supports_comparison=True,
            supported_displays=("value", "table"),
            units={"value": "count"},
            description="Total number of records in the eligible sample.",
        ),
        MeasurementSignature(
            "label_count",
            inputs={"value": "label"},
            supports_comparison=True,
            supported_displays=("value", "table"),
            units={"value": "count"},
            description="Count of records carrying a given label value.",
        ),
    ]
    measurement_set.extend([
        MeasurementSignature("categorical_count", inputs={"category": "label"},
                             supports_comparison=True, supported_displays=("bar", "pie"), units={"count": "count"}),
        MeasurementSignature("confusion_matrix", inputs={"ground_truth": "label", "prediction": "label"},
                             requires_threshold_policy=True, supports_comparison=True,
                             supported_displays=("confusion_matrix",), units={"count": "count"}),
    ])
    for metric_id in ("accuracy", "sensitivity", "specificity", "balanced_accuracy"):
        measurement_set.append(
            MeasurementSignature(
                metric_id,
                inputs={"ground_truth": "label", "prediction": "label"},
                requires_threshold_policy=True,
                supports_comparison=True,
                supported_displays=("value", "line", "bar"),
                units={"value": "ratio"},
                description=(
                    "Binary classification rate; the prediction may be a label or a thresholded score source."
                ),
            )
        )
    measurement_set.append(
        MeasurementSignature(
            "classification_summary",
            inputs={"ground_truth": "label", "prediction": "label"},
            requires_threshold_policy=True,
            supports_comparison=True,
            supported_displays=("table",),
            units={"n": "count", "accuracy": "ratio", "sensitivity": "ratio", "specificity": "ratio", "balanced_accuracy": "ratio"},
            description="Confusion-matrix derived table; the prediction may be a label or a thresholded score source.",
        )
    )
    measurement_set.append(
        MeasurementSignature(
            "duration_summary",
            inputs={"value": "duration"},
            supported_displays=("boxplot", "table"),
            units={"value": "seconds"},
            description="Descriptive statistics and box coordinates for a stored duration.",
        )
    )
    measurement_set.append(
        MeasurementSignature(
            "reference_agreement",
            inputs={"ground_truth": "label", "prediction": "label"},
            supported_displays=("table",),
            units={"agreement": "ratio", "kappa": "ratio"},
            description="Inter-reference agreement and Cohen's kappa between two label sources.",
        )
    )
    measurement_set.append(
        MeasurementSignature(
            "paired_reference_comparison",
            inputs={"ground_truth": "label", "alternative_reference": "label", "prediction": "label"},
            requires_threshold_policy=True,
            supported_displays=("table",),
            description="Paired comparison of two references on common complete rows; the prediction may be a thresholded score source.",
        )
    )
    for case_id in ("false_negatives", "false_positives"):
        measurement_set.append(
            MeasurementSignature(
                case_id,
                inputs={"ground_truth": "label", "prediction": "label"},
                supported_displays=("table",),
                description=f"Paginated {case_id.replace('_', ' ')} case selection with an aggregate count.",
            )
        )
    return {measurement.measurement_id: measurement for measurement in measurement_set}


SOURCES = _build_sources()
OUTCOMES = _build_outcomes()
COHORTS = _build_cohorts()
DIMENSIONS = _build_dimensions()
POLICIES = _build_policies()
MEASUREMENTS = _build_measurements()

PROJECT = ProjectDefinition(
    project_id=PROJECT_ID,
    display_name="PRIME chest radiography audit",
    timezone="Asia/Singapore",
    model_label=MODEL_LABEL,
    sources=SOURCES,
    outcomes=OUTCOMES,
    cohorts=COHORTS,
    dimensions=DIMENSIONS,
    policies=POLICIES,
    measurements=MEASUREMENTS,
)


def get_project_definition() -> ProjectDefinition:
    """Return the frozen PRIME :class:`ProjectDefinition` (used to bootstrap the production registry)."""
    return PROJECT
