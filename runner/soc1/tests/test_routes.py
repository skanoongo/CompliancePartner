"""The runner's /api/soc1 routes, offline: a fake OpenAI transport, no network.

Run from runner/:  python3 -m unittest discover -s soc1/tests -t .
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from soc1 import worker
from soc1.tests.test_worker import FakeTransport, payload, workbook

ALICE = {"id": "alice", "name": "Alice", "admin": False, "scopes": ["Workato"]}
BOB = {"id": "bob", "name": "Bob", "admin": False, "scopes": ["Workato"]}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(OPENAI_API_KEY="sk-test", SOC1_MODEL="offline-test", DOPPLER_TOKEN="")
        import app as host
        self.host = host
        host.SOC1_DIR = Path(self.tmp.name) / "soc1-jobs"
        host._soc1.clear()
        host._soc1_idem.clear()
        self.who = ALICE
        self.patches = [
            patch.object(host, "current_user", lambda: self.who),
            patch.object(host, "require_session", lambda: None),
        ]
        for p in self.patches:
            p.start()
        host.app.before_request_funcs[None] = []
        self.fake = FakeTransport(workbook())
        fake = self.fake

        class Agent(worker.Soc1Agent):
            def __init__(self, root, **kw):
                super().__init__(root, transport=fake, **kw)
        self.agent_patch = patch.object(worker, "Soc1Agent", Agent)
        self.agent_patch.start()
        self.client = host.app.test_client()

    def tearDown(self):
        self.agent_patch.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        for k in ("OPENAI_API_KEY", "SOC1_MODEL"):
            os.environ.pop(k, None)

    def start(self, key="k1", body=None):
        body = body or dict(payload(), system="Workato", assessment_id="Workato|2")
        return self.client.post("/api/soc1/jobs", json=body, headers={"Idempotency-Key": key})

    def wait(self, job_id):
        for _ in range(100):
            st = self.client.get(f"/api/soc1/jobs/{job_id}").get_json()
            if st["status"] in ("draft_ready", "failed"):
                return st
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_config_reports_model_never_key(self):
        body = self.client.get("/api/soc1").get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["model"], "offline-test")
        self.assertNotIn("sk-test", str(body))

    def test_draft_ready_then_download(self):
        r = self.start()
        self.assertEqual(r.status_code, 202)
        st = self.wait(r.get_json()["id"])
        self.assertEqual(st["status"], "draft_ready")
        self.assertEqual(st["artifacts"], ["SOC1_Draft.xlsx", "Open_Items.json"])
        self.assertNotIn("owner", st)
        dl = self.client.get(f"/api/soc1/jobs/{st['id']}/artifacts/SOC1_Draft.xlsx")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.headers["Cache-Control"], "no-store")
        dl.close()
        self.assertEqual(self.fake.uploaded, self.fake.deleted)

    def test_same_idempotency_key_is_one_preparation(self):
        a = self.start("same").get_json()["id"]
        b = self.start("same").get_json()["id"]
        self.assertEqual(a, b)
        self.wait(a)
        self.assertEqual(len(self.fake.uploaded), 3)

    def test_other_user_cannot_see_or_download(self):
        job = self.start().get_json()["id"]
        self.wait(job)
        self.who = BOB
        self.assertEqual(self.client.get(f"/api/soc1/jobs/{job}").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/soc1/jobs/{job}/artifacts/SOC1_Draft.xlsx").status_code, 404)

    def test_unknown_artifact_name_refused(self):
        job = self.start().get_json()["id"]
        self.wait(job)
        for name in ("controls.xlsx", "report.pdf", "manifest.json", "..%2Fx"):
            self.assertEqual(self.client.get(
                f"/api/soc1/jobs/{job}/artifacts/{name}").status_code, 404)

    def test_errors_map_to_status_codes(self):
        self.assertEqual(self.client.post("/api/soc1/jobs", json=payload()).status_code, 400)
        bad = dict(payload(), system="Workato")
        bad["context"]["review_end"] = "2025-01-01"
        self.assertEqual(self.start(body=bad).status_code, 400)
        self.assertEqual(self.start(body=dict(payload(), system="NetSuite")).status_code, 403)
        with patch.object(self.host, "SOC1_MAX_REQUEST", 10):
            self.assertEqual(self.start("big").status_code, 413)
        os.environ["SOC1_MODEL"] = ""
        with patch.object(worker, "_setting", lambda name: "" if "MODEL" in name else "k"):
            self.assertEqual(self.start("nocfg").status_code, 503)
        self.assertEqual(self.fake.uploaded, [])

    def test_status_survives_restart(self):
        job = self.start().get_json()["id"]
        self.wait(job)
        self.host._soc1.clear()
        st = self.client.get(f"/api/soc1/jobs/{job}").get_json()
        self.assertEqual(st["status"], "draft_ready")

    def test_failed_agent_withholds_draft(self):
        self.fake.missing = True
        job = self.start().get_json()["id"]
        st = self.wait(job)
        self.assertEqual(st["status"], "failed")
        self.assertEqual(self.client.get(
            f"/api/soc1/jobs/{job}/artifacts/SOC1_Draft.xlsx").status_code, 404)


if __name__ == "__main__":
    unittest.main()
