"""Cookie-authenticated configurable upload workflow; all mutations require CSRF."""
import json
from django.core.exceptions import ValidationError
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from agent_api.audit import record
from .ingestion import inspect, create_preview, confirm, formats
from .configuration import field_catalog
from .models import UploadPreview
from .submission import SubmissionError


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def manifest_value(raw):
    if not isinstance(raw, str) or len(raw) > 65536:
        raise ValueError('invalid_upload_manifest')
    return json.loads(raw, object_pairs_hook=unique_object)


@login_required
@require_http_methods(['POST'])
def precheck(request):
    try:
        if set(request.POST) - {'csrfmiddlewaretoken', 'upload_manifest', 'supplemental_steps', 'previous_preview'} or set(request.FILES) - {'files'}:
            raise ValueError('unexpected_fields')
        for name in ('upload_manifest', 'supplemental_steps', 'previous_preview'):
            if len(request.POST.getlist(name)) > 1:
                raise ValueError('duplicate_metadata')
        steps = request.POST.get('supplemental_steps', 'false')
        if steps not in ('true', 'false'):
            raise ValueError('invalid_supplemental_steps')
        manifest = manifest_value(request.POST['upload_manifest']) if 'upload_manifest' in request.POST else None
        payload, digest = inspect(request.FILES.getlist('files'), manifest)
        previous = request.POST.get('previous_preview')
        if previous:
            UploadPreview.objects.filter(pk=previous, owner=request.user, project='prime', state='ready').delete()
        body = {**payload['summary'], 'fields': {k: v for k, v in payload['catalogue'].items() if v['upload_allowed']},
                'formats': payload['formats']}
        if not body['blocked']:
            preview = create_preview(request.user, 'prime', payload, digest, steps == 'true')
            body['preview_id'] = str(preview.pk)
        record(request, 'upload_preview', project='prime', count=body['total'], blocked=body['blocked'])
        return JsonResponse(body)
    except SubmissionError as exc:
        return JsonResponse({'error': exc.code}, status=exc.status)
    except (ValueError, TypeError, KeyError, ValidationError):
        return JsonResponse({'error': 'Invalid files or mappings; check file limits, identifiers and formats.'}, status=400)


@login_required
@require_http_methods(['POST'])
def submit(request):
    try:
        if set(request.POST) - {'csrfmiddlewaretoken', 'preview_id'} or request.FILES or len(request.POST.getlist('preview_id')) != 1:
            raise ValueError('preview_required')
        preview = UploadPreview.objects.filter(pk=request.POST['preview_id'], owner=request.user, project='prime').first()
        if preview is None:
            return JsonResponse({'error': 'Preview not found'}, status=404)
        def audit():
            record(request, 'upload_confirm', project='prime', count=preview.summary['new_complete'],
                   enrichable=preview.summary['existing_enrichable'])
        return JsonResponse(confirm(preview, before_dispatch=audit))
    except SubmissionError as exc:
        return JsonResponse({'error': exc.code}, status=exc.status)
    except (ValueError, TypeError, KeyError, ValidationError):
        return JsonResponse({'error': 'Preview required; upload and check your files first.'}, status=400)


@login_required
@require_http_methods(['GET', 'POST'])
def retired_import(request):
    from .views import _is_admin
    if not _is_admin(request.user):
        return JsonResponse({'error': 'Administrator required'}, status=403)
    return JsonResponse({'error': 'Historical import retired. Use Upload with source column mappings. Generated LLM results cannot be imported; manual GT uses its dedicated page.'}, status=410)
