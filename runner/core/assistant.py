#!/usr/bin/env python3
"""
The help assistant: answers questions about Compliance Partner from docs/.

WHY IT RETRIEVES INSTEAD OF GENERATING
--------------------------------------
This bot answers questions about SOX controls. The failure that matters is not
"unhelpful", it is "confidently wrong": a user told the wrong due date, or sent
to a button that does not exist, has been misled about a control they are
attesting to. A generative answer is fluent whether or not it is true, and
nobody reading it can tell which.

So the default has no model in it at all. The answer is the guide's own words,
with the section it came from printed next to it. It cannot invent a control,
and when the guide does not cover something it says so instead of improvising.
That also makes it free to run and keeps the documentation inside the network.

docs/Compliance_Partner.md was written for this: section 12 asks for chunking by
heading with the FAQ split one chunk per question, and section 11 is 15 questions
already phrased the way users ask them. Those FAQ chunks do most of the work.

WHEN A MODEL IS CONFIGURED
--------------------------
Set ANTHROPIC_API_KEY (environment, or a Doppler secret) and answers are composed
by Claude *from the retrieved sections only*, still with citations, still refusing
when the sections do not cover the question. Retrieval runs first either way, so
the model never sees the question without the evidence, and any failure - no key,
timeout, bad response - falls back to the extractive answer rather than to an
apology. ASSISTANT_MODEL picks the model; the default is the cheapest one that
does this job well.

THE PASSWORD POLICY REFERENCE
-----------------------------
The guide describes a "Password Policy Alignment" check that compares systems
against the corporate password policy, but it does not contain that policy, so
"what is the minimum length?" used to end in a refusal with nowhere to go.
CW_PASSWORD_POLICY_DOC (environment, or a Doppler secret) names the policy:

  - an http(s) URL: password questions carry a link to it. It is not fetched.
    Policy pages sit behind SSO, and a server that fetches whatever URL its
    configuration names is a server that can be pointed at the network.
  - a .md or .txt path (absolute, or relative to the docs folder): it is also
    indexed, so its content answers and is cited like the guide's.

Unset, or set to something unusable, it changes nothing: no reference is better
than a link that goes nowhere. CW_PASSWORD_POLICY_TITLE sets the link text.
"""

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DOCS = Path(os.environ.get("ASSISTANT_DOCS", "/docs"))
MODEL = os.environ.get("ASSISTANT_MODEL", "claude-haiku-4-5-20251001")
TIMEOUT_S = float(os.environ.get("ASSISTANT_TIMEOUT", "20"))

MAX_CHUNK = 1800          # characters; longer sections are split on paragraphs
TOP_K = 4                 # sections retrieved per question
K1, B = 1.4, 0.72         # BM25 term saturation and length normalisation

# Dropped from queries and from the index. Short list on purpose: "all", "user"
# and "access" look like stopwords and are load-bearing here ("user access
# review", "access denied"), so only true function words are removed.
STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "doing", "to", "of", "in", "on", "at", "by", "for",
    "with", "from", "into", "and", "or", "but", "if", "then", "than", "that",
    "this", "these", "those", "it", "its", "as", "so", "such", "there", "here",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they", "them",
    "can", "could", "would", "should", "will", "shall", "may", "might", "must",
    "have", "has", "had", "get", "got", "please", "tell", "show", "about",
    # Interrogatives. They carry no topic, and left in they match headings that
    # merely start with the same word - "what is PBC" retrieved "What is
    # Compliance Partner?" instead of the glossary entry.
    "what", "how", "why", "when", "where", "which", "whom", "whose",
}

