#!/usr/bin/env python3
"""
Read a policy document from Google Drive, so the help assistant can answer from it.

WHY THIS IS NOT "FETCH THE URL"
-------------------------------
The assistant deliberately never fetches the URL its configuration names: a
server that fetches whatever it is told can be pointed at the internal network.
This module keeps that property. It accepts a Google Drive or Docs link, takes
only the file ID out of it, and talks to one fixed API (googleapis.com) about
that ID. Any other URL is refused, whatever it says.

ACCESS
------
Drive files here need a Google sign-in, which nothing unattended can complete.
So the runner reads them as a Google Cloud service account:

  - GOOGLE_SA_KEY: the service account's JSON key, as one Doppler secret (or an
    environment variable). The name ends in _KEY, so it is masked in job logs.
  - Share the document with the service account's email (Viewer is enough).

Read-only scope, and only files shared with that account are visible to it.

CACHING
-------
The text is cached under /data/policy-cache and re-read at most every
REFRESH_SECONDS, or immediately when Drive reports a newer modifiedTime. If a
refresh fails, the last good copy keeps answering and the failure is reported
by status() - a policy that answered yesterday should not vanish because
Google was slow today. With no copy at all, the assistant links the document
and says it could not read it.
"""

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3/files/"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
KEY_VAR = "GOOGLE_SA_KEY"
CACHE_DIR = Path(os.environ.get("CAPTURE_DATA_DIR", "/data")) / "policy-cache"
REFRESH_SECONDS = int(os.environ.get("POLICY_REFRESH_SECONDS", "3600"))
RETRY_SECONDS = 300            # after a failure, before trying Google again
TIMEOUT = 20
MAX_BYTES = 15 * 1024 * 1024

HOSTS = ("drive.google.com", "docs.google.com")
GOOGLE_DOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_lock = threading.Lock()
_token = {"value": "", "until": 0.0}
_state = {}                    # file id -> {"checked", "error", "name", "modified"}


class DriveError(Exception):
    pass


def file_id(url):
    """The Drive file ID in a Google Drive/Docs link, or None for anything else."""
    try:
        u = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return None
    if u.scheme != "https" or (u.hostname or "").lower() not in HOSTS:
        return None
    m = re.search(r"/(?:file|document|presentation|spreadsheets)/d/([A-Za-z0-9_-]{10,})", u.path)
    if m:
        return m.group(1)
    q = urllib.parse.parse_qs(u.query).get("id", [""])[0]
    return q if re.fullmatch(r"[A-Za-z0-9_-]{10,}", q) else None


def _setting(name):
    val = os.environ.get(name, "").strip()
    if val:
        return val
    try:
        from . import doppler
        if doppler.configured():
            return str((doppler.fetch() or {}).get(name, "") or "").strip()
    except Exception:                      # noqa: BLE001
        pass
    return ""


def configured():
    return bool(_setting(KEY_VAR))


def _access_token():
    if _token["value"] and _token["until"] > time.time() + 60:
        return _token["value"]
    raw = _setting(KEY_VAR)
    if not raw:
        raise DriveError(f"{KEY_VAR} is not set: add the service account's JSON key to Doppler")
    try:
        key = json.loads(raw)
        email, private_key = key["client_email"], key["private_key"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DriveError(f"{KEY_VAR} is not a service-account JSON key") from exc

    import jwt                             # PyJWT, already used for Okta

    now = int(time.time())
    # The token endpoint is fixed, not taken from the key file: the key is
    # configuration, and configuration does not get to choose where we post.
    assertion = jwt.encode({"iss": email, "scope": SCOPE, "aud": TOKEN_URL,
                            "iat": now, "exp": now + 3600},
                           private_key, algorithm="RS256")
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion}).encode()
    data = json.loads(_http(TOKEN_URL, data=body, auth=False))
    _token.update(value=data["access_token"], until=time.time() + int(data.get("expires_in", 3600)))
    return _token["value"]


