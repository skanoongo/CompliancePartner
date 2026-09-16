#!/usr/bin/env python3
"""
Compliance Partner - capture runner API.

Turns the "Prepare" button in the Compliance Partner web app into a real run of
workato_sox_capture.py, and reports what that run is doing while it does it.

Only one control is wired to a real capture today:

    system "Workato" + control "CM-02" (Change management)

Everything else answers 415 with a reason, and the web app falls back to its
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

from flask import Flask, jsonify, request, send_file

import environments

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CAPTURE_DATA_DIR", "/data"))
RUNS_DIR = DATA_DIR / "runs"
PROFILE_DIR = DATA_DIR / "browser-profile"
SESSION_URL = os.environ.get("CAPTURE_SESSION_URL", "")
LOG_TAIL_LINES = 400

# (system, control id) pairs backed by a real capture.
SUPPORTED = {("workato", "cm-02")}

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
        "sessionUrl": SESSION_URL if job["phase"] == "awaiting_login" else "",
        "log": job["log"][-LOG_TAIL_LINES:],
    }


PHASE_RE = re.compile(r"^PHASE::([a-z_]+)::(.*)$")
CAPTURED_RE = re.compile(r"captured -> ")
WARN_RE = re.compile(r"^\s*!!")


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
            env={**os.environ, "CAPTURE_SESSION_URL": SESSION_URL},
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
                    job["warnings"].append(line.strip())
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
        "live": [{"system": "Workato", "controlId": "CM-02",
                  "control": "Change management",
                  "produces": ["Excel workbook", "screenshot manifest", "screenshots"]}],
        "note": "Every other system and control is a prototype simulation.",
    })


def _projects():
    """Projects the capture script knows about - one per workbook tab."""
    import workato_sox_capture

    return workato_sox_capture.project_names()


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

    # Reject an unknown project here rather than let the capture sign in first and
    # fail afterwards - and so a typo can never widen the scope by falling back to
    # capturing everything.
    known = {p.lower() for p in _projects()}
    if project.lower() not in ("all", "*") and not all(
            p.strip().lower() in known for p in project.split(",") if p.strip()):
        return jsonify({
            "error": "unknown_project",
            "message": f"No project called {project!r}.",
            "projects": _projects(),
        }), 400

    if not workspace:
        workspace = _workspaces(system.lower())[1]

    if (system.lower(), control_id.lower()) not in SUPPORTED:
        return jsonify({
            "error": "not_wired",
            "message": f"{system or 'This system'} / {control_id or 'this control'} "
                       f"has no capture behind it. Only Workato / CM-02 does.",
        }), 415

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
    }
    _jobs[job_id] = job

    cmd = [
        "python3", str(APP_ROOT / "workato_sox_capture.py"),
        "--month", month,
        "--out", str(out),
        "--profile", str(PROFILE_DIR),
        "--no-pause",
        "--env", system.lower(),
        "--workspace", workspace,
        "--project", project,
        "--settle", os.environ.get("CAPTURE_SETTLE", "4.0"),
    ]
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
