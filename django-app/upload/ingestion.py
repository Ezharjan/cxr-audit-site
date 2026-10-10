"""Private previews, canonical dispatch and atomic study persistence."""
import json
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .configuration import field_catalog
from .contract import FINDINGS, ENRICHABLE, VIRTUAL_FIELDS, require_storage
from .models import CXRStudy, UploadFormat, UploadPreview, ProcessingTask
from .parsing import parse_files
from .submission import reserve_submission, release_submission, submit_files, SubmissionError
from .validation import prepare, serialize


def formats(project):
    return list(UploadFormat.objects.filter(project=project, enabled=True).values(
        'format_id', 'name', 'headers', 'mapping', 'rules', 'revision', 'position'))


def inspect(files, manifest, project='prime', *, api=False):
    require_storage(project)
    token = reserve_submission()
    try:
        parsed, digest = parse_files(files)
        catalogue = field_catalog(project)
        layouts = formats(project)
        payload = prepare(parsed, manifest, layouts, catalogue, project, api=api)
        if len(json.dumps(list(payload['new'].values())).encode()) > 48 * 1024**2:
            raise ValueError('canonical_payload_too_large')
        payload['catalogue'] = catalogue
        payload['formats'] = layouts
        return payload, digest
    finally:
        release_submission(token)


def cleanup():
    now = timezone.now()
    # Processing previews must survive while a required remote result is outstanding.
    UploadPreview.objects.filter(expires_at__lt=now, state__in=('ready','claiming','unknown')).delete()
    for preview in UploadPreview.objects.filter(task__status__in=('failed', 'cancelled')).exclude(state='failed'):
        preview.payload = {}
        preview.summary = public_counts(preview.summary)
        preview.state = 'failed'
        preview.save(update_fields=['payload', 'summary', 'state'])
    UploadPreview.objects.filter(expires_at__lt=now, state__in=('complete', 'failed')).delete()


def create_preview(owner, project, payload, digest, supplemental=False):
    cleanup()
    if payload['summary']['blocked']:
        raise ValueError('invalid_submission')
    if UploadPreview.objects.filter(owner=owner, state='ready').count() >= 3 or UploadPreview.objects.filter(state='ready').count() >= 100:
        raise ValueError('preview_limit')
    payload['supplemental_steps'] = supplemental
    return UploadPreview.objects.create(owner=owner, project=project, digest=digest, payload=payload,
        summary=payload['summary'], expires_at=timezone.now() + timedelta(minutes=30))


def check_current(payload):
    absent = list(payload['new']) + list(payload['excluded'])
    if CXRStudy.objects.filter(accession_no__in=absent).exists():
        raise SubmissionError('preview_stale_refresh_required', 409)
    existing = {str(row['accession_no']): row for row in CXRStudy.objects.select_for_update().filter(accession_no__in=payload['baseline']).values()}
    for key, baseline in payload['baseline'].items():
        if key not in existing or any(serialize(existing[key][name]) != value for name, value in baseline.items()):
            raise SubmissionError('preview_stale_refresh_required', 409)


