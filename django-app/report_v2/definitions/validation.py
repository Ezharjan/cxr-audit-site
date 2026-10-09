# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Semantic / display-level validation for already-structurally-valid report widget configs.

This module performs post-schema checks on plain dict/list/scalar config values handed in by
callers. It does NOT touch Django, the ORM, DB, the web tier, or any report_v2 runtime module.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

__all__ = [
    "NUMERIC_BENCHMARK_CHART_TYPES",
    "NONNUMERIC_CHART_TYPES",
    "CI_RENDER_KEYS",
    "ALLOWED_BUCKET_KEYS",
    "ALLOWED_WHISKER_STYLES",
    "DisplayValidationError",
    "WhiskerIsConfidenceIntervalError",
    "CalculatedBaselineBandError",
    "NumericBenchmarkOnNonNumericChartError",
    "BenchmarkUnitMismatchError",
    "UnknownWidgetInputError",
    "UnknownColumnError",
    "UnknownBucketError",
    "UnknownThresholdPolicyError",
    "DisallowedGroupingError",
    "CiOnUnknownMethodError",
    "ensure_whisker_is_not_ci",
    "ensure_no_calculated_baseline_band",
    "validate_numeric_benchmark",
    "validate_display",
    "build_ci_render_keys",
]

NUMERIC_BENCHMARK_CHART_TYPES: frozenset = frozenset({"line", "bar", "boxplot", "value"})
NONNUMERIC_CHART_TYPES: frozenset = frozenset({"pie", "confusion_matrix", "table"})
CI_RENDER_KEYS: frozenset = frozenset({"ci", "lower", "upper"})
ALLOWED_BUCKET_KEYS: frozenset = frozenset({"day", "week", "month", "year"})
ALLOWED_WHISKER_STYLES: frozenset = frozenset({"min_max", "p5_p95", "tukey"})

# ponytail: computed-source markers kept as a module-level frozenset for clarity; expand if new
# synthesising keys appear in upstream schemas.
_COMPUTED_SOURCE_KEYS: frozenset = frozenset({
    "computed", "calculate", "from_data", "derive", "derived", "synthetic", "auto", "generated",
})

# ponytail: keys that indicate a baseline references registered data rather than being synthesised.
_REGISTERED_REF_KEYS: frozenset = frozenset({
    "measurement", "policy", "threshold_policy", "cohort", "reference",
})

# ponytail: marker keys that signal a whisker is being presented as a CI (conflation).
_WHISKER_CI_MARKER_KEYS: frozenset = frozenset({"whisker_is_ci", "whiskers_as_ci"})


class DisplayValidationError(Exception):
    """Base class for all display-level validation failures."""

    kind: str = "display"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: str = message

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


class WhiskerIsConfidenceIntervalError(DisplayValidationError):
    kind = "whisker_is_ci"


class CalculatedBaselineBandError(DisplayValidationError):
    kind = "calculated_baseline_band"


class NumericBenchmarkOnNonNumericChartError(DisplayValidationError):
    kind = "numeric_benchmark_nonnumeric_chart"


class BenchmarkUnitMismatchError(DisplayValidationError):
    kind = "benchmark_unit_mismatch"


class UnknownWidgetInputError(DisplayValidationError):
    kind = "unknown_widget_input"


class UnknownColumnError(DisplayValidationError):
    kind = "unknown_column"


class UnknownBucketError(DisplayValidationError):
    kind = "unknown_bucket"


class UnknownThresholdPolicyError(DisplayValidationError):
    kind = "unknown_threshold_policy"


class DisallowedGroupingError(DisplayValidationError):
    kind = "disallowed_grouping"


class CiOnUnknownMethodError(DisplayValidationError):
    kind = "ci_on_unknown_method"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_mapping(obj: Any) -> bool:
    return isinstance(obj, Mapping)


def _is_number(val: Any) -> bool:
    """Strict numeric check: bool is NOT considered a number here."""
    if isinstance(val, bool):
        return False
    return isinstance(val, (int, float))


# ---------------------------------------------------------------------------
# Public validators
# ---------------------------------------------------------------------------

