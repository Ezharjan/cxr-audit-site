"""PRIME-owned storage and processing permissions. Configuration cannot expand them."""
from .models import CXRStudy

FINDINGS = ('atelectasis', 'calcification', 'cardiomegaly', 'consolidation', 'fibrosis',
            'mediastinal_widening', 'nodule', 'pleural_effusion', 'pneumoperitoneum', 'pneumothorax')
GENERATED = frozenset(('gt_llm', 'lunit_binarised', 'llm_grade', 'tb_llm',
                      'time_end_to_end_seconds', 'time_to_clinical_decision_seconds',
                      'created_at', 'updated_at', 'processing_batch_id', 'gt_manual') +
                     tuple(f + '_llm' for f in FINDINGS))
FIELDS = {f.name: f for f in CXRStudy._meta.fields}
from django.db import models
FIELDS['report_turn_around_minutes'] = models.FloatField(null=True, blank=True, name='report_turn_around_minutes', verbose_name='Report turnaround (minutes)')
VIRTUAL_FIELDS = frozenset({'report_turn_around_minutes'})
UPLOAD_FIELDS = frozenset(FIELDS) - GENERATED
REQUIRED = frozenset(('accession_no', 'text_report', 'workplace') + FINDINGS)
# Timing and demographics feed existing derived outputs: enrich only independent source annotations.
ENRICHABLE = frozenset(('study_id_anonymized', 'study_id', 'patient_name', 'patient_id',
                       'study_description', 'feedback', 'comments', 'status'))
PROCESSOR_NAMES = {
    'report_turn_around_minutes': 'REPORT_TURN_AROUND_TIME',
    'accession_no': 'ACCESSION_NO', 'text_report': 'TEXT_REPORT', 'workplace': 'WORKPLACE',
    'patient_age': 'PATIENT_AGE', 'patient_gender': 'PATIENT_GENDER',
    'medical_location_name': 'MEDICAL_LOCATION_NAME', 'ai_priority': 'AI_PRIORITY',
    'ai_flag_received_date': 'AI_FLAG_RECEIVED_DATE', 'procedure_start_date': 'PROCEDURE_START_DATE',
    'procedure_end_date': 'PROCEDURE_END_DATE', 'upload_date': 'Upload Date',
    'inference_date': 'Inference Date', 'study_id_anonymized': 'StudyIDAnonymized',
    'study_id': 'StudyID', 'patient_name': 'Patient Name', 'patient_id': 'Patient ID',
    'instances': 'Instances', 'study_description': 'Study Description', 'abnormal': 'Abnormal',
    'ai_report': 'AI Report', 'feedback': 'Feedback', 'comments': 'Comments', 'status': 'Status',
    **{f: f.replace('_', ' ').title() for f in FINDINGS + ('tuberculosis',)},
}
ALIASES = {f: [f, PROCESSOR_NAMES.get(f, f)] for f in UPLOAD_FIELDS}
ALIASES['accession_no'] += ['anon_id', 'Accession Number']
ALIASES['procedure_end_date'] += ['PROCEDURE_DATE']
ALIASES['medical_location_name'] += ['Institute']


def require_storage(project):
    if project != 'prime':
        raise ValueError('project_storage_unavailable')


def default_settings(field_id):
    from django.db import models
    f = FIELDS[field_id]
    interpretation = 'free_text'
    unit = ''
    if isinstance(f, models.DateTimeField):
        interpretation = 'date_time'
    elif isinstance(f, models.IntegerField):
        interpretation = 'integer'
    elif isinstance(f, models.FloatField):
        interpretation = 'measurement'
    if field_id == 'accession_no':
        interpretation = 'identifier'
    elif field_id == 'patient_age':
        interpretation, unit = 'age', 'years'
    elif field_id in FINDINGS + ('abnormal', 'tuberculosis'):
        interpretation, unit = 'measurement', '0-100'
    elif field_id.endswith('_llm') or field_id in ('gt_manual', 'lunit_binarised'):
        interpretation = 'binary'
    elif field_id == 'report_turn_around_minutes':
        interpretation, unit = 'duration', 'minutes'
    elif field_id.endswith('_seconds'):
        interpretation, unit = 'measurement', 'seconds'
    elif field_id in ('patient_gender', 'workplace', 'ai_priority', 'status'):
        interpretation = 'nominal'
    bins = []
    if field_id == 'patient_age':
        from report_v2.projects.prime import get_project_definition
        bins = [list(b) for b in get_project_definition().dimension('age').bands]
        bins[0][1] = None
    return dict(label='anon_id' if field_id == 'accession_no' else 'LLM Grade (1-6)' if field_id == 'llm_grade' else str(f.verbose_name),
                interpretation=interpretation, unit=unit, aliases=list(dict.fromkeys(ALIASES.get(field_id, []))),
                categories=['normal', 'abnormal'] if interpretation == 'binary' else [], closed=False, bins=bins, positive_class='abnormal' if interpretation == 'binary' else None)
