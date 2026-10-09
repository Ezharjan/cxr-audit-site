# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""Read-only visual editor transformations. YAML remains the only draft representation."""
from copy import deepcopy
import re
import yaml

from .loader import load_report_definition, flat_report_definition
from .validation import validate_display, DisplayValidationError
from ..projects.prime import get_project_definition
from ..seeding import COLUMN_ALLOW_LIST

STATIC_TYPES = {'text', 'divider', 'heading'}
PRESETS = {'value': [3, 3], 'table': [12, 5], 'line': [6, 6], 'bar': [6, 6],
           'pie': [6, 6], 'confusion_matrix': [6, 7], 'boxplot': [6, 6],
           'text': [12, 2], 'divider': [12, 1], 'heading': [12, 1]}


def catalog():
    project = get_project_definition()
    return {
        'presets': PRESETS,
        'measurements': [{'id': m.measurement_id, 'label': m.measurement_id.replace('_', ' ').capitalize(),
                          'description': m.description, 'inputs': dict(m.inputs),
                          'optional_inputs': dict(m.optional_inputs), 'displays': m.supported_displays,
                          'comparison': m.supports_comparison, 'units': dict(m.units),
                          'columns': COLUMN_ALLOW_LIST.get(m.measurement_id, [])}
                         for m in project.measurements.values()],
        'sources': [{'id': s.source_id, 'label': s.label or s.source_id.replace('_', ' ').capitalize(),
                     'kind': s.kind} for s in project.sources.values()],
        'dimensions': [{'id': d.dimension_id, 'label': d.label or d.dimension_id}
                       for d in project.dimensions.values()],
        'policies': [{'id': p.ref, 'label': p.policy_id.replace('_', ' ').capitalize() + ' (v' + str(p.version) + ')',
                      'findings': dict(p.findings), 'score_scale': list(p.score_scale)}
                     for p in project.policies.values()],
        'cohorts': [{'id': c.cohort_id, 'label': c.description or c.cohort_id}
                    for c in project.cohorts.values()],
    }


def validate_card(widget):
    if widget['type'] in STATIC_TYPES:
        return
    project = get_project_definition()
    query = widget['query']
    measurement = project.measurements.get(query['measurement'])
    if not measurement or widget['type'] not in measurement.supported_displays:
        raise ValueError('Choose a measurement compatible with this card type.')
    inputs = query['inputs']
    allowed = {**measurement.inputs, **measurement.optional_inputs}
    if set(measurement.inputs) - set(inputs) or set(inputs) - set(allowed):
        raise ValueError('Choose all required data sources for this measurement.')
    score_prediction = False
    for role, source_id in inputs.items():
        source = project.sources.get(source_id)
        if source is None:
            raise ValueError('Choose a registered data source.')
        score = role == 'prediction' and source.kind == 'score'
        if source.kind != allowed[role] and not score:
            raise ValueError('The selected data source does not match its role.')
        score_prediction |= score
    if (score_prediction or 'lunit_findings' in inputs.values()) and query.get('threshold_policy') not in {p.ref for p in project.policies.values()}:
        raise ValueError('Choose a threshold policy for score predictions.')
    if query.get('cohort') and query['cohort'] not in project.cohorts:
        raise ValueError('Choose a registered population.')
    controls = widget['controls']
    if any(d not in project.dimensions for d in controls['filters'] + controls['compare_by']):
        raise ValueError('Choose registered filters and grouping dimensions.')
    if widget.get('default_compare_by') and widget['default_compare_by'] not in controls['compare_by']:
        raise ValueError('The default group must be one of the permitted groups.')
    if controls['compare_by'] and not measurement.supports_comparison:
        raise ValueError('This measurement does not support comparison groups.')
    if widget['window']['end'] != 'D' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', widget['window']['end']):
        raise ValueError('Choose D or a calendar date for the end of the window.')
    if widget['type'] == 'line' and not widget.get('bucket'):
        raise ValueError('Choose a time interval for the line chart.')
    if widget.get('bucket') and widget['type'] != 'line':
        raise ValueError('Time intervals apply only to line charts.')
    ci = widget['ci']
    if ci.get('enabled') and query['measurement'] not in {'accuracy','sensitivity','specificity'}:
        raise ValueError('Confidence intervals are available for accuracy, sensitivity and specificity.')
    if not ci.get('enabled') and ci.get('method'):
        raise ValueError('Remove the confidence interval method when intervals are disabled.')
    if widget['type'] == 'pie' and set(widget.get('options', {})) - {'donut'}:
        raise ValueError('Only the doughnut option applies to pie charts.')
    if widget['type'] == 'boxplot' and set(widget.get('options', {})) - {'whiskers'}:
        raise ValueError('Only whisker settings apply to box plots.')
    if widget['type'] == 'confusion_matrix' and set(widget.get('options', {})) - {'normalization'}:
        raise ValueError('Only normalization applies to confusion matrices.')
    if widget['type'] not in {'pie','boxplot','confusion_matrix'} and widget.get('options'):
        raise ValueError('These appearance settings do not apply to this card type.')
    validate_display(widget, allowed_inputs=allowed,
                     declared_columns=COLUMN_ALLOW_LIST.get(query['measurement']),
                     declared_threshold_policies={p.ref for p in project.policies.values()},
                     allowed_grouping=project.dimensions, measure_unit=next(iter(measurement.units.values()), None),
                     ci_methods={'wilson'})


