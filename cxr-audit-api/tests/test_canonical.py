import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import pandas as pd
API_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(API_ROOT))
sys.path.insert(0,str(API_ROOT / 'cxr_audit'))
os.chdir(API_ROOT)
from canonical_processing import FINDINGS, process, validate_rows


def source(**extra):
    return {'accession_no':9007199254740993,'text_report':'Synthetic normal report.',
            'workplace':'SYN',**{f:0.0 for f in FINDINGS},**extra}


class CanonicalTests(unittest.TestCase):
    def test_complete_fixture_and_required_steps_no_intermediate_phi_files(self):
        processor=Mock()
        processor.process_full_pipeline.return_value=pd.DataFrame({'llm_grade':[1]})
        rows=process([source()],processor,{'default':{}},False)
        self.assertEqual(rows[0]['accession_no'],9007199254740993)
        self.assertEqual(rows[0]['gt_llm'],0)
        self.assertEqual(rows[0]['lunit_binarised'],0)
        self.assertFalse(processor.process_full_pipeline.call_args.kwargs['save_intermediate'])
        self.assertTrue(processor.process_full_pipeline.call_args.kwargs['strict'])

    def test_protected_destination_invalid_required_values_and_duplicate_id(self):
        for rows in ([source(llm_grade=1)], [source(atelectasis='bad')], [source(text_report='')],
                     [source(accession_no=1.0)], [source(),source()], [source(patient_age=True)]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):validate_rows(rows)

    def test_required_failure_and_invalid_output_never_default_to_normal(self):
        processor=Mock()
        processor.process_full_pipeline.side_effect=ValueError('synthetic failure')
        with self.assertRaises(ValueError):process([source()],processor,{'default':{}})
        processor.process_full_pipeline.side_effect=None
        for grade in (0,7,'1',True,None):
            processor.process_full_pipeline.return_value=pd.DataFrame({'llm_grade':[grade]})
            with self.subTest(grade=grade),self.assertRaises(ValueError):process([source()],processor,{'default':{}})
        processor.process_full_pipeline.return_value=pd.DataFrame({'llm_grade':[6]})
        self.assertEqual(process([source()],processor,{'default':{}})[0]['gt_llm'],1)

    def test_site_policy_timing_and_supplemental_outputs(self):
        processor=Mock();processor.process_full_pipeline.return_value=pd.DataFrame({
            'llm_grade':[2],**{f+'_llm':[False] for f in FINDINGS},'tb':[True]})
        row=source(workplace='YISCR01',nodule=6,report_turn_around_minutes=0,
            ai_flag_received_date='2026-10-11T10:02:00+08:00',procedure_end_date='2026-10-11T10:00:00+08:00')
        output=process([row],processor,{'default':{'Nodule':15},'YIS':{'Nodule':5}},True)[0]
        self.assertEqual(output['lunit_binarised'],1);self.assertEqual(output['tb_llm'],1)
        self.assertEqual(output['time_end_to_end_seconds'],120);self.assertEqual(output['time_to_clinical_decision_seconds'],0)
        processor.process_full_pipeline.return_value['tb']=['false']
        with self.assertRaises(ValueError):process([row],processor,{'default':{}},True)


class StrictBatchTests(unittest.TestCase):
    def test_failed_grading_and_incomplete_findings_cannot_be_normal_results(self):
        from cxr_audit.grade_batch_async import BatchCXRProcessor
        batch = BatchCXRProcessor({}, {}, {}, max_workers=1)
        classifier = Mock()
        with patch.object(batch, '_get_classifier', return_value=classifier):
            classifier.gradeReportLLM.side_effect = RuntimeError('synthetic failure')
            with self.assertRaises(ValueError): batch.process_llm_batch(pd.DataFrame({'REPORT':['Synthetic report']}), strict=True)
            classifier.gradeLunit.return_value = {}
            with self.assertRaises(ValueError): batch.process_lunit_batch(pd.DataFrame({'REPORT':['Synthetic report']}), strict=True)

    def test_endpoint_auth_and_invalid_input_never_dispatch(self):
        import os
        from fastapi.testclient import TestClient
        from combined_server import api_app
        with patch.dict(os.environ, {'DJANGO_DEBUG':'False','API_SECRET_KEY':'synthetic-secret'}), patch('combined_server.process_canonical_sync') as dispatched:
            client = TestClient(api_app)
            payload = dict(project='prime',rows=[source()],supplemental_steps=False)
            self.assertEqual(client.post('/analyze-canonical',json=payload).status_code,403)
            payload['rows'][0]['gt_llm']=1
            self.assertEqual(client.post('/analyze-canonical',json=payload,headers={'X-API-Key':'synthetic-secret'}).status_code,400)
            dispatched.assert_not_called()
