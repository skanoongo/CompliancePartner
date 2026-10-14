"""The audit draft agent and its /api/audit-draft routes, offline: a fake OpenAI transport, no network.

Run from runner/:  python3 -m unittest discover -s audit/tests -t .
"""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from audit import worker
from audit.worker import AgentLog, AuditAgent, read_events, validate_request

ALICE = {"id": "alice", "name": "Alice", "admin": False, "scopes": ["Workato"]}
BOB = {"id": "bob", "name": "Bob", "admin": False, "scopes": ["Workato"]}


def workbook(*sheets):
    wb = Workbook()
    wb.active.title = sheets[0] if sheets else 'Testing'
    for name in sheets[1:]:
        wb.create_sheet(name)
    wb.active.append(['Attribute', 'Result'])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def b64(content):
    return base64.b64encode(content).decode()


def payload(template=None, name='UAR Testing Template.xlsx', files=None):
    return {'context': {'working_on': 'Workato', 'system': 'Workato', 'control_number': 'C-UA-04',
                        'control_name': 'User access review', 'testing_period': '2026-07-01 to 2026-09-30',
                        'assessment': 'Operating Effectiveness Testing', 'preparer': 'Alice',
                        'ignored': 'not passed on'},
            'template': {'name': name, 'base64': b64(template or workbook())},
            'files': files if files is not None else [
                {'name': 'population.csv', 'base64': b64(b'id\n1\n2\n'), 'roles': ['population']},
                {'name': 'samples & evidence.xlsx', 'base64': b64(workbook()), 'roles': ['sample', 'evidence']}]}


class FakeTransport:
    def __init__(self, draft, draft_name='Audit_Draft.xlsx', missing=False, bad_items=False):
        self.draft, self.draft_name, self.missing, self.bad_items = draft, draft_name, missing, bad_items
        self.uploaded, self.names, self.deleted, self.requests = [], [], [], []

    def __call__(self, path, body=None, content_type=None, raw=False, method=None):
        if path == 'files':
            self.uploaded.append(f'file-{len(self.uploaded)}')
            self.names.append(body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode())
            return {'id': self.uploaded[-1]}
        if method == 'DELETE':
            self.deleted.append(path.split('/')[-1])
            return {'deleted': True}
        if path == 'responses':
            self.requests.append(body)
            names = [self.draft_name] if self.missing else [self.draft_name, 'Open_Items.json']
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'annotations': [
                {'type': 'container_file_citation', 'filename': '/mnt/data/' + n,
                 'container_id': 'test', 'file_id': n} for n in names]}]}]}
        if path.endswith(f'/{self.draft_name}/content'):
            return self.draft
        if path.endswith('/Open_Items.json/content'):
            items = [{}] if self.bad_items else [
                {'id': 'OI-1', 'issue': 'Sample 2 has no approval', 'evidence_needed': 'Approval ticket'}]
            return json.dumps({'items': items}).encode()
        raise AssertionError(path)


