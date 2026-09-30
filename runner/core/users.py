#!/usr/bin/env python3
"""
The user directory: who may sign in, and which SOX systems each of them sees.

WHY THIS IS SERVER-SIDE
-----------------------
The page already had roles, module gating and per-user system scopes, and got
them right - but it held them in a JavaScript array. Two consequences made that
unusable as access control rather than as a demo:

  1. An administrator's assignment vanished on reload and was never seen by the
     person it was granted to. Assigning access nobody else can observe is not
     assigning access.
  2. Identity was a dropdown. Anyone could pick "Admin" and the page believed
     them, because nothing had ever told the page who was asking.

So the directory lives here, in one JSON file on the runner's volume, and the
page is told who it is talking to.

WHAT THIS IS AND IS NOT
-----------------------
This decides what a signed-in person may SEE and START. It is authorization, not
proof of identity. In local mode a username is taken at face value - fine on a
trusted machine, and the reason the landing page says so plainly. With Okta
configured, identity comes from Okta and this file only maps that person to
systems. The distinction matters: turning this into the login for a shared
deployment would be relying on a name typed into a box.
"""

import json
import os
import re
import threading
from pathlib import Path

from core import systems

STORE = Path(os.environ.get("USERS_STORE", "/data/users.json"))

# Role -> the modules of the application that role may open. "admin" is the
# administration module, and only Admin has it: an administrator is the only
# person who can change who sees what.
ROLE_MODULES = {
    "Admin": ["prepare", "monitor", "audit", "admin"],
    "Internal Audit User": ["audit"],
    "Control Preparer": ["prepare"],
    "Compliance Monitor": ["monitor"],
}
ROLES = list(ROLE_MODULES)

# The inventory is administrator-maintained reference data; core.systems owns it.
# Re-exported so existing callers keep working.


def ALL_SYSTEMS():  # noqa: N802 - kept callable-compatible with the old constant
    return systems.all_systems()

# Seeded on first run so an empty directory never locks everyone out. These match
# the names the page shipped with, so nothing appears to change on upgrade.
SEED = [
    # Bootstrap administrator. A fresh volume has to contain someone who can
    # reach User Administration, or the directory is unmanageable from the UI.
    {"id": "admin", "name": "admin", "role": "Admin", "scopes": []},
    {"id": "minh", "name": "Minh Nguyen", "role": "Admin", "scopes": []},
    {"id": "saloni", "name": "Saloni Palkar", "role": "Internal Audit User", "scopes": []},
    {"id": "spandana", "name": "Spandana Bolla", "role": "Control Preparer",
     "scopes": ["Workato", "Orderful"]},
    {"id": "arjun", "name": "Arjun Satish", "role": "Compliance Monitor", "scopes": []},
]

_lock = threading.Lock()
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class UserError(Exception):
    pass


def _read():
    if not STORE.is_file():
        return None
    try:
        data = json.loads(STORE.read_text())
    except (ValueError, OSError):
        return None
    users = data.get("users") if isinstance(data, dict) else data
    return users if isinstance(users, list) else None


def _write(users):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": users}, indent=2))
    tmp.replace(STORE)          # atomic: a crash mid-write must not empty the directory


def all_users():
    """The directory, seeding it the first time."""
    with _lock:
        users = _read()
        if users is None:
            users = [dict(u) for u in SEED]
            try:
                _write(users)
            except OSError:
                pass            # read-only volume: serve the seed without persisting
        return [_shape(u) for u in users]


def _shape(u):
    role = u.get("role") if u.get("role") in ROLE_MODULES else "Control Preparer"
    scopes = u.get("scopes") or []
    is_admin = role == "Admin"
    return {
        "id": str(u.get("id", "")).lower(),
        "name": u.get("name") or str(u.get("id", "")),
        "role": role,
        # An Admin is scoped to everything by definition, and to the inventory as
        # it stands NOW - a list frozen when the account was made goes stale the
        # moment a system is added, and then the page shows an administrator
        # fewer systems than exist.
        "scopes": systems.all_systems() if is_admin else list(scopes),
        "modules": ROLE_MODULES[role][:],
        "admin": is_admin,
    }


def find(who):
    """By id, or by name, case-insensitively. Returns None if unknown."""
    want = str(who or "").strip().lower()
    if not want:
        return None
    users = all_users()
    for u in users:
        if u["id"] == want:
            return u
    for u in users:
        if u["name"].strip().lower() == want:
            return u
    return None


def upsert(entry):
    """Add or replace one user. Returns the stored shape."""
    uid = str(entry.get("id", "")).strip().lower()
    if not ID_RE.match(uid):
        raise UserError("id must be lowercase letters, digits, dot, dash or underscore")
    name = str(entry.get("name", "")).strip() or uid
    role = entry.get("role")
    if role not in ROLE_MODULES:
        raise UserError(f"role must be one of: {', '.join(ROLES)}")
    scopes = entry.get("scopes") or []
    if not isinstance(scopes, list):
        raise UserError("scopes must be a list of system names")

    with _lock:
        users = _read()
        if users is None:
            users = [dict(u) for u in SEED]

        # Names must be unique, because in local mode the NAME is what someone
        # signs in with. Two rows called "admin" under different ids would make
        # which one you become arbitrary - and if their roles differ, so is what
        # you may do. Dedup by id alone let that through.
        clash = [u for u in users
                 if str(u.get("name", "")).strip().lower() == name.strip().lower()
                 and str(u.get("id", "")).lower() != uid]
        if clash:
            raise UserError(
                f"the name {name!r} is already used by user id "
                f"{clash[0].get('id')!r}; names are how people sign in, so they "
                f"have to be unique")

        # Never remove the last administrator. Demoting the only Admin would leave
        # the directory with nobody able to grant access back.
        admins = [u for u in users if u.get("role") == "Admin"]
        if role != "Admin" and len(admins) == 1 and admins[0].get("id") == uid:
            raise UserError("this is the only Admin; promote someone else first")

        row = {"id": uid, "name": name, "role": role,
               "scopes": [str(s) for s in scopes]}
        for i, u in enumerate(users):
            if str(u.get("id", "")).lower() == uid:
                users[i] = row
                break
        else:
            users.append(row)
        _write(users)
    return _shape(row)


def remove(uid):
    uid = str(uid or "").strip().lower()
    with _lock:
        users = _read() or [dict(u) for u in SEED]
        admins = [u for u in users if u.get("role") == "Admin"]
        if len(admins) == 1 and str(admins[0].get("id", "")).lower() == uid:
            raise UserError("this is the only Admin; the directory would be unmanageable")
        rest = [u for u in users if str(u.get("id", "")).lower() != uid]
        if len(rest) == len(users):
            raise UserError("no such user")
        _write(rest)
    return True


def may_use(user, system):
    """Whether this person may work on `system`. Admin sees everything."""
    if not user:
        return False
    if user.get("admin"):
        return True
    want = str(system).strip().lower()
    return any(str(s).strip().lower() == want for s in user.get("scopes", []))


def drop_scope(system):
    """Remove one system from every user's scopes. Called when it leaves the
    inventory: a scope naming a system that no longer exists is an assignment
    nobody can see and nobody can revoke."""
    want = str(system).strip().lower()
    changed = 0
    with _lock:
        rows = _read()
        if rows is None:
            return 0
        for u in rows:
            before = u.get("scopes") or []
            after = [s for s in before if str(s).strip().lower() != want]
            if len(after) != len(before):
                u["scopes"] = after
                changed += 1
        if changed:
            _write(rows)
    return changed
