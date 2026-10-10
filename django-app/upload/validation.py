"""Shared validation/merge/classification. No processor calls or study writes."""
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from django.db import models
from .contract import FIELDS, FINDINGS, REQUIRED, ENRICHABLE, VIRTUAL_FIELDS, require_storage
from .configuration import validate_mapping, validate_format


def identifier(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('identifier_requires_exact_integer_token')
    token = str(value)
    if not re.fullmatch(r'(0|[1-9][0-9]{0,18})', token) or int(token) > 9223372036854775807:
        raise ValueError('invalid_identifier')
    return int(token)


def missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def typed_value(field_id, value, settings, rule=None):
    rule = rule or {}
    if missing(value) or str(value) in rule.get('missing', []):
        return None
    value = rule.get('values', {}).get(str(value), value)
    field = FIELDS[field_id]
    if field_id == 'accession_no':
        return identifier(value)
    if isinstance(field, models.DateTimeField):
        if not isinstance(value, str):
            raise ValueError('invalid_datetime')
        fmt = rule.get('date_format', 'iso')
        result = datetime.fromisoformat(value.replace('Z', '+00:00')) if fmt == 'iso' else datetime.strptime(value, fmt)
        if result.tzinfo is None:
            zone = ZoneInfo(rule.get('timezone', 'Asia/Singapore'))
            naive = result
            result = naive.replace(tzinfo=zone)
            from datetime import timezone as datetime_timezone
            if result.astimezone(datetime_timezone.utc).astimezone(zone).replace(tzinfo=None) != naive or result.utcoffset() != naive.replace(tzinfo=zone,fold=1).utcoffset():
                raise ValueError('ambiguous_or_nonexistent_local_time')
        value = result.isoformat()
    elif isinstance(field, models.IntegerField):
        if isinstance(value, bool):
            if settings.get('interpretation') != 'binary':
                raise ValueError('boolean_requires_binary_meaning')
            value = int(value)
        elif isinstance(value, float):
            if not value.is_integer() or abs(value) >= 10**15:
                raise ValueError('invalid_integer')
            value = int(value)
        elif not re.fullmatch(r'-?(0|[1-9][0-9]*)', str(value)):
            raise ValueError('invalid_integer')
        value = int(value)
        if not -2147483648 <= value <= 2147483647 or (field_id in ('patient_age', 'instances') and value < 0):
            raise ValueError('integer_out_of_range')
        if field_id == 'patient_age' and rule.get('unit', 'years') != 'years':
            divisor = {'months': 12, 'days': 365.25}.get(rule['unit'])
            if divisor is None or value % divisor:
                raise ValueError('age_requires_whole_years')
            value = int(value / divisor)
    elif isinstance(field, models.FloatField):
        if isinstance(value, bool) and settings.get('interpretation') != 'binary':
            raise ValueError('invalid_number')
        value = float(value)
        if not math.isfinite(value):
            raise ValueError('invalid_number')
        if field_id == 'report_turn_around_minutes' and value < 0:
            raise ValueError('negative_turnaround')
        if field_id in FINDINGS + ('abnormal', 'tuberculosis'):
            if rule.get('unit') == '0-1':
                value *= 100
            if not 0 <= value <= 100:
                raise ValueError('score_out_of_range')
    else:
        value = str(value).strip()
        if field.max_length and len(value) > field.max_length:
            raise ValueError('text_too_long')
    if settings.get('interpretation') == 'binary' and value not in (0, 1):
        raise ValueError('invalid_binary')
    if settings.get('closed') and str(value) not in settings.get('categories', []):
        raise ValueError('closed_vocabulary_violation')
    return value


def choose_layout(file, entry, formats, catalogue, *, api=False):
    headers = file['headers']
    if entry is not None:
        if not isinstance(entry, dict) or set(entry) - {'format_id', 'column_mapping'} or len(entry) != 1:
            raise ValueError('choose_exactly_one_mapping_mode')
        if 'format_id' in entry:
            matches = [f for f in formats if f['format_id'] == entry['format_id']]
            if not matches:
                raise ValueError('unknown_format')
            selected = matches[0]
            if api and set(headers) != set(selected['headers']):
                raise ValueError('format_header_mismatch')
            mapping = {h: selected['mapping'].get(h, '') for h in headers}
            rules = {h: r for h, r in selected['rules'].items() if h in headers and mapping.get(h)}
        else:
            selected, rules = None, {}
            mapping = entry['column_mapping']
    elif api:
        raise ValueError('upload_manifest_required')
    else:
        selected = next((f for f in formats if set(f['headers']) == set(headers)), None)
        if selected:
            mapping, rules = selected['mapping'], selected['rules']
        else:
            mapping, rules = {}, {}
            for header in headers:
                choices = [name for name, s in catalogue.items() if s['upload_allowed'] and header in s['aliases']]
                mapping[header] = choices[0] if len(choices) == 1 else ''
    mapping = validate_mapping(headers, mapping)
    validate_format(headers, mapping, rules)
    return dict(mapping=mapping, rules=rules, format_id=selected['format_id'] if selected else None,
                revision=selected['revision'] if selected else None)


def normalize(files, manifest, formats, catalogue, *, api=False):
    if manifest is not None and (not isinstance(manifest, list) or len(manifest) != len(files)):
        raise ValueError('manifest_file_count_mismatch')
    combined, provenance, details, errors = {}, {}, [], []
    invalid_ids = 0
    for index, file in enumerate(files):
        layout = choose_layout(file, manifest[index] if manifest is not None else None, formats, catalogue, api=api)
        details.append(dict(index=index, name=file['name'], rows=len(file['rows']), headers=file['headers'],
                            excluded=[h for h, d in layout['mapping'].items() if not d], **layout))
        for row_number, values in enumerate(file['rows'], 2):
            row = {}
            invalid = False
            for header, raw in zip(file['headers'], values):
                destination = layout['mapping'][header]
                if not destination:
                    continue
                try:
                    row[destination] = typed_value(destination, raw, catalogue[destination], layout['rules'].get(header))
                except (ValueError, TypeError, OverflowError):
                    invalid = True
                    if destination == 'accession_no':
                        invalid_ids += 1
                    if len(errors) < 100:
                        errors.append(dict(file=index, row=row_number, field=destination, code='invalid_value'))
            if not any(v is not None for v in row.values()) and not invalid:
                continue
            key = row.get('accession_no')
            if key is None:
                if not any(e.get('file') == index and e.get('row') == row_number and e.get('field') == 'accession_no' for e in errors):
                    invalid_ids += 1
                    if len(errors) < 100:
                        errors.append(dict(file=index, row=row_number, field='accession_no', code='missing_identifier'))
                continue
            target = combined.setdefault(str(key), {'accession_no': key})
            sources = provenance.setdefault(str(key), {})
            for name, v in row.items():
                if v is None:
                    continue
                if name in target and target[name] != v:
                    if len(errors) < 100:
                        errors.append(dict(file=index, row=row_number, field=name, code='conflicting_uploaded_values', previous=sources.get(name)))
                else:
                    target[name] = v
                    sources.setdefault(name, dict(file=index, row=row_number))
    return dict(rows=combined, provenance=provenance, files=details, errors=errors, invalid_raw_rows=invalid_ids)


def serialize(value):
    return value.isoformat() if isinstance(value, datetime) else value


def classify(normalized, existing):
    new, excluded, enrichment, baseline, disagreements, protected = {}, {}, {}, {}, [], []
    unchanged = 0
    for key, row in normalized['rows'].items():
        stored = existing.get(key)
        if stored is None:
            absent = sorted(f for f in REQUIRED if missing(row.get(f)))
            if absent:
                excluded[key] = absent
            else:
                new[key] = row
            continue
        additions = {}
        baseline[key] = {name: serialize(stored.get(name)) for name in row if name not in VIRTUAL_FIELDS}
        for name, value in row.items():
            if name == 'accession_no' or name in VIRTUAL_FIELDS:
                continue
            old = serialize(stored.get(name))
            if missing(old):
                if name in ENRICHABLE:
                    additions[name] = value
                elif len(protected) < 100:
                    protected.append(dict(field=name, code='derived_input_enrichment_protected'))
            elif old != value and len(disagreements) < 100:
                disagreements.append(dict(field=name, code='stored_value_preserved'))
        if additions:
            enrichment[key] = additions
        else:
            unchanged += 1
    summary = dict(total=len(normalized['rows']), new=len(new), new_complete=len(new),
                   new_incomplete=len(excluded), existing=len(existing), existing_enrichable=len(enrichment),
                   existing_unchanged=unchanged, invalid_raw_rows=normalized['invalid_raw_rows'],
                   errors=normalized['errors'], files=normalized['files'],
                   excluded_reasons=list(excluded.values())[:100], disagreements=disagreements,
                   protected_enrichment=protected, blocked=bool(normalized['errors']))
    return dict(new=new, enrichment=enrichment, baseline=baseline, excluded=excluded, summary=summary)


def prepare(files, manifest, formats, catalogue, project='prime', *, api=False):
    require_storage(project)
    normalized = normalize(files, manifest, formats, catalogue, api=api)
    from .models import CXRStudy
    existing = {str(row['accession_no']): row for row in CXRStudy.objects.filter(accession_no__in=list(normalized['rows'])).values()}
    return classify(normalized, existing)
