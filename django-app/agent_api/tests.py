import hashlib
import json
import smtplib
import tempfile
import uuid
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, Mock

from django.contrib.auth.models import User, Group
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, Client, RequestFactory, override_settings
from django.utils import timezone
from audit.models import AuditEvent
from report_v2 import data
from report_v2.exports import default_document
from report_v2.evaluation import EvaluationError
from report_v2.tests.test_pages import LAYOUT, _make_seam
from upload.models import ProcessingTask, CXRStudy, SubmissionReservation
from upload.submission import reserve_submission, release_submission, SubmissionError, persist_result

from .auth import create_key, APIError
from .models import APIConfiguration, APIKey, APIKeyProject, APIJob, IdempotencyRecord, PDFReservation
from .pdf import report_pdf, render_pdf


@override_settings(DEBUG=True)
class APITests(TestCase):
    databases = {"default", "audit"}

    def setUp(self):
        self.owner = User.objects.create_superuser("api-admin", "admin@example.invalid", "synthetic-password")
        self.other = User.objects.create_superuser("other-admin", "other@example.invalid", "synthetic-password")
        self.normal = User.objects.create_user("normal", password="synthetic-password")
        self.key, self.secret = create_key(self.owner, "Synthetic agent", timezone.now() + timedelta(days=90), ["prime"])
        self.headers = {"X-API-Key": self.secret}
        APIConfiguration.objects.create(enabled=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        override = override_settings(REPORT_V2_ROOT=self.temp.name)
        override.enable()
        self.addCleanup(override.disable)

    def upload(self, value="upload-one", content=b"SYNTH,CSV\n1,2\n", **kwargs):
        return self.client.post("/api/prime/jobs", {"files": [SimpleUploadedFile("synthetic.csv", content, "text/csv")], **kwargs},
                                headers={**self.headers, "Idempotency-Key": value})

    def email(self, value="email-one", body=None):
        return self.client.post("/api/prime/reports/t13report/email", json.dumps(body or {"recipients": ["auditor@example.invalid"]}),
            content_type="application/json", headers={**self.headers, "Idempotency-Key": value})

    def publish(self):
        client = Client()
        client.force_login(self.owner)
        result = client.post("/report/layout/editor/publish/", {"def_id": "t13report", "yaml_text": LAYOUT})
        self.assertEqual(result.status_code, 200, result.content[:200])
        return result.json()["version"]

    def test_all_endpoints_disabled_and_session_is_not_api_auth(self):
        routes = [("post", "/api/prime/jobs"), ("get", f"/api/prime/jobs/{uuid.uuid4()}"),
                  ("get", "/api/prime/reports"), ("get", "/api/prime/reports/t13report/pdf"),
                  ("post", "/api/prime/reports/t13report/email")]
        self.client.force_login(self.owner)
        APIConfiguration.objects.update(enabled=False)
        for method, url in routes:
            response = getattr(self.client, method)(url, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response["Cache-Control"], "no-store")
        APIConfiguration.objects.update(enabled=True)
        for method, url in routes:
            self.assertEqual(getattr(self.client, method)(url).status_code, 401)
        APIConfiguration.objects.all().delete()
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 503)

    def test_key_expiration_revocation_owner_demotion_and_hash(self):
        self.assertNotIn(self.secret, self.key.secret_digest)
        for raw in ("bad", self.secret.split(".")[0], "x" * 200):
            self.assertEqual(self.client.get("/api/prime/reports", headers={"X-API-Key": raw}).status_code, 401)
        for field, value in (("revoked_at", timezone.now()), ("expires_at", timezone.now() - timedelta(seconds=1))):
            old = getattr(self.key, field)
            APIKey.objects.filter(pk=self.key.pk).update(**{field: value})
            self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 401)
            APIKey.objects.filter(pk=self.key.pk).update(**{field: old})
        self.owner.is_superuser = False
        self.owner.save()
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 401)
        admins = Group.objects.create(name="admins")
        self.owner.groups.add(admins)
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 200)
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 401)
        events = str(list(AuditEvent.objects.values("action", "metadata")))
        self.assertNotIn(self.secret, events)

    def test_project_scope_and_no_overrides(self):
        self.key.projects.all().delete()
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get("/api/second/reports", headers=self.headers).status_code, 404)
        APIKeyProject.objects.create(key=self.key, project_id="prime")
        with patch("report_v2.data.fetch_project_rows") as rows:
            self.assertEqual(self.client.get("/api/prime/reports/t13report/pdf?date_from=2026-01-01", headers=self.headers).status_code, 400)
            self.assertEqual(self.email(body={"recipients": ["a@example.invalid"], "filters": {}}).status_code, 400)
            rows.assert_not_called()
        self.assertEqual(self.client.post("/api/prime/reports", headers=self.headers).status_code, 405)

    def test_admin_config_csrf_own_keys_and_show_once(self):
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.owner)
        self.assertEqual(secure_client.post("/settings/api/", {"action": "toggle", "confirmed": "yes"}).status_code, 403)
        self.client.force_login(self.normal)
        self.assertEqual(self.client.get("/settings/api/").status_code, 403)
        self.assertEqual(self.client.post("/settings/api/", {"action": "toggle"}).status_code, 403)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/settings/api/"), self.key.public_id)
        self.assertEqual(self.client.post("/settings/api/", {"action": "revoke", "confirmed": "yes", "key_id": self.key.pk}).status_code, 404)
        self.client.force_login(self.owner)
        page = self.client.get("/settings/api/")
        body = {"action": "create", "creation_token": page.context["creation_token"], "label": "New key", "projects": ["prime"],
                "expires": (timezone.localdate() + timedelta(days=30)).isoformat()}
        response = self.client.post("/settings/api/", body)
        self.assertEqual(response.status_code, 200)
        raw = response.context["secret"]
        self.assertTrue(raw.startswith("primer_"))
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.client.post("/settings/api/", body).status_code, 302)
        self.assertNotContains(self.client.get("/settings/api/"), raw)
        self.assertEqual(APIKey.objects.filter(owner=self.owner).count(), 2)
        for projects in (["second"], []):
            body["creation_token"] = self.client.get("/settings/api/").context["creation_token"]
            body["projects"] = projects
            self.assertEqual(self.client.post("/settings/api/", body).status_code, 400)
        self.assertEqual(self.client.post("/settings/api/", {"action": "toggle", "enabled": "false", "confirmed": "yes"}).status_code, 302)
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 503)
        self.client.post("/settings/api/", {"action": "toggle", "enabled": "true", "confirmed": "yes"})
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 200)

    @patch("upload.views._start_background_worker")
    @patch("upload.submission.requests.post")
    def test_upload_replay_validation_persistence_and_job_ownership(self, post, worker):
        task_id = str(uuid.uuid4())
        post.return_value = Mock(status_code=200, json=lambda: {"task_id": task_id})
        result = self.upload()
        self.assertEqual(result.status_code, 202, result.content)
        job = APIJob.objects.get(pk=result.json()["job_id"])
        self.assertEqual(self.upload().json(), result.json())
        self.assertEqual(post.call_count, 1)
        self.assertNotEqual(post.call_args.kwargs["headers"].get("X-API-Key"), self.secret)
        self.assertEqual(self.upload(content=b"different").status_code, 409)
        self.assertEqual(self.upload("bad", supplemental_steps="yes").status_code, 400)
        self.assertEqual(self.upload("empty", content=b"").status_code, 400)
        self.assertEqual(self.upload("busy").status_code, 409)
        with patch("agent_api.views.requests.get", return_value=Mock(status_code=200)):
            self.assertEqual(self.client.get(result.json()["status_url"], headers=self.headers).json()["status"], "queued")
        other_key, other_secret = create_key(self.other, "Other", timezone.now() + timedelta(days=1), ["prime"])
        self.assertEqual(self.client.get(result.json()["status_url"], headers={"X-API-Key": other_secret}).status_code, 404)
        replacement, raw = create_key(self.owner, "Replacement", timezone.now() + timedelta(days=1), ["prime"])
        with patch("agent_api.views.requests.get", return_value=Mock(status_code=404)):
            self.assertEqual(self.client.get(result.json()["status_url"], headers={"X-API-Key": raw}).json()["status"], "failed")
        job.task.refresh_from_db()
        job.task.status = "processing"
        job.task.save()
        result_csv = {"csv_data": "ACCESSION_NO,ABNORMAL\n990000001,42\n"}
        self.assertEqual(persist_result(task_id, result_csv), (1, 0))
        self.assertEqual(persist_result(task_id, result_csv), (1, 0))
        self.assertEqual(CXRStudy.objects.count(), 1)
        self.assertEqual(self.client.get(result.json()["status_url"], headers=self.headers).json()["status"], "completed")

    @patch("upload.views._start_background_worker")
    @patch("upload.submission.requests.post")
    def test_ambiguous_submission_stays_blocked(self, post, worker):
        import requests
        post.side_effect = requests.Timeout("synthetic")
        self.assertEqual(self.upload().status_code, 504)
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(IdempotencyRecord.objects.get().state, "unknown")
        with self.assertRaises(SubmissionError):
            reserve_submission()
        self.assertEqual(post.call_count, 1)

    def test_processing_reservation_shared_with_manual_and_terminal_release(self):
        token = reserve_submission()
        with self.assertRaises(SubmissionError):
            reserve_submission()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post("/upload/api/analyze-auto-sort", {"files": SimpleUploadedFile("synthetic.csv", b"x")}).status_code, 409)
        task = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="failed")
        SubmissionReservation.objects.update(task=task)
        second = reserve_submission()
        self.assertNotEqual(second, token)
        release_submission(second)

    def test_cancelled_local_task_keeps_reservation_until_backend_stops(self):
        task = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="cancelled")
        SubmissionReservation.objects.create(pk=1, token=uuid.uuid4(), task=task)
        with patch("upload.submission.requests.get", return_value=Mock(status_code=200, json=lambda: {"status": "processing"})):
            with self.assertRaises(SubmissionError):
                reserve_submission()
        with patch("upload.submission.requests.get", return_value=Mock(status_code=200, json=lambda: {"status": "completed"})):
            token = reserve_submission()
        release_submission(token)

    def test_audit_failure_blocks_upload_before_dispatch(self):
        from django.db import DatabaseError
        with patch("agent_api.views.record", side_effect=DatabaseError("synthetic")), patch("upload.submission.requests.post") as post:
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        post.assert_not_called()
        self.assertIsNone(SubmissionReservation.objects.get(pk=1).token)

    def test_missing_csv_does_not_mark_api_job_complete(self):
        task = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="processing")
        APIJob.objects.create(task=task, project_id="prime", owner=self.owner, creator_key=self.key)
        with self.assertRaises(ValueError):
            persist_result(task.pk, {})
        task.refresh_from_db()
        self.assertIsNone(task.results_saved_at)
        self.assertEqual(task.status, "processing")

    def test_default_snapshot_and_pdf_ignore_preferences_and_fail_on_widget_error(self):
        version = self.publish()
        seam = _make_seam()
        with patch("report_v2.data.fetch_project_rows", side_effect=seam):
            document = default_document("prime", "t13report")
        self.assertEqual(document["version"], version)
        self.assertTrue(all(w["applied"]["filters"] == {} for w in document["widgets"]))
        with patch("report_v2.data.fetch_project_rows", side_effect=_make_seam(error_ids=["v1"])):
            with self.assertRaises(EvaluationError):
                default_document("prime", "t13report")
        with patch("report_v2.data.fetch_project_rows", side_effect=seam), patch("agent_api.pdf.render_pdf", return_value=b"%PDF-synthetic") as renderer:
            response = self.client.get("/api/prime/reports/t13report/pdf", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertNotIn("window.print", renderer.call_args.args[0])
            self.assertIn("Routine report", renderer.call_args.args[0])
        PDFReservation.objects.update(token=uuid.uuid4(), expires_at=timezone.now() + timedelta(seconds=90))
        self.assertEqual(self.client.get("/api/prime/reports/t13report/pdf", headers=self.headers).status_code, 429)

    def test_report_list_and_missing_pdf(self):
        self.publish()
        result = self.client.get("/api/prime/reports", headers=self.headers)
        self.assertEqual([r["slug"] for r in result.json()["reports"]], ["t13report"])
        self.assertEqual(self.client.get("/api/prime/reports/missing/pdf", headers=self.headers).status_code, 404)

    @patch("agent_api.views.report_pdf", return_value=b"%PDF-synthetic")
    def test_email_pdf_bcc_retries_validation_and_disable_before_send(self, renderer):
        response = self.email()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "accepted_by_mail_server")
        self.assertEqual(mail.outbox[0].bcc, ["auditor@example.invalid"])
        self.assertEqual(mail.outbox[0].attachments[0][1], b"%PDF-synthetic")
        self.assertEqual(mail.outbox[0].attachments[0][2], "application/pdf")
        self.assertEqual(self.email().json(), response.json())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.email(body={"recipients": ["bad"]}).status_code, 400)
        self.assertEqual(self.email(body={"recipients": ["other@example.invalid"]}).status_code, 409)
        def disable(*args):
            APIConfiguration.objects.update(enabled=False)
            return b"%PDF-synthetic"
        renderer.side_effect = disable
        self.assertEqual(self.email("disabled").status_code, 503)
        self.assertEqual(len(mail.outbox), 1)

    @patch("agent_api.views.report_pdf", return_value=b"%PDF-synthetic")
    @patch("django.core.mail.message.EmailMessage.send", side_effect=smtplib.SMTPException("sensitive SMTP diagnostic"))
    def test_email_ambiguous_send_does_not_retry(self, send, renderer):
        response = self.email()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(b"sensitive", response.content)
        self.assertEqual(self.email().status_code, 409)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("auditor@example.invalid", str(list(IdempotencyRecord.objects.values())))

    def test_singleton_is_database_enforced(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            APIConfiguration.objects.create(pk=2)

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False)
    def test_production_transport_rejects_http_and_accepts_https(self):
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers, secure=True).status_code, 200)

    def test_oversized_file_unknown_endpoint_and_revocation_during_email(self):
        with patch("agent_api.views.MAX_UPLOAD_BYTES", 5):
            self.assertEqual(self.upload().status_code, 413)
        self.assertEqual(self.client.get("/api/prime/jobs/not-a-uuid", headers=self.headers).status_code, 404)
        def revoke(*args):
            APIKey.objects.filter(pk=self.key.pk).update(revoked_at=timezone.now())
            return b"%PDF-synthetic"
        with patch("agent_api.views.report_pdf", side_effect=revoke):
            self.assertEqual(self.email().status_code, 401)
        self.assertEqual(len(getattr(mail, "outbox", [])), 0)

    @patch("upload.views._start_background_worker")
    @patch("upload.submission.requests.post")
    def test_complete_routine_journey_with_real_pdf(self, post, worker):
        APIConfiguration.objects.update(enabled=False)
        self.client.force_login(self.owner)
        page = self.client.get("/settings/api/")
        created = self.client.post("/settings/api/", {"action": "create", "creation_token": page.context["creation_token"],
            "label": "Journey", "projects": ["prime"], "expires": (timezone.localdate() + timedelta(days=1)).isoformat()})
        self.headers = {"X-API-Key": created.context["secret"]}
        self.client.post("/settings/api/", {"action": "toggle", "enabled": "true", "confirmed": "yes"})
        task_id = str(uuid.uuid4())
        post.return_value = Mock(status_code=200, json=lambda: {"task_id": task_id})
        upload = self.upload()
        self.assertEqual(upload.status_code, 202)
        persist_result(task_id, {"csv_data": "ACCESSION_NO,ABNORMAL\n990000002,55\n"})
        self.assertEqual(self.client.get(upload.json()["status_url"], headers=self.headers).json()["status"], "completed")
        self.publish()
        self.assertEqual(len(self.client.get("/api/prime/reports", headers=self.headers).json()["reports"]), 1)
        with patch("report_v2.data.fetch_project_rows", side_effect=_make_seam()):
            pdf = self.client.get("/api/prime/reports/t13report/pdf", headers=self.headers)
            self.assertEqual(pdf.status_code, 200, pdf.content[:100])
            self.assertTrue(pdf.content.startswith(b"%PDF-"))
            emailed = self.email()
            self.assertEqual(emailed.status_code, 200, emailed.content)
        self.assertTrue(mail.outbox[0].attachments[0][1].startswith(b"%PDF-"))
        public_id = self.headers["X-API-Key"][7:].split(".")[0]
        key = APIKey.objects.get(public_id=public_id)
        self.client.post("/settings/api/", {"action": "revoke", "key_id": key.pk, "confirmed": "yes"})
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 401)
        self.client.post("/settings/api/", {"action": "toggle", "enabled": "false", "confirmed": "yes"})
        self.assertEqual(self.client.get("/api/prime/reports", headers=self.headers).status_code, 503)

    def test_never_expiry_creation_access_and_revocation(self):
        self.client.force_login(self.owner)
        page = self.client.get("/settings/api/")
        created = self.client.post("/settings/api/", {"action": "create", "creation_token": page.context["creation_token"], "label": "Never expires", "projects": ["prime"], "expiry_mode": "never"})
        self.assertEqual(created.status_code, 200)
        key = APIKey.objects.get(label="Never expires")
        self.assertIsNone(key.expires_at)
        headers = {"X-API-Key": created.context["secret"]}
        self.assertEqual(self.client.get("/api/prime/reports", headers=headers).status_code, 200)
        self.client.post("/settings/api/", {"action": "revoke", "key_id": key.pk, "confirmed": "yes"})
        self.assertEqual(self.client.get("/api/prime/reports", headers=headers).status_code, 401)

    def test_expiry_mode_requires_explicit_never_or_valid_date(self):
        self.client.force_login(self.owner)
        for mode, expiry in (("date", ""), ("date", "2000-01-01"), ("unknown", "")):
            page = self.client.get("/settings/api/")
            result = self.client.post("/settings/api/", {"action": "create", "creation_token": page.context["creation_token"], "label": "Invalid expiry", "projects": ["prime"], "expiry_mode": mode, "expires": expiry})
            self.assertEqual(result.status_code, 400)
        self.assertFalse(APIKey.objects.filter(label="Invalid expiry").exists())

    def test_job_backend_missing_cannot_overwrite_concurrent_completion(self):
        task = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="processing")
        job = APIJob.objects.create(task=task, owner=self.owner, creator_key=self.key, project_id="prime")
        def complete(*args, **kwargs):
            ProcessingTask.objects.filter(pk=task.pk).update(status="completed", results_saved_at=timezone.now(), csv_data="SYNTH")
            return Mock(status_code=404)
        with patch("agent_api.views.requests.get", side_effect=complete):
            result = self.client.get(f"/api/prime/jobs/{job.pk}", headers=self.headers)
        self.assertEqual(result.json()["status"], "completed")
        task.refresh_from_db()
        self.assertEqual(task.csv_data, "SYNTH")

    def test_finished_worker_slot_does_not_block_next_job_monitor(self):
        from upload.views import _start_background_worker
        old = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="completed")
        new = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="queued")
        with patch("upload.views._active_worker_task_id", old.pk), patch("upload.views.threading.Thread") as thread:
            _start_background_worker(new.pk)
            thread.return_value.start.assert_called_once()

    def test_stale_poll_cannot_erase_saved_results(self):
        from upload.views import _update_task_progress
        task = ProcessingTask.objects.create(task_id=str(uuid.uuid4()), status="completed", results_saved_at=timezone.now(), csv_data="SYNTH")
        _update_task_progress(task.pk, {"status": "processing", "progress_percent": 10})
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.csv_data, "SYNTH")