class WorkerTests(unittest.TestCase):
    def run_job(self, fake, body=None, job_id='job-1'):
        with tempfile.TemporaryDirectory() as tmp:
            result = AuditAgent(tmp, model='offline-test', transport=fake).run(job_id, body or payload())
            folder = Path(tmp) / job_id
            return result, {p.name for p in folder.iterdir()}

    def test_fills_template_and_returns_open_items(self):
        fake = FakeTransport(workbook())
        result, stored = self.run_job(fake)
        self.assertEqual(result['status'], 'draft_ready', result.get('message'))
        self.assertEqual(result['draft'], 'Audit_Draft.xlsx')
        self.assertEqual(result['open_items'][0]['id'], 'OI-1')
        self.assertIn('Audit_Draft.xlsx', stored)
        # The template and every evidence file reach the model, then are deleted from it.
        self.assertEqual(fake.names, ['TEMPLATE.xlsx', '01_population.csv', '02_samples_evidence.xlsx'])
        self.assertEqual(fake.uploaded, fake.deleted)
        req = fake.requests[0]
        self.assertFalse(req['store'])
        self.assertEqual(len(req['tools'][0]['container']['file_ids']), 3)
        self.assertIn('/mnt/data/Audit_Draft.xlsx', req['instructions'])
        self.assertIn('"purposes": ["sample", "evidence"]', req['input'])
        self.assertNotIn('not passed on', req['input'])

    def test_docx_and_text_templates_keep_their_format(self):
        result, _ = self.run_job(FakeTransport(b'Control: C-UA-04 filled', 'Audit_Draft.txt'),
                                 payload(b'Control: {{CONTROL_NUMBER}}', 'Template.txt'))
        self.assertEqual(result['status'], 'draft_ready', result.get('message'))
        self.assertEqual(result['draft'], 'Audit_Draft.txt')

    def test_changed_tabs_withhold_the_draft(self):
        result, stored = self.run_job(FakeTransport(workbook('Testing', 'Extra')))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('added Extra', result['message'])
        self.assertNotIn('Audit_Draft.xlsx', stored)

    def test_missing_artifact_or_bad_items_fail(self):
        self.assertEqual(self.run_job(FakeTransport(workbook(), missing=True))[0]['status'], 'failed')
        self.assertEqual(self.run_job(FakeTransport(workbook(), bad_items=True))[0]['status'], 'failed')

    def test_log_records_each_step(self):
        result, _ = self.run_job(FakeTransport(workbook()))
        steps = [e['text'] for e in result['log'] if e['kind'] == 'step']
        self.assertTrue(steps[0].startswith('Uploading the template and 2 evidence files'))
        self.assertIn('Uploaded population.csv (population, 1 KB)', steps)
        self.assertTrue(steps[-1].startswith('Draft ready with 1 open items'))
        self.assertEqual(result['message'], steps[-1])
        failed, _ = self.run_job(FakeTransport(workbook(), missing=True))
        self.assertTrue(failed['log'][-1]['text'].startswith('Failed: '))

    def test_stream_events_become_the_debug_log(self):
        events = [
            {'type': 'response.created'},
            {'type': 'response.output_item.added', 'item': {'type': 'code_interpreter_call'}},
            {'type': 'response.code_interpreter_call_code.done', 'code': 'import openpyxl'},
            {'type': 'response.output_item.done', 'item': {'type': 'code_interpreter_call',
             'outputs': [{'type': 'logs', 'logs': '3 samples'}]}},
            {'type': 'response.output_item.done', 'item': {'type': 'message',
             'content': [{'type': 'output_text', 'text': 'Filled the template.'}]}},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
        ]
        lines = [b'event: x\n'] + [l for ev in events for l in (f'data: {json.dumps(ev)}\n'.encode(), b'\n')]
        rec = {}
        log = AgentLog(rec)
        final = read_events(lines, log.on_event)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual([(e['kind'], e['text']) for e in rec['log']], [
            ('step', 'Model run started'), ('step', 'Running Python in the sandbox'),
            ('code', 'import openpyxl'), ('output', '3 samples'), ('text', 'Filled the template.'),
            ('step', 'Model run finished')])
        with self.assertRaises(ValueError):
            read_events([b'data: {"type": "response.created"}\n', b'\n'], log.on_event)
        with self.assertRaises(RuntimeError):
            read_events([b'data: {"type": "error", "message": "boom"}\n', b'\n'], log.on_event)

    def test_request_validation(self):
        context, template, files = validate_request(payload())
        self.assertNotIn('ignored', context)
        self.assertEqual(template['ext'], '.xlsx')
        self.assertEqual([f['roles'] for f in files], [['population'], ['sample', 'evidence']])
        bad = [
            payload(name='Template.pdf'),
            payload(files=[]),
            payload(files=[{'name': 'p.csv', 'base64': b64(b'x'), 'roles': ['population']}]),
            payload(files=[{'name': 'p.csv', 'base64': b64(b'x'), 'roles': []}]),
            payload(template=b'not a zip', name='Template.docx'),
        ]
        no_period = payload()
        no_period['context']['testing_period'] = ''
        bad.append(no_period)
        for body in bad:
            with self.assertRaises(ValueError):
                validate_request(body)
        with patch.object(worker, 'MAX_TOTAL', 10), self.assertRaises(ValueError):
            validate_request(payload())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(OPENAI_API_KEY='sk-test', AUDIT_MODEL='offline-test', DOPPLER_TOKEN='')
        import app as host
        self.host = host
        host.AUDIT_DIR = Path(self.tmp.name) / 'audit-jobs'
        host._audit.clear()
        host._audit_idem.clear()
        self.who = ALICE
        self.patches = [patch.object(host, 'current_user', lambda: self.who),
                        patch.object(host, 'require_session', lambda: None)]
        for p in self.patches:
            p.start()
        host.app.before_request_funcs[None] = []
        self.fake = fake = FakeTransport(workbook())

        class Agent(worker.AuditAgent):
            def __init__(self, root, **kw):
                super().__init__(root, transport=fake, **kw)
        self.agent_patch = patch.object(worker, 'AuditAgent', Agent)
        self.agent_patch.start()
        self.client = host.app.test_client()

    def tearDown(self):
        self.agent_patch.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        for k in ('OPENAI_API_KEY', 'AUDIT_MODEL'):
            os.environ.pop(k, None)

    def start(self, key='k1', body=None):
        return self.client.post('/api/audit-draft/jobs', json=body or payload(),
                                headers={'Idempotency-Key': key})

    def wait(self, job_id):
        for _ in range(100):
            st = self.client.get(f'/api/audit-draft/jobs/{job_id}').get_json()
            if st['status'] in ('draft_ready', 'failed'):
                return st
            time.sleep(0.05)
        self.fail('job did not finish')

    def test_config_reports_model_never_key(self):
        body = self.client.get('/api/audit-draft').get_json()
        self.assertTrue(body['available'])
        self.assertEqual(body['model'], 'offline-test')
        self.assertNotIn('sk-test', str(body))

    def test_draft_ready_then_download(self):
        r = self.start()
        self.assertEqual(r.status_code, 202)
        st = self.wait(r.get_json()['id'])
        self.assertEqual(st['status'], 'draft_ready', st.get('message'))
        self.assertEqual(st['draft'], 'Audit_Draft.xlsx')
        self.assertNotIn('owner', st)
        dl = self.client.get(f"/api/audit-draft/jobs/{st['id']}/artifacts/Audit_Draft.xlsx")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.headers['Cache-Control'], 'no-store')
        dl.close()
        for name in ('TEMPLATE.xlsx', 'manifest.json', '01_population.csv', '..%2Fx'):
            self.assertEqual(self.client.get(
                f"/api/audit-draft/jobs/{st['id']}/artifacts/{name}").status_code, 404)

    def test_same_idempotency_key_is_one_draft(self):
        a = self.start('same').get_json()['id']
        self.assertEqual(a, self.start('same').get_json()['id'])
        self.wait(a)
        self.assertEqual(len(self.fake.requests), 1)

    def test_other_user_cannot_see_or_download(self):
        job = self.start().get_json()['id']
        self.wait(job)
        self.who = BOB
        self.assertEqual(self.client.get(f'/api/audit-draft/jobs/{job}').status_code, 404)
        self.assertEqual(self.client.get(
            f'/api/audit-draft/jobs/{job}/artifacts/Audit_Draft.xlsx').status_code, 404)

    def test_status_survives_restart(self):
        job = self.start().get_json()['id']
        self.wait(job)
        self.host._audit.clear()
        self.assertEqual(self.client.get(f'/api/audit-draft/jobs/{job}').get_json()['status'], 'draft_ready')

    def test_errors_map_to_status_codes(self):
        self.assertEqual(self.client.post('/api/audit-draft/jobs', json=payload()).status_code, 400)
        self.assertEqual(self.start(body=payload(name='x.pdf')).status_code, 400)
        other = payload()
        other['context']['system'] = 'NetSuite'
        self.assertEqual(self.start(body=other).status_code, 403)
        with patch.object(self.host, 'AUDIT_MAX_REQUEST', 10):
            self.assertEqual(self.start('big').status_code, 413)
        with patch.object(worker, 'default_model', lambda: ''):
            self.assertEqual(self.start('nocfg').status_code, 503)
        self.assertEqual(self.fake.uploaded, [])


if __name__ == '__main__':
    unittest.main()
