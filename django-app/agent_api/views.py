import hashlib
import json
import smtplib
import socket

import requests
from django.utils import timezone
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from report_v2 import data
from report_v2.definitions.repository import validate_definition_id, InvalidDefinitionIdError
from upload.submission import SubmissionError, submit_files

from .audit import record
from .auth import APIError, api_endpoint, authenticate
from .idempotency import claim, finish, uncertain
from .models import APIJob
from .pdf import report_pdf

MAX_FILES = 20
MAX_UPLOAD_BYTES = 49 * 1024 * 1024


@api_endpoint("POST")
def jobs(request, project):
    if request.content_type != "multipart/form-data":
        raise APIError("multipart_required")
    if set(request.POST) - {"supplemental_steps"} or set(request.FILES) - {"files"}:
        raise APIError("unexpected_fields")
    steps = request.POST.get("supplemental_steps", "false")
    if steps not in ("true", "false") or len(request.POST.getlist("supplemental_steps")) > 1:
        raise APIError("invalid_supplemental_steps")
    files = request.FILES.getlist("files")
    if not 1 <= len(files) <= MAX_FILES:
        raise APIError("invalid_file_count")
    if sum(f.size for f in files) > MAX_UPLOAD_BYTES:
        raise APIError("upload_too_large", 413)
    digest = hashlib.sha256(steps.encode())
    forwarded = []
    for file in files:
        if file.size == 0 or len(file.name) > 255 or any(c in file.name for c in "\\/\r\n\x00") or PurePath(file.name).suffix.lower() not in {".csv", ".xlsx", ".xls"}:
            raise APIError("invalid_file")
        digest.update(json.dumps([file.name, file.size]).encode())
        for chunk in file.chunks():
            digest.update(chunk)
        file.seek(0)
        forwarded.append(("files", (file.name, file, file.content_type or "application/octet-stream")))
    entry, replay = claim(request, project, "upload", digest.hexdigest())
    if replay:
        return replay
    job = None
    def track(task):
        nonlocal job
        job = APIJob.objects.create(task=task, owner=request.user, creator_key=request.api_key, project_id=project)
    def before_dispatch():
        authenticate(request, project)
        record(request, "api_upload_dispatch", project=project, file_count=len(files))
    try:
        submit_files(forwarded, steps == "true", before_dispatch=before_dispatch, track=track)
    except SubmissionError as exc:
        if exc.ambiguous:
            uncertain(entry)
            raise APIError(exc.code, exc.status)
        return finish(entry, {"error": {"code": exc.code, "message": exc.code.replace("_", " ")}}, exc.status)
    except Exception:
        uncertain(entry)
        raise
    body = {"job_id": str(job.pk), "status": "queued", "status_url": reverse("agent_api:job_status", kwargs={"project": project, "job_id": job.pk})}
    record(request, "api_upload_accepted", project=project, job_id=str(job.pk))
    return finish(entry, body, 202)


@api_endpoint("GET")
def job_status(request, project, job_id):
    job = APIJob.objects.select_related("task").filter(pk=job_id, owner=request.user, project_id=project).first()
    if job is None:
        raise APIError("job_not_found", 404)
    task = job.task
    state = task.status
    if state in ("queued", "processing", "saving"):
        from upload.views import get_api_url, get_api_headers, _start_background_worker
        try:
            response = requests.get(f"{get_api_url()}/status/{task.pk}", headers=get_api_headers(), timeout=(5, 10))
            if response.status_code == 404:
                from upload.models import ProcessingTask
                ProcessingTask.objects.filter(pk=task.pk, results_saved_at=None).exclude(status__in=("completed", "cancelled")).update(
                    status="failed", completed_at=timezone.now(), error_message="Processor state unavailable; operator reconciliation required")
                task.refresh_from_db()
                state = task.status
            elif response.status_code == 200:
                _start_background_worker(task.pk)
                task.refresh_from_db()
                state = task.status
            else:
                raise APIError("processor_unavailable", 503)
        except requests.RequestException:
            raise APIError("processor_unavailable", 503) from None
    if state == "completed" and task.results_saved_at is None:
        state = "saving"
    if state == "cancelled":
        state = "failed"
    body = {"job_id": str(job.pk), "project": project, "status": state, "progress_percent": max(0, min(100, task.progress_percent))}
    if state == "failed":
        body["error"] = {"code": "processing_failed", "message": "processing failed"}
    return JsonResponse(body)


@api_endpoint("GET")
def reports(request, project):
    return JsonResponse({"reports": [dict(slug=r["slug"], title=r["title"], version=r["version"],
        pdf_url=reverse("agent_api:pdf", kwargs={"project": project, "report_slug": r["slug"]}),
        email_url=reverse("agent_api:email", kwargs={"project": project, "report_slug": r["slug"]}))
        for r in data.list_published(project_id=project)]})


@api_endpoint("GET")
def pdf(request, project, report_slug):
    content = report_pdf(project, report_slug)
    record(request, "api_pdf_download", project=project, report=report_slug)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{report_slug}.pdf"'
    return response


@api_endpoint("POST")
def email(request, project, report_slug):
    try:
        validate_definition_id(report_slug)
    except InvalidDefinitionIdError:
        raise APIError("report_not_found", 404) from None
    if request.content_type != "application/json":
        raise APIError("json_required")
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise APIError("invalid_json")
    if not isinstance(body, dict) or set(body) != {"recipients"}:
        raise APIError("unexpected_fields")
    recipients = body["recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 20:
        raise APIError("invalid_recipients")
    try:
        for recipient in recipients:
            if not isinstance(recipient, str):
                raise ValidationError("invalid")
            validate_email(recipient)
    except ValidationError:
        raise APIError("invalid_recipients")
    recipients = sorted(set(recipients))
    digest = hashlib.sha256(json.dumps(recipients).encode()).hexdigest()
    entry, replay = claim(request, project, f"email:{report_slug}", digest)
    if replay:
        return replay
    dispatched = False
    try:
        content = report_pdf(project, report_slug)
        authenticate(request, project)
        record(request, "api_email_dispatch", project=project, report=report_slug, recipient_count=len(recipients))
        connection = get_connection(timeout=20)
        message = EmailMessage(subject=f"PRIMER report: {report_slug}", body="The routine report PDF is attached.",
                               from_email=settings.DEFAULT_FROM_EMAIL, to=[settings.DEFAULT_FROM_EMAIL],
                               bcc=recipients, connection=connection)
        message.attach(f"{report_slug}.pdf", content, "application/pdf")
        dispatched = True
        if message.send(fail_silently=False) != 1:
            raise smtplib.SMTPException("not accepted")
        record(request, "api_email_accepted", project=project, report=report_slug, recipient_count=len(recipients))
        return finish(entry, {"status": "accepted_by_mail_server", "request_id": request.api_request_id})
    except APIError as exc:
        return finish(entry, {"error": {"code": exc.code, "message": exc.code.replace("_", " ")}}, exc.status)
    except (smtplib.SMTPException, OSError) as exc:
        uncertain(entry)
        raise APIError("email_outcome_unknown", 504 if isinstance(exc, (TimeoutError, socket.timeout)) else 502)
    except Exception:
        uncertain(entry)
        raise APIError("email_outcome_unknown" if dispatched else "email_failed", 502)


@api_endpoint(None)
def not_found(request, project, unmatched):
    raise APIError("endpoint_not_found", 404)
