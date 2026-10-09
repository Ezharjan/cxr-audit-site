# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Pure, side-effect-free threshold binarisation and aggregate prediction for the report-v2 layer.

This module turns a plain ``{finding_id: score_or_None}`` mapping into an immutable
:class:`PredictionResult` by resolving an adapter-owned :class:`~report_v2.projects.base.ThresholdPolicy`
through the Task-04 require-project-context guard and binning each covered finding with the explicit ``gt``
(strict greater-than) operator before aggregating under the policy's ``any_positive`` rule. It deliberately
contains **no** ORM, no Django import and no database access: predictions are *computed values*, never
stored labels, and nothing here may touch ``lunit_binarised`` or any other persisted column.

Design rules relied upon by the rest of the layer:

* Every public entry point routes identifier resolution through
  :func:`report_v2.projects.registry.require_project_context`, so an unknown or foreign reference fails with
  the *typed* errors already defined in :mod:`report_v2.projects.base`. There is no default policy version,
  no fallback project and no silent substitution anywhere.
* Malformed configuration (a bad score scale, an unsupported operator/aggregate, a non-numeric or
  out-of-range per-finding default) and malformed *input* (an uncovered finding id, a non-finite or
  out-of-range score, a boolean masquerading as a score) are rejected loudly with a dedicated
  :class:`PolicyEvaluationError` subclass that names the offending finding, never defaulted silently.
* A ``None`` score means the constituent is *absent*; under a ``require_all_scores`` policy any missing
  required constituent makes the aggregate **ineligible** (``label``/``positive`` are ``None``), never
  silently coerced to the negative class.
* Label-valued sources are passed straight through to an outcome vocabulary by :func:`classify_label_prediction`
  with no thresholding and no policy lookup at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence
from math import isfinite

from report_v2.projects.base import (
    AGGREGATES,
    ProjectCatalogError,
    ProjectDefinition,
    SOURCE_OPERATORS,
    ThresholdPolicy,
    UnknownPolicyError,
)
from report_v2.projects.registry import require_project_context

__all__ = [
    "OPERATOR_GT",
    "AGGREGATE_ANY_POSITIVE",
    "PolicyEvaluationError",
    "ScoreOutOfRangeError",
    "NonFiniteScoreError",
    "UncoveredFindingError",
    "MissingPolicyError",
    "InvalidPolicyConfigurationError",
    "OutOfVocabularyLabelError",
    "FindingDecision",
    "PredictionResult",
    "LabelPredictionResult",
    "resolve_policy",
    "classify_prediction",
    "classify_label_prediction",
]

#: The only comparison operator shipped today; mirrors ``base.SOURCE_OPERATORS``.
OPERATOR_GT = "gt"
#: The one aggregate rule shipped today; mirrors ``base.AGGREGATES``.
AGGREGATE_ANY_POSITIVE = "any_positive"


