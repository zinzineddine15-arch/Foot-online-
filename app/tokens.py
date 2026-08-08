"""HMAC-signed, short-lived tokens and signed URLs.

Three flavours, all HMAC-SHA256 over a server secret that never leaves disk:

* session token  - issued at /api/login, carried as `?token=` on playlists.
* path signature - `?exp=..&sig=..` attached to segment and key URLs.
  The signature is recomputed *every time a playlist is rendered*, so the
  URLs embedded in a playlist expire within seconds even if the playlist
  itself leaks.

All comparisons are constant time. Tokens carry their own expiry; there is
no server-side session store, so revocation = wait out the TTL (keep TTLs
short) or rotate the server secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from .settings import STATE_ROOT

_SECRET_PATH = STATE_ROOT / "server_secret.bin"


def _load_secret() -> bytes:
    try:
        data = _SECRET_PATH.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    _SECRET_PATH.write_bytes(data)
    os.chmod(_SECRET_PATH, 0o600)
    return data


SERVER_SECRET: bytes = _load_secret()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _mac(msg: bytes) -> bytes:
    return hmac.new(SERVER_SECRET, msg, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# Session tokens (JSON payload)
# ---------------------------------------------------------------------------
def issue_token(scope: str, ttl: int, **claims) -> str:
    payload = {"scope": scope, "iat": int(time.time()), "exp": int(time.time()) + ttl}
    payload.update(claims)
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_b64e(_mac(body.encode()))}"


def verify_token(token: str, scope: str) -> Optional[dict]:
    try:
        body, mac = token.split(".", 1)
        expected = _mac(body.encode())
        if not hmac.compare_digest(expected, _b64d(mac)):
            return None
        payload = json.loads(_b64d(body))
        if payload.get("scope") != scope:
            return None
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Signed URLs: sig = HMAC(exp | path)
# ---------------------------------------------------------------------------
def sign_path(path: str, ttl: int) -> dict:
    exp = int(time.time()) + ttl
    msg = f"{exp}|{path}".encode()
    sig = _b64e(_mac(msg))[:43]
    return {"exp": exp, "sig": sig}


def sign_path_quantized(path: str, ttl: int) -> dict:
    """Signature whose expiry snaps to the end of the current ttl-window.

    Every call inside the same window returns the *identical* (exp, sig),
    which keeps embedded URLs byte-stable across playlist re-renders. Used
    for key URLs: players cache keys by URI, so a stable URI prevents
    redundant key fetches while still bounding exposure to <= ttl.
    """
    exp = (int(time.time()) // ttl + 1) * ttl
    msg = f"{exp}|{path}".encode()
    sig = _b64e(_mac(msg))[:43]
    return {"exp": exp, "sig": sig}


def verify_path(path: str, exp, sig: str) -> bool:
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < time.time():
        return False
    if exp_i > time.time() + 3600:  # reject absurdly future expiries
        return False
    msg = f"{exp_i}|{path}".encode()
    expected = _b64e(_mac(msg))[:43]
    return hmac.compare_digest(expected, str(sig))
