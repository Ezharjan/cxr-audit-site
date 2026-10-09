# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Resolve card-local Lunit cutoffs without changing registered policies or stored labels."""
from dataclasses import replace
from math import isfinite
from collections.abc import Mapping


def configure_policy(policy, settings=None):
    if settings is None:
        return policy
    if not isinstance(settings, Mapping) or not settings or set(settings) - {"findings", "default", "overrides"}:
        raise ValueError("Choose valid Lunit threshold settings.")
    findings = settings.get("findings", list(policy.findings))
    if (not isinstance(findings, list) or not findings or any(not isinstance(f, str) for f in findings)
            or len(set(findings)) != len(findings) or set(findings) - set(policy.findings)):
        raise ValueError("Choose at least one registered Lunit finding, without duplicates.")
    overrides = settings.get("overrides", {})
    if not isinstance(overrides, Mapping) or set(overrides) - set(findings):
        raise ValueError("Threshold overrides must name included Lunit findings.")
    lo, hi = policy.score_scale
    for value in ([settings["default"]] if "default" in settings else []) + list(overrides.values()):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"Lunit cutoffs must be finite numbers from {lo:g} to {hi:g}.")
    return replace(policy, findings={f: overrides.get(f, settings.get("default", policy.findings[f])) for f in findings})


def widget_policy(query, project):
    settings = query.get("lunit_thresholds")
    if settings is not None and "lunit_findings" not in query.get("inputs", {}).values():
        raise ValueError("Custom cutoffs require the Lunit findings (policy-derived) data source.")
    ref = query.get("threshold_policy")
    if not ref:
        raise ValueError("Choose a threshold policy for Lunit findings.")
    return configure_policy(project.policy(ref), settings)


def threshold_metadata(query, project):
    if "lunit_findings" not in query.get("inputs", {}).values():
        return None
    policy = widget_policy(query, project)
    return {"policy": policy.ref, "operator": ">", "aggregate": policy.aggregate,
            "require_all_scores": policy.require_all_scores, "findings": dict(policy.findings)}


def threshold_caption(metadata):
    return "Lunit positive if any included finding exceeds its cutoff: " + "; ".join(
        f"{finding.replace('_', ' ')} > {value:g}" for finding, value in metadata["findings"].items()) + "."
