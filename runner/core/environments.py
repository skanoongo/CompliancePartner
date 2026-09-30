#!/usr/bin/env python3
"""
Per-system credentials and config.

SOURCE
------
Doppler, when DOPPLER_TOKEN is set - and then Doppler ONLY. The local
config/environments.yaml is not read at all in that case, deliberately: a
fallback that quietly reaches for a file on disk is how a run ends up
authenticating with a credential that was rotated precisely because it should
not be used any more. Without a token the YAML is used, so a checkout with no
Doppler access still works.

Doppler holds one secret per field, named

    ENVIRONMENTS_<SYSTEM>_<FIELD>

for example ENVIRONMENTS_WORKATO_PASSWORD, ENVIRONMENTS_NETSUITE_ACCOUNT_ID.
The naming is the schema: a new system needs no code change, only secrets.

RULES THAT DO NOT CHANGE WITH THE SOURCE
----------------------------------------
  1. A password read from here never comes back out anywhere except the login
     form. `public()` is the only view the API and the page see, and it cannot
     carry one. `redact()` scrubs any that reach a log line - and with Doppler it
     scrubs EVERY secret in the project, not only the ones this config used.
  2. In the YAML path a value written as ${VAR} is read from the process
     environment, so a secret can stay off disk even without Doppler.
"""

import os
import re
from pathlib import Path

import yaml

from core import doppler

CONFIG_PATH = Path(os.environ.get(
    "CAPTURE_CONFIG", "/config/environments.yaml"))

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_KEYS = ("password", "secret", "token", "passkey", "api_key")

LOGIN_MODES = ("form", "interactive")

# Doppler field suffix -> the key the rest of the code reads. Anything not listed
# is carried through lowercased, so a field added in Doppler arrives without a
# code change.
BOOL_FIELDS = {"enabled"}
LIST_FIELDS = {"workspaces"}
PREFIX = "ENVIRONMENTS_"


class ConfigError(Exception):
    pass


def _expand(value):
    """Replace ${VAR} with the environment's value. Unset -> empty string."""
    if not isinstance(value, str):
        return value
    return VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _expand_tree(node):
    if isinstance(node, dict):
        return {k: _expand_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_tree(v) for v in node]
    return _expand(node)


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _normalise(key, cfg):
    """Shared shaping, whichever source the fields came from."""
    mode = str(cfg.get("login", "interactive")).lower()
    if mode not in LOGIN_MODES:
        raise ConfigError(
            f"environment '{key}' has login: {mode!r}; "
            f"expected one of {', '.join(LOGIN_MODES)}")
    cfg["login"] = mode
    cfg.setdefault("name", str(key).title())
    cfg["enabled"] = cfg["enabled"] if isinstance(cfg.get("enabled"), bool) \
        else _truthy(cfg.get("enabled", False))
    cfg["key"] = str(key).lower()
    return cfg


def _from_doppler():
    """Build the environments from ENVIRONMENTS_<SYSTEM>_<FIELD> secrets."""
    try:
        secrets = doppler.fetch()
    except doppler.DopplerError as exc:
        # Hard failure on purpose - see the module docstring.
        raise ConfigError(f"Doppler is the configured source but unreadable: {exc}") from exc

    out = {}
    for name, value in secrets.items():
        if not name.startswith(PREFIX):
            continue
        rest = name[len(PREFIX):]
        # SYSTEM is the first token; everything after it is the field. Systems
        # here are single words, and the field may contain underscores
        # (ACCOUNT_ID, PRIVATE_KEY_PATH).
        system, _, field = rest.partition("_")
        if not system or not field:
            continue
        key = system.lower()
        cfg = out.setdefault(key, {})
        f = field.lower()
        if f in BOOL_FIELDS:
            cfg[f] = _truthy(value)
        elif f in LIST_FIELDS:
            cfg[f] = [p.strip() for p in str(value).split(",") if p.strip()]
        else:
            cfg[f] = value

    return {k: _normalise(k, v) for k, v in out.items()}


def _from_file(path):
    p = Path(path or CONFIG_PATH)
    if not p.is_file():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML: {exc}") from exc

    envs = raw.get("environments")
    if envs is None:
        raise ConfigError(f"{p} has no top-level 'environments:' section")
    if not isinstance(envs, dict):
        raise ConfigError(f"{p}: 'environments' must be a mapping of sections")

    out = {}
    for key, cfg in envs.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"{p}: environment '{key}' must be a mapping")
        out[str(key).lower()] = _normalise(key, _expand_tree(cfg))
    return out


def source():
    """Which backend is in use, and why. Non-secret; safe to return from the API."""
    if doppler.configured():
        return {"backend": "doppler", **doppler.describe()}
    return {"backend": "file", "path": str(CONFIG_PATH),
            "present": CONFIG_PATH.is_file()}


def load(path=None):
    """Return {key: environment dict}.

    Doppler when a token is set, and then Doppler only. Otherwise the YAML file;
    a missing file is not an error, it just means nothing is configured and every
    login is interactive.
    """
    if doppler.configured():
        return _from_doppler()
    return _from_file(path)


def find(name, path=None):
    """Look an environment up by key or by display name, case-insensitively."""
    envs = load(path)
    want = str(name).strip().lower()
    if want in envs:
        return envs[want]
    for cfg in envs.values():
        if str(cfg.get("name", "")).strip().lower() == want:
            return cfg
    return None


def public(cfg):
    """The view the API and the page may see. Cannot carry a secret."""
    if not cfg:
        return None
    return {
        "key": cfg.get("key", ""),
        "name": cfg.get("name", ""),
        "enabled": cfg.get("enabled", False),
        "baseUrl": cfg.get("base_url", ""),
        "login": cfg.get("login", "interactive"),
        "username": cfg.get("username", ""),
        "workspace": cfg.get("workspace", ""),
        # Whether a password resolved to anything - never the password.
        "hasPassword": bool(cfg.get("password")),
    }


def secrets(cfg):
    """Every secret value to keep out of output.

    With Doppler this returns every secret in the project, not only the fields
    this environment used. Redaction should not depend on having correctly
    guessed which secrets a run would touch.
    """
    values = []
    if cfg:
        values += [str(v) for k, v in cfg.items()
                   if any(s in k.lower() for s in SECRET_KEYS) and v]
    if doppler.configured():
        values += doppler.all_values()
    # Longest first, so a value that contains another is masked whole.
    return sorted({v for v in values if v}, key=len, reverse=True)


def redact(text, values):
    """Replace known secrets with a marker. Cheap insurance for log lines that
    were never meant to carry one."""
    out = str(text)
    for v in values:
        if v and len(str(v)) >= 4:
            out = out.replace(str(v), "********")
    return out


if __name__ == "__main__":
    import json
    import sys

    try:
        print(json.dumps({"source": source(),
                          "environments": [public(c) for c in load().values()]},
                         indent=2))
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
