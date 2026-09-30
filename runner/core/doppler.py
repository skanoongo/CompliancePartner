#!/usr/bin/env python3
"""
Doppler as the source of secrets.

WHY A MODULE AND NOT JUST `doppler run`
---------------------------------------
Wrapping the process in the Doppler CLI would put every secret into the
environment of the capture, its browser, and every child process - and this code
already screenshots what the browser shows and writes job logs to a web page.
Fetching them here keeps them in one process's memory, lets `redact()` know
exactly which strings must never appear in output, and means a missing secret is
reported by name instead of surfacing as an empty string three layers down.

TOKEN
-----
DOPPLER_TOKEN, a Doppler service token (dp.st....), scoped to one project and
config - so the token itself decides which environment is in use and there is
nothing else to get wrong. Never in a file that git can see.

FAILURE
-------
Hard. If Doppler is the configured source and cannot be read, this raises rather
than falling back to whatever is on disk: a silent fall back to a stale local
file is how a run ends up authenticating with a credential that was rotated
precisely because it should no longer be used.
"""

import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.doppler.com/v3/configs/config/secrets/download?format=json"
TIMEOUT = 20
CACHE_SECONDS = 300

_cache = {"at": 0.0, "secrets": None, "token": None}


class DopplerError(Exception):
    pass


def token():
    return (os.environ.get("DOPPLER_TOKEN") or "").strip()


def configured():
    """True when a Doppler token is present, which is what selects this backend."""
    return bool(token())


def fetch(force=False):
    """Every secret in the token's project/config, as a dict.

    Cached briefly: a capture asks for credentials several times per run and
    Doppler rate-limits. `force` re-reads, for a rotation mid-session.
    """
    tok = token()
    if not tok:
        raise DopplerError(
            "DOPPLER_TOKEN is not set. Put the service token in .env "
            "(gitignored) as DOPPLER_TOKEN=dp.st...., or export it.")

    fresh = (not force
             and _cache["secrets"] is not None
             and _cache["token"] == tok
             and _cache["at"] > time.time() - CACHE_SECONDS)
    if fresh:
        return _cache["secrets"]

    req = urllib.request.Request(API, method="GET")
    # A service token goes in as HTTP basic with an empty password.
    import base64

    basic = base64.b64encode(f"{tok}:".encode()).decode()
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            data = json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        hint = ""
        if exc.code in (401, 403):
            hint = (" - the token is wrong, revoked, or scoped to a different "
                    "project/config")
        raise DopplerError(f"Doppler returned HTTP {exc.code}{hint}: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DopplerError(f"could not reach Doppler: {exc}") from exc
    except ValueError as exc:
        raise DopplerError(f"Doppler returned something that is not JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise DopplerError("Doppler returned an unexpected shape")

    _cache.update(at=time.time(), secrets=data, token=tok)
    return data


def describe():
    """Non-secret facts about the source, safe to show in an API response."""
    if not configured():
        return {"configured": False}
    try:
        s = fetch()
    except DopplerError as exc:
        return {"configured": True, "reachable": False, "error": str(exc)}
    return {
        "configured": True,
        "reachable": True,
        "project": s.get("DOPPLER_PROJECT", ""),
        "config": s.get("DOPPLER_CONFIG", ""),
        "environment": s.get("DOPPLER_ENVIRONMENT", ""),
        "secretCount": len(s),
    }


# What makes a Doppler entry a SECRET rather than configuration. Doppler holds
# both: passwords sit next to base URLs and workspace names.
SECRET_NAME_PARTS = ("password", "secret", "token", "passkey", "api_key",
                     "private_key", "credential")


def is_secret_name(name):
    n = str(name).lower()
    return any(part in n for part in SECRET_NAME_PARTS)


def all_values():
    """Secret VALUES only, for redaction.

    Deliberately not "every value in the project". Doppler also holds base URLs,
    workspace names and account ids, and masking those turned a job log into
    "Logged in. Current URL: ********" - which removes the single most important
    fact in a SOX evidence trail to protect something that was never secret.
    """
    try:
        s = fetch()
    except DopplerError:
        return []
    return [str(v) for k, v in s.items() if is_secret_name(k) and v]
