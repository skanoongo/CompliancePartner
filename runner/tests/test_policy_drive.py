"""The help assistant reading its policy from Google Drive, offline.

Google is faked at urllib.request.urlopen; nothing leaves the machine.
Run from runner/:  python3 -m unittest discover -s tests -t .
"""
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

FID = "13lHa59DBZgB9F35rPmMDemKs1nRTsTAn"
URL = f"https://drive.google.com/file/d/{FID}/view"
POLICY = """CoreWeave Password Policy

1. Purpose
This policy sets the minimum requirements for passwords on CoreWeave systems.

4.2 Password length
Passwords must be at least 14 characters long.

4.3 Multi-factor authentication
MFA is required for every in-scope system that supports it.
"""


def sa_key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return json.dumps({"client_email": "cp@test.iam.gserviceaccount.com", "private_key": pem})


class FakeGoogle:
    def __init__(self):
        self.modified = "2026-10-01T00:00:00Z"
        self.text = POLICY
        self.fail = None
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if self.fail:
            raise urllib.error.HTTPError(url, self.fail, "x", {}, io.BytesIO(b""))
        if url.startswith("https://oauth2.googleapis.com/token"):
            body = {"access_token": "ya29.test", "expires_in": 3600}
        elif "/export?" in url:
            return io.BytesIO(self.text.encode())
        elif url.startswith("https://www.googleapis.com/drive/v3/files/" + FID + "?fields"):
            body = {"name": "CoreWeave Password Policy", "mimeType":
                    "application/vnd.google-apps.document", "modifiedTime": self.modified}
        else:
            raise AssertionError("unexpected request " + url)
        return io.BytesIO(json.dumps(body).encode())


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from core import gdrive
        self.g = gdrive
        gdrive.CACHE_DIR = Path(self.tmp.name)
        gdrive._state.clear()
        gdrive._token.update(value="", until=0)
        self.fake = FakeGoogle()
        self.env = patch.dict(os.environ, {"GOOGLE_SA_KEY": sa_key(), "DOPPLER_TOKEN": "",
                                           "CW_PASSWORD_POLICY_DOC": URL})
        self.env.start()
        self.net = patch("urllib.request.urlopen", self.fake)
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_only_google_drive_links_yield_an_id(self):
        g = self.g
        self.assertEqual(g.file_id(URL), FID)
        self.assertEqual(g.file_id(f"https://docs.google.com/document/d/{FID}/edit"), FID)
        self.assertEqual(g.file_id(f"https://drive.google.com/open?id={FID}"), FID)
        for bad in (f"http://drive.google.com/file/d/{FID}/view",
                    f"https://drive.google.com.evil.example/file/d/{FID}/view",
                    f"https://evil.example/drive.google.com/file/d/{FID}",
                    "javascript:alert(1)", "https://intranet/policy", ""):
            self.assertIsNone(g.file_id(bad), bad)

    def test_reads_caches_and_marks_clauses(self):
        path = self.g.cached(URL, "CoreWeave Password Policy")
        text = path.read_text()
        self.assertIn("## 4.2 Password length", text)
        self.assertIn("at least 14 characters", text)
        n = len(self.fake.calls)
        self.g.cached(URL, "CoreWeave Password Policy")          # fresh: no call
        self.assertEqual(len(self.fake.calls), n)

    def test_failed_refresh_keeps_last_copy(self):
        path = self.g.cached(URL, "T")
        os.utime(path, (time.time() - 99999, time.time() - 99999))   # make it due
        self.fake.fail = 403
        self.assertEqual(self.g.cached(URL, "T"), path)
        st = self.g.status(URL)
        self.assertTrue(st["readable"])
        self.assertIn("not shared", st["error"])

    def test_newer_version_replaces_text(self):
        path = self.g.cached(URL, "T")
        os.utime(path, (time.time() - 99999, time.time() - 99999))
        self.fake.modified, self.fake.text = "2026-10-08T00:00:00Z", POLICY.replace("14", "16")
        self.g.cached(URL, "T")
        self.assertIn("at least 16 characters", path.read_text())

    def test_assistant_answers_from_the_policy_and_links_it(self):
        from core import assistant
        with tempfile.TemporaryDirectory() as docs, \
                patch.object(assistant, "DOCS", Path(docs)), \
                patch.object(assistant, "provider", lambda: None):
            assistant._index.update(chunks=[], stamp=None)
            r = assistant.ask("What is the minimum password length?")
            self.assertIn("14 characters", r["answer"])
            self.assertTrue(any(s["policy"] for s in r["sources"]))
            self.assertEqual(r["references"][0]["url"], URL)
            self.assertNotIn("path", r["references"][0])
            st = assistant.configured()["policyRead"]
            self.assertTrue(st["readable"])
            self.assertEqual(st["source"], "google-drive")

    def test_without_a_key_it_is_only_linked(self):
        from core import assistant
        with patch.dict(os.environ, {"GOOGLE_SA_KEY": ""}), \
                tempfile.TemporaryDirectory() as docs, \
                patch.object(assistant, "DOCS", Path(docs)):
            assistant._index.update(chunks=[], stamp=None)
            pol = assistant.policy()
            self.assertEqual(pol["kind"], "url")
            self.assertNotIn("path", pol)
            self.assertIn("GOOGLE_SA_KEY", self.g.status(URL)["error"])
        self.assertFalse(any("googleapis" in c for c in self.fake.calls))


if __name__ == "__main__":
    unittest.main()
