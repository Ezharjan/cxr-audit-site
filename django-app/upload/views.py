# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""
Views for the CXR Upload and Processing application.

This Django app interfaces with the existing combined_server.py API.
The API server must be running separately.

Background processing: after a task is submitted to the FastAPI backend,
a daemon thread polls for status and auto-saves results to the Django DB
when the task completes — even if the user leaves the page.
"""

import json
import os
import threading
import traceback
import requests
from requests.exceptions import ConnectionError, Timeout

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone
from django.contrib.auth.decorators import login_required, user_passes_test

from .models import CXRStudy, ProcessingTask


def _is_admin(user):
    """Check if user is in the 'admins' group or is a superuser."""
    return user.is_superuser or user.groups.filter(name='admins').exists()


admin_required = user_passes_test(_is_admin)


# ============ Background Task Worker ============

_active_worker_lock = threading.Lock()
_active_worker_task_id = None  # tracks which task the background thread is monitoring


def _get_active_task():
    """Return the currently active (queued/processing) ProcessingTask, or None."""
    return ProcessingTask.objects.filter(
        status__in=['queued', 'processing', 'saving']
    ).order_by('-created_at').first()


def _update_task_progress(task_id, status_data):
    """Progress polling must never overwrite already-persisted results."""
    fields = {
        'status': 'saving' if status_data.get('status') == 'completed' else status_data.get('status', 'processing'),
        'progress_message': status_data.get('progress', ''),
        'progress_percent': status_data.get('progress_percent', 0),
    }
    details = status_data.get('progress_details') or {}
    if details:
        fields.update(progress_step=details.get('step', ''), progress_current=details.get('current', 0), progress_total=details.get('total', 0))
    ProcessingTask.objects.filter(pk=task_id, results_saved_at=None).exclude(status__in=['completed', 'failed', 'cancelled']).update(**fields)


def _background_poll_and_save(task_id, api_url):
    """Poll processing and persist results before declaring completion."""
    import time
    import django.db
    global _active_worker_task_id
    try:
        django.db.connections.close_all()
        while True:
            time.sleep(3)
            task = ProcessingTask.objects.filter(pk=task_id).first()
            if task is None or task.status in ('completed', 'failed', 'cancelled'):
                break
            try:
                response = requests.get(f"{api_url}/status/{task_id}", headers=get_api_headers(), timeout=30)
                if response.status_code == 404:
                    ProcessingTask.objects.filter(pk=task_id, results_saved_at=None).exclude(status='cancelled').update(
                        status='failed', completed_at=timezone.now(), error_message='Processor state unavailable')
                    break
                if response.status_code != 200:
                    continue
                status_data = response.json()
            except Exception:
                print(f"[BG Worker] Status poll error for {task_id}.")
                continue
            _update_task_progress(task_id, status_data)
            if status_data.get('status') == 'completed':
                _background_fetch_and_save(task_id, api_url, task)
                break
            if status_data.get('status') == 'failed':
                ProcessingTask.objects.filter(pk=task_id, results_saved_at=None).exclude(status='cancelled').update(
                    status='failed', completed_at=timezone.now(), error_message='Processor reported failure')
                break
    except Exception:
        print(f"[BG Worker] Task {task_id} monitoring failed.")
        try:
            ProcessingTask.objects.filter(pk=task_id, results_saved_at=None).exclude(status__in=['completed', 'cancelled']).update(
                status='failed', completed_at=timezone.now(), error_message='Background monitoring failed')
        except Exception:
            pass
    finally:
        with _active_worker_lock:
            if _active_worker_task_id == task_id:
                _active_worker_task_id = None
        django.db.connections.close_all()


def _background_fetch_and_save(task_id, api_url, task):
    """Fetch results from FastAPI and save to DB (called from background thread)."""
    try:
        r = requests.get(f"{api_url}/results/{task_id}", headers=get_api_headers(), timeout=120)
        if r.status_code != 200:
            raise ValueError('Results unavailable')

        result = r.json()

        from .submission import persist_result
        new_count, skipped_count = persist_result(task_id, result)
        print(f"[BG Worker] Task {task_id}: saved {new_count} new, {skipped_count} skipped.")

    except Exception:
        print(f"[BG Worker] Task {task_id} result persistence failed.")
        ProcessingTask.objects.filter(pk=task_id, results_saved_at=None).exclude(status__in=['completed', 'cancelled']).update(
            status='failed', completed_at=timezone.now(), error_message='Result persistence failed')


def _start_background_worker(task_id):
    """Launch the background polling thread for a task, if not already running."""
    global _active_worker_task_id
    api_url = get_api_url()

    with _active_worker_lock:
        if _active_worker_task_id is not None:
            if _active_worker_task_id == task_id or ProcessingTask.objects.filter(
                    pk=_active_worker_task_id, status__in=['queued', 'processing', 'saving']).exists():
                return
        _active_worker_task_id = task_id

    t = threading.Thread(
        target=_background_poll_and_save,
        args=(task_id, api_url),
        daemon=True,
        name=f'cxr-bg-{task_id[:8]}'
    )
    t.start()
    print(f"[BG Worker] Started background thread for task {task_id}")


# ============ Backfill Worker ============

_SUPPLEMENTAL_FIELDS = [
    'atelectasis_llm', 'calcification_llm', 'cardiomegaly_llm', 'consolidation_llm',
    'fibrosis_llm', 'mediastinal_widening_llm', 'nodule_llm', 'pleural_effusion_llm',
    'pneumoperitoneum_llm', 'pneumothorax_llm', 'tb_llm',
]

_BACKFILL_BATCH_SIZE = 20


def _background_backfill(task_id, date_from, date_to):
    """
    Background thread for the supplemental-analysis backfill.
    Queries CXRStudy records missing *_llm fields in the given date range,
    sends them to the FastAPI /grade-supplemental-batch endpoint in batches,
    and writes the results back to the DB.

    Holds _active_worker_task_id for its entire duration, so upload endpoints
    get the same 409 they would for a running upload task.
    """
    import time
    import django.db
    global _active_worker_task_id

    try:
        django.db.connections.close_all()
        api_url = get_api_url()

        task = ProcessingTask.objects.get(task_id=task_id)
        task.update_progress('backfill', 0, 0, 'Querying records to backfill…', percent=2)

        qs = CXRStudy.objects.filter(
            atelectasis_llm__isnull=True,
            text_report__isnull=False,
        ).exclude(text_report='')
        if date_from:
            qs = qs.filter(procedure_start_date__date__gte=date_from)
        if date_to:
            qs = qs.filter(procedure_start_date__date__lte=date_to)

        records = list(qs.values_list('accession_no', 'text_report'))
        total = len(records)

        if total == 0:
            task.status = 'completed'
            task.completed_at = timezone.now()
            task.progress_percent = 100
            task.progress_message = 'No records need backfilling in this date range.'
            task.total_records_processed = 0
            task.new_records_added = 0
            task.existing_records_skipped = 0
            task.save()
            print(f"[Backfill] Task {task_id}: no records to process.")
            return

        task.update_progress('backfill', 0, total,
                             f'Found {total} records. Starting backfill…', percent=5)

        updated = 0
        failed = 0

        for batch_start in range(0, total, _BACKFILL_BATCH_SIZE):
            # Respect cancellation
            task.refresh_from_db(fields=['status'])
            if task.status == 'cancelled':
                print(f"[Backfill] Task {task_id} cancelled by user.")
                break

            batch = records[batch_start:batch_start + _BACKFILL_BATCH_SIZE]
            payload = {
                'reports': [
                    {'accession_no': int(acc), 'text_report': rpt}
                    for acc, rpt in batch
                ]
            }

            try:
                r = requests.post(
                    f"{api_url}/grade-supplemental-batch",
                    json=payload,
                    headers=get_api_headers(),
                    timeout=300,
                )
                if r.status_code != 200:
                    print(f"[Backfill] API error {r.status_code} on batch at {batch_start}")
                    failed += len(batch)
                else:
                    for result in r.json().get('results', []):
                        acc_no = result.get('accession_no')
                        if not acc_no:
                            continue
                        update_kwargs = {f: result[f] for f in _SUPPLEMENTAL_FIELDS
                                         if f in result and result[f] is not None}
                        if update_kwargs:
                            CXRStudy.objects.filter(accession_no=acc_no).update(**update_kwargs)
                            updated += 1
                        else:
                            failed += 1

            except Exception as e:
                print(f"[Backfill] Batch error at {batch_start}: {e}")
                failed += len(batch)

            completed_so_far = min(batch_start + _BACKFILL_BATCH_SIZE, total)
            task.update_progress(
                'backfill', completed_so_far, total,
                f'Backfilling: {completed_so_far}/{total} — {updated} updated, {failed} failed',
                percent=5 + (completed_so_far / total) * 90,
            )

        task.status = 'completed'
        task.completed_at = timezone.now()
        task.progress_percent = 100
        task.progress_message = (
            f'Backfill complete: {updated} records updated'
            + (f', {failed} failed' if failed else '')
        )
        task.total_records_processed = total
        task.new_records_added = updated
        task.existing_records_skipped = failed
        task.save()
        print(f"[Backfill] Task {task_id}: {updated} updated, {failed} failed out of {total}.")

    except Exception as e:
        print(f"[Backfill] Unhandled error for {task_id}: {e}")
        traceback.print_exc()
        try:
            task = ProcessingTask.objects.get(task_id=task_id)
            if task.status not in ('completed', 'failed'):
                task.mark_failed(f'Backfill error: {e}')
        except Exception:
            pass
    finally:
        with _active_worker_lock:
            if _active_worker_task_id == task_id:
                _active_worker_task_id = None
        django.db.connections.close_all()


def _start_backfill_worker(task_id, date_from, date_to):
    """Launch the backfill background thread, if no other worker is running."""
    global _active_worker_task_id

    with _active_worker_lock:
        if _active_worker_task_id is not None:
            return False  # another task is already running
        _active_worker_task_id = task_id

    t = threading.Thread(
        target=_background_backfill,
        args=(task_id, date_from, date_to),
        daemon=True,
        name=f'cxr-backfill-{task_id[:8]}'
    )
    t.start()
    print(f"[Backfill] Started thread for task {task_id} ({date_from} → {date_to})")
    return True


def get_api_url():
    """
    Get the API URL from Django settings.
    Configure API_IP and API_PORT in settings.py CXR_API_CONFIG.
    """
    api_ip = settings.CXR_API_CONFIG.get('API_IP', 'localhost')
    api_port = settings.CXR_API_CONFIG.get('API_PORT', 1221)
    return f"http://{api_ip}:{api_port}"


def get_api_headers():
    """Return headers required to authenticate with the FastAPI backend."""
    headers = {}
    api_key = os.environ.get("API_SECRET_KEY", "")
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


@login_required
def index(request):
    """Render the main upload interface."""
    api_ip = settings.CXR_API_CONFIG.get('API_IP', 'localhost')
    api_port = settings.CXR_API_CONFIG.get('API_PORT', 1221)
    
    context = {
        'api_ip': api_ip,
        'api_port': api_port,
    }
    return render(request, 'upload/upload_interface.html', context)


@login_required
@admin_required
def tasks_list(request):
    """List all processing tasks."""
    tasks = ProcessingTask.objects.all()  # ordered by -created_at via Meta
    return render(request, 'upload/tasks.html', {'tasks': tasks})


@login_required
@admin_required
@csrf_exempt
@require_http_methods(["POST"])
def backfill_supplemental(request):
    """
    Start a supplemental-analysis backfill over a date range.

    Checks for any currently running task and returns 409 if one exists,
    so backfill and upload analysis are mutually exclusive.
    """
    import uuid
    from datetime import date

    active = _get_active_task()
    if active:
        return JsonResponse({
            'error': (
                f'A task is already running (ID: {active.task_id}). '
                f'Please wait for it to complete before starting a backfill.'
            )
        }, status=409)

    date_from_str = request.POST.get('date_from', '').strip()
    date_to_str = request.POST.get('date_to', '').strip()

    try:
        date_from = date.fromisoformat(date_from_str) if date_from_str else None
    except ValueError:
        return JsonResponse({'error': f'Invalid date_from: {date_from_str!r}'}, status=400)

    try:
        date_to = date.fromisoformat(date_to_str) if date_to_str else None
    except ValueError:
        return JsonResponse({'error': f'Invalid date_to: {date_to_str!r}'}, status=400)

    task_id = str(uuid.uuid4())

    from .submission import reserve_submission, release_submission, SubmissionError
    from .models import SubmissionReservation
    try:
        reservation_token = reserve_submission()
    except SubmissionError as exc:
        return JsonResponse({'error': exc.code}, status=exc.status)
    try:
        task = ProcessingTask.objects.create(
            task_id=task_id,
            task_type='backfill',
            status='processing',
            supplemental_steps=True,
            progress_message='Backfill queued…',
            progress_percent=0,
        )
        SubmissionReservation.objects.filter(pk=1, token=reservation_token).update(task=task)
    except Exception:
        release_submission(reservation_token)
        raise

    started = _start_backfill_worker(task_id, date_from, date_to)
    if not started:
        # Lock was grabbed by another worker between the DB check and the lock acquisition
        task.mark_failed('Could not start: another task began simultaneously.')
        return JsonResponse({'error': 'Another task started simultaneously. Please retry.'}, status=409)

    return JsonResponse({
        'ok': True,
        'task_id': task_id,
        'message': (
            f'Backfill started'
            + (f' from {date_from}' if date_from else '')
            + (f' to {date_to}' if date_to else '')
            + '.'
        ),
    })


@login_required
@admin_required
@csrf_exempt
@require_http_methods(["POST"])
def delete_task(request, task_id):
    """Delete a processing task by ID."""
    try:
        task = ProcessingTask.objects.get(task_id=task_id)
    except ProcessingTask.DoesNotExist:
        return JsonResponse({'error': 'Task not found'}, status=404)

    # Don't allow deleting a currently-running task
    if task.status in ('queued', 'processing', 'saving'):
        return JsonResponse(
            {'error': 'Cannot delete a task that is still running. '
                      'Wait for it to complete or fail first.'},
            status=409
        )

    task.delete()
    return JsonResponse({'ok': True})


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def cancel_task(request, task_id):
    """Cancel a running task by marking it as cancelled in the DB.
    The background worker will stop polling on its next cycle."""
    try:
        task = ProcessingTask.objects.get(task_id=task_id)
    except ProcessingTask.DoesNotExist:
        return JsonResponse({'error': 'Task not found'}, status=404)

    if task.status not in ('queued', 'processing'):
        return JsonResponse({'error': 'Task is not running'}, status=409)

    task.status = 'cancelled'
    task.completed_at = timezone.now()
    task.error_message = 'Cancelled by user'
    task.save()
    return JsonResponse({'ok': True})


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def analyze_auto_sort(request):
    """Submit auto-sorted files through the shared processing reservation."""
    from .submission import submit_files, SubmissionError
    files = request.FILES.getlist('files')
    if not files:
        return JsonResponse({'error': 'No files uploaded'}, status=400)
    forwarded = [('files', (f.name, f, f.content_type or 'application/octet-stream')) for f in files]
    try:
        return JsonResponse(submit_files(forwarded, request.POST.get('supplemental_steps', 'false').lower() == 'true'))
    except SubmissionError as exc:
        return JsonResponse({'error': exc.code}, status=exc.status)
    except Exception:
        return JsonResponse({'error': 'Processing service unavailable'}, status=503)


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def analyze_multiple(request):
    """Submit explicitly grouped files through the same reservation as auto-sort."""
    from .submission import submit_files, SubmissionError
    carpl_files = request.FILES.getlist('lunit_files')
    ge_files = request.FILES.getlist('ground_truth_files')
    if not carpl_files or not ge_files:
        return JsonResponse({'error': 'Both CARPL and GE/RIS files are required'}, status=400)
    forwarded = [(name, (f.name, f, f.content_type or 'application/octet-stream'))
                 for name, files in [('lunit_files', carpl_files), ('ground_truth_files', ge_files)] for f in files]
    try:
        return JsonResponse(submit_files(forwarded, request.POST.get('supplemental_steps', 'false').lower() == 'true', endpoint='analyze-multiple'))
    except SubmissionError as exc:
        return JsonResponse({'error': exc.code}, status=exc.status)
    except Exception:
        return JsonResponse({'error': 'Processing service unavailable'}, status=503)


@login_required
@require_http_methods(["GET"])
def get_status(request, task_id):
    """
    Proxy status request to the API server.
    """
    api_url = get_api_url()
    
    try:
        response = requests.get(f"{api_url}/status/{task_id}", headers=get_api_headers(), timeout=30)
        
        if response.status_code != 200:
            return JsonResponse({'error': response.text}, status=response.status_code)
        
        result = response.json()
        
        _update_task_progress(task_id, result)

        return JsonResponse(result)
        
    except ConnectionError:
        return JsonResponse({
            'error': f'Cannot connect to API server at {api_url}'
        }, status=503)
    except Timeout:
        return JsonResponse({'error': 'API server request timed out'}, status=504)
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def get_results(request, task_id):
    """
    Proxy results request to the API server and save to database.
    """
    api_url = get_api_url()
    
    try:
        response = requests.get(f"{api_url}/results/{task_id}", headers=get_api_headers(), timeout=60)
        
        if response.status_code != 200:
            return JsonResponse({'error': response.text}, status=response.status_code)
        
        result = response.json()
        
        from .submission import persist_result
        new_count, skipped_count = persist_result(task_id, result)
        result['new_records_added'] = new_count
        result['existing_records_skipped'] = skipped_count
        result['total_records_processed'] = new_count + skipped_count

        return JsonResponse(result)
        
    except ConnectionError:
        return JsonResponse({
            'error': f'Cannot connect to API server at {api_url}'
        }, status=503)
    except Timeout:
        return JsonResponse({'error': 'API server request timed out'}, status=504)
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def check_api_connection(request):
    """
    Check connection to the API server.
    """
    api_url = get_api_url()

    try:
        response = requests.get(f"{api_url}/", headers=get_api_headers(), timeout=5)
        return JsonResponse({
            'connected': True,
            'api_url': api_url,
            'status_code': response.status_code
        })
    except (ConnectionError, Timeout):
        return JsonResponse({
            'connected': False,
            'api_url': api_url,
            'error': 'Cannot connect to API server'
        })
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({
            'connected': False,
            'api_url': api_url,
            'error': str(e)
        })


@login_required
@require_http_methods(["GET"])
def check_llm_connection(request):
    """
    Check connection to the LLM server (OpenAI-compatible API).

    The django container is on internal-only networks and cannot reach the LLM
    host directly, so this proxies the check through the api container's
    /llm-health endpoint. The api is the only service on the inference network
    and holds the LLM API key.
    """
    api_url = get_api_url()

    try:
        response = requests.get(
            f"{api_url}/llm-health", headers=get_api_headers(), timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        return JsonResponse({
            'connected': bool(payload.get('connected')),
            'llm_url': payload.get('llm_url'),
            'status_code': payload.get('status_code'),
            'error': payload.get('error'),
        })
    except (ConnectionError, Timeout):
        return JsonResponse({
            'connected': False,
            'error': 'Cannot reach API server to check LLM status',
        })
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({
            'connected': False,
            'error': str(e),
        })


@login_required
@require_http_methods(["GET"])
def get_active_task(request):
    """
    Check if there's a currently running task.
    Used on page load to resume monitoring UI.
    """
    active = _get_active_task()
    if not active:
        return JsonResponse({'active': False})

    return JsonResponse({
        'active': True,
        'task_id': active.task_id,
        'status': active.status,
        'progress_message': active.progress_message or '',
        'progress_percent': active.progress_percent,
        'progress_step': active.progress_step or '',
        'progress_current': active.progress_current,
        'progress_total': active.progress_total,
    })


@login_required
@require_http_methods(["GET"])
def get_task_results(request, task_id):
    """
    Return saved results from the Django DB for a completed task.
    Used when the user returns to the page after the background worker
    has already fetched and saved results.
    """
    try:
        task = ProcessingTask.objects.get(task_id=task_id)
    except ProcessingTask.DoesNotExist:
        return JsonResponse({'error': 'Task not found'}, status=404)

    if task.status != 'completed':
        return JsonResponse({'error': f'Task is {task.status}, not completed'}, status=400)

    return JsonResponse({
        'status': 'completed',
        'new_records_added': task.new_records_added,
        'existing_records_skipped': task.existing_records_skipped,
        'total_records_processed': task.total_records_processed,
        'csv_data': task.csv_data or '',
        'txt_report': task.txt_report or '',
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def precheck_files(request):
    """
    Reads ACCESSION_NO from each CSV/Excel (including password-protected),
    checks which already exist in the DB.
    Returns counts and lists so the user can decide whether to proceed.
    """
    import pandas as pd
    from io import BytesIO

    XLSX_PASSWORD = os.environ.get("XLSX_DECRYPT_PASSWORD", "")

    def _read_file_to_df(raw_bytes, filename):
        """Read CSV or Excel (with optional decryption) into a DataFrame."""
        name_lower = filename.lower()
        if name_lower.endswith('.csv'):
            return pd.read_csv(BytesIO(raw_bytes))
        elif name_lower.endswith(('.xlsx', '.xls')):
            # Try plain read first, then try decrypting
            try:
                return pd.read_excel(BytesIO(raw_bytes))
            except Exception:
                pass
            # Try password-protected
            try:
                from msoffcrypto import OfficeFile
                decrypted = BytesIO()
                office_file = OfficeFile(BytesIO(raw_bytes))
                office_file.load_key(password=XLSX_PASSWORD)
                office_file.decrypt(decrypted)
                decrypted.seek(0)
                return pd.read_excel(decrypted)
            except Exception as decrypt_err:
                raise ValueError(f'Cannot read Excel file (tried plain + password): {decrypt_err}')
        else:
            raise ValueError('unsupported file type')

    try:
        files = request.FILES.getlist('files')
        if not files:
            return JsonResponse({'error': 'No files provided'}, status=400)

        all_accessions = set()
        file_details = []
        parse_errors = []

        for f in files:
            try:
                raw = f.read()
                df = _read_file_to_df(raw, f.name)

                if 'ACCESSION_NO' not in df.columns:
                    # Not a CARPL file (probably a RIS/GE file) — skip silently
                    file_details.append({'name': f.name, 'rows': len(df), 'accessions': 0, 'type': 'ris'})
                    continue

                accessions = df['ACCESSION_NO'].dropna().astype('Int64').dropna().unique()
                acc_set = set(int(a) for a in accessions)
                all_accessions |= acc_set
                file_details.append({'name': f.name, 'rows': len(df), 'accessions': len(acc_set), 'type': 'carpl'})
            except Exception as e:
                parse_errors.append(f'{f.name}: {str(e)}')

        if not all_accessions:
            return JsonResponse({
                'total': 0, 'new': 0, 'existing': 0,
                'files': file_details, 'errors': parse_errors,
            })

        # Query DB for existing accession numbers in bulk
        existing_set = set(
            CXRStudy.objects.filter(
                accession_no__in=list(all_accessions)
            ).values_list('accession_no', flat=True)
        )

        new_accessions = all_accessions - existing_set

        return JsonResponse({
            'total': len(all_accessions),
            'new': len(new_accessions),
            'existing': len(existing_set),
            'existing_accessions': sorted(existing_set),
            'files': file_details,
            'errors': parse_errors,
        })

    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'error': str(e)}, status=500)


# ============ Database Helper Functions ============

def save_results_to_database(api_result, batch_id, *, strict=False):
    """
    Save processed results from API to the CXRStudy database.
    Checks if accession number exists before adding.
    
    Returns:
        tuple: (new_records_count, skipped_records_count)
    """
    import pandas as pd
    from io import StringIO
    
    new_count = 0
    skipped_count = 0
    
    csv_data = api_result.get('csv_data', '')
    if not csv_data:
        if strict:
            raise ValueError("Missing result CSV")
        return new_count, skipped_count
    
    try:
        df = pd.read_csv(StringIO(csv_data))
    except Exception as e:
        if strict:
            raise
        print(f"Error parsing CSV data: {e}")
        traceback.print_exc()
        return new_count, skipped_count
    
    # Column mapping from API CSV output to model fields
    # Plain columns (direct value copy)
    column_mapping = {
        'ACCESSION_NO': 'accession_no',
        'StudyIDAnonymized': 'study_id_anonymized',
        'StudyID': 'study_id',
        'Patient Name': 'patient_name',
        'Patient ID': 'patient_id',
        'PATIENT_AGE': 'patient_age',
        'PATIENT_GENDER': 'patient_gender',
        'Instances': 'instances',
        'Study Description': 'study_description',
        'WORKPLACE': 'workplace',
        'MEDICAL_LOCATION_NAME': 'medical_location_name',
        'Institute': 'medical_location_name',
        'Abnormal': 'abnormal',
        'Atelectasis': 'atelectasis',
        'Calcification': 'calcification',
        'Cardiomegaly': 'cardiomegaly',
        'Consolidation': 'consolidation',
        'Fibrosis': 'fibrosis',
        'Mediastinal Widening': 'mediastinal_widening',
        'Nodule': 'nodule',
        'Pleural Effusion': 'pleural_effusion',
        'Pneumoperitoneum': 'pneumoperitoneum',
        'Pneumothorax': 'pneumothorax',
        'Tuberculosis': 'tuberculosis',
        'AI Report': 'ai_report',
        'AI_PRIORITY': 'ai_priority',
        'Feedback': 'feedback',
        'Comments': 'comments',
        'Status': 'status',
        'TEXT_REPORT': 'text_report',
        'gt_manual': 'gt_manual',
        'manual_gt': 'gt_manual',
        'gt_llm': 'gt_llm',
        'lunit_binarised': 'lunit_binarised',
        'llm_grade': 'llm_grade',
        # LLM finding columns
        'atelectasis_llm': 'atelectasis_llm',
        'calcification_llm': 'calcification_llm',
        'cardiomegaly_llm': 'cardiomegaly_llm',
        'consolidation_llm': 'consolidation_llm',
        'fibrosis_llm': 'fibrosis_llm',
        'mediastinal_widening_llm': 'mediastinal_widening_llm',
        'nodule_llm': 'nodule_llm',
        'pleural_effusion_llm': 'pleural_effusion_llm',
        'pneumoperitoneum_llm': 'pneumoperitoneum_llm',
        'pneumothorax_llm': 'pneumothorax_llm',
        'tb': 'tb_llm',
        'tb_llm': 'tb_llm',
    }

    # Datetime columns that need pd.to_datetime parsing
    datetime_columns = {
        'Upload Date': 'upload_date',
        'Inference Date': 'inference_date',
        'AI_FLAG_RECEIVED_DATE': 'ai_flag_received_date',
        'PROCEDURE_START_DATE': 'procedure_start_date',
        'PROCEDURE_END_DATE': 'procedure_end_date',
    }

    # Timedelta columns: stored as strings like "0 days 00:03:00" in CSV,
    # need to be converted to total seconds (float) for the model
    timedelta_columns = {
        'Time_End_to_End': 'time_end_to_end_seconds',
        'Time to clinical_decision(mins)': 'time_to_clinical_decision_seconds',
    }

    # Also accept the numeric REPORT_TURN_AROUND_TIME as a fallback for
    # time_to_clinical_decision_seconds (it's already in minutes as a float)
    report_tat_col = 'REPORT_TURN_AROUND_TIME'

    def _parse_datetime(value):
        """Parse a datetime value from CSV, returning a timezone-aware datetime or None."""
        if pd.isna(value) or value is None:
            return None
        try:
            from django.utils.timezone import make_aware, is_naive, get_current_timezone
            dt = pd.to_datetime(value).to_pydatetime()
            if is_naive(dt):
                dt = make_aware(dt, get_current_timezone())
            return dt
        except Exception:
            return None

    def _parse_timedelta_to_seconds(value):
        """
        Convert a timedelta string (e.g. '0 days 00:03:00') or numeric
        value to total seconds as a float. Returns None on failure.
        """
        if pd.isna(value) or value is None:
            return None
        try:
            td = pd.to_timedelta(value)
            return td.total_seconds()
        except Exception:
            # Maybe it's already a numeric seconds value
            try:
                return float(value)
            except (ValueError, TypeError):
                return None

    for _, row in df.iterrows():
        accession_no = row.get('ACCESSION_NO')
        if pd.isna(accession_no):
            if strict:
                raise ValueError('Missing accession in result CSV')
            continue
        
        try:
            accession_no = int(accession_no)
        except (ValueError, TypeError):
            if strict:
                raise ValueError('Invalid accession in result CSV') from None
            continue
        
        # Check if exists
        if CXRStudy.accession_exists(accession_no):
            skipped_count += 1
            continue
        
        # Build model data
        model_data = {'accession_no': accession_no, 'processing_batch_id': batch_id}
        
        # 1. Plain columns
        for df_col, model_field in column_mapping.items():
            if df_col in row.index and model_field != 'accession_no':
                value = row[df_col]
                if pd.isna(value):
                    value = None
                model_data[model_field] = value

        # 2. Datetime columns
        for df_col, model_field in datetime_columns.items():
            if df_col in row.index:
                model_data[model_field] = _parse_datetime(row[df_col])

        # 3. Timedelta columns → store as total seconds
        for df_col, model_field in timedelta_columns.items():
            if df_col in row.index:
                model_data[model_field] = _parse_timedelta_to_seconds(row[df_col])

        # 4. Fallback: if time_to_clinical_decision_seconds is still empty,
        #    try REPORT_TURN_AROUND_TIME (stored as minutes float in some outputs)
        if model_data.get('time_to_clinical_decision_seconds') is None and report_tat_col in row.index:
            tat_val = row[report_tat_col]
            if not pd.isna(tat_val):
                try:
                    model_data['time_to_clinical_decision_seconds'] = float(tat_val) * 60.0
                except (ValueError, TypeError):
                    pass
        
        # Create the record
        try:
            CXRStudy.objects.create(**model_data)
            new_count += 1
        except Exception as e:
            if strict:
                raise
            print(f"Error saving accession {accession_no}: {e}")
            traceback.print_exc()
    
    return new_count, skipped_count


# ============ CSV Import (Historical Data) ============

# Shared column mapping definition for import preview & confirm.
# Maps CSV column names → CXRStudy model field names.

_IMPORT_COLUMN_MAP = {
    # ── Identifiers ──
    'ACCESSION_NO': 'accession_no',
    'StudyIDAnonymized': 'study_id_anonymized',
    'StudyID': 'study_id',
    'Patient Name': 'patient_name',
    'Patient ID': 'patient_id',
    'PATIENT_AGE': 'patient_age',
    'PATIENT_GENDER': 'patient_gender',
    'Instances': 'instances',
    'Study Description': 'study_description',
    'WORKPLACE': 'workplace',
    'MEDICAL_LOCATION_NAME': 'medical_location_name',
    'Institute': 'medical_location_name',
    # ── Lunit scores ──
    'Abnormal': 'abnormal',
    'Atelectasis': 'atelectasis',
    'Calcification': 'calcification',
    'Cardiomegaly': 'cardiomegaly',
    'Consolidation': 'consolidation',
    'Fibrosis': 'fibrosis',
    'Mediastinal Widening': 'mediastinal_widening',
    'Nodule': 'nodule',
    'Pleural Effusion': 'pleural_effusion',
    'Pneumoperitoneum': 'pneumoperitoneum',
    'Pneumothorax': 'pneumothorax',
    'Tuberculosis': 'tuberculosis',
    # ── AI workflow ──
    'AI Report': 'ai_report',
    'AI_PRIORITY': 'ai_priority',
    'Feedback': 'feedback',
    'Comments': 'comments',
    'Status': 'status',
    # ── Reports / grading ──
    'TEXT_REPORT': 'text_report',
    'gt_manual': 'gt_manual',
    'manual_gt': 'gt_manual',
    'gt_llm': 'gt_llm',
    'lunit_binarised': 'lunit_binarised',
    'llm_grade': 'llm_grade',
    # ── LLM finding columns ──
    'atelectasis_llm': 'atelectasis_llm',
    'calcification_llm': 'calcification_llm',
    'cardiomegaly_llm': 'cardiomegaly_llm',
    'consolidation_llm': 'consolidation_llm',
    'fibrosis_llm': 'fibrosis_llm',
    'mediastinal_widening_llm': 'mediastinal_widening_llm',
    'nodule_llm': 'nodule_llm',
    'pleural_effusion_llm': 'pleural_effusion_llm',
    'pneumoperitoneum_llm': 'pneumoperitoneum_llm',
    'pneumothorax_llm': 'pneumothorax_llm',
    'tb': 'tb_llm',
    'tb_llm': 'tb_llm',
}

_IMPORT_DATETIME_COLS = {
    'Upload Date': 'upload_date',
    'Inference Date': 'inference_date',
    'AI_FLAG_RECEIVED_DATE': 'ai_flag_received_date',
    'PROCEDURE_START_DATE': 'procedure_start_date',
    'PROCEDURE_END_DATE': 'procedure_end_date',
}

_IMPORT_TIMEDELTA_COLS = {
    'Time_End_to_End': 'time_end_to_end_seconds',
    'Time to clinical_decision(mins)': 'time_to_clinical_decision_seconds',
}

# Columns to ignore silently (index / junk columns)
_IMPORT_IGNORE_COLS = {'Unnamed: 0', '', 'Unnamed: 0.1'}


def _parse_bool_to_int(value):
    """Convert 'True'/'False'/bool to 1/0 for SmallIntegerField. Returns None for NaN."""
    import pandas as pd
    if pd.isna(value) or value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    s = str(value).strip().lower()
    if s in ('true', '1', '1.0'):
        return 1
    if s in ('false', '0', '0.0'):
        return 0
    if s == '':
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _sanitise_row(row, csv_columns):
    """
    Build a dict of model field values from a CSV row.
    Returns (model_data_dict, error_string_or_None).
    """
    import pandas as pd
    from django.utils.timezone import make_aware, is_naive, get_current_timezone

    accession_raw = row.get('ACCESSION_NO')
    if pd.isna(accession_raw) or accession_raw is None:
        return None, 'missing ACCESSION_NO'
    try:
        accession_no = int(float(accession_raw))
    except (ValueError, TypeError):
        return None, f'invalid ACCESSION_NO: {accession_raw!r}'

    data = {'accession_no': accession_no}

    # SmallInteger fields that may contain bool-like strings
    _smallint_fields = {
        'gt_manual', 'gt_llm', 'lunit_binarised', 'llm_grade',
        'atelectasis_llm', 'calcification_llm', 'cardiomegaly_llm',
        'consolidation_llm', 'fibrosis_llm', 'mediastinal_widening_llm',
        'nodule_llm', 'pleural_effusion_llm', 'pneumoperitoneum_llm',
        'pneumothorax_llm', 'tb_llm',
    }

    # 1. Plain / SmallInt columns
    for csv_col in csv_columns:
        model_field = _IMPORT_COLUMN_MAP.get(csv_col)
        if not model_field or model_field == 'accession_no':
            continue
        value = row.get(csv_col)
        if pd.isna(value) or value is None:
            value = None
        elif model_field in _smallint_fields:
            value = _parse_bool_to_int(value)
        data[model_field] = value

    # 2. Datetime columns
    for csv_col, model_field in _IMPORT_DATETIME_COLS.items():
        if csv_col in csv_columns:
            val = row.get(csv_col)
            if pd.isna(val) or val is None:
                data[model_field] = None
            else:
                try:
                    dt = pd.to_datetime(val).to_pydatetime()
                    if is_naive(dt):
                        dt = make_aware(dt, get_current_timezone())
                    data[model_field] = dt
                except Exception:
                    data[model_field] = None

    # 3. Timedelta columns → total seconds
    for csv_col, model_field in _IMPORT_TIMEDELTA_COLS.items():
        if csv_col in csv_columns:
            val = row.get(csv_col)
            if pd.isna(val) or val is None:
                data[model_field] = None
            else:
                try:
                    td = pd.to_timedelta(val)
                    data[model_field] = td.total_seconds()
                except Exception:
                    try:
                        data[model_field] = float(val)
                    except (ValueError, TypeError):
                        data[model_field] = None

    # 4. Fallback: REPORT_TURN_AROUND_TIME → clinical decision time
    if data.get('time_to_clinical_decision_seconds') is None and 'REPORT_TURN_AROUND_TIME' in csv_columns:
        tat = row.get('REPORT_TURN_AROUND_TIME')
        if not pd.isna(tat) and tat is not None:
            try:
                data['time_to_clinical_decision_seconds'] = float(tat) * 60.0
            except (ValueError, TypeError):
                pass

    return data, None


@login_required
@admin_required
def import_data(request):
    """Render the import historical data page."""
    return render(request, 'upload/import_data.html')


@login_required
@admin_required
@csrf_exempt
@require_http_methods(["POST"])
def import_preview(request):
    """
    Accept a CSV upload, parse it, and return a preview with:
    - total rows, new vs duplicate counts, invalid row count
    - column mapping (CSV col → DB field or 'ignored')
    - warnings (e.g. missing expected columns)
    """
    import pandas as pd

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    mode = request.POST.get('mode', 'skip')  # 'skip' or 'update'

    try:
        df = pd.read_csv(uploaded)
    except Exception as e:
        return JsonResponse({'error': f'Could not parse CSV: {e}'}, status=400)

    csv_columns = list(df.columns)

    # ── Validate: must have ACCESSION_NO ──
    if 'ACCESSION_NO' not in csv_columns:
        return JsonResponse({'error': 'CSV must contain an ACCESSION_NO column'}, status=400)

    # ── Build column mapping for display ──
    all_mapped = _IMPORT_COLUMN_MAP.copy()
    all_mapped.update(_IMPORT_DATETIME_COLS)
    all_mapped.update(_IMPORT_TIMEDELTA_COLS)
    # REPORT_TURN_AROUND_TIME is a special fallback
    all_mapped['REPORT_TURN_AROUND_TIME'] = 'time_to_clinical_decision_seconds (fallback)'

    column_mapping = []
    for col in csv_columns:
        if col in _IMPORT_IGNORE_COLS:
            continue
        db_field = all_mapped.get(col)
        column_mapping.append([col, db_field])  # None if unmapped

    # ── Warnings ──
    warnings = []
    unmapped = [col for col, db in column_mapping if db is None]
    if unmapped:
        warnings.append(f'{len(unmapped)} column(s) not mapped and will be ignored: {", ".join(unmapped[:5])}{"…" if len(unmapped) > 5 else ""}')

    # Check for expected columns that are missing
    expected = {'ACCESSION_NO', 'WORKPLACE', 'TEXT_REPORT', 'gt_llm', 'lunit_binarised'}
    missing_expected = expected - set(csv_columns)
    if missing_expected:
        warnings.append(f'Expected column(s) missing: {", ".join(sorted(missing_expected))}')

    # ── Count duplicates, valid, invalid ──
    total_rows = len(df)
    invalid_count = 0
    valid_accessions = []

    for _, row in df.iterrows():
        acc = row.get('ACCESSION_NO')
        if pd.isna(acc):
            invalid_count += 1
            continue
        try:
            valid_accessions.append(int(float(acc)))
        except (ValueError, TypeError):
            invalid_count += 1

    # De-duplicate within the CSV itself
    unique_accessions = list(dict.fromkeys(valid_accessions))  # preserves order
    csv_internal_dupes = len(valid_accessions) - len(unique_accessions)
    if csv_internal_dupes > 0:
        warnings.append(f'{csv_internal_dupes} duplicate accession number(s) within the CSV (only first occurrence will be used)')
        invalid_count += csv_internal_dupes

    # Check against DB
    existing_in_db = set(
        CXRStudy.objects.filter(
            accession_no__in=unique_accessions
        ).values_list('accession_no', flat=True)
    )

    duplicate_count = len(existing_in_db)
    new_count = len(unique_accessions) - duplicate_count

    return JsonResponse({
        'total_rows': total_rows,
        'new_count': new_count,
        'duplicate_count': duplicate_count,
        'invalid_count': invalid_count,
        'column_mapping': column_mapping,
        'warnings': warnings,
        'mode': mode,
    })


@login_required
@admin_required
@csrf_exempt
@require_http_methods(["POST"])
def import_confirm(request):
    """
    Actually import the CSV into the database.
    Supports two modes:
      - 'skip': only insert rows whose accession_no doesn't exist (default)
      - 'update': insert new rows AND update existing rows
    """
    import pandas as pd
    import uuid

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    mode = request.POST.get('mode', 'skip')

    try:
        df = pd.read_csv(uploaded)
    except Exception as e:
        return JsonResponse({'error': f'Could not parse CSV: {e}'}, status=400)

    if 'ACCESSION_NO' not in df.columns:
        return JsonResponse({'error': 'CSV must contain an ACCESSION_NO column'}, status=400)

    csv_columns = list(df.columns)
    batch_id = str(uuid.uuid4())

    new_count = 0
    updated_count = 0
    skipped_count = 0
    invalid_count = 0
    seen_accessions = set()  # track intra-CSV duplicates

    for _, row in df.iterrows():
        model_data, error = _sanitise_row(row, csv_columns)
        if error:
            invalid_count += 1
            continue

        accession_no = model_data['accession_no']

        # Skip intra-CSV duplicates (keep first occurrence)
        if accession_no in seen_accessions:
            invalid_count += 1
            continue
        seen_accessions.add(accession_no)

        model_data['processing_batch_id'] = batch_id

        existing = CXRStudy.get_or_none(accession_no)

        if existing:
            if mode == 'update':
                # Update existing record — only set non-None values
                for field, value in model_data.items():
                    if field == 'accession_no':
                        continue
                    if value is not None:
                        setattr(existing, field, value)
                try:
                    existing.save()
                    updated_count += 1
                except Exception as e:
                    print(f"Error updating accession {accession_no}: {e}")
                    traceback.print_exc()
            else:
                skipped_count += 1
        else:
            try:
                CXRStudy.objects.create(**model_data)
                new_count += 1
            except Exception as e:
                print(f"Error creating accession {accession_no}: {e}")
                traceback.print_exc()
                invalid_count += 1

    return JsonResponse({
        'new_count': new_count,
        'updated_count': updated_count,
        'skipped_count': skipped_count,
        'invalid_count': invalid_count,
        'batch_id': batch_id,
    })
