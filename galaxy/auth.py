"""Access modes and device pairing (Phase 9).

GALAXY_AUTH selects how a browser or a phone gets in:

- paired (default). At boot the server prints a one-time pairing code (eight digits, valid for ten minutes, five tries).
  The first device that enters it gets a session: a user token for the owner, kept in an HttpOnly cookie that page scripts
  cannot read. The session expires after 30 days and can be rotated or revoked. Admin routes still need the operator token,
  which is written to the server log and never shown on a page.
- local. No credentials: every request acts as the owner. Use it on one machine only. The server refuses to start on any
  address other than 127.0.0.1, and the launcher opens no public tunnel in this mode, because a tunnel forwards remote
  requests to 127.0.0.1 and they would look local.

The pairing code is stored only as a hash, works once, expires, and is replaced whenever a new one is issued.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable, Optional

MODES = ("paired", "local")
COOKIE_NAME = "galaxy_session"
PAIR_TTL_S = 600
PAIR_MAX_TRIES = 5
PUBLIC_POST_PATHS = frozenset({"/api/pair"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def mode_from_env(value: Optional[str]) -> str:
    chosen = (value or "paired").strip().lower()
    if chosen not in MODES:
        raise ValueError(f"GALAXY_AUTH must be one of {MODES}; got {value!r}")
    return chosen


def check_startup(mode: str, host: str) -> None:
    if mode == "local" and host not in LOOPBACK_HOSTS:
        raise RuntimeError(f"GALAXY_AUTH=local is only allowed on 127.0.0.1; HOST is {host!r}")


def tunnel_allowed(mode: str) -> bool:
    return mode != "local"


def is_https(request) -> bool:
    """True when the browser reached us over https, directly or through a proxy (Hugging Face Spaces, tunnels)."""
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return forwarded == "https"


def bypasses_guard(method: str, path: str) -> bool:
    """Only the pairing route is reachable without a session; it is protected by the code's single use and tries."""
    return method.upper() == "POST" and path in PUBLIC_POST_PATHS


def session_cookie(value: str, max_age_s: int, secure: bool) -> str:
    parts = [f"{COOKIE_NAME}={value}", "Path=/", f"Max-Age={int(max_age_s)}", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def cookie_value(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value
    return None


class PairingBook:
    """One pairing code at a time. Issuing a new code replaces the old one."""

    def __init__(self, clock: Callable[[], float] = time.time, ttl_s: float = PAIR_TTL_S,
                 max_tries: int = PAIR_MAX_TRIES) -> None:
        self._clock = clock
        self._ttl = ttl_s
        self._max_tries = max_tries
        self._active: Optional[dict] = None

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    def issue(self) -> str:
        code = f"{secrets.randbelow(10 ** 8):08d}"
        self._active = {"digest": self._digest(code), "expires": self._clock() + self._ttl, "tries": 0}
        return code

    def redeem(self, code: str) -> bool:
        active = self._active
        if active is None:
            return False
        if self._clock() > active["expires"]:
            self._active = None
            return False
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        active["tries"] += 1
        if active["tries"] > self._max_tries:
            self._active = None          # too many wrong tries: the code is burned
            return False
        if digits and hmac.compare_digest(self._digest(digits), active["digest"]):
            self._active = None          # single use
            return True
        return False

    def pending(self) -> bool:
        return self._active is not None and self._clock() <= self._active["expires"]