class ConcurrentClaimTests(TransactionTestCase):
    def run_parallel(self, operation):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, DatabaseError
        barrier = Barrier(2)
        def attempt():
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                operation()
                return "claimed"
            except (SubmissionError, APIError, DatabaseError):
                return "blocked"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(outcomes.count("claimed"), 1, outcomes)

    def test_submission_reservation_across_connections(self):
        SubmissionReservation.objects.create(pk=1)
        self.run_parallel(reserve_submission)

    def test_idempotency_claim_across_connections(self):
        from .idempotency import claim
        owner = User.objects.create_superuser("parallel-admin", "synthetic@example.invalid", "synthetic")
        key, _ = create_key(owner, "Parallel", timezone.now() + timedelta(days=1), ["prime"])
        def operation():
            request = RequestFactory().post("/api/prime/jobs", headers={"Idempotency-Key": "parallel"})
            request.api_key = key
            claim(request, "prime", "upload", "synthetic-digest")
        self.run_parallel(operation)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)


class PDFRendererTests(TestCase):
    def test_renderer_timeout_kills_private_process_and_removes_profile(self):
        import os
        import subprocess
        process = Mock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired("synthetic-renderer", 40), (b"", None)]
        with patch("agent_api.pdf.subprocess.Popen", return_value=process) as popen, patch("agent_api.pdf.subprocess.run") as kill_tree, patch("agent_api.pdf.os.killpg", create=True) as kill_group:
            with self.assertRaises(APIError) as caught:
                render_pdf("<h1>SYNTH</h1>")
            self.assertEqual(caught.exception.code, "pdf_timeout")
            self.assertFalse(Path(popen.call_args.kwargs["env"]["TMPDIR"]).exists())
            if os.name == "nt":
                kill_tree.assert_called_once()
            else:
                kill_group.assert_called_once()

    def test_real_pdf_multipage_text_and_no_remote_or_file_fetch(self):
        from pypdf import PdfReader
        html = '<html><body><h1>PRIMER synthetic report</h1>' + ''.join(f'<p>SYNTH row {i}</p>' for i in range(180)) + '<img src="http://127.0.0.1:1/never"><img src="file:///etc/passwd"></body></html>'
        content = render_pdf(html)
        self.assertTrue(content.startswith(b"%PDF-"))
        reader = PdfReader(BytesIO(content))
        self.assertGreater(len(reader.pages), 1)
        text = ''.join(page.extract_text() for page in reader.pages)
        self.assertIn("PRIMER synthetic report", text)
        self.assertIn("SYNTH row 179", text)
        self.assertNotIn("root:", text)
