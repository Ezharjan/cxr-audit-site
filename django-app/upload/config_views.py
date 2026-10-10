"""Admin-only, optimistic revision checked configuration; never edits study results."""
import json
import re
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from agent_api.audit import record
from .configuration import seed, field_catalog, validate_settings, validate_format, suggest_bins
from .models import FieldConfiguration, UploadFormat, CXRStudy
from .workflow_views import manifest_value
from .views import _is_admin


@login_required
@require_http_methods(['GET', 'POST'])
def configuration(request):
    if not _is_admin(request.user):
        return JsonResponse({'error': 'Administrator required'}, status=403)
    if request.method == 'GET':
        return render(request, 'upload/configuration.html', {'catalogue': field_catalog('prime'),
            'formats': list(UploadFormat.objects.filter(project='prime').values())})
    try:
        action = request.POST.get('action')
        if action == 'seed':
            seed()
            record(request, 'upload_configuration_seed', project='prime')
            return JsonResponse({'ok': True})
        identifier = request.POST.get('identifier', '')
        revision = int(request.POST.get('revision', '0'))
        if action == 'profile':
            catalogue = field_catalog('prime')
            if identifier not in catalogue:
                raise ValueError('unknown_field')
            if catalogue[identifier]['interpretation'] not in {'measurement', 'age', 'duration', 'integer'}:
                return JsonResponse({'bins': [], 'categorical_suggestion': False})
            values = list(CXRStudy.objects.order_by('accession_no').values_list(identifier, flat=True)[:1000])
            bins = suggest_bins(values)
            record(request, 'upload_field_profile', project='prime', field=identifier, count=len(values))
            return JsonResponse({'bins': bins, 'categorical_suggestion': 1 <= len({v for v in values if v is not None}) <= 10,
                                 'sample_limit': 1000, 'advisory_only': True})
        settings = manifest_value(request.POST.get('settings', ''))
        with transaction.atomic():
            if action == 'field':
                values = validate_settings(identifier, settings)
                entry = FieldConfiguration.objects.select_for_update().filter(project='prime', field_id=identifier).first()
                if entry and entry.revision != revision or not entry and revision != 0:
                    return JsonResponse({'error': 'Configuration changed; refresh.'}, status=409)
                if entry is None:
                    entry = FieldConfiguration(project='prime', field_id=identifier, revision=0)
                entry.settings, entry.revision = values, entry.revision + 1
                entry.save()
            elif action == 'format':
                if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', identifier) or not isinstance(settings, dict) or set(settings) != {'name','headers','mapping','rules','role','position','enabled'}:
                    raise ValueError('invalid_format')
                validate_format(settings['headers'], settings['mapping'], settings['rules'], settings['role'])
                if not isinstance(settings['name'], str) or not 1 <= len(settings['name']) <= 128 or type(settings['position']) is not int or not 0 <= settings['position'] <= 10000 or type(settings['enabled']) is not bool:
                    raise ValueError('invalid_format_settings')
                entry = UploadFormat.objects.select_for_update().filter(project='prime', format_id=identifier).first()
                if entry and entry.revision != revision or not entry and revision != 0:
                    return JsonResponse({'error': 'Configuration changed; refresh.'}, status=409)
                if entry is None:
                    entry = UploadFormat(project='prime', format_id=identifier, revision=0)
                for name, value in settings.items():
                    setattr(entry, name, value)
                entry.revision += 1
                entry.save()
            else:
                raise ValueError('invalid_action')
            record(request, 'upload_configuration_change', project='prime', kind=action, identifier=identifier, revision=entry.revision)
        return JsonResponse({'ok': True, 'revision': entry.revision})
    except (ValueError, TypeError, KeyError):
        return JsonResponse({'error': 'Invalid settings. Storage type and upload permissions cannot be changed.'}, status=400)
