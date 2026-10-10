#!/usr/bin/env python3
"""
Compliance Partner - capture runner API.

Turns the "Prepare" button in the Compliance Partner web app into a real capture,
and reports what that run is doing while it does it.

Captures live in one package per system - workato/, netsuite/ - over the shared
platform in core/. CAPTURES below is the only place that maps a (system, control)
to the module that backs it; a pair absent from it has no capture, and
/api/prepare answers 415 with a reason, and the web app falls back to its
prototype simulation for those. That boundary is deliberate: the page should never
imply it collected real evidence when it did not.

The runner holds ONE job at a time. The capture drives a single X display and a
single browser profile, so two concurrent runs would photograph each other's
windows and silently produce mixed-up evidence.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, request, send_file

from core import assistant, auth, environments, systems, users

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CAPTURE_DATA_DIR", "/data"))
RUNS_DIR = DATA_DIR / "runs"
PROFILE_DIR = DATA_DIR / "browser-profile"
SESSION_URL = os.environ.get("CAPTURE_SESSION_URL", "")
LOG_TAIL_LINES = 400

# (system, control id) -> the capture that backs it. A pair absent from here has no
# real capture, and /api/prepare refuses it rather than let the page imply one ran.
CAPTURES = {
    ("workato", "cm-02"): {
        "module": "workato.sox_capture",
        "control": "Change management",
        "flags": ["out", "profile", "no-pause", "browser", "env", "settle", "workspace",
                  "month", "config"],
        # No "project": this capture is scoped by its config sheet's Capture(Y/N)
        # column now, not by a --project flag. Passing one made argparse reject
        # the whole run before it started.
        "scope": ["workspace"],
        "produces": ["Excel workbook", "screenshot manifest", "screenshots"],
    },
    ("workato", "ua-04"): {
        "module": "workato.uar_capture",
        "control": "User access review",
        "flags": ["out", "profile", "no-pause", "browser", "env", "settle", "workspace", "period"],
        "scope": ["workspace", "period"],
        "produces": ["Excel workbook", "users.csv", "screenshot manifest", "screenshots"],
    },
    ("netsuite", "ua-04"): {
        "module": "netsuite.uar_capture",
        "control": "User access review",
        "flags": ["out", "profile", "no-pause", "browser", "env", "settle", "period"],
        "scope": ["period"],
        "produces": ["Excel workbook", "users.csv", "screenshot manifest", "screenshots"],
    },
    ("netsuite", "cm-02"): {
        "module": "netsuite.sox_capture",
        "control": "Change management",
        "flags": ["out", "profile", "no-pause", "browser", "env", "settle", "period", "area"],
        "scope": ["period", "area"],
        "produces": ["Excel workbook", "changes.csv", "screenshot manifest", "screenshots"],
    },
}

def wired_systems():
    """Systems a capture is attached to, spelled as the inventory spells them.

    CAPTURES is keyed lowercase; title-casing that gives "Netsuite" where the
    inventory says "NetSuite". Matching is case-insensitive so the lock held
    either way, but a label shown to an administrator should be the real name.
    """
    inventory = systems.all_systems()
    out = []
    for key, _ in CAPTURES:
        match = next((s for s in inventory if s.lower() == key.lower()), key.title())
        if match not in out:
            out.append(match)
    return sorted(out)

app = Flask(__name__)

_lock = threading.Lock()
_jobs = {}          # job_id -> dict
_active = None      # job_id of the running job, or None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_dir(job_id):
    return RUNS_DIR / job_id


def _public(job):
    """The view of a job the browser gets - no local paths beyond artifact names."""
    return {
        "id": job["id"],
        "system": job["system"],
        "controlId": job["controlId"],
        "period": job["period"],
        "month": job["month"],
        "workspace": job["workspace"],
        "project": job["project"],
        "status": job["status"],
        "phase": job["phase"],
        "phaseDetail": job["phaseDetail"],
        "startedAt": job["startedAt"],
        "finishedAt": job["finishedAt"],
        "exitCode": job["exitCode"],
        "error": job["error"],
        "screenshots": job["screenshots"],
        "warnings": job["warnings"],
        "artifacts": job["artifacts"],
        "users": job["users"],
        "sessionUrl": SESSION_URL if job["phase"] == "awaiting_login" else "",
        "log": job["log"][-LOG_TAIL_LINES:],
    }


PHASE_RE = re.compile(r"^PHASE::([a-z_]+)::(.*)$")
CAPTURED_RE = re.compile(r"captured -> ")
# "!!" marks a warning, but log() stamps every line with "[HH:MM:SS] " first, so the
# marker is never at the start. Anchoring on "^\s*!!" silently matched nothing and
# every run reported zero warnings however many it had.
WARN_RE = re.compile(r"^(?:\[[0-9:]+\]\s*)?\s*!!")


def _collect_artifacts(job):
    """Index whatever the run actually produced. Called once the process exits."""
    out = _job_dir(job["id"]) / "out"
    if not out.is_dir():
        return

    found = []
    for xlsx in sorted(out.glob("*.xlsx")):
        found.append({"name": xlsx.name, "kind": "workbook", "bytes": xlsx.stat().st_size})

    manifest = out / "manifest.json"
    if manifest.is_file():
        found.append({"name": manifest.name, "kind": "manifest", "bytes": manifest.stat().st_size})
        try:
            data = json.loads(manifest.read_text())
            # MERGE, never replace. The manifest carries only the workspace-visibility
            # warnings the capture tracks itself; the "!!" lines picked off the log
            # (an unreachable folder, a recipe count that never recovered) exist only
            # in the stream. Overwriting here reported a clean run for one that was
            # not, which is the one failure this tool must never have.
            seen = list(job["warnings"])
            for w in data.get("warnings", []):
                if w not in seen:
                    seen.append(w)
            job["warnings"] = seen
            job["screenshots"] = len(data.get("captures", []))
        except (ValueError, OSError):
            pass

    # A user access review's real output is the listing, not the screenshots, so the
    # counts go on the job and the page can show them without downloading anything.
    users_json = out / "users.json"
    if users_json.is_file():
        found.append({"name": users_json.name, "kind": "users",
                      "bytes": users_json.stat().st_size})
        try:
            data = json.loads(users_json.read_text())
            rows = data.get("users", [])
            job["users"] = {
                "total": len(rows),
                "active": sum(1 for u in rows if u.get("active") is True),
                "inactive": sum(1 for u in rows if u.get("status") == "inactive"),
                "pending": sum(1 for u in rows if u.get("status") == "pending"),
                "unknown": sum(1 for u in rows if u.get("active") is None),
                # None, not 0, when no Workday list was supplied: "0 terminated
                # users still listed" is a finding, "not checked" is not one.
                "terminated": (sum(1 for u in rows if u.get("terminated"))
                               if data.get("terminations_file") else None),
                "terminationsFile": data.get("terminations_file", ""),
                "period": data.get("period", ""),
                "workspace": data.get("workspace", ""),
                "capturedAt": data.get("captured", ""),
                "sourceUrl": data.get("source_url", ""),
                "rows": rows,
            }
        except (ValueError, OSError):
            pass

    users_csv = out / "users.csv"
    if users_csv.is_file():
        found.append({"name": users_csv.name, "kind": "userscsv",
                      "bytes": users_csv.stat().st_size})

    shots = sorted(out.glob("*.png"))
    if shots:
        bundle = out / "screenshots.zip"
        try:
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
                for s in shots:
                    z.write(s, s.name)
            found.append({"name": bundle.name, "kind": "screenshots",
                          "bytes": bundle.stat().st_size, "count": len(shots)})
        except OSError as exc:
            job["log"].append(f"could not bundle screenshots: {exc}")

    job["artifacts"] = found


def _run(job, cmd, secrets=()):
    """Run the capture script, streaming its output into the job record.

    Every line is redacted before it is stored or written. The capture script does
    not print passwords, but a stack trace or a page dump could, and the job log is
    served to the browser.
    """
    global _active
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_job_dir(job["id"])),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,     # no TTY: the script takes its non-interactive path
            text=True,
            bufsize=1,
            env={**os.environ, "CAPTURE_SESSION_URL": SESSION_URL,
                 "PYTHONPATH": str(APP_ROOT)},
        )
        job["pid"] = proc.pid
        logfile = _job_dir(job["id"]) / "run.log"
        with logfile.open("w") as lf:
            for line in proc.stdout:
                line = environments.redact(line.rstrip("\n"), secrets)
                lf.write(line + "\n")
                lf.flush()

                m = PHASE_RE.match(line)
                if m:
                    job["phase"], job["phaseDetail"] = m.group(1), m.group(2)
                    continue          # markers are control data, not log prose
                if CAPTURED_RE.search(line):
                    job["screenshots"] += 1
                if WARN_RE.match(line):
                    # Store the bare message, not the raw line. The manifest records
                    # the same warning without the "[HH:MM:SS]   !! " stamp, and the
                    # merge in _collect_artifacts dedupes on exact text - keeping the
                    # stamp here listed every warning twice.
                    job["warnings"].append(WARN_RE.sub("", line).strip())
                job["log"].append(line)

        code = proc.wait()
        job["exitCode"] = code
        _collect_artifacts(job)
        if job["status"] == "cancelled":
            pass
        elif code == 0:
            job["status"], job["phase"] = "succeeded", "done"
        else:
            job["status"] = "failed"
            job["error"] = f"capture exited with code {code}"
    except Exception as exc:                       # noqa: BLE001 - surfaced to the UI
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finishedAt"] = _now()
        with _lock:
            _active = None


# --------------------------------------------------------------------------- #
# sign-in                                                                      #
# --------------------------------------------------------------------------- #
def _cfg():
    try:
        return auth.load_config()
    except auth.ConfigError as exc:
        app.logger.error("auth config: %s", exc)
        # A broken config must not silently mean "no auth". Treat it as enabled
        # with no way in, so the failure is visible instead of wide open.
        return {"enabled": True, "_broken": str(exc), "entitlements": {},
                "admin_groups": [], "session_hours": 8}


def current_session():
    """The signed-in user, or None."""
    return auth.read_session(request.cookies.get(auth.SESSION_COOKIE))


def _secure_cookie(resp, name, value, seconds):
    resp.set_cookie(
        name, value,
        max_age=seconds, httponly=True, samesite="Lax",
        # Set only over https, since the cookie is what stands between a stranger
        # and a signed-in Workato window. On plain http this stays False or the
        # browser drops it entirely.
        secure=request.headers.get("X-Forwarded-Proto", request.scheme) == "https",
        path="/",
    )
    return resp


LOCAL_COOKIE = "cp_local_user"


def local_session():
    """The locally signed-in user, when Okta is not configured."""
    payload = auth.read_session(request.cookies.get(LOCAL_COOKIE))
    return users.find(payload.get("id")) if payload else None


def current_user():
    """Who is asking, from whichever identity source is in play.

    Okta when configured - the directory then only maps that person to systems.
    Otherwise the name taken at the landing page. Authorization comes from the
    directory either way, so there is one answer to "what may they see".
    """
    cfg = _cfg()
    if cfg.get("enabled"):
        sess = current_session()
        if not sess:
            return None
        who = users.find(sess.get("email", "")) or users.find(
            (sess.get("email", "").split("@") or [""])[0])
        if who:
            # Okta groups may grant admin even where the directory does not.
            if sess.get("admin") and not who["admin"]:
                who = {**who, "role": "Admin", "admin": True,
                       "modules": users.ROLE_MODULES["Admin"][:]}
            return who
        # Signed in to Okta but absent from the directory: no systems, and the
        # picker says so. Better than inventing access for an unknown person.
        return {"id": sess.get("email", "unknown"), "name": sess.get("name") or sess.get("email", ""),
                "role": "Control Preparer", "scopes": [], "modules": [], "admin": False,
                "unlisted": True}
    return local_session()


def _identity_mode():
    return "okta" if _cfg().get("enabled") else "local"


@app.post("/api/signin")
def api_signin():
    """Local sign-in: a name from the directory.

    Refused when Okta is configured - two ways in would mean the weaker one
    decides, and a typed name is the weaker one.
    """
    if _identity_mode() == "okta":
        return jsonify({"error": "okta_only",
                        "message": "Okta is configured; sign in with Okta."}), 409
    body = request.get_json(silent=True) or {}
    who = users.find(body.get("username", ""))
    if not who:
        return jsonify({
            "error": "unknown_user",
            "message": "That name is not in the user directory. An administrator "
                       "adds people under User Administration.",
        }), 404
    token = auth.sign_session({"id": who["id"]}, hours=12)
    resp = make_response(jsonify({"ok": True, "user": who}))
    return _secure_cookie(resp, LOCAL_COOKIE, token, 12 * 3600)


@app.post("/api/signout")
def api_signout():
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(LOCAL_COOKIE, "", max_age=0, path="/")
    resp.set_cookie(auth.SESSION_COOKIE, "", max_age=0, path="/")
    return resp


@app.get("/api/whoami")
def api_whoami():
    """Who is signed in, and what they may see. The page drives itself from this."""
    who = current_user()
    if not who:
        return jsonify({"authenticated": False, "identity": _identity_mode()}), 401
    return jsonify({"authenticated": True, "identity": _identity_mode(), "user": who})


@app.get("/api/directory-names")
def api_directory_names():
    """Names and roles only, so the landing page need not be a guessing game.

    Local mode only. With Okta configured this would hand the staff list to
    anyone who can load the sign-in page, and there identity comes from Okta so
    the list serves no purpose anyway.
    """
    if _identity_mode() == "okta":
        return jsonify({"names": []})
    return jsonify({"names": [{"name": u["name"], "role": u["role"]}
                              for u in users.all_users()]})


@app.post("/api/ask")
def api_ask():
    """The help assistant. Answers from docs/, and says so when it cannot.

    Behind the same session gate as the rest of /api/: the guide describes how
    access is granted and which systems are live, which is not something to
    serve to anyone who can reach the port.
    """
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "")
    try:
        return jsonify(assistant.ask(question, body.get("history")))
    except Exception as exc:              # noqa: BLE001 - help must not 500
        app.logger.warning("assistant failed: %s", exc)
        return jsonify({
            "answer": "The guide could not be searched just now. "
                      "Try again, or ask a Compliance Partner administrator.",
            "sources": [], "mode": "error", "confidence": 0.0,
            "engine": "guide", "model": assistant.GUIDE_ENGINE,
        })


@app.get("/api/ask")
def api_ask_status():
    """What the assistant has indexed - the page uses it to decide whether to
    offer the bubble at all."""
    try:
        return jsonify(dict(assistant.configured(), available=True))
    except Exception:                     # noqa: BLE001
        return jsonify({"available": False, "docs": [], "sections": 0, "model": None})


@app.get("/api/sox-systems")
def api_sox_systems():
    """The inventory: every system a person can be assigned to."""
    return jsonify({
        "systems": systems.all_systems(),
        "wired": wired_systems(),       # these cannot be removed
        "roles": users.ROLES,
        "roleModules": users.ROLE_MODULES,
    })


@app.post("/api/sox-systems")
def api_sox_system_add():
    who, err = _require_admin()
    if err:
        return err
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    try:
        inventory = systems.add(name)
    except systems.SystemError_ as exc:
        return jsonify({"error": "invalid", "message": str(exc)}), 400
    return jsonify({"ok": True, "systems": inventory, "added": name})


@app.delete("/api/sox-systems/<path:name>")
def api_sox_system_remove(name):
    who, err = _require_admin()
    if err:
        return err
    try:
        # Dropping it from every user's scopes is part of removing it, not a
        # follow-up someone has to remember.
        inventory = systems.remove(name, wired=wired_systems(),
                                   on_removed=users.drop_scope)
    except systems.SystemError_ as exc:
        return jsonify({"error": "invalid", "message": str(exc)}), 400
    return jsonify({"ok": True, "systems": inventory, "removed": name})


def _require_admin():
    who = current_user()
    if not who:
        return None, (jsonify({"error": "not_signed_in"}), 401)
    if not who.get("admin"):
        return None, (jsonify({
            "error": "not_admin",
            "message": "Only an administrator can change who has access.",
        }), 403)
    return who, None


@app.get("/api/users")
def api_users():
    who, err = _require_admin()
    if err:
        return err
    return jsonify({"users": users.all_users(), "roles": users.ROLES})


@app.put("/api/users/<uid>")
@app.post("/api/users")
def api_user_save(uid=None):
    who, err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if uid:
        body = {**body, "id": uid}
    try:
        saved = users.upsert(body)
    except users.UserError as exc:
        return jsonify({"error": "invalid", "message": str(exc)}), 400
    return jsonify({"ok": True, "user": saved})


@app.delete("/api/users/<uid>")
def api_user_remove(uid):
    who, err = _require_admin()
    if err:
        return err
    if uid.strip().lower() == who["id"]:
        return jsonify({"error": "invalid",
                        "message": "You cannot remove your own access."}), 400
    try:
        users.remove(uid)
    except users.UserError as exc:
        return jsonify({"error": "invalid", "message": str(exc)}), 400
    return jsonify({"ok": True})


@app.get("/auth/config")
def auth_config():
    """Whether sign-in is on. The login page reads this; it exposes no secret."""
    cfg = _cfg()
    # Say which step is outstanding. "Sign-in is off" is true whether the config
    # file is absent, present but half-filled, or deliberately disabled, and
    # telling someone to copy a file they already copied is no help at all.
    have_file = auth.CONFIG_PATH.is_file()
    missing = [k for k in ("issuer", "client_id", "redirect_uri") if not cfg.get(k)]
    if not have_file:
        step = "no_config"
    elif missing:
        step = "incomplete"
    elif not cfg.get("enabled"):
        step = "disabled"
    else:
        step = "ready"
    return jsonify({
        "enabled": bool(cfg.get("enabled")),
        "configured": not cfg.get("_broken"),
        "error": cfg.get("_broken", ""),
        "issuer": cfg.get("issuer", "") if cfg.get("enabled") else "",
        "step": step,
        "missing": missing,
        "configPath": str(auth.CONFIG_PATH),
    })


@app.get("/auth/login")
def auth_login():
    cfg = _cfg()
    if not cfg.get("enabled"):
        return redirect("/")
    if cfg.get("_broken"):
        return jsonify({"error": "auth_misconfigured", "message": cfg["_broken"]}), 500
    nxt = request.args.get("next", "/")
    try:
        url, flow = auth.begin_login(cfg, nxt)
    except auth.AuthError as exc:
        return jsonify({"error": "okta_unreachable", "message": str(exc)}), 502
    return _secure_cookie(make_response(redirect(url)), auth.FLOW_COOKIE, flow, 900)


@app.get("/auth/callback")
def auth_callback():
    cfg = _cfg()
    if not cfg.get("enabled"):
        return redirect("/")
    if request.args.get("error"):
        return jsonify({
            "error": request.args["error"],
            "message": request.args.get("error_description", "Okta refused the sign-in."),
        }), 401
    try:
        user, nxt = auth.complete_login(
            cfg, request.args.get("code"), request.args.get("state"),
            request.cookies.get(auth.FLOW_COOKIE))
    except auth.AuthError as exc:
        return jsonify({"error": "sign_in_failed", "message": str(exc)}), 401

    systems = auth.entitled_systems(cfg, user["groups"])
    admin = auth.is_admin(cfg, user["groups"])
    payload = {**user, "systems": systems, "admin": admin}
    token = auth.sign_session(payload, cfg.get("session_hours", 8))

    # Straight to the picker unless they were heading somewhere specific.
    dest = nxt if nxt and nxt not in ("/", "") else "/choose"
    resp = make_response(redirect(dest))
    _secure_cookie(resp, auth.SESSION_COOKIE, token, int(cfg.get("session_hours", 8) * 3600))
    resp.set_cookie(auth.FLOW_COOKIE, "", max_age=0, path="/")
    return resp


@app.get("/auth/me")
def auth_me():
    cfg = _cfg()
    if not cfg.get("enabled"):
        return jsonify({"authenticated": False, "authRequired": False,
                        "systems": [], "admin": False})
    sess = current_session()
    if not sess:
        return jsonify({"authenticated": False, "authRequired": True}), 401
    return jsonify({
        "authenticated": True, "authRequired": True,
        "email": sess.get("email", ""), "name": sess.get("name", ""),
        "groups": sess.get("groups", []),
        "systems": sess.get("systems", []),
        "admin": sess.get("admin", False),
    })


@app.get("/auth/verify")
def auth_verify():
    """What nginx asks on every request (auth_request). 200 = let it through.

    Accepts either identity. With Okta off the site is still gated - by the
    landing page asking who you are - because the alternative is a workspace
    that shows every system to anyone who opens the address.
    """
    cfg = _cfg()
    if cfg.get("_broken"):
        return "", 401
    who = current_user()
    if not who:
        return "", 401
    resp = make_response("", 200)
    resp.headers["X-Auth-User"] = who.get("id", "")
    return resp


@app.get("/auth/logout")
def auth_logout():
    cfg = _cfg()
    target = auth.logout_url(cfg) if cfg.get("enabled") else None
    resp = make_response(redirect(target or "/login"))
    resp.set_cookie(auth.SESSION_COOKIE, "", max_age=0, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Gate every /api route as well, not only at the edge. nginx auth_request is the
# front door, but the runner is directly reachable inside the compose network,
# and only the app knows which SOX systems a given person may work on.
OPEN_PATHS = ("/auth/", "/api/health", "/api/signin", "/api/signout",
              "/api/whoami", "/api/directory-names")


@app.before_request
def require_session():
    path = request.path
    if path.startswith(OPEN_PATHS) or not path.startswith("/api/"):
        return None
    cfg = _cfg()
    if cfg.get("_broken"):
        return jsonify({"error": "auth_misconfigured", "message": cfg["_broken"]}), 500
    if not current_user():
        return jsonify({
            "error": "not_signed_in",
            "message": ("Sign in with Okta to use this." if cfg.get("enabled")
                        else "Sign in at the landing page to use this."),
        }), 401
    return None


@app.get("/api/health")
def health():
    with _lock:
        busy = _active
    return jsonify({
        "ok": True,
        "display": os.environ.get("DISPLAY", ""),
        "sessionUrl": SESSION_URL,
        "activeJob": busy,
        "loggedInProfile": PROFILE_DIR.is_dir(),
    })


@app.get("/api/capabilities")
def capabilities():
    """What the page is allowed to run for real. The UI reads this on load."""
    return jsonify({
        "live": [{"system": sys_.title(), "controlId": cid.upper(),
                  "control": c["control"], "scope": c["scope"],
                  "produces": c["produces"]}
                 for (sys_, cid), c in CAPTURES.items()],
        "note": "Every other system and control is a prototype simulation.",
    })


def _flag_args(key, system="workato"):
    """The actual argv a flags key expands to. One place, so the builder and the
    contract check below cannot disagree about what gets passed."""
    return {
        "out": ["--out", "<out>"],
        "profile": ["--profile", "<profile>"],
        "no-pause": ["--no-pause"],
        "browser": ["--browser", "chromium"],
        "env": ["--env", system.lower()],
        "settle": ["--settle", "4.0"],
        "workspace": ["--workspace", "<ws>"],
        "month": ["--month", "<month>"],
        "period": ["--period", "<period>"],
        "project": ["--project", "all"],
        "area": ["--area", "all"],
        "config": _config_source(system),
    }[key]


def validate_flag_contracts():
    """Check every flag a capture will be sent is one its argparse accepts.

    argparse rejects the whole run on a single unknown flag, so a capture that
    drops an option breaks every job started from the page - which has happened
    three times here (--project, --env, then a missing recipe list). This checks
    the EXPANDED argv rather than the flags keys: an earlier version compared the
    keys themselves and reported "config" as unknown, which is a guard nobody
    would keep listening to.
    """
    import ast
    import importlib

    problems = []
    for (system, cid), cap in CAPTURES.items():
        try:
            mod = importlib.import_module(cap["module"])
            tree = ast.parse(Path(mod.__file__).read_text())
        except Exception as exc:                     # noqa: BLE001
            problems.append(f"{system}/{cid}: cannot inspect {cap['module']}: {exc}")
            continue
        accepted = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                for a in node.args:
                    if isinstance(a, ast.Constant) and str(a.value).startswith("--"):
                        accepted.add(str(a.value))
        sent = []
        for key in cap["flags"]:
            try:
                sent += _flag_args(key, system)
            except KeyError:
                problems.append(f"{system}/{cid}: flags lists {key!r}, which is not a known key")
        unknown = sorted({a for a in sent if a.startswith("--")} - accepted)
        if unknown:
            problems.append(f"{system}/{cid}: {cap['module']} does not accept {unknown}")
    return problems


def _config_source(system):
    """How this capture is told what to capture: CSV, sheet, or the built-in list.

    Read from the environment's own config (Doppler, or the YAML), so it is set
    in the same place as everything else about that system.
    """
    try:
        cfg = environments.find(system) or {}
    except environments.ConfigError:
        cfg = {}
    csv_path = str(cfg.get("config_csv") or os.environ.get("CAPTURE_CONFIG_CSV", "")).strip()
    if csv_path:
        return ["--config-csv", csv_path]
    sheet = str(cfg.get("config_sheet") or os.environ.get("CAPTURE_CONFIG_SHEET", "")).strip()
    if sheet:
        return ["--config-sheet", sheet]
    # Nothing configured. The built-in list keeps the run working rather than
    # dying on a missing flag; the capture logs loudly that it may be stale.
    return ["--use-builtin"]


def _projects():
    """Workbook tabs the Workato change-management capture knows about.

    Read defensively. That capture now takes its scope from a Google Sheet or a
    CSV rather than a hardcoded list, and dropped the helper this used to call -
    which turned /api/scope into a 500 and took the whole workspace page with it.
    An endpoint that only lists choices should degrade to listing none.
    """
    try:
        from workato import sox_capture
    except Exception:
        return []
    fn = getattr(sox_capture, "project_names", None)
    if callable(fn):
        try:
            return list(fn())
        except Exception:
            return []
    # Fall back to the built-in sections the script still ships as its default.
    try:
        return [s["tab"] for s in getattr(sox_capture, "SECTIONS", []) if s.get("tab")]
    except Exception:
        return []


def _workspaces(env_key="workato"):
    """Workspaces offered for an environment.

    From `workspaces:` in the config when it is listed, otherwise just the single
    `workspace:` value. Never invented: a workspace name that does not exist would
    fail the capture's own check after a sign-in, several minutes in.
    """
    try:
        cfg = environments.find(env_key) or {}
    except environments.ConfigError:
        cfg = {}
    listed = cfg.get("workspaces")
    if isinstance(listed, list) and listed:
        names = [str(w).strip() for w in listed if str(w).strip()]
    else:
        names = []
    default = str(cfg.get("workspace", "") or os.environ.get("CAPTURE_WORKSPACE", "Production"))
    if default and default not in names:
        names.insert(0, default)
    return names, default


@app.get("/api/scope")
def scope():
    """What the Prepare selectors offer: which workspace, and which projects."""
    env_key = request.args.get("system", "workato").lower()
    names, default = _workspaces(env_key)
    return jsonify({
        "workspaces": names,
        "defaultWorkspace": default,
        "projects": _projects(),
    })


@app.get("/api/environments")
def list_environments():
    """Configured environments, as the page may see them.

    environments.public() decides what is in each entry, and it cannot carry a
    password - only whether one resolved to something.
    """
    try:
        envs = environments.load()
    except environments.ConfigError as exc:
        return jsonify({"error": "config_error", "message": str(exc)}), 500
    return jsonify({
        # Which backend answered, so it is never a guess whether a run is using
        # Doppler or a file on disk.
        "source": environments.source(),
        "configPath": str(environments.CONFIG_PATH),
        "configured": bool(envs),
        "environments": [environments.public(c) for c in envs.values()],
    })


@app.post("/api/prepare")
def prepare():
    global _active
    body = request.get_json(silent=True) or {}
    system = str(body.get("system", "")).strip()
    control_id = str(body.get("controlId", "")).strip()
    period = str(body.get("period", "")).strip()
    month = str(body.get("month", "")).strip() or datetime.now().strftime("%B %Y")
    workspace = str(body.get("workspace", "")).strip()
    project = str(body.get("project", "")).strip() or "all"

    # Signing in is not the same as being allowed to work on THIS system. A
    # reviewer entitled to NetSuite must not be able to start a Workato capture
    # just by posting a different body than the picker offered them.
    # Signing in is not being allowed to work on THIS system. Checked here as
    # well as in the page, because a request can be made without the page.
    who = current_user()
    if not users.may_use(who, system):
        return jsonify({
            "error": "not_entitled",
            "message": f"You are not assigned to {system or 'this system'}. "
                       f"An administrator assigns systems under User Administration.",
            "systems": (who or {}).get("scopes", []),
        }), 403

    capture = CAPTURES.get((system.lower(), control_id.lower()))
    if capture is None:
        wired = ", ".join(f"{s.title()}/{c.upper()}" for s, c in CAPTURES)
        return jsonify({
            "error": "not_wired",
            "message": f"{system or 'This system'} / {control_id or 'this control'} "
                       f"has no capture behind it. Wired: {wired}.",
        }), 415

    # Reject an unknown project here rather than let the capture sign in first and
    # fail afterwards - and so a typo can never widen the scope by falling back to
    # capturing everything.
    if "project" in capture["scope"] and project.lower() not in ("all", "*"):
        known = {p.lower() for p in _projects()}
        if not all(p.strip().lower() in known for p in project.split(",") if p.strip()):
            return jsonify({
                "error": "unknown_project",
                "message": f"No project called {project!r}.",
                "projects": _projects(),
            }), 400

    if not workspace:
        workspace = _workspaces(system.lower())[1]

    with _lock:
        if _active:
            return jsonify({
                "error": "busy",
                "message": "A capture is already running. It drives the shared browser "
                           "session, so it has to finish first.",
                "activeJob": _active,
            }), 409
        job_id = uuid.uuid4().hex[:12]
        _active = job_id

    out = _job_dir(job_id) / "out"
    out.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id, "system": system, "controlId": control_id,
        "period": period, "month": month,
        "workspace": workspace, "project": project,
        "status": "running", "phase": "starting", "phaseDetail": "",
        "startedAt": _now(), "finishedAt": None, "exitCode": None, "error": None,
        "screenshots": 0, "warnings": [], "artifacts": [], "log": [], "pid": None,
        "users": None,
    }
    _jobs[job_id] = job

    # Build only the flags this capture actually accepts. argparse rejects the
    # whole run on one unknown flag, so a capture that drops an option silently
    # breaks every job started from the page - which is how --project, --env and
    # a missing --month all reached production at once. "flags" in CAPTURES is
    # the contract; anything not listed is never passed.
    available = {
        "out": ["--out", str(out)],
        # A profile per system: a shared one would mean two signed-in sessions in
        # the same browser, and a capture landing in the wrong system's tab.
        "profile": ["--profile", str(PROFILE_DIR if system.lower() == "workato"
                                     else DATA_DIR / f"{system.lower()}-profile")],
        "no-pause": ["--no-pause"],
        # The change-management capture refuses to run without a recipe list, and
        # exits before taking a single screenshot. It is the runner's job to
        # supply one: a job started from the page cannot pass a flag.
        #
        # A Google Sheet is the intended source but needs a Google sign-in the
        # first time, which nothing unattended can complete - so a CSV wins where
        # one is configured, and the built-in list is the floor. The run says
        # which it used either way.
        "config": _config_source(system),
        # The image ships Playwright's Chromium, not Google Chrome. A capture
        # that defaults to real Chrome (sensible on a desktop) dies here with
        # "Chromium distribution 'chrome' is not found", so the container states
        # which browser it actually has rather than relying on a default.
        "browser": ["--browser", os.environ.get("CAPTURE_BROWSER", "chromium")],
        "env": ["--env", system.lower()],
        "settle": ["--settle", os.environ.get("CAPTURE_SETTLE", "4.0")],
        "workspace": ["--workspace", workspace],
        "month": ["--month", month],
        "period": ["--period", period],
        "project": ["--project", project],
        "area": ["--area", str(body.get("area", "")).strip() or "all"],
    }
    cmd = ["python3", "-m", capture["module"]]
    for flag in capture["flags"]:
        cmd += available[flag]
    try:
        env_cfg = environments.find(system)
    except environments.ConfigError as exc:
        with _lock:
            _active = None
        return jsonify({"error": "config_error", "message": str(exc)}), 500
    job["log"].append(f"$ {' '.join(cmd)}")

    threading.Thread(target=_run, args=(job, cmd, environments.secrets(env_cfg)),
                     daemon=True).start()
    return jsonify(_public(job)), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_public(job))


@app.get("/api/jobs/<job_id>/log")
def job_log(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    return "\n".join(job["log"]), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/api/jobs/<job_id>/artifacts/<path:name>")
def job_artifact(job_id, name):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    # Resolve and confirm containment: never serve a path that climbs out of the run.
    out = (_job_dir(job_id) / "out").resolve()
    target = (out / name).resolve()
    if not target.is_file() or out not in target.parents:
        return jsonify({"error": "not_found"}), 404
    return send_file(target, as_attachment=True, download_name=target.name)


@app.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if job["status"] != "running":
        return jsonify(_public(job))
    job["status"] = "cancelled"
    job["error"] = "cancelled by the operator"
    if job["pid"]:
        try:
            os.kill(job["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return jsonify(_public(job))


# --------------------------------------------------------------------------- #
# SOC 1 assessment agent                                                       #
# --------------------------------------------------------------------------- #
#
# The routes soc1-agent-handoff's INTEGRATION.md asks the host to mount. The
# worker (soc1/worker.py) sends three uploads to the OpenAI account configured
# in Doppler and returns a draft in the uploaded template plus open items. A
# draft is a draft: the page carries it into the same human validation and
# management review as every other workpaper.
#
# Separate from CAPTURES on purpose. A capture drives the shared browser and
# runs one at a time with the others; this needs no browser, so it neither
# waits for a capture nor blocks one.

SOC1_DIR = DATA_DIR / "soc1-jobs"
SOC1_MAX_REQUEST = 85 * 1024 * 1024     # three base64 files of up to 20 MiB each
SOC1_ARTIFACTS = ("SOC1_Draft.xlsx", "Open_Items.json")
SOC1_PROVIDER = "OpenAI"        # soc1/worker.py talks to the OpenAI Responses API only
SOC1_PUBLIC = ("id", "status", "message", "artifacts", "open_items", "cleanup_pending",
               "provider",
               "assessment_id", "system", "vendor", "review_start", "review_end",
               "model", "created_at", "finished_at")

_soc1 = {}                      # job id -> record (the worker updates it in place)
_soc1_idem = {}                 # (owner, Idempotency-Key) -> job id
_soc1_lock = threading.Lock()
# One model run at a time: each one is a long Code Interpreter session over three
# documents, and spend is bounded by not running them side by side. Others wait
# as "queued".
_soc1_slot = threading.Semaphore(1)


def _soc1_status_code(exc):
    return 503 if "Configure" in str(exc) or "Set OPENAI" in str(exc) else 400


def _soc1_load(job_id):
    """A job from memory, or from disk after a restart. None when unknown."""
    if not re.fullmatch(r"[0-9a-f]{16}", job_id or ""):
        return None
    with _soc1_lock:
        if job_id in _soc1:
            return _soc1[job_id]
    host = SOC1_DIR / f"{job_id}.host.json"
    if not host.is_file():
        return None
    try:
        rec = json.loads(host.read_text())
        status = SOC1_DIR / job_id / "status.json"
        if status.is_file():
            rec.update(json.loads(status.read_text()))
        elif rec.get("status") in ("queued", "running"):
            # The process stopped mid-run, so it is not still running somewhere.
            rec.update(status="failed", message="The runner restarted during this "
                       "preparation. Prepare again; no draft was produced.")
    except (ValueError, OSError):
        return None
    with _soc1_lock:
        _soc1.setdefault(job_id, rec)
        return _soc1[job_id]


def _soc1_visible(rec, who):
    return bool(who) and (who.get("admin") or rec.get("owner") == who.get("id"))


def _soc1_public(rec):
    out = {k: rec[k] for k in SOC1_PUBLIC if k in rec}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _soc1_run(job_id, payload, rec):
    from soc1.worker import Soc1Agent

    with _soc1_slot:
        rec.update(status="running", message="Uploading the three files to the model")
        try:
            Soc1Agent(SOC1_DIR).run(job_id, payload, state=rec)
        except Exception as exc:                   # noqa: BLE001 - surfaced to the page
            rec.update(status="failed", message=str(exc)[:500])
        finally:
            rec["finished_at"] = _now()
            payload.clear()                        # the evidence is on disk; drop the copy
            try:
                (SOC1_DIR / f"{job_id}.host.json").write_text(json.dumps(
                    {k: v for k, v in rec.items() if k != "open_items"}, indent=2))
            except OSError:
                pass


@app.get("/api/soc1")
def api_soc1_config():
    """Whether the SOC 1 agent can run, and on which model - never the key."""
    from soc1 import worker

    key, model = bool(worker.api_key()), worker.default_model()
    reason = ("" if key and model else
              "OPENAI_SA_KEY is not set in Doppler" if not key else
              "OPENAI_MODEL_NAME is not set in Doppler")
    return jsonify({"available": key and bool(model), "model": model or "",
                    "provider": SOC1_PROVIDER,
                    "reason": reason, "maxFileBytes": 20 * 1024 * 1024})


@app.post("/api/soc1/jobs")
def api_soc1_start():
    from soc1 import worker

    if (request.content_length or 0) > SOC1_MAX_REQUEST:
        return jsonify({"error": "too_large",
                        "message": "The three files together are too large (85 MB limit)."}), 413
    who = current_user()
    idem = (request.headers.get("Idempotency-Key") or "").strip()[:200]
    if not idem:
        return jsonify({"error": "invalid", "message": "Idempotency-Key header is required."}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid", "message": "Send the request as JSON."}), 400
    system = str(payload.get("system", "")).strip()
    if not users.may_use(who, system):
        return jsonify({"error": "not_entitled",
                        "message": f"You are not assigned to {system or 'this system'}."}), 403

    with _soc1_lock:
        existing = _soc1_idem.get((who.get("id"), idem))
    if existing and _soc1_load(existing):
        # A repeated click is the same preparation, never a second draft.
        return _soc1_public(_soc1_load(existing)), 202

    model = worker.default_model()
    if not worker.api_key() or not model:
        return jsonify({"error": "not_configured",
                        "message": "The SOC 1 agent is not configured: set OPENAI_SA_KEY "
                                   "and OPENAI_MODEL_NAME in Doppler."}), 503
    try:
        context, _ = worker.validate_request(payload)
    except Exception as exc:                        # noqa: BLE001 - input problems
        msg = str(exc) if isinstance(exc, ValueError) else "The upload could not be read."
        return jsonify({"error": "invalid", "message": msg[:300]}), _soc1_status_code(exc)

    job_id = uuid.uuid4().hex[:16]
    rec = {"id": job_id, "status": "queued", "message": "Waiting for the agent",
           "owner": who.get("id"), "owner_name": who.get("name", ""),
           "assessment_id": str(payload.get("assessment_id", ""))[:200],
           "system": system, "vendor": str(context.get("vendor", ""))[:200],
           "review_start": context.get("review_start", ""),
           "review_end": context.get("review_end", ""),
           "model": model, "provider": SOC1_PROVIDER,
           "created_at": _now(), "finished_at": None}
    SOC1_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    (SOC1_DIR / f"{job_id}.host.json").write_text(json.dumps(rec, indent=2))
    with _soc1_lock:
        _soc1[job_id] = rec
        _soc1_idem[(who.get("id"), idem)] = job_id
    threading.Thread(target=_soc1_run, args=(job_id, payload, rec), daemon=True).start()
    return _soc1_public(rec), 202


@app.get("/api/soc1/jobs/<job_id>")
def api_soc1_status(job_id):
    rec = _soc1_load(job_id)
    if not rec or not _soc1_visible(rec, current_user()):
        return jsonify({"error": "not_found"}), 404
    return _soc1_public(rec)


@app.get("/api/soc1/jobs/<job_id>/artifacts/<name>")
def api_soc1_artifact(job_id, name):
    rec = _soc1_load(job_id)
    if not rec or not _soc1_visible(rec, current_user()):
        return jsonify({"error": "not_found"}), 404
    if name not in SOC1_ARTIFACTS or rec.get("status") != "draft_ready":
        return jsonify({"error": "not_found"}), 404
    path = SOC1_DIR / job_id / name
    if not path.is_file():
        return jsonify({"error": "not_found"}), 404
    vendor = re.sub(r"[^A-Za-z0-9._-]+", "_", rec.get("vendor") or "vendor")[:60]
    resp = send_file(path, as_attachment=True, download_name=f"{vendor}_{name}")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/session/reset")
def session_reset():
    """Forget the stored browser profile, forcing a fresh SSO login next run."""
    with _lock:
        if _active:
            return jsonify({"error": "busy", "message": "Stop the running capture first."}), 409
    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    return jsonify({"ok": True, "message": "Browser profile cleared; the next run will ask for SSO."})


if __name__ == "__main__":
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for problem in validate_flag_contracts():
        print(f"!! flag contract: {problem}", flush=True)
    from waitress import serve

    port = int(os.environ.get("PORT", "8000"))
    print(f"capture runner listening on :{port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8)
