"""Workato termination reconciliation snapshots, stored on the data volume."""

import csv
import io
import json
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 2 * 1024 * 1024
REQUIRED = {"Full name", "Employee ID", "Worker Type", "Termination Date",
            "Term Entered On", "Okta Disable Date", "Status", "Result"}
KNOWN = {"OK", "MISSED", "REHIRED", "LATE"}
_lock = threading.Lock()


class SnapshotError(ValueError):
    pass


def store_path():
    return Path(os.environ.get("TERMINATION_RECON_STORE") or
                str(Path(os.environ.get("CAPTURE_DATA_DIR", "/data")) /
                    "termination-reconciliation.json"))


def parse(content):
    if len(content) > MAX_BYTES:
        raise SnapshotError("The CSV must be no larger than 2 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SnapshotError("Export the CSV using UTF-8 encoding.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = reader.fieldnames or []
        missing = REQUIRED - set(headers)
        if missing:
            raise SnapshotError("Missing CSV columns: " + ", ".join(sorted(missing)))
        if len(headers) != len(set(headers)):
            raise SnapshotError("The CSV has duplicate column names.")
        records, ids = [], set()
        for line, raw in enumerate(reader, 2):
            if None in raw or any(v is None for v in raw.values()):
                raise SnapshotError(f"CSV row {line} has an unexpected number of fields.")
            row = {k: v.strip() for k, v in raw.items()}
            if not row["Employee ID"] or not row["Full name"]:
                raise SnapshotError(f"CSV row {line} needs a worker name and employee ID.")
            if row["Employee ID"] in ids:
                raise SnapshotError(f"Duplicate employee ID at row {line}.")
            ids.add(row["Employee ID"])
            for field in ("Termination Date", "Term Entered On", "Okta Disable Date"):
                value = row[field]
                if not value and field == "Okta Disable Date":
                    continue
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError()
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise SnapshotError(f"Invalid {field} at row {line}; use YYYY-MM-DD.") from exc
            for field in ("Entry Lag Days", "Term>Disable Days", "Entry>Disable Days"):
                if row.get(field) and not re.fullmatch(r"-?\d+", row[field]):
                    raise SnapshotError(f"Invalid {field} at row {line}; use whole days.")
            status = row["Status"]
            bucket = status.upper() if status.upper() in KNOWN else "REVIEW"
            records.append({
                "name": row["Full name"], "id": row["Employee ID"],
                "type": "Contractor" if row["Worker Type"] == "Contingent Worker"
                        else row["Worker Type"],
                "sourceWorkerType": row["Worker Type"],
                "term": row["Termination Date"], "entered": row["Term Entered On"],
                "disabled": row["Okta Disable Date"],
                "days": row.get("Term>Disable Days", ""),
                "entryDays": row.get("Entry>Disable Days", ""),
                "entryLag": row.get("Entry Lag Days", ""),
                "status": status, "bucket": bucket, "result": row["Result"],
                "entryAdjusted": bucket == "OK" and row["Result"].startswith(
                    "Within SLA allowing entry lag"),
            })
    except csv.Error as exc:
        raise SnapshotError("The CSV could not be read: " + str(exc)) from exc
    if not records:
        raise SnapshotError("The CSV has no worker rows; it cannot establish an all-clear result.")
    counts = Counter(r["bucket"] for r in records)
    dates = sorted(r["term"] for r in records)
    return {"records": records, "summary": {
        "total": len(records), "counts": {key: counts[key] for key in
                                         ("OK", "MISSED", "REHIRED", "LATE", "REVIEW")},
        "entryAdjusted": sum(r["entryAdjusted"] for r in records),
        "workerTypes": dict(Counter(r["type"] for r in records)),
        "effectiveStart": dates[0], "effectiveEnd": dates[-1],
    }}


def load():
    try:
        with _lock:
            data = json.loads(store_path().read_text(encoding="utf-8"))
        report = parse(data["csv"].encode("utf-8"))
        report["source"] = {key: data.get(key, "") for key in
                            ("filename", "importedAt", "url")}
        return report
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SnapshotError("The saved CSV snapshot cannot be read. Import a valid CSV again.") from exc


def save(content, filename, source_url=""):
    report = parse(content)
    if source_url:
        try:
            url = urlsplit(source_url)
        except ValueError as exc:
            raise SnapshotError("The optional source link must be an HTTPS Google Drive URL.") from exc
        if url.scheme != "https" or url.hostname != "drive.google.com" or url.username:
            raise SnapshotError("The optional source link must be an HTTPS Google Drive URL.")
    filename = Path(filename.replace("\\", "/")).name or "termination-reconciliation.csv"
    metadata = {"filename": filename, "url": source_url,
                "importedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = store_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({**metadata, "csv": content.decode("utf-8-sig")}),
                             encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    report["source"] = metadata
    return report
