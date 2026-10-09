import os
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from report_v2 import data
from report_v2.definitions.repository import InvalidDefinitionIdError
from report_v2.evaluation import EvaluationError
from report_v2.exports import default_document, _print_view_model

from .auth import APIError
from .models import PDFReservation


def render_pdf(html):
    with tempfile.TemporaryDirectory(prefix="primer-pdf-") as private_temp:
        env = dict(os.environ, TMPDIR=private_temp, TMP=private_temp, TEMP=private_temp)
        process = subprocess.Popen([sys.executable, "-m", "agent_api.pdf_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=settings.BASE_DIR, env=env, start_new_session=os.name != "nt")
        try:
            output, _ = process.communicate(html.encode("utf-8"), timeout=40)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            process.communicate()
            raise APIError("pdf_timeout", 504) from None
        if process.returncode != 0 or not output.startswith(b"%PDF-"):
            raise APIError("pdf_render_failed", 502)
        if len(output) > 20 * 1024 * 1024:
            raise APIError("pdf_too_large", 413)
        return output


def report_pdf(project, slug):
    PDFReservation.objects.get_or_create(pk=1)
    token = uuid.uuid4()
    now = timezone.now()
    if not PDFReservation.objects.filter(pk=1).filter(Q(token=None) | Q(expires_at__lt=now)).update(token=token, expires_at=now + timedelta(seconds=90)):
        raise APIError("pdf_capacity_busy", 429)
    try:
        try:
            set_isolation = connection.vendor == "postgresql" and not connection.in_atomic_block
            with transaction.atomic():
                if set_isolation:
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                document = default_document(project, slug)
        except (data.PublishedNotFoundError, InvalidDefinitionIdError):
            raise APIError("report_not_found", 404) from None
        except (EvaluationError, data.AdapterError):
            raise APIError("report_evaluation_failed", 422) from None
        css = (Path(settings.BASE_DIR) / "report_v2/static/report_v2/print.css").read_text(encoding="utf-8")
        html = render_to_string("report_v2/print.html", {"print": _print_view_model(document), "automated_pdf": True, "pdf_css": css})
        if not PDFReservation.objects.filter(pk=1, token=token).update(expires_at=timezone.now() + timedelta(seconds=50)):
            raise APIError("pdf_capacity_busy", 429)
        return render_pdf(html)
    finally:
        PDFReservation.objects.filter(pk=1, token=token).update(token=None, expires_at=None)