def _http(url, data=None, auth=True):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if auth:
        req.add_header("Authorization", f"Bearer {_access_token()}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            out = res.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        hint = {401: " - the key was rejected", 403: " - the file is not shared with the "
                "service account, or the Drive API is not enabled", 404: " - no such file, "
                "or it is not shared with the service account"}.get(exc.code, "")
        raise DriveError(f"Google returned HTTP {exc.code}{hint}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DriveError(f"could not reach Google: {exc}") from exc
    if len(out) > MAX_BYTES:
        raise DriveError("the document is larger than 15 MB")
    return out


def _docx_text(content):
    """Paragraph text from a .docx, headings marked so they become sections."""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t")).strip()
        if not text:
            continue
        style = p.find("w:pPr/w:pStyle", ns)
        name = (style.get(f"{{{ns['w']}}}val") if style is not None else "") or ""
        m = re.match(r"Heading(\d)", name)
        lines.append(("#" * (int(m.group(1)) + 1) + " " if m else "") + text)
    return "\n\n".join(lines)


FOOTER_RE = re.compile(r"(?m)^\s*(Confidential and Proprietary - Internal Use|Internal Use Only|"
                       r"Page \d+( of \d+)?)\s*\d*\s*$")


def pdf_text(content):
    """Readable text from a PDF, repaired for how policy PDFs extract.

    Exported Google Docs come out of pypdf with many sentences broken one word
    per line ("policy\\n \\ndefines\\n \\nminimum"), ligatures as single
    glyphs ("ﬁ") and a footer on every page. Left like that, retrieval matches
    fragments and an answer quotes a footer. Layout mode is no better here - it
    splits words ("p assword"). So: plain extraction, ligatures normalised,
    the word-per-line runs joined, footers dropped, and numbered clauses
    ("2.2. Password Parameters") put on their own line so they become headings.
    """
    import unicodedata
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        t = unicodedata.normalize("NFKC", page.extract_text() or "")
        t = re.sub(r"\s*\n \n\s*", " ", t)
        t = FOOTER_RE.sub("", t)
        t = re.sub(r"[ \t]{2,}", " ", t)
        pages.append(t)
    text = "\n".join(pages)
    for footer in ("Confidential and Proprietary - Internal Use",):
        text = re.sub(re.escape(footer) + r"\s*\d*", " ", text)
    # "... customer use. 3. Service Account Passwords 3.1. Secure Storage ● ..."
    clause = r"(\d+(?:\.\d+)*\.)\s+([A-Z][A-Za-z0-9/&,'()\- ]{2,60}?)"
    text = re.sub(r"(?:(?<=\s)|^)" + clause + r"\s*(?=●|\n|\d+\.\d+\.\s)", r"\n\1 \2\n", text)
    return re.sub(r"[ \t]*\n[ \t]*", "\n", text)


_pdf_text = pdf_text


# "4.2 Password length" - a numbered clause, as policies are written. Made a
# heading so an answer can cite the clause rather than "the policy".
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+([A-Z][^.]{2,80})$")
# "Purpose", "Personnel Responsibilities" - a section title on its own line.
SECTION_RE = re.compile(r"^[A-Z][a-z]+(?: (?:[A-Z][a-z]+|&|and|of)){0,3}$")


def _as_markdown(title, text):
    out = [f"# {title}", ""]
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("#"):
            out.append(s)
        elif CLAUSE_RE.match(s) and len(s) <= 90:
            out.append(f"## {s}")
        elif SECTION_RE.match(s) and len(s) <= 40:
            out.append(f"## {s}")
        else:
            out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def as_markdown(title, text):
    return _as_markdown(title, text)


def _read(fid):
    meta = json.loads(_http(API + fid + "?fields=name,mimeType,modifiedTime&supportsAllDrives=true"))
    mime = meta.get("mimeType", "")
    if mime == GOOGLE_DOC:
        text = _http(API + fid + "/export?mimeType=text/plain").decode("utf-8", "replace")
    else:
        content = _http(API + fid + "?alt=media&supportsAllDrives=true")
        if mime == "application/pdf":
            text = _pdf_text(content)
        elif mime == DOCX:
            text = _docx_text(content)
        elif mime.startswith("text/"):
            text = content.decode("utf-8", "replace")
        else:
            raise DriveError(f"cannot read a {mime or 'file of unknown type'}: share it as a "
                             f"Google Doc, PDF, .docx or text file")
    if not text.strip():
        raise DriveError("the document has no readable text (a scanned PDF needs OCR first)")
    return meta, text


def cached(url, title):
    """Path to the cached text of a Drive document, refreshed when due, or None.

    Never raises: the assistant asks on every question, and a Drive problem must
    cost it the policy, not the answer.
    """
    fid = file_id(url)
    if not fid:
        return None
    path = CACHE_DIR / f"{fid}.md"
    meta_path = CACHE_DIR / f"{fid}.json"
    with _lock:
        st = _state.setdefault(fid, {"checked": 0.0, "error": ""})
        # The cached file's age is the clock (an unchanged document is touched
        # on each check). After a failure, Google is not asked again for
        # RETRY_SECONDS whatever the age, so an outage is not a request per question.
        since_check = time.time() - st["checked"]
        if path.is_file():
            if time.time() - path.stat().st_mtime < REFRESH_SECONDS:
                return path
            if st["error"] and since_check < RETRY_SECONDS:
                return path
        elif st["checked"] and since_check < RETRY_SECONDS:
            return None
        st["checked"] = time.time()
        try:
            meta, text = _read(fid)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            old = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
            if not path.is_file() or old.get("modifiedTime") != meta.get("modifiedTime"):
                path.write_text(_as_markdown(title, text))
            else:
                path.touch()               # unchanged: just restart the clock
            meta_path.write_text(json.dumps(meta))
            st.update(error="", name=meta.get("name", ""), modified=meta.get("modifiedTime", ""))
        except Exception as exc:           # noqa: BLE001 - reported, never raised
            st["error"] = str(exc)[:300]
        return path if path.is_file() else None


def status(url):
    """Non-secret facts about the cached document, for /api/ask."""
    fid = file_id(url)
    if not fid:
        return None
    path = CACHE_DIR / f"{fid}.md"
    meta_path = CACHE_DIR / f"{fid}.json"
    meta = {}
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    except (ValueError, OSError):
        pass
    st = _state.get(fid, {})
    return {"readable": path.is_file(), "name": meta.get("name", ""),
            "modified": meta.get("modifiedTime", ""), "credential": configured(),
            "error": st.get("error", "")}
