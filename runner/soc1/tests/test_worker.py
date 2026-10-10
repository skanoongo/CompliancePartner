import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from openpyxl import Workbook
from pypdf import PdfWriter
from soc1.worker import Soc1Agent, validate_request


def workbook(headers=None):
    wb = Workbook()
    wb.active.title = 'Assessment'
    wb.active.append(headers or ['Template question', 'Pending'])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def payload():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    values = {'controls': workbook(['Control UID', 'Description']),
              'template': workbook(), 'report': out.getvalue()}
    return {'context': {'vendor': 'Synthetic fixture', 'review_start': '2026-01-01', 'review_end': '2026-12-31'},
            'files': {role: {'name': role, 'base64': base64.b64encode(content).decode()} for role, content in values.items()}}


class FakeTransport:
    def __init__(self, draft, missing=False, bad_items=False):
        self.draft, self.missing, self.bad_items = draft, missing, bad_items
        self.uploaded = []
        self.deleted = []
    def __call__(self, path, body=None, content_type=None, raw=False, method=None):
        if path == 'files':
            file_id = f'file-{len(self.uploaded)}'
            self.uploaded.append(file_id)
            return {'id': file_id}
        if method == 'DELETE':
            self.deleted.append(path.split('/')[-1])
            return {'deleted': True}
        if path == 'responses':
            assert body['store'] is False
            assert len(body['tools'][0]['container']['file_ids']) == 3
            names = ['SOC1_Draft.xlsx'] if self.missing else ['SOC1_Draft.xlsx', 'Open_Items.json']
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'annotations': [
                {'type': 'container_file_citation', 'filename': '/mnt/data/' + name,
                 'container_id': 'test', 'file_id': name} for name in names]}]}]}
        if path.endswith('/SOC1_Draft.xlsx/content'):
            return self.draft
        if path.endswith('/Open_Items.json/content'):
            return json.dumps({'items': [{}] if self.bad_items else []}).encode()
        raise AssertionError(path)


class WorkerTests(unittest.TestCase):
    def run_job(self, fake, job_id='test-job'):
        with tempfile.TemporaryDirectory() as folder:
            state = Soc1Agent(folder, model='offline-test', transport=fake).run(job_id, payload())
            path = Path(folder) / job_id
            self.assertEqual(json.loads((path / 'status.json').read_text())['status'], state['status'])
            return state, (path / 'SOC1_Draft.xlsx').exists()

    def test_success_and_upload_cleanup(self):
        fake = FakeTransport(workbook())
        state, exists = self.run_job(fake)
        self.assertEqual(state['status'], 'draft_ready')
        self.assertTrue(exists)
        self.assertEqual(fake.uploaded, fake.deleted)

    def test_missing_artifact_withholds_draft_and_cleans_uploads(self):
        fake = FakeTransport(workbook(), missing=True)
        state, exists = self.run_job(fake)
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(exists)
        self.assertEqual(fake.uploaded, fake.deleted)

    def test_changed_template_tabs_withholds_draft(self):
        wb = Workbook()
        wb.active.title = 'Wrong tab'
        out = io.BytesIO()
        wb.save(out)
        state, exists = self.run_job(FakeTransport(out.getvalue()))
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(exists)

    def test_invalid_open_item_withholds_draft(self):
        state, exists = self.run_job(FakeTransport(workbook(), bad_items=True))
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(exists)

    def test_rejects_path_traversal_before_upload(self):
        fake = FakeTransport(workbook())
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                Soc1Agent(folder, model='offline-test', transport=fake).run('../other', payload())
        self.assertEqual(fake.uploaded, [])

    def test_refuses_existing_job(self):
        fake = FakeTransport(workbook())
        with tempfile.TemporaryDirectory() as folder:
            agent = Soc1Agent(folder, model='offline-test', transport=fake)
            agent.run('existing', payload())
            with self.assertRaises(FileExistsError):
                agent.run('existing', payload())
        self.assertEqual(len(fake.uploaded), 3)

    def test_invalid_dates_and_missing_uploads(self):
        data = payload()
        data['context']['review_start'] = '2027-01-01'
        with self.assertRaises(ValueError):
            validate_request(data)
        data = payload()
        del data['files']['report']
        with self.assertRaises(ValueError):
            validate_request(data)

    def test_missing_model_before_upload(self):
        from unittest.mock import patch
        fake = FakeTransport(workbook())
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'SOC1_MODEL': '', 'OPENAI_MODEL_NAME': '', 'DOPPLER_TOKEN': ''}):
            with self.assertRaises(ValueError):
                Soc1Agent(folder, transport=fake).run('test', payload())
        self.assertEqual(fake.uploaded, [])


if __name__ == '__main__':
    unittest.main()