# The guide and its users do not always use the same word. Each entry adds terms
# to the query; it never replaces them, so a query that already matches stays
# matching. Kept short and reviewable - an aggressive thesaurus makes every
# question retrieve the same few sections.
SYNONYMS = {
    "login": ["sign", "signing", "okta"], "log": ["sign"], "logon": ["sign"],
    "signin": ["sign"], "password": ["okta", "credentials"],
    "uar": ["user", "access", "review", "ua"],
    "cm": ["change", "management"], "elc": ["soc", "entity"],
    "rcm": ["control", "catalog", "risk"], "pbc": ["evidence", "supporting"],
    "due": ["deadline", "overdue", "quarter"], "deadline": ["due", "overdue"],
    "workpaper": ["workpapers"], "ticket": ["jira"], "jira": ["ticket"],
    "download": ["downloading"], "export": ["csv", "reports"],
    "exception": ["issue", "issues"], "issue": ["exception", "exceptions"],
    "admin": ["administration", "administrator"],
    "permission": ["access", "scope", "assigned"],
    "screenshot": ["capture", "evidence"],
    "bot": ["chatbot", "assistant"], "chatbot": ["assistant"],
}

_lock = threading.Lock()
_index = {"chunks": [], "df": {}, "avglen": 1.0, "stamp": None}


POLICY_VAR = "CW_PASSWORD_POLICY_DOC"
POLICY_TITLE = os.environ.get("CW_PASSWORD_POLICY_TITLE", "CoreWeave Password Policy")

# A question touching any of these is a password-policy question, and gets the
# policy as a reference whether or not the guide could answer it.
POLICY_TERMS = {
    "password", "passwords", "passphrase", "passphrases", "complexity",
    "expiry", "expire", "expiration", "rotation", "rotate", "lockout",
    "mfa", "2fa", "multifactor", "credential", "credentials",
}


