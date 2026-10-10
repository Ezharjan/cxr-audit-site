"""Populate only the localhost preview storage; run from the checkout root."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

repo = Path.cwd()
assert (repo / 'django-app/manage.py').is_file(), 'Run from the checkout root'
sys.path.insert(0, str(repo / 'django-app'))
os.environ['DJANGO_SETTINGS_MODULE'] = 'agent_api.test_settings'
from django.conf import settings

root = Path(tempfile.gettempdir()).resolve() / 'primer-full-preview'
settings.DATABASES = {
    alias: {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(root / (alias + '.sqlite3'))}
    for alias in ('default', 'audit')
}
settings.REPORT_V2_ROOT = root / 'reports'
assert (root / 'default.sqlite3').is_file(), 'Start the full preview server first'
import django
django.setup()
from django.db import transaction
from upload.models import CXRStudy
from report_v2.tests.factories import _synthetic_identity, _baseline_findings, _assert_synthetic_only
from report_v2.definitions.repository import DefinitionRepository
from report_v2.seed import report_seed_text, seed_text

rows = []
for index in range(2000):
    identity = _synthetic_identity(10000 + index)
    positive = index % 5 < 2
    predicted = positive if index % 7 else not positive
    study = CXRStudy(
        **identity, **_baseline_findings({'consolidation': 65.0 if predicted else 5.0}),
        workplace='SYNTH-SITE-A' if index % 2 else 'SYNTH-SITE-B',
        procedure_start_date=datetime(2025, 12, 12, tzinfo=timezone.utc) + timedelta(days=index % 290),
        gt_manual=int(positive), gt_llm=int(positive if index % 11 else not positive),
        llm_grade=3 if positive else 1, lunit_binarised=int(predicted),
        time_end_to_end_seconds=300 + index % 3600,
        time_to_clinical_decision_seconds=60 + index % 1800,
        processing_batch_id='synthetic-preview-2000',
    )
    _assert_synthetic_only(study)
    rows.append(study)
with transaction.atomic():
    existing = set(CXRStudy.objects.filter(accession_no__in=[r.pk for r in rows]).values_list('accession_no', flat=True))
    CXRStudy.objects.bulk_create([row for row in rows if row.pk not in existing])
count = CXRStudy.objects.filter(processing_batch_id='synthetic-preview-2000').count()
assert count == 2000, 'Synthetic accession collision or incomplete preview population'
repository = DefinitionRepository(settings.REPORT_V2_ROOT, project_id='prime')
for def_id, text in [('overview', report_seed_text()), ('showcase', seed_text('showcase.v1.yaml'))]:
    if repository.is_deleted(def_id):
        raise RuntimeError(f'{def_id} was deleted; refusing to recreate it')
    if repository.get_current_version(def_id):
        print(f'Preserved existing published {def_id}')
        continue
    draft = root / 'reports' / 'drafts' / def_id
    if draft.exists():
        if draft.read_text(encoding='utf-8') != text.replace('\r\n', '\n'):
            raise RuntimeError(f'{def_id} has an edited draft; publish it in the editor to preserve edits')
    else:
        repository.save_draft(def_id, text, expected_revision=None, create_only=True)
    repository.publish(def_id, draft.read_text(encoding='utf-8'))
    print(f'Published preview {def_id}')
print(f'Synthetic studies: {count}; storage: {root}')
