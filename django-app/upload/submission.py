import uuid

import requests
from django.db import transaction
from django.utils import timezone

from .models import ProcessingTask, SubmissionReservation

ACTIVE = ("queued", "processing", "saving")


class SubmissionError(Exception):
    def __init__(self, code, status=503, ambiguous=False):
        self.code, self.status, self.ambiguous = code, status, ambiguous
        super().__init__(code)


def reserve_submission():
    reservation, _ = SubmissionReservation.objects.get_or_create(pk=1)
    if reservation.token and reservation.task_id:
        task = ProcessingTask.objects.filter(pk=reservation.task_id).first()
        terminal = task is not None and task.status not in ACTIVE
        if terminal and task.status == "cancelled":
            # Local cancellation stops monitoring, not the remote processor.
            from .views import get_api_url, get_api_headers
            try:
                status = requests.get(f"{get_api_url()}/status/{task.pk}", headers=get_api_headers(), timeout=(5, 10))
                terminal = status.status_code == 404 or (status.status_code == 200 and status.json().get("status") in ("completed", "failed", "cancelled"))
            except (requests.RequestException, ValueError):
                raise SubmissionError("processor_unavailable", 503) from None
        if terminal:
            SubmissionReservation.objects.filter(pk=1, token=reservation.token).update(token=None, task=None)
    token = uuid.uuid4()
    if not SubmissionReservation.objects.filter(pk=1, token=None).update(token=token, task=None, acquired_at=timezone.now()):
        raise SubmissionError("processing_busy", 409)
    if ProcessingTask.objects.filter(status__in=ACTIVE).exists():
        release_submission(token)
        raise SubmissionError("processing_busy", 409)
    return token


def release_submission(token):
    SubmissionReservation.objects.filter(pk=1, token=token).update(token=None, task=None)


def submit_files(files, supplemental_steps=False, *, endpoint="analyze-auto-sort", before_dispatch=None, track=None):
    from .views import get_api_url, get_api_headers, _start_background_worker
    token = reserve_submission()
    dispatched = False
    try:
        if before_dispatch:
            before_dispatch()
        dispatched = True
        response = requests.post(f"{get_api_url()}/{endpoint}", files=files,
                                 data={"supplemental_steps": "true" if supplemental_steps else "false"},
                                 headers=get_api_headers(), timeout=(10, 75))
        if response.status_code >= 500:
            raise SubmissionError("submission_outcome_unknown", 502, ambiguous=True)
        if response.status_code != 200:
            raise SubmissionError("processing_rejected", 400 if response.status_code < 500 else 502)
        result = response.json()
        task_id = str(uuid.UUID(result["task_id"]))
        with transaction.atomic():
            task = ProcessingTask.objects.create(task_id=task_id, status="queued", supplemental_steps=supplemental_steps,
                                                 progress_message="Submitted to API server")
            SubmissionReservation.objects.filter(pk=1, token=token).update(task=task)
            if track:
                track(task)
        _start_background_worker(task_id)
        return result
    except SubmissionError as exc:
        if not exc.ambiguous:
            release_submission(token)
        raise
    except Exception as exc:
        if not dispatched:
            release_submission(token)
            raise
        # Keep the reservation: a timeout may have submitted work successfully.
        raise SubmissionError("submission_outcome_unknown", 504 if isinstance(exc, requests.Timeout) else 502, ambiguous=True) from None


def persist_result(task_id, result):
    from .views import save_results_to_database
    import json
    with transaction.atomic():
        task = ProcessingTask.objects.select_for_update().get(pk=task_id)
        if task.results_saved_at:
            return task.new_records_added, task.existing_records_skipped
        if task.status in ("failed", "cancelled"):
            raise SubmissionError("task_not_running", 409)
        # Write first so SQLite also acquires a write reservation before study reads.
        ProcessingTask.objects.filter(pk=task_id).update(status="saving")
        counts = save_results_to_database(result, task_id, strict=hasattr(task, "apijob"))
        task.status = "completed"
        task.completed_at = task.results_saved_at = timezone.now()
        task.progress_percent = 100
        task.txt_report = result.get("txt_report", "")
        task.csv_data = result.get("csv_data", "")
        task.new_records_added, task.existing_records_skipped = counts
        task.total_records_processed = sum(counts)
        if result.get("false_negatives"):
            task.false_negatives_json = json.dumps(result["false_negatives"])
        task.save()
        return counts
