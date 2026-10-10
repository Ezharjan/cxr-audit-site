import json
import uuid
from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, TransactionTestCase
from django.utils import timezone
from .configuration import field_catalog, seed, validate_settings, validate_format, suggest_bins
from .contract import FINDINGS, FIELDS, GENERATED, default_settings
from .ingestion import inspect, create_preview, confirm, commit_studies, cleanup
from .models import CXRStudy, UploadFormat, UploadPreview, FieldConfiguration, ProcessingTask
from .parsing import parse_files
from .submission import persist_result, SubmissionError
from .validation import identifier, normalize, classify, typed_value


def source(key=990000001, **extra):
    return {'accession_no':key,'text_report':'Synthetic normal chest report.','workplace':'SYN',
            **{f:0.0 for f in FINDINGS}, **extra}


def csv_file(rows=None):
    import csv
    from io import StringIO
    rows = rows or [source()]
    stream=StringIO(); writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return SimpleUploadedFile('synthetic.csv',stream.getvalue().encode(),'text/csv')


def outputs(rows):
    return [{**r,'llm_grade':1,'gt_llm':0,'lunit_binarised':0} for r in rows]


class ValidationTests(TestCase):
    databases={'default','audit'}

    def setUp(self):
        self.admin=User.objects.create_superuser('synthetic-config','synthetic@example.invalid','synthetic-pass')
        self.user=User.objects.create_user('synthetic-user',password='synthetic-pass')
        self.catalogue=field_catalog('prime')

    def test_exact_identifiers_and_unsupported_tokens(self):
        self.assertEqual(identifier('9007199254740993'),9007199254740993)
        for bad in ('01','1.0','1e3','x','-1','9223372036854775808',True,1.0,None,' 1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError): identifier(bad)

    def normalize(self, rows, headers=None, manifest=None):
        headers=headers or list(rows[0])
        files=[dict(name='synthetic.csv',headers=headers,rows=[[r.get(h) for h in headers] for r in rows])]
        return normalize(files,manifest,[],self.catalogue)

    def test_combine_before_completeness_and_unique_counts(self):
        row=source()
        files=[dict(name='a.csv',headers=['anon_id','text_report','workplace'],rows=[[row['accession_no'],row['text_report'],row['workplace']]]),
               dict(name='b.csv',headers=['anon_id',*FINDINGS],rows=[[row['accession_no'],*[0]*10]])]
        result=classify(normalize(files,None,[],self.catalogue),{})
        self.assertEqual(result['summary']['total'],1)
        self.assertEqual(result['summary']['new_complete'],1)
        self.assertFalse(result['summary']['blocked'])

    def test_incomplete_excluded_but_malformed_values_still_block(self):
        result=classify(self.normalize([{'anon_id':'1','patient_age':'12'}]),{})
        self.assertEqual(result['summary']['new_incomplete'],1)
        self.assertFalse(result['summary']['blocked'])
        bad=classify(self.normalize([{'anon_id':'1','patient_age':'not-an-age'}]),{})
        self.assertTrue(bad['summary']['blocked'])
        self.assertEqual(bad['summary']['new_incomplete'],1)

    def test_missing_id_is_blocking_separate_raw_row_count(self):
        result=classify(self.normalize([{'anon_id':'','patient_age':'12'}]),{})
        self.assertTrue(result['summary']['blocked']);self.assertEqual(result['summary']['invalid_raw_rows'],1)
        self.assertEqual(result['summary']['total'],0)

    def test_blank_identical_merge_and_conflict_reject(self):
        result=self.normalize([{'anon_id':'1','patient_age':'12'},{'anon_id':'1','patient_age':''},{'anon_id':'1','patient_age':'12'}])
        self.assertEqual(len(result['rows']),1);self.assertEqual(result['errors'],[])
        result=self.normalize([{'anon_id':'1','patient_age':'12'},{'anon_id':'1','patient_age':'13'}])
        self.assertEqual(result['errors'][0]['code'],'conflicting_uploaded_values')
        self.assertEqual(result['errors'][0]['previous']['row'],2)

    def test_alias_ambiguity_exclusion_and_protected_destinations(self):
        for destination in GENERATED:
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.normalize([{'ID':'1'}],manifest=[{'column_mapping':{'ID':destination}}])
        result=self.normalize([{'anon_id':'1','gt_llm':'malformed'}])
        self.assertEqual(result['files'][0]['excluded'],['gt_llm'])
        self.catalogue['patient_age']['aliases'].append('anon_id')
        result=self.normalize([{'anon_id':'1'}])
        self.assertEqual(result['files'][0]['mapping']['anon_id'],'')

    def test_duplicate_destinations_and_manifest_modes(self):
        for entry in ({}, {'format_id':'x','column_mapping':{}}, {'column_mapping':{'a':'patient_age','b':'patient_age'}}, {'column_mapping':{'unknown':'patient_age'}}):
            with self.subTest(entry=entry),self.assertRaises(ValueError):
                self.normalize([{'a':'12','b':'13'}],manifest=[entry])

    def test_format_header_sets_order_precedence_and_api_strictness(self):
        from .validation import choose_layout
        file={'headers':['anon_id','age']}
        formats=[dict(format_id='first',headers=['age','anon_id'],mapping={'anon_id':'accession_no','age':'patient_age'},rules={},revision=1),
                 dict(format_id='second',headers=['anon_id','age'],mapping={'anon_id':'accession_no','age':'instances'},rules={},revision=1)]
        self.assertEqual(choose_layout(file,None,formats,self.catalogue)['format_id'],'first')
        with self.assertRaises(ValueError): choose_layout({'headers':['anon_id']},{'format_id':'first'},formats,self.catalogue,api=True)
        self.assertEqual(choose_layout({'headers':['anon_id']},{'format_id':'first'},formats,self.catalogue)['mapping'],{'anon_id':'accession_no'})

    def test_category_rules_and_numeric_boundaries(self):
        config=default_settings('patient_gender');config.update(categories=['F','M'],closed=True)
        self.assertEqual(typed_value('patient_gender','female',config,{'values':{'female':'F'}}),'F')
        with self.assertRaises(ValueError):typed_value('patient_gender','other',config)
        config['closed']=False;self.assertEqual(typed_value('patient_gender','other',config),'other')
        self.assertEqual(suggest_bins([]),[]);self.assertEqual(suggest_bins([0,0]),[['All values',None,None]])
        bins=suggest_bins([0,100]);self.assertEqual(bins[0],["1",None,20.0]);self.assertEqual(bins[-1],["5",80.0,None])
        from report_v2.projects.base import Dimension
        dim=Dimension('numeric','numeric',bands=tuple(tuple(b) for b in bins))
        self.assertEqual(dim.group_value(20),'2');self.assertEqual(dim.group_value(-1),'1');self.assertEqual(dim.group_value(101),'5');self.assertEqual(dim.group_value(None),'Unknown')

    def test_no_admin_permission_escalation_storage_or_new_columns(self):
        for field,settings in [('accession_no',{'interpretation':'measurement'}),('unknown',{}),('patient_age',{'upload_allowed':True}),('patient_age',{'interpretation':'free_text'})]:
            with self.assertRaises(ValueError):validate_settings(field,settings)
        with self.assertRaises(ValueError):validate_format(['anon_id'],{'anon_id':'llm_grade'},{})

    def test_seeds_preserve_admin_changes_and_do_not_write_studies(self):
        seed();FieldConfiguration.objects.filter(field_id='patient_age').update(settings={'label':'Custom age'})
        seed();self.assertEqual(FieldConfiguration.objects.get(field_id='patient_age').settings,{'label':'Custom age'})
        self.assertEqual(CXRStudy.objects.count(),0)
        self.assertEqual(UploadFormat.objects.count(),2)
        FieldConfiguration.objects.create(project='synthetic-second',field_id='patient_age',settings={'label':'Second'})
        self.assertNotEqual(field_catalog('prime')['patient_age']['label'],field_catalog('synthetic-second')['patient_age']['label'])
        with self.assertRaises(ValueError):inspect([csv_file()],None,'synthetic-second')

    def test_isolated_csv_and_spreadsheet_precision_formula_duplicate_headers(self):
        parsed,_=parse_files([csv_file([source(9007199254740993)])]);self.assertEqual(parsed[0]['rows'][0][0],'9007199254740993')
        for body in (b'anon_id,anon_id\n1,2',b'anon_id, age ,age\n1,2,3'):
            with self.assertRaises(ValueError):parse_files([SimpleUploadedFile('synthetic.csv',body)])
        import openpyxl
        for cell in ('9007199254740993',9007199254740993,'=1+1'):
            workbook=openpyxl.Workbook();sheet=workbook.active;sheet.append(['anon_id']);sheet.append([cell]);stream=BytesIO();workbook.save(stream)
            file=SimpleUploadedFile('synthetic.xlsx',stream.getvalue())
            if isinstance(cell,str) and not cell.startswith('='):
                parsed,_=parse_files([file]);self.assertEqual(parsed[0]['rows'][0][0],cell)
            else:
                with self.assertRaises(ValueError):parse_files([file])

    def test_encrypted_spreadsheet_and_unsupported_features(self):
        import os, openpyxl, msoffcrypto, zipfile
        workbook=openpyxl.Workbook();workbook.active.append(['anon_id']);workbook.active.append(['9007199254740993'])
        plain=BytesIO();workbook.save(plain)
        encrypted=BytesIO();msoffcrypto.OfficeFile(BytesIO(plain.getvalue())).encrypt('synthetic-password',encrypted)
        with patch.dict(os.environ,{'XLSX_DECRYPT_PASSWORD':'synthetic-password'}):
            parsed,_=parse_files([SimpleUploadedFile('synthetic.xlsx',encrypted.getvalue())])
        self.assertEqual(parsed[0]['rows'][0][0],'9007199254740993')
        for feature in ('xl/vbaProject.bin','xl/externalLinks/externalLink1.xml'):
            stream=BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:archive.writestr(feature,'synthetic')
            with self.assertRaises(ValueError):parse_files([SimpleUploadedFile('synthetic.xlsx',stream.getvalue())])

    def test_admin_auth_csrf_revision_and_forged_settings(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/upload/configuration/').status_code,403)
        self.client.force_login(self.admin)
        response=self.client.post('/upload/configuration/',{'action':'field','identifier':'patient_age','revision':'0','settings':json.dumps({'label':'Age years'})})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post('/upload/configuration/',{'action':'field','identifier':'patient_age','revision':'0','settings':'{}'}).status_code,409)
        secure=Client(enforce_csrf_checks=True);secure.force_login(self.admin)
        self.assertEqual(secure.post('/upload/configuration/',{'action':'seed'}).status_code,403)
        self.assertEqual(secure.post('/upload/api/precheck',{}).status_code,403)
        self.assertEqual(self.client.post('/upload/api/import/confirm',{}).status_code,410)

    def preview(self, rows):
        payload,digest=inspect([csv_file(rows)],None)
        return create_preview(self.admin,'prime',payload,digest)

    @patch('upload.submission.requests.post')
    def test_noop_enrichment_preserve_populated_zero_and_generated_dependencies(self,post):
        CXRStudy.objects.create(accession_no=1,patient_age=0,patient_name=None)
        preview=self.preview([{'accession_no':1,'patient_age':12,'patient_name':'Synthetic','text_report':'Synthetic input'}])
        self.assertEqual(preview.summary['existing_enrichable'],1)
        self.assertTrue(preview.summary['disagreements']);self.assertTrue(preview.summary['protected_enrichment'])
        result=confirm(preview);self.assertEqual(result['status'],'completed');post.assert_not_called()
        study=CXRStudy.objects.get(pk=1);self.assertEqual(study.patient_name,'Synthetic');self.assertEqual(study.patient_age,0);self.assertIsNone(study.text_report)
        self.assertEqual(confirm(preview)['status'],'completed')
        preview=self.preview([{'accession_no':2,'patient_name':'Incomplete synthetic'}]);confirm(preview)
        self.assertFalse(CXRStudy.objects.filter(pk=2).exists());post.assert_not_called()

    @patch('upload.views._start_background_worker')
    @patch('upload.submission.requests.post')
    def test_atomic_outputs_enrichment_rollback_and_idempotent_commit(self,post,worker):
        CXRStudy.objects.create(accession_no=2)
        rows=[source(1,patient_name='New synthetic'),source(2,patient_name='Enriched synthetic')]
        preview=self.preview(rows)
        task_id=str(uuid.uuid4());post.return_value=Mock(status_code=200,json=lambda:{'task_id':task_id})
        confirm(preview)
        self.assertEqual(post.call_args.kwargs['json']['rows'],[rows[0]])
        valid=outputs([rows[0]])
        for bad in ([], valid*2, [{**valid[0],'llm_grade':0}], [{**valid[0],'accession_no':3}], [{**valid[0],'gt_llm':1}], [{**valid[0],'gt_manual':1}]):
            with self.assertRaises(ValueError):
                persist_result(task_id,{'canonical_rows':bad})
            self.assertEqual(CXRStudy.objects.count(),1);self.assertIsNone(CXRStudy.objects.get(pk=2).patient_name)
        result={'canonical_rows':valid}
        self.assertEqual(persist_result(task_id,result),(1,1));self.assertEqual(persist_result(task_id,result),(1,1))
        self.assertEqual(CXRStudy.objects.get(pk=2).patient_name,'Enriched synthetic')
        preview.refresh_from_db();self.assertEqual(preview.payload,{})

    @patch('upload.submission.requests.post')
    def test_stale_preview_owner_and_expiry(self,post):
        CXRStudy.objects.create(accession_no=1)
        preview=self.preview([{'accession_no':1,'patient_name':'Synthetic'}])
        CXRStudy.objects.filter(pk=1).update(patient_name='Other synthetic')
        with self.assertRaises(SubmissionError):confirm(preview)
        post.assert_not_called()
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/upload/api/analyze-auto-sort',{'preview_id':str(preview.pk)}).status_code,404)
        UploadPreview.objects.filter(pk=preview.pk).update(expires_at=timezone.now()-timedelta(seconds=1));cleanup()
        self.assertFalse(UploadPreview.objects.filter(pk=preview.pk).exists())

    def test_report_snapshot_stability_and_identifier_rejection(self):
        from report_v2.field_semantics import current_settings,pin,widget_project,validate_bindings
        from report_v2.projects.base import Dimension
        settings=current_settings();settings['patient_age']['bins']=[['Young',None,40],['Older',40,None]]
        widget={'type':'value','query':{'measurement':'record_count','inputs':{}},'controls':{'filters':[],'compare_by':['age']}}
        pin(widget,settings);settings['patient_age']['bins']=[['Changed',None,None]]
        self.assertEqual(widget_project(widget).dimension('age').group_value(40),'Older')
        self.assertEqual(widget_project({'query':{}}).dimension('age').group_value(40),'40–59')
        bad={'query':{'measurement':'duration_summary','inputs':{'value':'record_id'}}}
        with self.assertRaises(ValueError):validate_bindings(bad,widget_project(bad))


class APIIngestionTests(TestCase):
    databases={'default','audit'}

    def setUp(self):
        from agent_api.auth import create_key
        from agent_api.models import APIConfiguration
        self.user=User.objects.create_superuser('synthetic-ingestion-api','synthetic@example.invalid','synthetic')
        key,raw=create_key(self.user,'Synthetic',timezone.now()+timedelta(days=1),['prime'])
        APIConfiguration.objects.create(enabled=True)
        self.headers={'X-API-Key':raw,'Idempotency-Key':'synthetic-one'}

    def post(self, manifest=None, files=None, key='synthetic-one'):
        body={'files':files or [csv_file()]}
        if manifest is not None:body['upload_manifest']=json.dumps(manifest)
        return self.client.post('/api/prime/jobs',body,secure=True,headers={**self.headers,'Idempotency-Key':key})

    @patch('upload.submission.requests.post')
    def test_missing_both_protected_unknown_or_mismatched_manifest_never_dispatch(self,post):
        for index,manifest in enumerate((None,[],[{}],[{'format_id':'x','column_mapping':{}}],[{'column_mapping':{'text_report':'gt_llm'}}],[{'column_mapping':{'absent':'patient_age'}}])):
            self.assertEqual(self.post(manifest,key='invalid-'+str(index)).status_code,400)
        post.assert_not_called();self.assertEqual(CXRStudy.objects.count(),0)

    @patch('upload.views._start_background_worker')
    @patch('upload.submission.requests.post')
    def test_mixed_file_modes_strict_signature_replay_pins_configuration_and_counts(self,post,worker):
        row=source()
        headers=list(row)
        UploadFormat.objects.create(project='prime',format_id='synthetic',name='Synthetic',headers=headers,mapping={h:h for h in headers})
        manifest=[{'format_id':'synthetic'},{'column_mapping':{'accession_no':'accession_no','patient_name':'patient_name'}}]
        task_id=str(uuid.uuid4());post.return_value=Mock(status_code=200,json=lambda:{'task_id':task_id})
        def files():return [csv_file([row]),csv_file([{'accession_no':row['accession_no'],'patient_name':'Synthetic name'}])]
        response=self.post(manifest,files())
        self.assertEqual(response.status_code,202,response.content)
        self.assertEqual(response.json()['counts']['total'],1)
        UploadFormat.objects.filter(format_id='synthetic').update(mapping={},revision=2)
        self.assertEqual(self.post(manifest,files()).json(),response.json());self.assertEqual(post.call_count,1)
        self.assertEqual(persist_result(task_id,{'canonical_rows':outputs([{**row,'patient_name':'Synthetic name'}])}),(1,0))
        # A fresh named-format request must use an exact signature.
        bad=self.post([{'format_id':'synthetic'}],[csv_file([{'accession_no':2}])],key='mismatch')
        self.assertEqual(bad.status_code,400);self.assertEqual(post.call_count,1)

    @patch('upload.submission.requests.post')
    def test_invalid_values_conflicts_and_incomplete_terminal_response(self,post):
        mapping={n:n for n in source()}
        response=self.post([{'column_mapping':mapping}],[csv_file([source(atelectasis='bad')])])
        self.assertEqual(response.status_code,400);self.assertTrue(response.json()['counts']['blocked'])
        response=self.post([{'column_mapping':{'accession_no':'accession_no'}}],[csv_file([{'accession_no':1}])],key='incomplete')
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['status'],'completed');self.assertEqual(response.json()['counts']['new_incomplete'],1)
        self.assertNotIn('job_id',response.json());post.assert_not_called()
        self.assertEqual(CXRStudy.objects.count(),0)

    @patch('upload.submission.requests.post')
    def test_duplicate_form_and_json_metadata_reject(self,post):
        from django.http import QueryDict
        body={'files':csv_file(),'upload_manifest':['[]','[]']}
        response=self.client.post('/api/prime/jobs',body,secure=True,headers=self.headers)
        self.assertEqual(response.status_code,400)
        body={'files':csv_file(),'upload_manifest':'[{"column_mapping":{},"column_mapping":{}}]'}
        self.assertEqual(self.client.post('/api/prime/jobs',body,secure=True,headers=self.headers).status_code,400)
        post.assert_not_called()


class MigrationPreservationTests(TransactionTestCase):
    databases={'default','audit'}

    def test_supporting_migration_and_seeds_do_not_change_historical_study(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor=MigrationExecutor(connection)
        before=('upload','0004_processingtask_results_saved_at_and_more')
        after=('upload','0005_fieldconfiguration_uploadformat_uploadpreview')
        executor.migrate([before])
        try:
            apps=executor.loader.project_state([before]).apps
            old=apps.get_model('upload','CXRStudy')
            old.objects.create(accession_no=77,text_report='Synthetic historical fixture.',patient_age=0,gt_llm=1,llm_grade=4)
            original=old.objects.values().get(pk=77)
        finally:
            executor=MigrationExecutor(connection);executor.migrate([after])
        seed()
        self.assertEqual(CXRStudy.objects.values().get(pk=77),original)
