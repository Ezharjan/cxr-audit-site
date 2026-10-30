"""Real LLM smoke test for the local profile, using the application's strict pipeline.

Sends only synthetic report text from docs/test-data/uploader/ris-valid.csv to the
configured loopback OpenAI-compatible endpoint (local Ollama). It does not mock
anything; if the endpoint identifies itself as the repository's mock server the
result is labelled MOCK and must not be reported as real inference.
"""
import argparse
import contextlib
import csv
import io
import ipaddress
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
API_DIR = REPO / "cxr-audit-api"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(API_DIR / "cxr_audit"))


def loopback(url):
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_label(base_url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url.rstrip("/") + "/models", timeout=5) as response:
            owners = {m.get("owned_by") for m in json.loads(response.read()).get("data", [])}
    except Exception:
        return "UNREACHABLE"
    return "MOCK (not real inference)" if "primer-mock-llm" in owners else "REAL (local endpoint)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=int, default=3)
    args = parser.parse_args()
    base_url = os.environ.get("OLLAMA_BASE_URL", "")
    model = os.environ.get("OLLAMA_MODEL", "")
    if not loopback(base_url):
        print("Refusing: OLLAMA_BASE_URL is not a loopback address")
        return 2
    label = endpoint_label(base_url)
    print(f"Endpoint: {base_url}  model: {model}  workers: {os.environ.get('OLLAMA_MAX_WORKERS')}  -> {label}")
    if label == "UNREACHABLE":
        print("FAIL: endpoint unreachable. Start Ollama and run: ollama pull " + model)
        return 1

    os.chdir(API_DIR)
    import pandas as pd
    import class_process_carpl  # builds the shared processor from the same env as the API

    with open(REPO / "docs/test-data/uploader/ris-valid.csv", encoding="utf-8") as handle:
        reports = [row["TEXT_REPORT"] for row in csv.DictReader(handle) if row["TEXT_REPORT"].strip()]
    reports = reports[:max(1, min(args.reports, len(reports)))]
    df = pd.DataFrame({"TEXT_REPORT": reports})

    captured = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(captured):
            graded = class_process_carpl.processor.process_full_pipeline(
                df, report_column="TEXT_REPORT", steps=["llm", "lunit"], save_intermediate=False, strict=True)
    except Exception as exc:
        log = captured.getvalue()
        print(f"FAIL: strict pipeline rejected the model output ({type(exc).__name__}: {exc})")
        print(f"  structured-output fallbacks: {log.count('Structured output unavailable')}, "
              f"repair requests: {log.count('requesting one repair')}")
        print("  Hints: run `ollama pull " + model + "`; try LLM_DISABLE_THINKING=True in local.env; "
              "update Ollama if structured outputs are unsupported.")
        return 1
    elapsed = time.monotonic() - started
    log = captured.getvalue()
    findings = [c for c in graded.columns if c.endswith("_llm")] + ["tb"]
    for index, text in enumerate(reports):
        positives = [c for c in findings if bool(graded.at[index, c])]
        print(f"  report {index + 1}: grade={int(graded.at[index, 'llm_grade'])}  findings={positives or 'none'}  "
              f"| {text[:70]}")
    print(f"PASS ({label}): {len(reports)} synthetic reports, strict schema valid, "
          f"{elapsed:.1f}s total, {elapsed / len(reports):.1f}s/report")
    print(f"  structured-output fallbacks: {log.count('Structured output unavailable')}  "
          f"repair requests: {log.count('requesting one repair')}")
    print("  Grades are from a small local model on synthetic text; they are not a clinical accuracy measure.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
