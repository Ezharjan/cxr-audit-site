"""Field catalogue adapter and embedded card semantics; legacy cards use their original catalogue."""
from copy import deepcopy
from dataclasses import replace
from .projects.prime import get_project_definition
from .projects.base import Source, Dimension


def configured_project(settings):
    from upload.configuration import validate_settings
    from upload.contract import VIRTUAL_FIELDS
    base = get_project_definition()
    sources, dimensions = dict(base.sources), dict(base.dimensions)
    for field_id, raw in settings.items():
        if field_id in VIRTUAL_FIELDS:
            continue
        if not isinstance(raw, dict):
            raise ValueError('Invalid field semantics.')
        config = validate_settings(field_id, {k:v for k,v in raw.items() if k != 'revision'})
        meaning = config['interpretation']
        kind = {'identifier':'record_id', 'integer':'score', 'measurement':'score', 'age':'score',
                'binary':'label', 'nominal':'label', 'ordered':'label', 'date_time':'timestamp',
                'duration':'duration', 'free_text':'text'}[meaning]
        if field_id.endswith('_seconds') and config['unit'] == 'seconds':
            kind = 'duration'
        matches = [s for s in sources.values() if s.field == field_id]
        for source in matches:
            sources[source.source_id] = replace(source, label=config['label'], unit=config['unit'], kind=kind)
        if not matches:
            name = 'field_' + field_id
            sources[name] = Source(name, kind, field_id, unit=config['unit'], label=config['label'])
        if meaning in {'nominal','ordered'} or config['bins']:
            names = [d.dimension_id for d in dimensions.values() if d.field == field_id] or ['field_' + field_id]
            for name in names:
                dimensions[name] = Dimension(name, field_id, label=config['label'],
                    allowed_values=tuple(config['categories']) if config['closed'] else None,
                    bands=tuple(tuple(b) for b in config['bins']))
    return replace(base, sources=sources, dimensions=dimensions)


def current_settings():
    from upload.configuration import field_catalog
    return {name:{k:v for k,v in value.items() if k not in {'storage_type','upload_allowed'}}
            for name,value in field_catalog('prime').items()}


def widget_project(widget):
    settings = widget.get('query', {}).get('field_semantics')
    return configured_project(settings) if settings is not None else get_project_definition()


def pin(widget, settings):
    if widget.get('type') in {'text','divider','heading'}:
        return widget
    query = widget['query']
    if 'field_semantics' in query:
        widget_project(widget)
        return widget
    project = configured_project(settings)
    fields = {project.source(source).field for source in query.get('inputs', {}).values()}
    controls = widget.get('controls', {})
    fields |= {project.dimension(d).field for d in controls.get('filters', []) + controls.get('compare_by', [])}
    query['field_semantics'] = {f:deepcopy(settings[f]) for f in fields if f in settings}
    return widget


def validate_bindings(widget, project):
    query = widget.get('query', {})
    settings = query.get('field_semantics', {})
    controls = widget.get('controls', {})
    if 'field_semantics' in query:
        required = {project.source(s).field for s in query.get('inputs', {}).values()}
        required |= {project.dimension(d).field for d in controls.get('filters', []) + controls.get('compare_by', [])}
        if set(settings) != required:
            raise ValueError('Field snapshot must cover exactly the referenced sources and dimensions.')
    roles = query.get('inputs', {})
    for role, source_id in roles.items():
        source = project.source(source_id)
        if role == 'value' and source.kind in {'record_id','identifier','text','timestamp'} and query.get('measurement') != 'record_count':
            raise ValueError('Identifiers and text cannot be aggregated numerically.')
    binary = [project.source(roles[r]) for r in ('ground_truth','prediction','alternative_reference') if r in roles]
    meanings = []
    for source in binary:
        config = settings.get(source.field)
        if config:
            if source.kind == 'label' and config['interpretation'] != 'binary':
                raise ValueError('Classification requires binary outcome sources.')
            if source.kind == 'score' and config['unit'] != '0-100':
                raise ValueError('Score scale is incompatible with the threshold policy.')
            if source.kind == 'label':
                meanings.append((tuple(config['categories']), config['positive_class']))
    if len(set(meanings)) > 1:
        raise ValueError('Outcome meanings and positive classes must match.')
    if query.get('measurement') == 'duration_summary':
        if any(project.source(s).unit != 'seconds' for s in roles.values()):
            raise ValueError('Duration summaries require seconds.')
