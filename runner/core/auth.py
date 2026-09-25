#!/usr/bin/env python3
"""
Compliance Partner - Okta sign-in and SOX system entitlements.

WHY THIS EXISTS
---------------
The runner holds a browser that is signed in to Workato, and exposes it over
/session. Without a login in front, anyone who can reach the site has an
interactive, authenticated Workato window - not a screenshot of one. That is a
larger risk than the evidence itself, and it is what this module closes.

HOW IT WORKS
------------
OIDC Authorization Code flow with PKCE against Okta:

  /auth/login     -> redirect to Okta, with state + PKCE verifier in a short cookie
  /auth/callback  -> exchange the code, verify the ID token against Okta's JWKS,
                     then issue our own signed session cookie
  /auth/verify    -> what nginx asks on every request (auth_request)
  /auth/me        -> who is signed in and which SOX systems they may work on
  /auth/logout    -> drop the session, and optionally end the Okta session too

The ID token is verified properly - signature against the published JWKS, plus
issuer, audience and expiry. An unverified token is just a base64 string anyone
can write, so trusting its claims would make the login decorative.

ENTITLEMENTS
------------
Which SOX systems a person may work on comes from their Okta groups, mapped in
config/auth.yaml. The mapping is deny-by-default: a group that is not mapped
grants nothing, and a user in no mapped group can sign in but reaches an empty
system picker rather than everything.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
import yaml
from jwt import PyJWKClient

CONFIG_PATH = Path(os.environ.get("AUTH_CONFIG", "/config/auth.yaml"))

# Where the signing key for our own session cookie is kept, so sessions survive a
# restart. Generated once if absent.
SECRET_PATH = Path(os.environ.get("AUTH_SECRET_PATH", "/data/session-secret"))

SESSION_COOKIE = "cp_session"
FLOW_COOKIE = "cp_oidc_flow"
DEFAULT_SESSION_HOURS = 8
HTTP_TIMEOUT = 15


class AuthError(Exception):
    pass


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- #
# configuration                                                                #
# --------------------------------------------------------------------------- #
VAR_RE = __import__("re").compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    if isinstance(value, str):
        return VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path=None):
    """Read config/auth.yaml. A missing file means auth is OFF - see enabled()."""
    p = Path(path or CONFIG_PATH)
    if not p.is_file():
        return {"enabled": False, "_reason": f"no config file at {p}"}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML: {exc}") from exc

    cfg = _expand(raw.get("auth") or {})
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg.setdefault("session_hours", DEFAULT_SESSION_HOURS)
    cfg.setdefault("scopes", ["openid", "profile", "email", "groups"])
    cfg.setdefault("groups_claim", "groups")
    cfg.setdefault("entitlements", {})
    cfg.setdefault("admin_groups", [])

    if cfg["enabled"]:
        missing = [k for k in ("issuer", "client_id", "redirect_uri") if not cfg.get(k)]
        if missing:
            raise ConfigError(
                f"{p}: auth is enabled but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set")
    return cfg


def enabled(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("enabled"))


# --------------------------------------------------------------------------- #
# session cookie signing                                                       #
# --------------------------------------------------------------------------- #
def _secret():
    """Stable signing key, so a restart does not sign everyone out."""
    try:
        if SECRET_PATH.is_file():
            data = SECRET_PATH.read_bytes().strip()
            if len(data) >= 32:
                return data
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_bytes(48)
        SECRET_PATH.write_bytes(data)
        try:
            SECRET_PATH.chmod(0o600)
        except OSError:
            pass
        return data
    except OSError:
        # Read-only /data: fall back to a per-process key. Sessions then end at
        # restart, which is inconvenient but never insecure.
        global _EPHEMERAL
        try:
            return _EPHEMERAL
        except NameError:
            _EPHEMERAL = secrets.token_bytes(48)
            return _EPHEMERAL


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_decode(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_session(payload, hours):
    """Our own compact signed token. HMAC, not a JWT - nothing else consumes it."""
    body = dict(payload)
    body["exp"] = int(time.time()) + int(hours * 3600)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    mac = hashlib.blake2b(raw, key=_secret(), digest_size=32).digest()
    return f"{_b64u(raw)}.{_b64u(mac)}"


def read_session(token):
    """Return the payload, or None if the token is missing, altered or expired."""
    if not token or "." not in token:
        return None
    body_b64, mac_b64 = token.rsplit(".", 1)
    try:
        raw = _b64u_decode(body_b64)
        mac = _b64u_decode(mac_b64)
    except (ValueError, TypeError):
        return None
    expected = hashlib.blake2b(raw, key=_secret(), digest_size=32).digest()
    if not secrets.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


# --------------------------------------------------------------------------- #
# Okta discovery                                                               #
# --------------------------------------------------------------------------- #
_discovery_cache = {}


def discover(issuer):
    """Fetch and cache Okta's OIDC metadata."""
    issuer = issuer.rstrip("/")
    hit = _discovery_cache.get(issuer)
    if hit and hit["fetched"] > time.time() - 3600:
        return hit["doc"]
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as res:
            doc = json.loads(res.read().decode())
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise AuthError(f"could not read Okta metadata from {url}: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if key not in doc:
            raise AuthError(f"Okta metadata at {url} has no {key}")
    _discovery_cache[issuer] = {"doc": doc, "fetched": time.time()}
    return doc


# --------------------------------------------------------------------------- #
# the flow                                                                     #
# --------------------------------------------------------------------------- #
def begin_login(cfg, next_url="/"):
    """Return (okta_url, flow_state) - flow_state goes in a short-lived cookie."""
    doc = discover(cfg["issuer"])
    verifier = _b64u(secrets.token_bytes(64))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    state = _b64u(secrets.token_bytes(24))
    nonce = _b64u(secrets.token_bytes(24))

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": " ".join(cfg["scopes"]),
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{doc['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
    flow = sign_session({"state": state, "nonce": nonce, "verifier": verifier,
                         "next": next_url if next_url.startswith("/") else "/"},
                        hours=0.25)
    return url, flow


def _post_form(url, fields, auth=None):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    if auth:
        basic = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise AuthError(f"Okta returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise AuthError(f"could not reach Okta: {exc}") from exc


def complete_login(cfg, code, state, flow_cookie):
    """Exchange the code and verify the ID token. Returns the session payload."""
    flow = read_session(flow_cookie)
    if not flow:
        raise AuthError("the sign-in attempt expired or the cookie was lost - try again")
    if not secrets.compare_digest(str(flow.get("state", "")), str(state or "")):
        raise AuthError("state mismatch - the sign-in did not come from this site")

    doc = discover(cfg["issuer"])
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
        "client_id": cfg["client_id"],
        "code_verifier": flow["verifier"],
    }
    auth = None
    if cfg.get("client_secret"):
        # Confidential client: Okta wants the secret, not client_id in the body.
        fields.pop("client_id")
        auth = (cfg["client_id"], cfg["client_secret"])

    tokens = _post_form(doc["token_endpoint"], fields, auth=auth)
    id_token = tokens.get("id_token")
    if not id_token:
        raise AuthError("Okta did not return an ID token")

    # Verify properly. An unverified token is attacker-writable text.
    try:
        signing_key = PyJWKClient(doc["jwks_uri"]).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=cfg["client_id"],
            issuer=cfg["issuer"].rstrip("/"),
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception as exc:                      # noqa: BLE001 - any failure is fatal
        raise AuthError(f"the ID token did not verify: {type(exc).__name__}: {exc}") from exc

    if claims.get("nonce") and claims["nonce"] != flow.get("nonce"):
        raise AuthError("nonce mismatch - possible replay of an old sign-in")

    groups = claims.get(cfg["groups_claim"]) or []
    if isinstance(groups, str):
        groups = [groups]

    return {
        "sub": claims.get("sub", ""),
        "email": claims.get("email") or claims.get("preferred_username", ""),
        "name": claims.get("name", ""),
        "groups": list(groups),
    }, flow.get("next", "/")


def logout_url(cfg, id_token_hint=None):
    try:
        doc = discover(cfg["issuer"])
    except AuthError:
        return None
    endpoint = doc.get("end_session_endpoint")
    if not endpoint or not cfg.get("post_logout_redirect_uri"):
        return None
    params = {"post_logout_redirect_uri": cfg["post_logout_redirect_uri"]}
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------- #
# entitlements                                                                 #
# --------------------------------------------------------------------------- #
def entitled_systems(cfg, groups):
    """SOX systems this person may work on, from their Okta groups.

    Deny by default: an unmapped group grants nothing, and no mapped group means
    an empty list. A blank list is a real answer - the picker shows "no access"
    rather than falling open to everything.
    """
    mapping = cfg.get("entitlements") or {}
    have = {str(g).strip().lower() for g in groups}
    systems = []
    for group, granted in mapping.items():
        if str(group).strip().lower() not in have:
            continue
        if isinstance(granted, str):
            granted = [granted]
        for s in granted or []:
            if s not in systems:
                systems.append(s)
    return systems


def is_admin(cfg, groups):
    admin = {str(g).strip().lower() for g in (cfg.get("admin_groups") or [])}
    return bool(admin & {str(g).strip().lower() for g in groups})


def may_use(cfg, session, system):
    """True if this session is allowed to work on `system`."""
    if not session:
        return False
    if is_admin(cfg, session.get("groups", [])):
        return True
    allowed = session.get("systems")
    if allowed is None:
        allowed = entitled_systems(cfg, session.get("groups", []))
    return any(str(s).strip().lower() == str(system).strip().lower() for s in allowed)
