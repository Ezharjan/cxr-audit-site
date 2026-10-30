"""Synthetic end-to-end check against the running local services.

Flow: login -> service/LLM connection checks -> invalid-input rejections ->
precheck (CARPL CSV + RIS CSV, and RIS XLSX) -> submit -> FastAPI grading via
the configured LLM -> results saved -> viewer list/detail -> viewer CSV export
-> report pages and report CSV exports.

Only repository synthetic files are uploaded. The password is read from the
PRIMER_E2E_PASSWORD environment variable or prompted; it is never a CLI argument.
The script states which LLM endpoint answered; a mock endpoint is labelled MOCK.
"""
import argparse
import csv
import getpass
import io
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "docs" / "test-data" / "uploader"
IDS = [991011000000 + n for n in range(1, 7)]
results = []


def check(name, passed, detail=""):
    results.append((name, bool(passed)))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    return passed


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False  # never route loopback traffic through a proxy

    def url(self, path):
        return self.base + path

    def get(self, path, **kwargs):
        return self.session.get(self.url(path), timeout=60, **kwargs)

    def post(self, path, **kwargs):
        headers = {"X-CSRFToken": self.session.cookies.get("csrftoken", ""), "Referer": self.url("/upload/")}
        return self.session.post(self.url(path), headers=headers, timeout=120, **kwargs)


def files(*names):
    return [("files", (name, (DATA / name).read_bytes())) for name in names]