def ensure_whisker_is_not_ci(config: Mapping[str, Any]) -> None:
    """Reject configs that would present a box/Tukey whisker as a confidence interval."""
    options: Mapping[str, Any] = config.get("options") or {}
    ci: Mapping[str, Any] = config.get("ci") or {}
    ci_enabled: bool = bool(ci.get("enabled"))
    widget_type: Optional[str] = config.get("type")
    whiskers_style: Optional[str] = options.get("whiskers")

    # Check for marker keys inside options (generic conflation detection).
    has_marker: bool = (
        any(options.get(k) for k in _WHISKER_CI_MARKER_KEYS)
        or "ci" in options
    )

    # Boxplot + whiskers + CI enabled + marker present => conflation.
    if whiskers_style is not None and ci_enabled and widget_type == "boxplot" and has_marker:
        raise WhiskerIsConfidenceIntervalError(
            "Boxplot whiskers must not be presented as a confidence interval."
        )

    # Tukey whiskers + CI enabled + marker present => conflation.
    if whiskers_style == "tukey" and ci_enabled and has_marker:
        raise WhiskerIsConfidenceIntervalError(
            "Tukey whiskers must not be presented as a confidence interval."
        )

    # A whisker style string that claims CI semantics.
    if whiskers_style is not None and isinstance(whiskers_style, str):
        lowered: str = whiskers_style.casefold()
        if "ci" in lowered or "confidence" in lowered:
            raise WhiskerIsConfidenceIntervalError(
                f"Whisker style '{whiskers_style}' claims confidence-interval semantics."
            )


def ensure_no_calculated_baseline_band(config: Mapping[str, Any]) -> None:
    """Reject baselines that synthesise from widget data rather than referencing registered data."""
    baseline: Optional[Mapping[str, Any]] = config.get("baseline") or config.get("baseline_band")
    if baseline is None:
        return

    # Reject computed/synthesised source keys.
    if any(key in baseline for key in _COMPUTED_SOURCE_KEYS):
        raise CalculatedBaselineBandError(
            "Baseline must reference registered data, not compute/synthesise from widget data."
        )

    # Valid if it references registered data via a known key.
    if any(key in baseline for key in _REGISTERED_REF_KEYS):
        return

    # Valid if it is an explicit constant: numeric value + unit pair.
    if _is_number(baseline.get("value")) and "unit" in baseline:
        return

    raise CalculatedBaselineBandError(
        "Baseline neither references registered data nor provides an explicit constant benchmark."
    )


def validate_numeric_benchmark(
    config: Mapping[str, Any],
    *,
    chart_type: Optional[str] = None,
    measure_unit: Optional[str] = None,
) -> None:
    """Validate numeric benchmarks attached to the widget."""
    chart_type = chart_type if chart_type is not None else config.get("type")
    benchmarks = config.get("benchmarks") or []

    for bench in benchmarks:
        value = bench.get("value")
        unit = bench.get("unit")

        # Validate that value is a real number if present.
        if value is not None:
            if not _is_number(value) or (isinstance(value, float) and not math.isfinite(value)):
                raise DisplayValidationError(
                    f"Benchmark value {value!r} is not a valid finite number."
                )

            # Numeric benchmark on non-numeric chart or unknown chart type.
            if chart_type in NONNUMERIC_CHART_TYPES or (
                chart_type not in NUMERIC_BENCHMARK_CHART_TYPES
                and chart_type not in NONNUMERIC_CHART_TYPES
            ):
                raise NumericBenchmarkOnNonNumericChartError(
                    f"Numeric benchmark attached to chart type '{chart_type}'."
                )

        # Unit mismatch check (skip when measure_unit is None).
        if measure_unit is not None and unit is not None:
            if str(unit).strip().casefold() != str(measure_unit).strip().casefold():
                raise BenchmarkUnitMismatchError(
                    f"Benchmark unit '{unit}' does not match measure unit '{measure_unit}'."
                )