# ---------------------------------------------------------------------------
# Error model (mirrors the base.ProjectCatalogError / loader.DefinitionError shape).
# ---------------------------------------------------------------------------
class PolicyEvaluationError(Exception):
    """Common base for every threshold-evaluation rejection.

    Carries a single human ``message`` plus a stable machine-readable ``kind`` discriminator that
    subclasses override. The string form prefixes the message with the ``kind`` so logs and editors can
    triage without parsing. The constructor is ``Exception.__init__(message)``-compatible so a subclass is
    built with a single positional message (``ScoreOutOfRangeError("...")``).
    """

    kind = "policy-evaluation"

    def __init__(self, message: str) -> None:
        self.message = str(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


class ScoreOutOfRangeError(PolicyEvaluationError):
    """A provided numeric score falls outside the policy's declared ``score_scale``."""

    kind = "score-out-of-range"


class NonFiniteScoreError(PolicyEvaluationError):
    """A provided score is not a finite number (``nan`` / ``inf``)."""

    kind = "non-finite-score"


class UncoveredFindingError(PolicyEvaluationError):
    """A referenced finding id is not covered by the resolved policy's findings map."""

    kind = "uncovered-finding"


class MissingPolicyError(PolicyEvaluationError):
    """The resolved reference was not itself a :class:`ThresholdPolicy` (belt-and-suspenders guard)."""

    kind = "missing-policy"


class InvalidPolicyConfigurationError(PolicyEvaluationError):
    """The resolved policy is internally inconsistent (bad scale/operator/aggregate/default)."""

    kind = "invalid-policy-configuration"


class OutOfVocabularyLabelError(PolicyEvaluationError):
    """A label-valued prediction was outside the addressed outcome's declared class vocabulary."""

    kind = "out-of-vocabulary-label"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_real_number(value: object) -> bool:
    """True only for a genuine ``int``/``float`` -- ``bool`` is explicitly rejected.

    ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)`` is ``True``; a ``True`` /
    ``False`` is never a score or a threshold and must not be silently read as ``1`` / ``0``.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FindingDecision:
    """The immutable binarisation outcome for one covered finding under a threshold policy."""

    finding_id: str
    threshold: float
    score: float | None
    positive: bool | None
    #: e.g. "score 11.0 is strictly greater than threshold 10.0".
    reason: str

    def as_dict(self) -> dict:
        """Return a JSON-friendly view of this single decision."""
        return {
            "finding_id": self.finding_id,
            "threshold": self.threshold,
            "score": self.score,
            "positive": self.positive,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PredictionResult:
    """The immutable aggregate prediction produced from a numeric findings mapping."""

    project_id: str
    policy_ref: str
    policy_version: int
    eligible: bool
    label: str | None
    positive: bool | None
    reason: str
    findings: Mapping[str, FindingDecision]
    missing_findings: tuple[str, ...]

    def as_dict(self) -> dict:
        """Return a deterministic, JSON-friendly snapshot (findings as a list sorted by finding id)."""
        return {
            "project_id": self.project_id,
            "policy_ref": self.policy_ref,
            "policy_version": self.policy_version,
            "eligible": self.eligible,
            "label": self.label,
            "positive": self.positive,
            "reason": self.reason,
            "missing_findings": list(self.missing_findings),
            "findings": [self.findings[fid].as_dict() for fid in sorted(self.findings)],
        }


@dataclass(frozen=True)
class LabelPredictionResult:
    """The immutable pass-through prediction produced from a stored label value (no thresholding)."""

    project_id: str
    label_value: str | None
    eligible: bool
    label: str | None
    positive: bool | None
    reason: str

    def as_dict(self) -> dict:
        """Return a JSON-friendly view of this label prediction."""
        return {
            "project_id": self.project_id,
            "label_value": self.label_value,
            "eligible": self.eligible,
            "label": self.label,
            "positive": self.positive,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def resolve_policy(
    project_id: str, policy_ref: str, *, registry=None
) -> ThresholdPolicy:
    """Resolve an immutable, project-local policy by ``id@version`` through the Task-04 guard.

    Delegates to :func:`report_v2.projects.registry.require_project_context` so an UNKNOWN or FOREIGN policy
    fails with the typed errors from :mod:`report_v2.projects.base` (``UnknownPolicyError`` /
    ``CrossProjectReferenceError`` / ``ProjectCatalogError``). Those errors are *not* caught or converted --
    they propagate so the caller sees the precise cause. There is no fallback and no default version. Returns
    the resolved :class:`ThresholdPolicy` read back off the resolved :class:`ProjectDefinition`, i.e. the exact
    registered immutable object.
    """
    project = require_project_context(project_id, policy_ref=policy_ref, registry=registry)
    return project.policy(policy_ref)


# ---------------------------------------------------------------------------
# Numeric classification
# ---------------------------------------------------------------------------
def classify_prediction(
    findings: Mapping[str, float | None],
    *,
    project_id: str,
    policy_ref: str,
    registry=None,
    outcome_id: str = "abnormal_lunit",
    threshold_settings: Mapping | None = None,
) -> PredictionResult:
    """Validate inputs, bin each covered finding with the explicit ``gt`` operator and aggregate.

    Validation happens up front (see the module docstring): the input must be a :class:`Mapping`, the policy
    must resolve to a coherent :class:`ThresholdPolicy`, every referenced finding must be covered by the
    policy, and every provided score must be a finite in-range real number. After validation the per-finding
    decisions are computed with strict ``score > threshold`` and combined with the policy's aggregate rule
    under its ``require_all_scores`` setting. The function is pure: it never mutates ``findings``, the policy
    or the outcome, and it never touches the database.
    """
    # (B.1) The container itself must be a Mapping -- a programming error, not a policy error.
    if not isinstance(findings, Mapping):
        raise TypeError(f"findings must be a Mapping, got {type(findings).__name__}")

    # (B.2) Resolve through the guard; a non-policy resolution is a configuration error.
    project = require_project_context(project_id, policy_ref=policy_ref, registry=registry)
    policy = project.policy(policy_ref)
    if not isinstance(policy, ThresholdPolicy):
        raise MissingPolicyError(f"resolved reference {policy_ref!r} for project {project_id!r} is not a ThresholdPolicy")

    if threshold_settings is not None:
        from ..thresholds import configure_policy
        try:
            policy = configure_policy(policy, threshold_settings)
        except ValueError as exc:
            raise InvalidPolicyConfigurationError(str(exc)) from exc

    return _classify_policy_prediction(findings, project=project, policy=policy, outcome_id=outcome_id)


def _classify_policy_prediction(findings, *, project, policy, outcome_id="abnormal_lunit"):
    """Classify under a card's already-resolved policy; retain score and missing-data validation."""
    if not isinstance(findings, Mapping):
        raise TypeError("findings must be a Mapping")
    project_id = project.project_id

    # (B.3) score_scale guard: a finite, strictly increasing [lo, hi] pair.
    scale = tuple(policy.score_scale)
    if len(scale) != 2:
        raise InvalidPolicyConfigurationError(
            f"policy {policy.policy_id!r} score_scale must be a [lo, hi] pair, got {policy.score_scale!r}"
        )
    lo, hi = scale
    if not (isfinite(lo) and isfinite(hi)) or not lo < hi:
        raise InvalidPolicyConfigurationError(
            f"policy {policy.policy_id!r} has an invalid score_scale {policy.score_scale!r}"
        )

    # (B.4) operator guard: only the explicit gt operator is supported.
    if policy.operator != OPERATOR_GT:
        raise InvalidPolicyConfigurationError(
            f"policy {policy.policy_id!r} uses operator {policy.operator!r}; only the explicit gt operator is supported"
        )

    # (B.5) aggregate guard: only any_positive is supported.
    if policy.aggregate != AGGREGATE_ANY_POSITIVE:
        raise InvalidPolicyConfigurationError(
            f"policy {policy.policy_id!r} uses aggregate {policy.aggregate!r}; only any_positive is supported"
        )

    # (B.6) every required finding carries exactly one finite in-range numeric default threshold.
    for finding_id, threshold in policy.findings.items():
        if not _is_real_number(threshold) or not isfinite(threshold) or not (lo <= threshold <= hi):
            raise InvalidPolicyConfigurationError(
                f"policy {policy.policy_id!r} has an invalid default threshold for finding "
                f"{finding_id!r}: {threshold!r} (expected a finite real number within [{lo!r}, {hi!r}])"
            )

    # (B.7) coverage guard: every referenced finding id must be covered by the policy.
    for finding_id in findings:
        if finding_id not in policy.findings:
            raise UncoveredFindingError(
                f"finding {finding_id!r} is not covered by policy {policy.ref!r} for project {project_id!r}"
            )

    # (B.8) value guard on each provided (non-absent) score.
    for finding_id, score in findings.items():
        if score is None:
            continue  # absent constituent -- handled at binarisation / aggregation time.
        if not _is_real_number(score):
            raise ScoreOutOfRangeError(
                f"score for finding {finding_id!r} is not a numeric value: {score!r}"
            )
        if not isfinite(score):
            raise NonFiniteScoreError(f"score for finding {finding_id!r} is not finite: {score!r}")
        if not (lo <= score <= hi):
            raise ScoreOutOfRangeError(
                f"score for finding {finding_id!r} value {score!r} is outside the policy score scale [{lo!r}, {hi!r}]"
            )

    # (C) per-finding binarisation with the explicit strict-greater-than operator.
    decisions: dict[str, FindingDecision] = {}
    for finding_id in policy.findings:
        score = findings.get(finding_id, None)
        threshold = float(policy.findings[finding_id])
        if score is None or not isfinite(score):
            decisions[finding_id] = FindingDecision(
                finding_id=finding_id,
                threshold=threshold,
                score=None,
                positive=None,
                reason=f"{finding_id} score is missing",
            )
        else:
            positive = score > threshold  # STRICT: score == threshold => negative.
            decisions[finding_id] = FindingDecision(
                finding_id=finding_id,
                threshold=threshold,
                score=float(score),
                positive=positive,
                reason=(
                    f"{finding_id} score {score!r} is strictly greater than threshold {threshold!r}"
                    if positive
                    else f"{finding_id} score {score!r} does not strictly exceed threshold {threshold!r}"
                ),
            )

    missing_findings = tuple(sorted(fid for fid, dec in decisions.items() if dec.score is None))

    # (E) bind to the addressed outcome vocabulary to name the labels.
    outcome = project.outcome(outcome_id)
    positive_class = outcome.positive_class
    negative_class = next(cls for cls in outcome.classes if cls != positive_class)

    # (D) aggregate under the policy's require-all setting.
    if policy.require_all_scores and missing_findings:
        reason = (
            "ineligible under require-all policy: missing constituent score(s): "
            + ", ".join(missing_findings)
        )
        return PredictionResult(
            project_id=project_id,
            policy_ref=policy.ref,
            policy_version=policy.version,
            eligible=False,
            label=None,
            positive=None,
            reason=reason,
            findings=decisions,
            missing_findings=missing_findings,
        )

    any_positive = any(dec.positive is True for dec in decisions.values())
    covered = len(decisions)
    if any_positive:
        reason = f"aggregate positive via any_positive over {covered} covered finding(s)"
    else:
        reason = "aggregate negative: no covered finding strictly exceeded its threshold"
    return PredictionResult(
        project_id=project_id,
        policy_ref=policy.ref,
        policy_version=policy.version,
        eligible=True,
        label=positive_class if any_positive else negative_class,
        positive=any_positive,
        reason=reason,
        findings=decisions,
        missing_findings=missing_findings,
    )


# ---------------------------------------------------------------------------
# Label pass-through (no thresholding, no policy lookup)
# ---------------------------------------------------------------------------
def classify_label_prediction(
    label_value: str | None,
    *,
    project_id: str,
    outcome_id: str,
    registry=None,
) -> LabelPredictionResult:
    """Pass a stored label value straight through to an outcome vocabulary (no thresholding at all).

    Resolves only the addressed :class:`~report_v2.projects.base.Outcome` through the require guard; there is
    deliberately no policy resolution and no numeric validation. ``None`` makes the prediction ineligible; a
    value outside the outcome's declared classes raises :class:`OutOfVocabularyLabelError`; otherwise the
    prediction simply mirrors the label and reports whether it equals the positive class.
    """
    project = require_project_context(project_id, outcome_id=outcome_id, registry=registry)
    outcome = project.outcome(outcome_id)
    positive_class = outcome.positive_class

    if label_value is None:
        return LabelPredictionResult(
            project_id=project_id,
            label_value=None,
            eligible=False,
            label=None,
            positive=None,
            reason="label value is missing",
        )

    if label_value not in outcome.classes:
        raise OutOfVocabularyLabelError(
            f"label value {label_value!r} is outside the declared classes {tuple(outcome.classes)!r} "
            f"of outcome {outcome_id!r} in project {project_id!r}"
        )

    positive = label_value == positive_class
    which = "positive" if positive else "negative"
    return LabelPredictionResult(
        project_id=project_id,
        label_value=label_value,
        eligible=True,
        label=label_value,
        positive=positive,
        reason=f"label passthrough: {label_value!r} equals {which} class",
    )