def login(client, username, password):
    client.get("/login/")
    response = client.session.post(client.url("/login/"), data={
        "username": username, "password": password,
        "csrfmiddlewaretoken": client.session.cookies.get("csrftoken", ""),
    }, headers={"Referer": client.url("/login/")}, allow_redirects=False, timeout=60)
    page = client.get("/upload/", allow_redirects=False)
    return response.status_code == 302 and page.status_code == 200


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default=os.environ.get("PRIMER_E2E_USER"))
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait for LLM grading")
    args = parser.parse_args()
    username = args.username or input("管理员用户名: ").strip()
    password = os.environ.get("PRIMER_E2E_PASSWORD") or getpass.getpass("管理员密码（不回显）: ")
    client = Client(args.base_url)

    print("== 登录与连接 ==")
    if not check("superuser login", login(client, username, password)):
        return 1
    api = client.get("/upload/api/check-connection")
    check("Django -> FastAPI (authenticated)", api.status_code == 200 and api.json().get("connected"), api.status_code)
    llm = client.get("/upload/api/check-llm")
    llm_info = llm.json() if llm.headers.get("content-type", "").startswith("application/json") else {}
    check("FastAPI -> LLM endpoint", llm.status_code == 200 and llm_info.get("connected"), llm_info.get("llm_url"))
    mock = False
    try:
        models = requests.Session()
        models.trust_env = False
        owners = {m.get("owned_by") for m in models.get(str(llm_info.get("llm_url", "")).rstrip("/") + "/models", timeout=5).json().get("data", [])}
        mock = "primer-mock-llm" in owners
    except Exception:
        pass
    label = "MOCK LLM (application flow only, not real inference)" if mock else "REAL local LLM endpoint"
    print(f"  LLM endpoint type: {label}")

    print("== 输入校验仍然生效 ==")
    # Current behaviour: invalid rows are excluded and counted with an error code,
    # valid studies in the same upload continue. Previews are chained via
    # previous_preview so the per-user ready-preview limit (3) is never hit.
    state = {"preview": ""}

    def precheck(names, **data):
        if state["preview"]:
            data["previous_preview"] = state["preview"]
        response = client.post("/upload/api/precheck", files=files(*names), data=data)
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code == 200:
            state["preview"] = body.get("preview_id", "")
        return response, body

    def codes(body):
        return {(e.get("field"), e.get("code")) for e in body.get("errors", [])}

    _, body = precheck(["carpl-out-of-range.csv"])
    check("score > 100 excluded with invalid_value", body.get("new_complete") == 0 and body.get("invalid_studies") == 1
          and ("pneumothorax", "invalid_value") in codes(body), body.get("errors"))
    _, body = precheck(["ris-invalid-id.csv"])
    check("fractional identifier excluded", body.get("total") == 0 and body.get("invalid_raw_rows") == 1
          and ("accession_no", "invalid_value") in codes(body), body.get("errors"))
    _, body = precheck(["carpl-valid.csv", "ris-conflict.csv"])
    kept = body.get("new_complete", 0) + body.get("existing_unchanged", 0) + body.get("existing_enrichable", 0)
    check("conflicting report excluded, other 5 kept", kept == 5 and body.get("invalid_studies") == 1
          and ("text_report", "conflicting_uploaded_values") in codes(body), body.get("new_complete"))
    bad_type = client.post("/upload/api/precheck", files=[("files", ("synthetic.txt", b"a\n1\n"))])
    check("unsupported file type rejected", bad_type.status_code == 400, bad_type.status_code)
    oversize = client.post("/upload/api/precheck", files=[("files", ("big.csv", b"a\n" + b"1\n" * (25 * 1024 * 1024)))])
    check("upload over size limit rejected", oversize.status_code in (400, 413), oversize.status_code)
    no_csrf = requests.post(client.url("/upload/api/precheck"), cookies=client.session.cookies,
                            files=files("carpl-valid.csv"), timeout=60)
    check("POST without CSRF token rejected", no_csrf.status_code == 403, no_csrf.status_code)

    print("== 预检（CARPL + RIS） ==")
    xlsx, xlsx_body = precheck(["carpl-valid.csv", "ris-valid.xlsx"])
    if xlsx.status_code != 200:
        xlsx_body = {"error": xlsx.text[:200]}
    precheck_response, pre = precheck(["carpl-valid.csv", "ris-valid.csv"], supplemental_steps="true")
    if precheck_response.status_code != 200:
        pre = {"error": precheck_response.text[:200]}
    already = pre.get("new_complete") == 0 and pre.get("existing_unchanged", 0) + pre.get("existing_enrichable", 0) >= 6
    if already:
        if os.environ.get("PRIMER_E2E_KNOWN_XLSX_LIMITATION") and xlsx.status_code != 200:
            print("  KNOWN-CI-LIMITATION  CARPL CSV + RIS XLSX precheck not verified on this CI kernel")
        else:
            check("CARPL CSV + RIS XLSX precheck parsed", xlsx.status_code == 200 and not xlsx_body.get("blocked"), xlsx.status_code)
        print("  synthetic studies already stored from an earlier run; skipping new grading")
    else:
        if os.environ.get("PRIMER_E2E_KNOWN_XLSX_LIMITATION") and xlsx_body.get("new_complete") != 6:
            # Only set by the Linux cloud harness, whose kernel Landlock policy blocks
            # /etc/mime.types for the Linux parser. Reported, never counted as a pass.
            print("  KNOWN-CI-LIMITATION  CARPL CSV + RIS XLSX precheck not verified on this CI kernel")
        else:
            check("CARPL CSV + RIS XLSX precheck: 6 complete", xlsx_body.get("new_complete") == 6, xlsx_body.get("new_complete", xlsx_body.get("error")))
        if not check("CARPL CSV + RIS CSV precheck: 6 complete", pre.get("new_complete") == 6 and pre.get("preview_id"),
                     pre.get("new_complete", pre.get("error"))):
            return summary(label)

        print(f"== 提交并分级（{label}） ==")
        submitted = client.post("/upload/api/analyze-auto-sort", data={"preview_id": pre["preview_id"]})
        task_id = submitted.json().get("task_id") if submitted.status_code == 200 else None
        if not check("submission accepted", task_id, submitted.status_code if not task_id else task_id):
            print("   ", submitted.text[:300])
            return summary(label)
        started, status, last = time.monotonic(), None, None
        while time.monotonic() - started < args.timeout:
            state = client.get(f"/upload/api/status/{task_id}")
            data = state.json() if state.status_code == 200 else {}
            status = data.get("status")
            progress = f"{data.get('progress_percent')}% {data.get('progress', '')}"
            if progress != last:
                print(f"    {int(time.monotonic() - started):>4}s  {status}  {progress}", flush=True)
                last = progress
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(3)
        elapsed = time.monotonic() - started
        check("grading completed and results saved", status == "completed", f"{status} after {elapsed:.0f}s")
        saved = client.get(f"/upload/api/task-results/{task_id}")
        saved_body = saved.json() if saved.status_code == 200 else {}
        check("6 new studies stored", saved_body.get("new_records_added") == 6, saved_body.get("new_records_added"))

    print("== 数据查看与导出 ==")
    viewer = client.get("/view/")
    check("viewer page", viewer.status_code == 200 and str(IDS[0]) in viewer.text, viewer.status_code)
    detail = client.get(f"/view/study/{IDS[1]}/")
    check("study detail page", detail.status_code == 200, detail.status_code)
    export = client.get("/view/export/", params={"q": "99101100000"})
    rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig")))) if export.status_code == 200 else []
    keys = {k.lower(): k for k in (rows[0].keys() if rows else [])}
    acc_key = next((keys[k] for k in keys if "accession" in k), None)
    grade_key = next((keys[k] for k in keys if k in ("llm_grade", "llm grade")), None)
    exported = {int(float(r[acc_key])) for r in rows if acc_key and r[acc_key]}
    check("viewer CSV export has the 6 synthetic studies", set(IDS) <= exported, f"{len(rows)} rows")
    grades = [r.get(grade_key) for r in rows if grade_key and acc_key and r[acc_key] and int(float(r[acc_key])) in IDS]
    check("exported LLM grades are 1-6", grades and all(g and 1 <= int(float(g)) <= 6 for g in grades), grades)

    print("== 报表 ==")
    index = client.get("/report/?list=1")
    check("report list page", index.status_code == 200, index.status_code)
    page = client.get("/report/overview/")
    check("overview report page", page.status_code == 200, page.status_code)
    # CSV exports are widget-scoped and need the signed context token rendered on the page.
    import html
    import re
    widgets = []
    for tag in re.findall(r"<article[^>]*data-widget-id=[^>]*>", page.text):
        attrs = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', tag))
        if attrs.get("context-token"):
            widgets.append({k: html.unescape(v) for k, v in attrs.items()})
    check("report widgets rendered with signed context", widgets, f"{len(widgets)} widgets")
    discrepancy = next((w for w in widgets if w.get("measurement") in ("false_negatives", "false_positives",
                        "classification_summary", "reference_agreement", "paired_reference_comparison")), None)
    for kind in ("full", "false_negatives", "false_positives"):
        widget = widgets[0] if kind == "full" else discrepancy
        if not widget:
            check(f"report CSV export: {kind}", False, "no suitable widget on the overview layout")
            continue
        response = client.get(f"/report/overview/csv/{kind}/", params={"widget": widget["widget-id"], "context": widget["context-token"]})
        is_csv = response.status_code == 200 and "csv" in response.headers.get("content-type", "")
        check(f"report CSV export: {kind}", is_csv, f"{response.status_code} {response.headers.get('content-type', '')}")
    legacy = client.get("/report-old/generate/", params={"date_from": "2026-01-01", "date_to": "2026-12-31"})
    check("legacy report generation", legacy.status_code == 200, legacy.status_code)
    legacy_csv = client.get("/report-old/export-csv/", params={"date_from": "2026-01-01", "date_to": "2026-12-31"})
    check("legacy report CSV export", legacy_csv.status_code == 200 and str(IDS[0]) in legacy_csv.text, legacy_csv.status_code)
    return summary(label)


def summary(label):
    failed = [name for name, passed in results if not passed]
    print("")
    print(f"结果：{len(results) - len(failed)}/{len(results)} 通过  （LLM: {label}）")
    for name in failed:
        print(f"  未通过: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