def validate_display(
    config: Mapping[str, Any],
    *,
    allowed_inputs: Optional[Any] = None,
    declared_columns: Optional[Any] = None,
    allowed_buckets: Optional[frozenset] = None,
    declared_threshold_policies: Optional[Any] = None,
    allowed_grouping: Optional[Any] = None,
    measure_unit: Optional[str] = None,
    ci_methods: Optional[Any] = None,
) -> None:
    """Run all semantic display validations against a widget config dict."""
    if not isinstance(config, Mapping):
        raise DisplayValidationError("config must be a mapping.")

    query: Mapping[str, Any] = config.get("query") or {}

    widgets = config.get("widgets") or [w for section in config.get("sections", []) for w in section["widgets"]] or [config]
    for widget in widgets:
        widget_query = widget.get("query") or {}
        if "lunit_thresholds" in widget_query:
            from ..projects.prime import get_project_definition
            from ..projects.base import ProjectCatalogError
            from ..thresholds import widget_policy
            try:
                if config.get("project", "prime") != "prime":
                    raise ValueError("Custom Lunit cutoffs require the prime project.")
                widget_policy(widget_query, get_project_definition())
            except (ValueError, ProjectCatalogError) as exc:
                raise DisplayValidationError(str(exc)) from exc

    # 1. Inputs.
    if allowed_inputs is not None:
        for key in query.get("inputs") or {}:
            if key not in allowed_inputs:
                raise UnknownWidgetInputError(f"Unknown widget input: {key!r}")

    # 2. Columns.
    if declared_columns is not None:
        for col in config.get("columns") or []:
            if col not in declared_columns:
                raise UnknownColumnError(f"Unknown column: {col!r}")

    # 3. Buckets.
    if allowed_buckets is not None or "bucket" in config:
        bucket = config.get("bucket")
        if bucket is not None:
            effective_buckets = allowed_buckets if allowed_buckets is not None else ALLOWED_BUCKET_KEYS
            if bucket not in effective_buckets:
                raise UnknownBucketError(f"Unknown bucket: {bucket!r}")

    # 4. Threshold policies.
    if declared_threshold_policies is not None:
        policy = query.get("threshold_policy")
        if policy is not None and policy not in declared_threshold_policies:
            raise UnknownThresholdPolicyError(f"Unknown threshold policy: {policy!r}")

    # 5. Benchmarks / units.
    validate_numeric_benchmark(config, measure_unit=measure_unit)

    # 6. Allowed grouping.
    if allowed_grouping is not None:
        controls: Mapping[str, Any] = config.get("controls") or {}
        for entry in controls.get("compare_by") or []:
            if entry not in allowed_grouping:
                raise DisallowedGroupingError(f"Disallowed grouping: {entry!r}")
        dcb = config.get("default_compare_by")
        if dcb is not None and dcb not in allowed_grouping:
            raise DisallowedGroupingError(f"Disallowed default_compare_by: {dcb!r}")

    # 7. Whisker-vs-CI and baseline-band checks.
    ensure_whisker_is_not_ci(config)
    ensure_no_calculated_baseline_band(config)

    # 8. CI method allow-list.
    ci: Mapping[str, Any] = config.get("ci") or {}
    if ci.get("enabled") and ci_methods is not None:
        method: str = ci.get("method") or "wilson"
        if method not in ci_methods:
            raise CiOnUnknownMethodError(f"Unknown CI method: {method!r}")


def build_ci_render_keys(
    config: Mapping[str, Any],
    *,
    lower: Any = None,
    upper: Any = None,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Build the CI render keys for the emitted widget data."""
    ci: Any = config.get("ci")
    if not isinstance(ci, Mapping) or not ci.get("enabled"):
        return {}

    # Validate bounds.
    if (
        not _is_number(lower) or not _is_number(upper)
        or (isinstance(lower, float) and not math.isfinite(lower))
        or (isinstance(upper, float) and not math.isfinite(upper))
        or not (0 <= float(lower) <= float(upper) <= 1)
    ):
        raise DisplayValidationError(
            "CI bounds must be finite and ordered: 0 <= lower <= upper <= 1."
        )

    method: str = str(ci.get("method") or "wilson")
    return {
        "ci": {
            "enabled": True,
            "method": method,
            "confidence_level": float(confidence_level),
        },
        "lower": float(lower),
        "upper": float(upper),
    }


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # CI-off empty-dict contract.
    assert build_ci_render_keys({}) == {}
    assert build_ci_render_keys({"ci": {"enabled": False}}) == {}

    # Whisker-vs-CI conflation.
    try:
        ensure_whisker_is_not_ci({
            "type": "boxplot",
            "options": {"whiskers": "tukey", "whisker_is_ci": True},
            "ci": {"enabled": True},
        })
        assert False, "expected WhiskerIsConfidenceIntervalError"
    except WhiskerIsConfidenceIntervalError:
        pass

    # Calculated baseline.
    try:
        ensure_no_calculated_baseline_band({"baseline": {"computed": True}})
        assert False, "expected CalculatedBaselineBandError"
    except CalculatedBaselineBandError:
        pass

    # Numeric benchmark on non-numeric chart.
    try:
        validate_numeric_benchmark(
            {"type": "pie", "benchmarks": [{"value": 1, "unit": "%"}]},
        )
        assert False, "expected NumericBenchmarkOnNonNumericChartError"
    except NumericBenchmarkOnNonNumericChartError:
        pass

    # Unknown bucket.
    try:
        validate_display({"bucket": "fortnight"})
        assert False, "expected UnknownBucketError"
    except UnknownBucketError:
        pass

    # Unknown widget input.
    try:
        validate_display({"query": {"inputs": {"x": "y"}}}, allowed_inputs=frozenset({"a"}))
        assert False, "expected UnknownWidgetInputError"
    except UnknownWidgetInputError:
        pass

    # Build CI render keys.
    keys = build_ci_render_keys({"ci": {"enabled": True, "method": "normal"}}, lower=0.1, upper=0.9)
    assert set(keys.keys()) == {"ci", "lower", "upper"}
    assert keys["ci"]["method"] == "normal"

    print("validation self-check ok")
