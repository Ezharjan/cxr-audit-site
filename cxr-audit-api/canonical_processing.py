"""Authenticated canonical PRIME processing; no legacy merge/coercion path."""
import math
from datetime import datetime
import pandas as pd

FINDINGS = ('atelectasis', 'calcification', 'cardiomegaly', 'consolidation', 'fibrosis', 'mediastinal_widening',
            'nodule', 'pleural_effusion', 'pneumoperitoneum', 'pneumothorax')
TEXT = {'study_id_anonymized', 'study_id', 'patient_name', 'patient_id', 'patient_gender', 'study_description',
        'workplace', 'medical_location_name', 'ai_report', 'ai_priority', 'feedback', 'comments', 'status', 'text_report'}
DATES = {'upload_date', 'inference_date', 'ai_flag_received_date', 'procedure_start_date', 'procedure_end_date'}
ALLOWED = TEXT | DATES | set(FINDINGS) | {'accession_no', 'patient_age', 'instances', 'abnormal', 'tuberculosis', 'report_turn_around_minutes'}


def validate_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 60000:
        raise ValueError('invalid_canonical_rows')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - ALLOWED or set(FINDINGS + ('accession_no', 'text_report', 'workplace')) - set(row):
            raise ValueError('invalid_canonical_fields')
        key = row['accession_no']
        if type(key) is not int or not 0 <= key <= 9223372036854775807 or key in seen:
            raise ValueError('invalid_canonical_identity')
        seen.add(key)
        for name, value in row.items():
            if value is None:
                if name in FINDINGS or name in ('text_report', 'workplace'):
                    raise ValueError('missing_required_input')
                continue
            if name in TEXT and (not isinstance(value, str) or len(value) > 65536 or (name in ('text_report', 'workplace') and not value.strip())):
                raise ValueError('invalid_canonical_text')
            if name in DATES:
                if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError('invalid_canonical_date')
            if name in set(FINDINGS) | {'abnormal', 'tuberculosis'} and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError('invalid_canonical_score')
            if name == 'report_turn_around_minutes' and (type(value) not in (int,float) or not math.isfinite(value) or value < 0):
                raise ValueError('invalid_turnaround')
            if name in ('patient_age', 'instances') and (type(value) is not int or not 0 <= value <= 2147483647):
                raise ValueError('invalid_canonical_integer')
    return rows


def process(rows, batch_processor, thresholds, supplemental=False):
    validate_rows(rows)
    df = pd.DataFrame({'TEXT_REPORT': [row['text_report'] for row in rows]})
    graded = batch_processor.process_full_pipeline(df, report_column='TEXT_REPORT',
        steps=['llm', 'lunit'] if supplemental else ['llm'], save_intermediate=False, strict=True)
    if len(graded) != len(rows) or list(graded.index) != list(df.index):
        raise ValueError('invalid_output_cardinality')
    outputs = []
    for index, row in enumerate(rows):
        result = dict(row)
        grade = graded.at[index, 'llm_grade']
        if isinstance(grade, bool) or not isinstance(grade, (int, float)) and not hasattr(grade, 'item'):
            raise ValueError('invalid_grade')
        grade = grade.item() if hasattr(grade, 'item') else grade
        if type(grade) is not int or not 1 <= grade <= 6:
            raise ValueError('invalid_grade')
        result.update(llm_grade=grade, gt_llm=int(grade > 1))
        # Preserve existing site policy selection and strict > thresholds.
        site = {'GLCR01':'GEY','KALCR01':'KAL','SEMCR01':'SEM','TPYCR01':'TPY','KHACR01':'KHA','WDLCR01':'WDL','HOUCR01':'HOU','AMKCR01':'AMK','YISCR01':'YIS','SERCR01':'SER'}.get(row['workplace'], 'default')
        policy = {**thresholds['default'], **thresholds.get(site, {})}
        result['lunit_binarised'] = int(any(row[f] > policy.get(f.replace('_', ' ').title(), policy.get(f, 15 if f == 'nodule' else 10)) for f in FINDINGS))
        if supplemental:
            for field in FINDINGS + ('tb',):
                source = field + '_llm' if field != 'tb' else 'tb'
                v = graded.at[index, source]
                v = v.item() if hasattr(v, 'item') else v
                if type(v) is not bool:
                    raise ValueError('invalid_supplemental_result')
                result['tb_llm' if field == 'tb' else source] = int(v)
        flag = row.get('ai_flag_received_date')
        end = row.get('procedure_end_date')
        if flag and end:
            seconds = (datetime.fromisoformat(flag) - datetime.fromisoformat(end)).total_seconds()
            result['time_end_to_end_seconds'] = 0 if row.get('ai_priority') == '4 URGENT' else seconds
            if row.get('ai_priority') == '5 AI_ROUTINE':
                result['time_to_clinical_decision_seconds'] = seconds
        if row.get('ai_priority') != '5 AI_ROUTINE' and row.get('report_turn_around_minutes') is not None:
            result['time_to_clinical_decision_seconds'] = row['report_turn_around_minutes'] * 60
        outputs.append(result)
    return outputs


def safe_csv(rows):
    import csv
    from io import StringIO
    stream = StringIO()
    names = list(dict.fromkeys(name for row in rows for name in row))
    writer = csv.DictWriter(stream, fieldnames=names)
    writer.writeheader()
    for row in rows:
        writer.writerow({name: "'" + value if isinstance(value,str) and (value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n'))) else value for name,value in row.items()})
    return stream.getvalue()