def commit_studies(preview, outputs):
    payload = preview.payload
    expected = payload['new']
    if not isinstance(outputs, list) or len(outputs) != len(expected):
        raise ValueError('invalid_output_cardinality')
    required = {'llm_grade', 'gt_llm', 'lunit_binarised'}
    if payload['supplemental_steps']:
        required |= {f + '_llm' for f in FINDINGS} | {'tb_llm'}
    allowed = required | {'time_end_to_end_seconds', 'time_to_clinical_decision_seconds'}
    seen = set()
    studies = []
    for output in outputs:
        if not isinstance(output, dict) or type(output.get('accession_no')) is not int:
            raise ValueError('invalid_output_identity')
        key = str(output['accession_no'])
        if key not in expected or key in seen:
            raise ValueError('unexpected_or_duplicate_output')
        seen.add(key)
        source = expected[key]
        if any(output.get(name) != value for name, value in source.items()) or set(output) - set(source) - allowed or required - set(output):
            raise ValueError('invalid_output_schema')
        if type(output['llm_grade']) is not int or not 1 <= output['llm_grade'] <= 6 or output['gt_llm'] != int(output['llm_grade'] > 1):
            raise ValueError('invalid_output_grade')
        for name in required - {'llm_grade'}:
            if type(output[name]) is not int or output[name] not in (0, 1):
                raise ValueError('invalid_output_binary')
        for name in allowed - required:
            if name in output:
                import math
                if type(output[name]) not in (int, float) or not math.isfinite(output[name]):
                    raise ValueError('invalid_output_duration')
        study = CXRStudy(**{name:value for name,value in output.items() if name not in VIRTUAL_FIELDS}, processing_batch_id=str(preview.task_id or preview.pk))
        study.full_clean(validate_unique=False, validate_constraints=False)
        studies.append(study)
    check_current(payload)
    for study in studies:
        study.save(force_insert=True)
    for key, additions in payload['enrichment'].items():
        if set(additions) - ENRICHABLE:
            raise ValueError('protected_enrichment')
        baseline = payload['baseline'][key]
        if not CXRStudy.objects.filter(accession_no=key, **{name: baseline[name] for name in additions}).update(**additions, updated_at=timezone.now()):
            raise SubmissionError('preview_stale_refresh_required', 409)
    return len(studies), payload['summary']['existing']


def confirm(preview, *, before_dispatch=None, track=None):
    if preview.state == 'submitted' and preview.task_id:
        return dict(task_id=preview.task_id, status=preview.task.status, counts=preview.summary)
    if preview.state == 'complete':
        return dict(status='completed', counts=preview.summary)
    if preview.state != 'ready' or preview.expires_at <= timezone.now():
        raise SubmissionError('preview_expired_or_used', 409)
    if not UploadPreview.objects.filter(pk=preview.pk, state='ready').update(state='claiming'):
        raise SubmissionError('preview_already_claimed', 409)
    dispatched = False
    try:
        with transaction.atomic():
            check_current(preview.payload)
        if not preview.payload['new']:
            token = reserve_submission()
            try:
                if before_dispatch:
                    before_dispatch()
                with transaction.atomic():
                    # Obtain SQLite write lock before reads; all accepted writes stay in this transaction.
                    UploadPreview.objects.filter(pk=preview.pk).update(state='claiming')
                    commit_studies(preview, [])
                    preview.state, preview.payload = 'complete', {}
                    preview.summary = public_counts(preview.summary)
                    preview.save(update_fields=['state', 'payload', 'summary'])
                return dict(status='completed', counts=preview.summary)
            finally:
                release_submission(token)
        def bind(task):
            nonlocal dispatched
            preview.task, preview.state = task, 'submitted'
            preview.save(update_fields=['task', 'state'])
            dispatched = True
            if track:
                track(task)
        def recheck():
            if before_dispatch:
                before_dispatch()
            with transaction.atomic():
                check_current(preview.payload)
        result = submit_files([], preview.payload['supplemental_steps'], endpoint='analyze-canonical',
            canonical={'project': preview.project, 'rows': list(preview.payload['new'].values()),
                       'supplemental_steps': preview.payload['supplemental_steps']},
            before_dispatch=recheck, track=bind)
        return {**result, 'counts': preview.summary}
    except SubmissionError as exc:
        if exc.ambiguous:
            UploadPreview.objects.filter(pk=preview.pk).update(state='unknown')
        elif not dispatched:
            UploadPreview.objects.filter(pk=preview.pk).update(state='ready')
        raise
    except Exception:
        if not dispatched:
            UploadPreview.objects.filter(pk=preview.pk).update(state='failed', payload={}, summary=public_counts(preview.summary))
        raise


def public_counts(summary):
    return {name:value for name,value in summary.items() if type(value) in (int,bool)}
