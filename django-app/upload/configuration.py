"""Validated declarative field/format settings and idempotent PRIME seeds."""
import math
import re
from django.db import models
from .contract import FIELDS, UPLOAD_FIELDS, PROCESSOR_NAMES, default_settings
from .models import FieldConfiguration, UploadFormat

INTERPRETATIONS = {'identifier', 'integer', 'measurement', 'age', 'binary', 'nominal', 'ordered', 'date_time', 'duration', 'free_text'}


def validate_settings(field_id, settings):
    if field_id not in FIELDS or not isinstance(settings, dict) or set(settings) - set(default_settings(field_id)):
        raise ValueError('invalid_field_settings')
    result = {**default_settings(field_id), **settings}
    f = FIELDS[field_id]
    meaning = result['interpretation']
    compatible = {'free_text', 'nominal', 'ordered'}
    if isinstance(f, models.IntegerField):
        compatible = {'integer', 'measurement', 'age', 'binary', 'nominal', 'ordered'}
    elif isinstance(f, models.FloatField):
        compatible = {'measurement', 'duration', 'binary', 'nominal', 'ordered'}
    elif isinstance(f, models.DateTimeField):
        compatible = {'date_time'}
    if field_id == 'accession_no':
        compatible = {'identifier'}
    if meaning not in compatible:
        raise ValueError('storage_incompatible_meaning')
    for key in ('label', 'unit'):
        if not isinstance(result[key], str) or len(result[key]) > 128 or (key == 'label' and not result[key].strip()):
            raise ValueError('invalid_label_or_unit')
    for key in ('aliases', 'categories'):
        values = result[key]
        if not isinstance(values, list) or len(values) > 128 or any(not isinstance(v, str) or not v.strip() or len(v) > 128 for v in values) or len(set(values)) != len(values):
            raise ValueError('invalid_aliases_or_categories')
    if type(result['closed']) is not bool or (result['closed'] and not result['categories']):
        raise ValueError('invalid_closed_vocabulary')
    if result['positive_class'] is not None and result['positive_class'] not in result['categories']:
        raise ValueError('invalid_positive_class')
    bins = result['bins']
    if not isinstance(bins, list) or len(bins) > 32:
        raise ValueError('invalid_bins')
    if bins and meaning not in {'measurement', 'age', 'duration', 'integer'}:
        raise ValueError('bins_require_measurement')
    if bins:
        from report_v2.projects.base import Dimension
        if any(not isinstance(b, list) or len(b) != 3 for b in bins):
            raise ValueError('invalid_bins')
        Dimension(field_id, field_id, bands=tuple(tuple(b) for b in bins))
        if bins[0][1] is not None or bins[-1][2] is not None or any(bins[i][2] != bins[i+1][1] for i in range(len(bins)-1)):
            raise ValueError('bins_must_cover_all_values')
    return result


def suggest_bins(values):
    values = [float(v) for v in values if v is not None and not isinstance(v, bool) and math.isfinite(float(v))]
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [['All values', None, None]]
    edges = [lo + (hi - lo) * i / 5 for i in range(1, 5)]
    return [[str(i + 1), lower, upper] for i, (lower, upper) in enumerate(zip([None] + edges, edges + [None]))]


def validate_mapping(headers, mapping):
    if not isinstance(mapping, dict) or set(mapping) - set(headers):
        raise ValueError('invalid_column_mapping')
    destinations = [v for v in mapping.values() if v is not None and v != '']
    if any(not isinstance(v, str) or v not in UPLOAD_FIELDS for v in destinations):
        raise ValueError('protected_or_unknown_destination')
    if len(destinations) != len(set(destinations)):
        raise ValueError('duplicate_destination')
    return {h: mapping.get(h) or '' for h in headers}


def validate_format(headers, mapping, rules, role='source'):
    if not isinstance(headers, list) or not 1 <= len(headers) <= 128 or any(not isinstance(h, str) or not h or h != h.strip() or len(h) > 128 for h in headers) or len(set(headers)) != len(headers):
        raise ValueError('invalid_headers')
    validate_mapping(headers, mapping)
    if role not in ('source', 'ris', 'carpl') or not isinstance(rules, dict) or set(rules) - set(headers):
        raise ValueError('invalid_format_rules')
    for h, rule in rules.items():
        if not mapping.get(h) or not isinstance(rule, dict) or set(rule) - {'missing', 'values', 'date_format', 'timezone', 'unit'}:
            raise ValueError('invalid_parsing_rule')
        if not isinstance(rule.get('missing', []), list) or len(rule.get('missing', [])) > 32 or any(not isinstance(v, str) or len(v) > 128 for v in rule.get('missing', [])):
            raise ValueError('invalid_missing_sentinels')
        translations = rule.get('values', {})
        if not isinstance(translations, dict) or len(translations) > 128 or any(not isinstance(k, str) or not isinstance(v, (str, int, float, bool)) or len(str(k)) > 128 or len(str(v)) > 128 for k, v in translations.items()):
            raise ValueError('invalid_value_map')
        if rule.get('date_format', 'iso') not in ('iso', 'source-export', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%Y-%m-%d', '%d/%m/%Y'):
            raise ValueError('unsupported_date_format')
        from zoneinfo import ZoneInfo
        ZoneInfo(rule.get('timezone', 'Asia/Singapore'))
        if rule.get('unit', '') not in ('', 'years', 'months', 'days', 'seconds', 'minutes', '0-1', '0-100'):
            raise ValueError('unsupported_unit')


def field_catalog(project):
    saved = {f.field_id: f for f in FieldConfiguration.objects.filter(project=project)}
    return {name: {**default_settings(name), **(saved[name].settings if name in saved else {}),
                   'revision': saved[name].revision if name in saved else 0,
                   'storage_type': field.get_internal_type(), 'upload_allowed': name in UPLOAD_FIELDS}
            for name, field in FIELDS.items()}


def seed(project='prime'):
    from .contract import require_storage
    require_storage(project)
    for name in FIELDS:
        FieldConfiguration.objects.get_or_create(project=project, field_id=name, defaults={'settings': default_settings(name)})
    carpl = {PROCESSOR_NAMES[n]: n for n in UPLOAD_FIELDS if n not in {'text_report', 'workplace', 'medical_location_name', 'ai_priority', 'ai_flag_received_date', 'procedure_start_date', 'procedure_end_date', 'patient_age', 'patient_gender'}}
    carpl.pop('ACCESSION_NO')
    carpl['Accession Number'] = 'accession_no'
    ris = {PROCESSOR_NAMES[n]: n for n in ('accession_no', 'text_report', 'workplace', 'medical_location_name', 'ai_priority', 'ai_flag_received_date', 'procedure_start_date', 'procedure_end_date', 'patient_age', 'patient_gender')}
    for name, mapping in (('carpl', carpl), ('ris', ris)):
        UploadFormat.objects.get_or_create(project=project, format_id='prime-' + name,
            defaults=dict(name='PRIME ' + name.upper() + ' example', headers=list(mapping), mapping=mapping, role=name))
