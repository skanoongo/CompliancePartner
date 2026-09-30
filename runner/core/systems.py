#!/usr/bin/env python3
"""
The SOX system inventory: which systems exist and can be assigned to people.

WHY IT IS A STORE AND NOT A CONSTANT
------------------------------------
The list was written in three places - a sidebar array in the page, a longer
inventory beside it, and a Python constant - and adding a system meant editing
code in two languages and rebuilding an image. It is reference data an
administrator maintains, so it lives with the user directory on the runner's
volume and is served from one place.

REMOVING A SYSTEM
-----------------
Removal cascades into the directory: a scope naming a system that no longer
exists is an assignment nobody can see and nobody can revoke. And a system that
a capture is wired to is refused outright - the control would still be offered
while the system it reads had stopped being assignable, so nobody could run it
and the reason would be invisible.
"""

import json
import os
import re
import threading
from pathlib import Path

STORE = Path(os.environ.get("SYSTEMS_STORE", "/data/systems.json"))

# Seeded on first run. The inventory the page shipped with.
SEED = [
    "1Password", "Argo", "Billing TSDB", "CoStar", "Coupa", "Data Lake", "Doppler",
    "Equity Edge", "FloQast", "GitHub", "JPMorgan", "Kyriba", "Linux (OS)",
    "NetSuite", "Okta", "Orderful", "Salesforce", "Snowflake", "Vanta",
    "Wiz", "Workato", "Workday", "Zip", "Zuora",
]

# A name goes into URLs, JSON and a shell argument, so keep it conservative.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()._/+-]{0,48}$")

_lock = threading.Lock()


class SystemError_(Exception):
    pass


def _read():
    if not STORE.is_file():
        return None
    try:
        data = json.loads(STORE.read_text())
    except (ValueError, OSError):
        return None
    names = data.get("systems") if isinstance(data, dict) else data
    return names if isinstance(names, list) else None


def _write(names):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"systems": names}, indent=2))
    tmp.replace(STORE)          # atomic: a crash must not leave an empty inventory


def all_systems():
    """The inventory, sorted, seeding it the first time."""
    with _lock:
        names = _read()
        if names is None:
            names = list(SEED)
            try:
                _write(names)
            except OSError:
                pass            # read-only volume: serve the seed without persisting
        return sorted({str(n).strip() for n in names if str(n).strip()},
                      key=str.lower)


def exists(name):
    want = str(name).strip().lower()
    return any(s.lower() == want for s in all_systems())


def add(name):
    """Add one system. Returns the new inventory."""
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise SystemError_(
            "a system name starts with a letter or digit and may contain letters, "
            "digits, spaces and ( ) . _ / + -")
    with _lock:
        names = _read()
        if names is None:
            names = list(SEED)
        if any(str(n).strip().lower() == name.lower() for n in names):
            raise SystemError_(f"{name!r} is already in the inventory")
        names.append(name)
        _write(names)
    return all_systems()


def remove(name, wired=(), on_removed=None):
    """Remove one system.

    `wired` names systems a capture is attached to; those are refused. A capture
    whose system is not assignable is a control that is offered and cannot be
    run, with nothing on screen explaining why.

    `on_removed` is called with the removed name so the caller can clean up
    references - a scope pointing at a system that no longer exists is an
    assignment nobody can see and nobody can revoke.
    """
    name = str(name or "").strip()
    want = name.lower()
    if any(str(w).strip().lower() == want for w in wired):
        raise SystemError_(
            f"{name!r} has a capture wired to it; removing it would leave that "
            f"control offered but impossible to run")
    with _lock:
        names = _read()
        if names is None:
            names = list(SEED)
        rest = [n for n in names if str(n).strip().lower() != want]
        if len(rest) == len(names):
            raise SystemError_(f"{name!r} is not in the inventory")
        if not rest:
            raise SystemError_("the inventory cannot be emptied")
        _write(rest)
    if on_removed:
        try:
            on_removed(name)
        except Exception:       # noqa: BLE001 - the removal itself already stands
            pass
    return all_systems()