def project_document(text):
    document = load_report_definition(text)
    validate_display(document)
    if document['project'] != 'prime':
        raise ValueError('Choose a report from the current project.')
    section_ids = [s['id'] for s in document['sections']]
    ids = [w['id'] for s in document['sections'] for w in s['widgets']]
    if len(set(ids)) != len(ids) or len(set(section_ids)) != len(section_ids):
        raise ValueError('Section and card identifiers must be unique.')
    flat = flat_report_definition(document)
    document["sections"] = [{"id": "canvas", "title": "", "widgets": flat["widgets"]}]
    return document


def transform(text, operation):
    document = project_document(text)
    if not isinstance(operation, dict) or operation.get('action') not in {'add','edit','delete','move','resize'}:
        raise ValueError('Choose a supported card action.')
    section = next((s for s in document['sections'] if s['id'] == operation.get('section_id')), None)
    if section is None and operation.get('section_id') in {s['id'] for s in load_report_definition(text)['sections']} and len(document['sections']) == 1:
        section = document['sections'][0]
    if section is None:
        raise ValueError('Choose an existing canvas.')
    cards = section['widgets']
    action = operation['action']
    card = next((w for w in cards if w['id'] == operation.get('widget_id')), None)
    if action != 'add' and card is None:
        raise ValueError('Choose a card in this section.')
    if action in {'add','edit'}:
        patch = operation.get('card')
        if not isinstance(patch, dict):
            raise ValueError('Provide the card options.')
        candidate = deepcopy(patch)
        if action == 'add':
            used = {w['id'] for s in document['sections'] for w in s['widgets']}
            index = 1
            while 'card_' + str(index) in used:
                index += 1
            candidate['id'] = 'card_' + str(index)
        elif candidate.get('id') != card['id']:
            raise ValueError('Card identity cannot change.')
        if action == 'edit':
            cards[cards.index(card)] = candidate
        else:
            index = operation.get('index', len(cards))
            if type(index) is not int or not 0 <= index <= len(cards):
                raise ValueError('Choose a valid position in this section.')
            cards.insert(index, candidate)
    elif action == 'delete':
        if len(cards) == 1:
            raise ValueError('Keep at least one card in this section.')
        cards.remove(card)
    elif action == 'move':
        index = operation.get('index')
        if type(index) is not int or not 0 <= index < len(cards):
            raise ValueError('Choose a valid position in this section.')
        cards.remove(card)
        cards.insert(index, card)
    else:
        card['layout'] = operation.get('layout')
    serialized = yaml.safe_dump(flat_report_definition(document), sort_keys=False, allow_unicode=True)
    checked = project_document(serialized)
    if action in {'add','edit'}:
        validate_card(next(w for s in checked['sections'] for w in s['widgets'] if w['id'] == candidate['id']))
    return serialized, checked
