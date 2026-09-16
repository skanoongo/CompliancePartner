#!/usr/bin/env python3
"""
Loads config/environments.yaml - one section per system offered in the page.

Two rules shape everything here:

  1. A password read from this file must never come back out anywhere except the
     login form. `public()` is the only view the API and the page ever see, and it
     cannot carry one. `redact()` scrubs any that reach a log line.
  2. A value written as ${VAR} is read from the process environment instead of the
     file, so the real secret can live somewhere that is not a file on disk.
"""

import os
import re
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get(
    "CAPTURE_CONFIG", "/config/environments.yaml"))

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_KEYS = ("password", "secret", "token", "passkey", "api_key")

LOGIN_MODES = ("form", "interactive")


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


def load(path=None):
    """Return {key: environment dict}. Missing file is not an error - it just
    means no environment is configured and every login is interactive."""
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
        cfg = _expand_tree(cfg)
        mode = str(cfg.get("login", "interactive")).lower()
        if mode not in LOGIN_MODES:
            raise ConfigError(
                f"{p}: environment '{key}' has login: {mode!r}; "
                f"expected one of {', '.join(LOGIN_MODES)}")
        cfg["login"] = mode
        cfg.setdefault("name", str(key).title())
        cfg["enabled"] = bool(cfg.get("enabled", False))
        cfg["key"] = str(key).lower()
        out[cfg["key"]] = cfg
    return out


def find(name, path=None):
    """Look an environment up by section key or by display name, case-insensitively."""
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
    """Every secret value in a config, for redaction."""
    if not cfg:
        return []
    return [str(v) for k, v in cfg.items()
            if any(s in k.lower() for s in SECRET_KEYS) and v]


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
        loaded = load(sys.argv[1] if len(sys.argv) > 1 else None)
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    print(json.dumps([public(c) for c in loaded.values()], indent=2))
