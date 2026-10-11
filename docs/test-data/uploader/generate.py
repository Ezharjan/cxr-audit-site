"""Generate fictional PRIME upload examples; no deployment database access."""
import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'django-app'))
os.environ['DJANGO_SETTINGS_MODULE'] = 'agent_api.test_settings'
import django
django.setup()
from upload.contract import FINDINGS, PROCESSOR_NAMES, UPLOAD_FIELDS
from openpyxl import Workbook

OUT = Path(__file__).resolve().parent
carpl = {PROCESSOR_NAMES[n]: n for n in sorted(UPLOAD_FIELDS) if n not in {'text_report', 'workplace', 'medical_location_name', 'ai_priority', 'ai_flag_received_date', 'procedure_start_date', 'procedure_end_date', 'patient_age', 'patient_gender'}}
carpl.pop('ACCESSION_NO')
carpl['Accession Number'] = 'accession_no'
ris = {PROCESSOR_NAMES[n]: n for n in ('accession_no', 'text_report', 'workplace', 'medical_location_name', 'ai_priority', 'ai_flag_received_date', 'procedure_start_date', 'procedure_end_date', 'patient_age', 'patient_gender')}
reports = [
    'SYNTHETIC TEST ONLY. Lungs are clear. No pleural effusion or pneumothorax.',
    'SYNTHETIC TEST ONLY. Right lower zone consolidation with a small right pleural effusion.',
    'SYNTHETIC TEST ONLY. Small left apical pneumothorax. No mediastinal shift.',
    'SYNTHETIC TEST ONLY. Cardiomegaly and bilateral interstitial pulmonary oedema.',
    'SYNTHETIC TEST ONLY. Stable calcified granuloma. No acute pulmonary abnormality.',
    'SYNTHETIC TEST ONLY. Mild bibasal atelectasis. No focal consolidation.',
]
source = []
for i, report in enumerate(reports):
    row = {n: '' for n in UPLOAD_FIELDS}
    row.update(accession_no=str(991011000001+i), text_report=report, workplace='SYNTH-A' if i%2 == 0 else 'SYNTH-B', medical_location_name='Fictional Test Hospital', patient_age=25+i*10, patient_gender='F' if i%2 == 0 else 'M', instances=1, study_description='SYNTHETIC Chest X-ray', patient_name=f'SYNTHETIC PERSON {i+1}', patient_id=f'SYNTH-{i+1:03}', study_id=f'SYNTH-STUDY-{i+1:03}', status='SYNTHETIC', abnormal=5 if i in (0,4) else 90, report_turn_around_minutes=30+i)
    row.update({f: 0 for f in FINDINGS})
    row[FINDINGS[i]] = 10+i*12
    if i == 1: row.update(consolidation=95, pleural_effusion=75)
    if i == 2: row['pneumothorax'] = 98
    if i == 3: row['cardiomegaly'] = 90
    for name in ('upload_date','inference_date','ai_flag_received_date','procedure_start_date','procedure_end_date'):
        row[name] = f'2026-10-10T{9+i:02}:00:00+08:00'
    source.append(row)

def write(name, mapping, rows):
    values = [[r.get(n, '') for n in mapping.values()] for r in rows]
    if name.endswith('.xlsx'):
        wb = Workbook(); ws = wb.active; ws.title = 'Synthetic'; ws.append(list(mapping))
        for row in values: ws.append(row)
        wb.save(OUT / name)
    else:
        with (OUT / name).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream); writer.writerow(mapping); writer.writerows(values)

write('carpl-valid.csv', carpl, source)
write('ris-valid.csv', ris, source)
write('ris-valid.xlsx', ris, source)
mixed = [dict(r) for r in source]; mixed[-1]['text_report'] = ''
write('ris-one-incomplete.csv', ris, mixed)
write('ris-all-incomplete.csv', ris, [dict(r, text_report='') for r in source])
write('ris-conflict.csv', ris, source + [dict(source[0], text_report='SYNTHETIC conflicting report.')])
write('ris-invalid-id.csv', ris, [dict(source[0], accession_no='991011000001.5')])
write('carpl-invalid-score.csv', carpl, [dict(source[0], pneumothorax='not-a-number')])
write('carpl-out-of-range.csv', carpl, [dict(source[0], pneumothorax=101)])
assert len({r['accession_no'] for r in source}) == 6
assert all(set(FINDINGS) <= set(r) and all(0 <= r[f] <= 100 for f in FINDINGS) for r in source)
assert all('SYNTHETIC TEST ONLY' in r['text_report'] for r in source)
write('existing-annotations.csv', {'Accession Number': 'accession_no', 'Comments': 'comments'}, [dict(source[0], comments='Conflicting synthetic comment'), dict(source[1], comments='New synthetic comment')])
print('Generated 10 synthetic upload fixtures; six distinct identifiers.')
