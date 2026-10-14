"""CSV classification, persistence, and access checks using synthetic workers."""
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import terminations


def csv_bytes(statuses=("OK", "MISSED", "REHIRED")):
    buffer = io.StringIO(newline="")
    columns = sorted(terminations.REQUIRED) + ["Term>Disable Days", "Entry>Disable Days"]
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for i, status in enumerate(statuses, 1):
        writer.writerow({"Full name": f"Test Worker {i}", "Employee ID": str(i),
                         "Worker Type": "Employee" if i == 1 else "Contingent Worker",
                         "Termination Date": "2026-09-01", "Term Entered On": "2026-09-03",
                         "Okta Disable Date": "" if status in ("MISSED", "REHIRED") else "2026-09-04",
                         "Status": status, "Result": "Within SLA allowing entry lag" if status == "OK"
                         else "Not in Okta log" if status == "MISSED" else "Source result",
                         "Term>Disable Days": "3" if status == "OK" else "",
                         "Entry>Disable Days": "1" if status == "OK" else ""})
    return buffer.getvalue().encode("utf-8")


class CsvTests(unittest.TestCase):
    def test_preserves_source_status_and_entry_adjusted_results(self):
        report = terminations.parse(csv_bytes())
        self.assertEqual(report["summary"]["total"], 3)
        self.assertEqual(report["summary"]["counts"],
                         {"OK": 1, "MISSED": 1, "REHIRED": 1, "LATE": 0, "REVIEW": 0})
        self.assertEqual(report["summary"]["entryAdjusted"], 1)
        self.assertEqual(report["records"][0]["days"], "3")
        self.assertEqual(report["records"][0]["entryDays"], "1")
        self.assertEqual(report["records"][1]["disabled"], "")

    def test_unknown_and_blank_statuses_need_review(self):
        report = terminations.parse(csv_bytes(("LATE", "NEW_STATUS", "")))
        self.assertEqual(report["summary"]["counts"]["LATE"], 1)
        self.assertEqual(report["summary"]["counts"]["REVIEW"], 2)
        self.assertEqual(report["summary"]["counts"]["OK"], 0)
        self.assertEqual(report["records"][1]["status"], "NEW_STATUS")

    def test_empty_missing_columns_and_malformed_csv_are_rejected(self):
        for content in (b"", b"Name,Status\nTest,OK\n", csv_bytes(()),
                        csv_bytes().replace(b"Test Worker 1", b'"unclosed')):
            with self.subTest(content=content[:30]), self.assertRaises(terminations.SnapshotError):
                terminations.parse(content)

    def test_duplicate_workers_invalid_dates_and_days_are_rejected(self):
        for content in (csv_bytes().replace(b"\r\n2,", b"\r\n1,"),
                        csv_bytes().replace(b"2026-09-01", b"2026-02-31"),
                        csv_bytes().replace(b",3,1\r\n", b",many,1\r\n")):
            with self.subTest(content=content[:30]), self.assertRaises(terminations.SnapshotError):
                terminations.parse(content)

    def test_utf8_bom_and_negative_offsets_are_supported(self):
        report = terminations.parse(b"\xef\xbb\xbf" + csv_bytes(("OK",)).replace(b",3,1\r\n", b",-3,1\r\n"))
        self.assertEqual(report["summary"]["total"], 1)
        self.assertEqual(report["records"][0]["days"], "-3")

    def test_oversized_file_rejected(self):
        with self.assertRaises(terminations.SnapshotError):
            terminations.parse(b"x" * (terminations.MAX_BYTES + 1))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"TERMINATION_RECON_STORE": str(Path(self.tmp.name) / "snapshot.json")})
        self.env.start()
        import app as host
        self.host = host
        self.who = {"id": "test-admin", "admin": True, "modules": [], "scopes": []}
        self.patches = [patch.object(host, "current_user", lambda: self.who),
                        patch.object(host, "_cfg", lambda: {"enabled": False})]
        for p in self.patches:
            p.start()
        self.before = host.app.before_request_funcs.get(None, [])[:]
        host.app.before_request_funcs[None] = [host.require_session]
        self.client = host.app.test_client()

    def tearDown(self):
        self.host.app.before_request_funcs[None] = self.before
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def upload(self, content=None, source_url=""):
        return self.client.post("/api/termination-reconciliation", data={
            "file": (io.BytesIO(csv_bytes() if content is None else content), "test.csv"),
            "source_url": source_url}, content_type="multipart/form-data")

    def test_missing_snapshot_is_not_an_all_clear(self):
        response = self.client.get("/api/termination-reconciliation")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["available"])

    def test_admin_import_persists_and_get_disables_caching(self):
        self.assertEqual(self.upload(source_url="https://drive.google.com/file/d/test/view").status_code, 200)
        response = self.client.get("/api/termination-reconciliation")
        self.assertEqual(response.get_json()["summary"]["total"], 3)
        self.assertEqual(response.get_json()["source"]["filename"], "test.csv")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_invalid_import_preserves_existing_snapshot(self):
        self.upload()
        self.assertEqual(self.upload(b"invalid").status_code, 400)
        self.assertEqual(terminations.load()["summary"]["total"], 3)

    def test_unsigned_read_and_import_are_denied(self):
        self.who = None
        self.assertEqual(self.client.get("/api/termination-reconciliation").status_code, 401)
        self.assertEqual(self.upload().status_code, 401)

    def test_read_requires_monitoring_and_both_source_systems(self):
        self.upload()
        self.who = {"admin": False, "modules": ["monitor"], "scopes": ["Okta", "Workday"]}
        self.assertEqual(self.client.get("/api/termination-reconciliation").status_code, 200)
        self.assertEqual(self.upload().status_code, 403)
        for modules, scopes in ((["monitor"], ["Okta"]), (["prepare"], ["Okta", "Workday"]),
                                (["monitor"], ["Workato"])):
            self.who = {"admin": False, "modules": modules, "scopes": scopes}
            self.assertEqual(self.client.get("/api/termination-reconciliation").status_code, 403)

    def test_bad_source_url_and_corrupt_snapshot_do_not_show_green(self):
        for url in ("javascript:alert(1)", "https://drive.google.com.evil.test/file/d/x"):
            self.assertEqual(self.upload(source_url=url).status_code, 400)
        terminations.store_path().write_text("broken json")
        self.assertEqual(self.client.get("/api/termination-reconciliation").status_code, 422)


if __name__ == "__main__":
    unittest.main()