def _setting(name):
    """A setting from the environment, else from Doppler; '' when absent."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    try:                                   # Doppler is this project's secret store
        from . import doppler
        if doppler.configured():
            return str((doppler.fetch() or {}).get(name, "") or "").strip()
    except Exception:                      # noqa: BLE001 - never block an answer
        pass
    return ""


def policy():
    """The configured password policy, or None.

    None also for a value that cannot be used - a file that is not there, a type
    that is not text, a scheme that is not http(s) - because a reference the
    reader cannot follow is worse than none.
    """
    val = _setting(POLICY_VAR)
    if not val:
        return None
    if re.match(r"^https?://\S+$", val, re.I):
        return {"title": POLICY_TITLE, "kind": "url", "url": val}
    if "://" in val:
        return None                        # javascript:, file:, ftp: - never linked
    path = Path(val)
    if not path.is_absolute():
        path = DOCS / path
    try:
        if path.is_file() and path.suffix.lower() in (".md", ".txt"):
            return {"title": POLICY_TITLE, "kind": "file", "path": path,
                    "doc": path.name}
    except OSError:
        pass
    return None


def _reference(pol):
    """What the page is told about the policy - never the container path."""
    ref = {"title": pol["title"], "kind": pol["kind"]}
    if pol["kind"] == "url":
        ref["url"] = pol["url"]
    else:
        ref["doc"] = pol["doc"]
    return ref


# ------------------------------------------------------------------ indexing

def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) > 1 and t not in STOP]


def _expand(terms):
    out = list(terms)
    for t in terms:
        out.extend(SYNONYMS.get(t, ()))
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


def _split_sections(text):
    """(heading-path, body) per markdown heading, document order."""
    lines = text.split("\n")
    path, buf, out = [], [], []
    title = ""

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append((" > ".join(path), body))

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not m:
            buf.append(line); continue
        flush(); buf = []
        depth, head = len(m.group(1)), m.group(2).strip()
        if depth == 1 and not title:
            title = head
        path = path[: depth - 1]
        while len(path) < depth - 1:
            path.append("")
        path.append(head)
    flush()
    return title, [(p.strip(" >"), b) for p, b in out]


def _faq_chunks(heading, body):
    """One chunk per Q/A. The question text is what users actually type."""
    parts = re.split(r"\n(?=\*\*Q:)", body)
    out = []
    for part in parts:
        m = re.match(r"\*\*Q:\s*(.+?)\*\*\s*\n?A:\s*(.*)$", part.strip(), re.S)
        if not m:
            continue
        q, a = m.group(1).strip(), m.group(2).strip()
        out.append({"heading": heading, "question": q, "text": a, "faq": True})
    return out


def _table_rows(heading, body):
    """One chunk per markdown table row.

    Tables here are lookup tables - glossary terms, roles and their modules,
    controls and their cadence, systems and their links. Whole-section scoring
    buries them: the section is long, so length normalisation penalises it, and
    the one row that answers the question is a few words in a few hundred. Each
    row is cheap to index and is the unit a user is actually asking for.
    """
    out = []
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or len(cells) > 5:
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue                      # the |---|---| separator
        if not cells[0] or cells[0].lower() in ("term", "page", "role", "system",
                                                "module", "control", "check",
                                                "example id", "field", "option"):
            continue                      # header row
        rest = " - ".join(c for c in cells[1:] if c)
        if not rest:
            continue
        out.append({"heading": heading, "question": cells[0],
                    "text": f"{cells[0]} - {rest}", "faq": False, "row": True})
    return out


def _wrap(heading, body):
    """Split an over-long section on paragraph boundaries, keeping the heading."""
    if len(body) <= MAX_CHUNK:
        return [{"heading": heading, "question": "", "text": body, "faq": False}]
    out, cur = [], ""
    for para in body.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > MAX_CHUNK:
            out.append({"heading": heading, "question": "", "text": cur.strip(),
                        "faq": False})
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        out.append({"heading": heading, "question": "", "text": cur.strip(),
                    "faq": False})
    return out


def _sources_on_disk():
    """(path, is_policy) for everything the index is built from."""
    found = [(p, False) for p in sorted(DOCS.glob("*.md"))] if DOCS.is_dir() else []
    pol = policy()
    if pol and pol["kind"] == "file":
        target = pol["path"].resolve()
        # A policy that already lives in docs/ is indexed once, flagged as policy.
        found = [(p, p.resolve() == target or flag) for p, flag in found]
        if not any(p.resolve() == target for p, _ in found):
            found.append((pol["path"], True))
    return found


def _stamp():
    out = []
    for p, flag in _sources_on_disk():
        try:
            st = p.stat()
            out.append((str(p), flag, st.st_mtime, st.st_size))
        except OSError:
            continue
    # The setting itself is part of the stamp: switching it from a URL to a file
    # must rebuild even though no file under docs/ changed.
    return tuple(sorted(out)) + (("policy", _setting(POLICY_VAR)),)


def _build():
    chunks = []
    for path, is_policy in _sources_on_disk():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title, sections = _split_sections(text)
        if is_policy:
            title = POLICY_TITLE
        for heading, body in sections:
            if "**Q:" in body:
                made = _faq_chunks(heading, body)
            else:
                made = _wrap(heading, body) + _table_rows(heading, body)
            for c in made:
                c["doc"] = path.name
                c["title"] = title or path.stem
                c["policy"] = is_policy
                if is_policy and not c["heading"]:
                    c["heading"] = POLICY_TITLE     # a .txt has no headings
                # The heading and the question are the strongest signal of what a
                # chunk is about, so they are indexed as well as the body.
                c["terms"] = _tokens(c["heading"] + " " + c["question"] + " " + c["text"])
                c["head_terms"] = set(_tokens(c["heading"] + " " + c["question"]))
                chunks.append(c)

    df = {}
    for c in chunks:
        for t in set(c["terms"]):
            df[t] = df.get(t, 0) + 1
    avg = sum(len(c["terms"]) for c in chunks) / max(1, len(chunks))
    return {"chunks": chunks, "df": df, "avglen": avg or 1.0, "stamp": _stamp()}


def index():
    """The index, rebuilt when a doc changes on disk."""
    with _lock:
        if _index["chunks"] and _index["stamp"] == _stamp():
            return _index
        built = _build()
        _index.update(built)
        return _index


# ----------------------------------------------------------------- retrieval

def search(question, k=TOP_K):
    idx = index()
    chunks, df, avg = idx["chunks"], idx["df"], idx["avglen"]
    N = len(chunks)
    if not N:
        return [], 0.0

    asked = _tokens(question)
    if not asked:
        return [], 0.0
    terms = _expand(asked)
    phrase = " ".join(asked)

    scored = []
    for c in chunks:
        tf = {}
        for t in c["terms"]:
            tf[t] = tf.get(t, 0) + 1
        dl = len(c["terms"]) or 1
        s = 0.0
        for t in terms:
            n = df.get(t, 0)
            if not n:
                continue
            idf = math.log(1 + (N - n + 0.5) / (n + 0.5))
            f = tf.get(t, 0)
            if f:
                s += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / avg))
            if t in c["head_terms"]:
                s += idf * 1.6            # heading match: strong topical signal
        if len(asked) > 1 and phrase in " ".join(c["terms"]):
            s += 6.0                      # the whole question appears verbatim
        if c["faq"]:
            s *= 1.12                     # written as an answer already
        if c.get("row") and len(asked) > 4:
            s *= 0.75                     # a broad question wants the section
        if s > 0:
            scored.append((s, c))

    scored.sort(key=lambda t: -t[0])
    top = scored[:k]
    if not top:
        return [], 0.0

    # Confidence is how much of the question the best section actually accounts
    # for, not the raw BM25 score - which has no scale a threshold can use.
    best = top[0][1]
    content = set(asked)
    covered = len({t for t in content if t in best["terms"] or t in best["head_terms"]})
    coverage = covered / max(1, len(content))
    return [c for _, c in top], coverage


# ------------------------------------------------------------ answer shaping

def _trim(text, limit=900):
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n\n", ". ", "\n"):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[: i + (1 if sep == ". " else 0)].strip() + " ..."
    return cut.strip() + " ..."


# What answered. Reported on every reply, because "the guide said so" and "a
# model wrote this from the guide" are different claims and a reader checking a
# control needs to know which one they are looking at.
GUIDE_ENGINE = "guide retrieval"


def _engine(model=None):
    return {"engine": "claude" if model else "guide",
            "model": model or GUIDE_ENGINE}


def _source(c):
    return {"doc": c["doc"], "heading": c["heading"],
            "question": c.get("question", ""), "policy": bool(c.get("policy"))}


NO_ANSWER = ("That is not covered in the Compliance Partner guide. "
             "Try rephrasing, or ask a Compliance Partner administrator.")


def _extractive(question, hits, coverage):
    best = hits[0]
    if best["faq"]:
        body = best["text"]
    else:
        body = _trim(best["text"])
    return dict({"answer": body, "sources": [_source(c) for c in hits[:3]],
                 "mode": "guide", "confidence": round(coverage, 2)}, **_engine())


# ------------------------------------------------------------- optional model

def _api_key():
    return _setting("ANTHROPIC_API_KEY")


SYSTEM = (
    "You are the Compliance Partner Assistant for CoreWeave employees.\n"
    "Answer ONLY from the sections provided in the user message: the Compliance "
    "Partner guide and, where present, the CoreWeave password policy.\n"
    "- Use the exact button, tab and control names as written.\n"
    "- If the sections do not answer the question, reply exactly: " + NO_ANSWER + "\n"
    "- Never state a due date, control name or behaviour that is not in the sections.\n"
    "- Never claim the app creates Jira tickets or changes source systems.\n"
    "- Remind the user that humans own all decisions only when they ask about "
    "validation, sign-off or AI-generated content.\n"
    "- Two or three sentences. No preamble, no markdown headings."
)


def _llm(question, hits, key):
    ctx = "\n\n".join(
        f"[{c['doc']} > {c['heading']}]\n" + (f"Q: {c['question']}\n" if c["question"] else "")
        + c["text"] for c in hits)
    payload = json.dumps({
        "model": MODEL, "max_tokens": 600, "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"Guide sections:\n\n{ctx}\n\nQuestion: {question}"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload, method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = json.loads(r.read().decode())
    parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


# ------------------------------------------------------------------ the entry

DEFAULT_INTRO = ("Ask about preparing controls, monitoring, audit testing or "
                 "access. Answers come from the Compliance Partner guide.")


def _intro(chunks):
    """The opening line, taken from the guide's own description of itself.

    Hardcoding it in the page meant two descriptions of the product that could
    drift apart, and the one in the chat was the one nobody would notice was
    stale.
    """
    for c in chunks:
        if c["faq"] or c.get("row"):
            continue
        if "what is" not in c["heading"].lower():
            continue
        for para in c["text"].split("\n\n"):
            para = " ".join(para.split())
            if len(para) > 60 and not para.startswith(("|", ">", "-", "#")):
                return re.sub(r"[*`]", "", para)
    return DEFAULT_INTRO


def starters(limit=4):
    """Opening questions, taken from the guide's FAQ.

    These are the questions the guide's authors expected to be asked, phrased
    the way they expected them - better suggestions than anything invented in
    the page, and they follow the docs when the docs change. Spread across the
    list rather than taken from the front, so they do not all land in one module.
    """
    chunks = index()["chunks"]
    qs = [c["question"] for c in chunks
          if c.get("faq") and 12 <= len(c.get("question", "")) <= 74]
    seen, uniq = set(), []
    for q in qs:
        k = q.lower()
        if k not in seen:
            seen.add(k); uniq.append(q)
    if len(uniq) <= limit:
        return uniq
    step = len(uniq) / limit
    return [uniq[int(i * step)] for i in range(limit)]


def configured():
    idx = index()
    model = MODEL if _api_key() else None
    return {"docs": sorted({c["doc"] for c in idx["chunks"]}),
            "sections": len(idx["chunks"]),
            "model": model or GUIDE_ENGINE,
            "engine": "claude" if model else "guide",
            "intro": _intro(idx["chunks"]),
            "starters": starters(),
            "policy": _reference(policy()) if policy() else None}


NO_ANSWER_POLICY = ("The Compliance Partner guide does not cover that. For password "
                    "requirements, the {title} is the reference.")


def ask(question):
    """An answer, plus the password policy as a reference when it applies."""
    result = _answer(question)
    pol = policy()
    if not pol or result.get("mode") == "empty":
        return result
    about_passwords = bool(POLICY_TERMS & set(_tokens(question or "")))
    cited = any(s.get("policy") for s in result.get("sources", []))
    if not (about_passwords or cited):
        return result
    result["references"] = [_reference(pol)]
    if result.get("mode") == "unknown" and about_passwords:
        # Not a dead end any more: there is somewhere to go for this one.
        result["answer"] = NO_ANSWER_POLICY.format(title=pol["title"])
    return result


def _answer(question):
    question = (question or "").strip()
    if not question:
        return dict({"answer": "Ask me anything about Compliance Partner.",
                     "sources": [], "mode": "empty", "confidence": 0.0}, **_engine())
    if len(question) > 500:
        question = question[:500]

    hits, coverage = search(question)
    # Below this the best section shares almost nothing with the question, and
    # answering from it would be answering a question nobody asked.
    if not hits or coverage < 0.34:
        # Refusal comes from the retrieval score, so it is the guide's verdict
        # even when a model is configured - the model is never asked.
        return dict({"answer": NO_ANSWER, "sources": [_source(c) for c in hits[:2]],
                     "mode": "unknown", "confidence": round(coverage, 2)}, **_engine())

    key = _api_key()
    if key:
        try:
            text = _llm(question, hits, key)
            if text:
                return dict({"answer": text, "sources": [_source(c) for c in hits[:3]],
                             "mode": "claude", "confidence": round(coverage, 2)},
                            **_engine(MODEL))
        except Exception:                  # noqa: BLE001 - fall back, never fail
            pass
    return _extractive(question, hits, coverage)
