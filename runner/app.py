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

from core import auth, environments

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
        "scope": ["workspace", "project"],
        "produces": ["Excel workbook", "screenshot manifest", "screenshots"],
    },
    ("workato", "ua-04"): {
        "module": "workato.uar_capture",
        "control": "User access review",
        "scope": ["workspace", "period"],
        "produces": ["Excel workbook", "users.csv", "screenshot manifest", "screenshots"],
    },
    ("netsuite", "ua-04"): {
        "module": "netsuite.uar_capture",
        "control": "User access review",
        "scope": ["period"],
        "produces": ["Excel workbook", "users.csv", "screenshot manifest", "screenshots"],
    },
    ("netsuite", "cm-02"): {
        "module": "netsuite.sox_capture",
        "control": "Change management",
        "scope": ["period", "area"],
        "produces": ["Excel workbook", "changes.csv", "screenshot manifest", "screenshots"],
    },
}

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


@app.get("/auth/config")
def auth_config():
    """Whether sign-in is on. The login page reads this; it exposes no secret."""
    cfg = _cfg()
    return jsonify({
        "enabled": bool(cfg.get("enabled")),
        "configured": not cfg.get("_broken"),
        "error": cfg.get("_broken", ""),
        "issuer": cfg.get("issuer", "") if cfg.get("enabled") else "",
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
    """What nginx asks on every request (auth_request). 200 = let it through."""
    cfg = _cfg()
    if not cfg.get("enabled"):
        return "", 200
    if cfg.get("_broken"):
        return "", 401
    sess = current_session()
    if not sess:
        return "", 401
    resp = make_response("", 200)
    resp.headers["X-Auth-User"] = sess.get("email", "")
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
OPEN_PATHS = ("/auth/", "/api/health")


@app.before_request
def require_session():
    path = request.path
    if path.startswith(OPEN_PATHS) or not path.startswith("/api/"):
        return None
    cfg = _cfg()
    if not cfg.get("enabled"):
        return None
    if cfg.get("_broken"):
        return jsonify({"error": "auth_misconfigured", "message": cfg["_broken"]}), 500
    if not current_session():
        return jsonify({"error": "not_signed_in",
                        "message": "Sign in with Okta to use this."}), 401
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


def _projects():
    """Projects the capture script knows about - one per workbook tab."""
    from workato import sox_capture

    return sox_capture.project_names()


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
    cfg = _cfg()
    if cfg.get("enabled"):
        sess = current_session()
        if not auth.may_use(cfg, sess, system):
            return jsonify({
                "error": "not_entitled",
                "message": f"You are not entitled to work on {system or 'this system'}. "
                           f"Ask a SOX administrator to add the Okta group that grants it.",
                "systems": (sess or {}).get("systems", []),
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

    cmd = [
        "python3", "-m", capture["module"],
        "--out", str(out),
        # A profile per system: a shared one would mean two signed-in sessions in
        # the same browser, and a capture landing in the wrong system's tab.
        "--profile", str(PROFILE_DIR if system.lower() == "workato"
                         else DATA_DIR / f"{system.lower()}-profile"),
        "--no-pause",
        "--env", system.lower(),
        "--settle", os.environ.get("CAPTURE_SETTLE", "4.0"),
    ]
    # A workspace is a Workato concept. NetSuite is scoped by account instead,
    # which the capture reads from config, so passing it here would be a lie.
    if "workspace" in capture["scope"]:
        cmd += ["--workspace", workspace]
    # Each capture takes the scope that means something to it. The change-management
    # run is monthly and per-project; the access review is a point-in-time listing
    # labelled with the review period.
    if "project" in capture["scope"]:
        cmd += ["--month", month, "--project", project]
    if "period" in capture["scope"]:
        cmd += ["--period", period]
    if "area" in capture["scope"]:
        cmd += ["--area", str(body.get("area", "")).strip() or "all"]
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
    from waitress import serve

    port = int(os.environ.get("PORT", "8000"))
    print(f"capture runner listening on :{port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8)
