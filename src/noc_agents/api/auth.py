"""Minimal auth / RBAC seam for the NOC API (spec §7.0.5).

Three things live here and nothing else:

1. **Roles** — the nine NOC roles the spec enumerates, as a ``Literal`` plus a
   runtime tuple so a typo in a route's allow-list fails at import, not in prod.
2. **``require_role(*allowed)``** — a FastAPI dependency. It reads a *signed*
   session cookie (HMAC-SHA256 over the claims, keyed by ``NOC_SESSION_SECRET``,
   using the **stdlib** ``hmac`` module — no new dependency, no JWT library).
   When ``AUTH_DISABLED`` is true (the demo default) it falls back to the
   existing role switcher (``/api/v1/session``) and **never rejects**: the whole
   demo, and the whole test suite, runs under that flag, so every gate added
   here must be completely inert until someone sets ``AUTH_DISABLED=false``.
3. **``authorise_socket(ws, *allowed)``** — the same decision for a WebSocket
   handshake, which no HTTP dependency can make: it closes with 1008 before
   ``accept()`` instead of raising, and is inert under ``AUTH_DISABLED=true``.
4. **The per-client role switcher store** — what used to be one global
   ``_SESSIONS["default"]`` dict in ``main.py``, keyed per client instead, so
   two browsers pointed at the same demo do not overwrite each other's role.

Full identity-provider integration is out of scope (spec §13, D15). This module
is the seam that provider plugs into: replace :func:`current_principal` and
everything above it keeps working.

Environment (all read at *call* time, never frozen at import, so tests and the
demo can flip them without a reload):

``AUTH_DISABLED``      ``true``/``false`` — default ``true`` (demo).
``NOC_ENV``            ``demo``/``production`` — default ``demo``.
``NOC_SESSION_SECRET`` HMAC key for the session cookie; required once
                       ``AUTH_DISABLED=false``.
``CORS_ORIGINS``       comma-separated browser origins; default the Vite dev
                       server ``http://localhost:5173``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Literal, get_args
from uuid import uuid4

from fastapi import HTTPException, Request, Response, WebSocket
from starlette.requests import HTTPConnection

log = logging.getLogger("noc_agents.auth")

# --------------------------------------------------------------------------
# Roles (spec §7.0.5)
# --------------------------------------------------------------------------

Role = Literal[
    "noc_analyst",
    "shift_supervisor",
    "duty_manager",
    "management",
    "msp_coordinator",
    "field_engineer",
    "planning",
    "legal",
    "admin",
]

#: The same nine roles at runtime, for validation. Kept derived from ``Role`` so
#: the two can never drift.
ROLES: tuple[str, ...] = tuple(get_args(Role))

DEFAULT_ROLE: str = "noc_analyst"

#: Cookie holding the HMAC-signed session (set by a future login route).
SESSION_COOKIE = "noc_session"
#: Cookie identifying one demo browser, so the role switcher is per-client.
CLIENT_COOKIE = "noc_client"

#: Scope key used to carry a freshly minted client id through the request that
#: mints it (the cookie only reaches us on the *next* request).
_SCOPE_CLIENT_KEY = "noc_client_key"


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def auth_disabled() -> bool:
    """True when the API runs with no authentication at all (demo default)."""
    return _env_bool("AUTH_DISABLED", True)


def noc_env() -> str:
    """``demo`` (default) or ``production``."""
    return (os.getenv("NOC_ENV") or "demo").strip().lower() or "demo"


def is_production() -> bool:
    return noc_env() == "production"


def session_secret() -> str:
    return (os.getenv("NOC_SESSION_SECRET") or "").strip()


def cors_origins() -> list[str]:
    """Allowed browser origins — ``CORS_ORIGINS``, default the Vite dev server.

    Never ``["*"]`` by default: the API answers with ``allow_credentials=True``
    and a wildcard there is both a browser error and a CSRF invitation.
    """
    raw = os.getenv("CORS_ORIGINS")
    if raw is None or not raw.strip():
        return ["http://localhost:5173"]
    return [o.strip() for o in raw.split(",") if o.strip()]


# --------------------------------------------------------------------------
# Signed session cookie (stdlib hmac — no new dependency)
# --------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def sign_session(claims: dict[str, Any], secret: str | None = None, ttl_seconds: int | None = None) -> str:
    """``<base64url(claims)>.<base64url(hmac-sha256)>``.

    Used by whatever issues sessions (a login route, or a test). ``ttl_seconds``
    stamps an ``exp`` claim that :func:`read_session` enforces.
    """
    key = secret if secret is not None else session_secret()
    if not key:
        raise RuntimeError("NOC_SESSION_SECRET is not set; cannot sign a session")
    body = dict(claims)
    if ttl_seconds is not None:
        body["exp"] = int(time.time()) + int(ttl_seconds)
    payload_b64 = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, key)}"


def read_session(token: str | None, secret: str | None = None) -> dict[str, Any] | None:
    """Verified claims, or ``None`` for anything not provably ours and unexpired."""
    key = secret if secret is not None else session_secret()
    if not token or not key or "." not in token:
        return None
    payload_b64, _, sig = token.partition(".")
    if not payload_b64 or not sig:
        return None
    if not hmac.compare_digest(_sign(payload_b64, key), sig):  # constant time
        return None
    try:
        claims = json.loads(_b64d(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if exp is not None:
        try:
            if float(exp) < time.time():
                return None
        except (TypeError, ValueError):
            return None
    return claims


# --------------------------------------------------------------------------
# Per-client role switcher store (was one global dict in main.py)
# --------------------------------------------------------------------------


def _default_session() -> dict[str, Any]:
    return {
        "display_name": "NOC Analyst",
        "role": DEFAULT_ROLE,
        "region_filter": None,
        "msp_filter": None,
    }


#: Demo-scale, in-process, oldest-evicted. Not durable and not meant to be:
#: it is a UI role switcher, not an identity store.
_MAX_SESSIONS = 512
_SESSIONS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def reset_sessions() -> None:
    """Drop every stored role-switcher session (tests, and process restarts)."""
    _SESSIONS.clear()


def client_key(request: Request) -> str:
    """A stable-per-client key: signed session > client cookie > address+agent.

    The last fallback keeps the demo working for a browser that sends no cookies
    (cross-origin dev without ``credentials: "include"``) — it is a convenience
    key for a role switcher, never an authentication decision.
    """
    minted = request.scope.get(_SCOPE_CLIENT_KEY)
    if minted:
        return str(minted)
    claims = read_session(request.cookies.get(SESSION_COOKIE))
    if claims and claims.get("sub"):
        return f"u:{claims['sub']}"
    cid = request.cookies.get(CLIENT_COOKIE)
    if cid:
        return f"c:{cid[:64]}"
    host = request.client.host if request.client else "-"
    agent = request.headers.get("user-agent", "-")
    return "a:" + hashlib.sha256(f"{host}|{agent}".encode("utf-8")).hexdigest()[:16]


def bind_client(request: Request, response: Response) -> str:
    """This client's key, minting the ``noc_client`` cookie when there isn't one."""
    if request.scope.get(_SCOPE_CLIENT_KEY):
        return str(request.scope[_SCOPE_CLIENT_KEY])
    if request.cookies.get(SESSION_COOKIE) or request.cookies.get(CLIENT_COOKIE):
        return client_key(request)
    cid = uuid4().hex
    key = f"c:{cid}"
    request.scope[_SCOPE_CLIENT_KEY] = key
    response.set_cookie(
        CLIENT_COOKIE,
        cid,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return key


def get_client_session(request: Request) -> dict[str, Any]:
    """This client's role-switcher session (seeded with the demo defaults)."""
    key = client_key(request)
    stored = _SESSIONS.get(key)
    if stored is None:
        return _default_session()
    _SESSIONS.move_to_end(key)
    return dict(stored)


def set_client_session(request: Request, data: dict[str, Any], response: Response | None = None) -> dict[str, Any]:
    """Store this client's role-switcher session and return it."""
    key = bind_client(request, response) if response is not None else client_key(request)
    _SESSIONS[key] = dict(data)
    _SESSIONS.move_to_end(key)
    while len(_SESSIONS) > _MAX_SESSIONS:
        _SESSIONS.popitem(last=False)  # evict the least recently used
    return dict(_SESSIONS[key])


# --------------------------------------------------------------------------
# Principal + the dependency
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who the request is acting as. ``authenticated`` is False for the demo."""

    role: str
    display_name: str
    authenticated: bool
    source: str  # "cookie" | "role_switcher"
    subject: str | None = None


def _role_switcher_principal(request: HTTPConnection) -> Principal:
    data = get_client_session(request)
    role = str(data.get("role") or DEFAULT_ROLE)
    return Principal(
        role=role,
        display_name=str(data.get("display_name") or "NOC Analyst"),
        authenticated=False,
        source="role_switcher",
    )


def current_principal(request: HTTPConnection) -> Principal | None:
    """The caller, or ``None`` when auth is on and the cookie is missing/bad.

    With ``AUTH_DISABLED=true`` this is always a principal (the role switcher).
    This is the seam a real identity provider replaces (spec §13, D15).

    Takes an ``HTTPConnection``, which is the parent of both ``Request`` and
    ``WebSocket``: it needs the cookies, the headers and the scope, and a socket
    handshake carries all three (:func:`authorise_socket` is the socket's caller).
    """
    if auth_disabled():
        return _role_switcher_principal(request)
    claims = read_session(request.cookies.get(SESSION_COOKIE))
    if not claims:
        return None
    role = str(claims.get("role") or "")
    if role not in ROLES:
        return None
    return Principal(
        role=role,
        display_name=str(claims.get("name") or role),
        authenticated=True,
        source="cookie",
        subject=str(claims["sub"]) if claims.get("sub") else None,
    )


def require_role(*allowed: Role) -> Callable[[Request], Principal]:
    """FastAPI dependency allowing only ``allowed`` roles.

    * ``AUTH_DISABLED=true`` (demo default): returns the role-switcher principal
      and **never** raises — every gate is inert.
    * ``AUTH_DISABLED=false``: 503 when no ``NOC_SESSION_SECRET`` is configured,
      401 without a valid signed cookie, 403 when the role is not in ``allowed``.

    ``admin`` gets no implicit bypass: list it explicitly where it belongs, so
    the allow-list on each route is the whole truth about that route.
    """
    unknown = [r for r in allowed if r not in ROLES]
    if unknown:  # a typo fails at import, not at 3am
        raise ValueError(f"unknown role(s) {unknown}; expected any of {list(ROLES)}")
    allowed_set = frozenset(allowed) if allowed else frozenset(ROLES)

    def dependency(request: Request) -> Principal:
        if auth_disabled():
            return _role_switcher_principal(request)
        if not session_secret():
            raise HTTPException(503, "auth is enabled but NOC_SESSION_SECRET is not configured")
        principal = current_principal(request)
        if principal is None:
            raise HTTPException(401, "authentication required")
        if principal.role not in allowed_set:
            raise HTTPException(403, f"role '{principal.role}' may not use this route")
        return principal

    dependency.__name__ = "require_role_" + ("_".join(allowed) if allowed else "any")
    return dependency


# --------------------------------------------------------------------------
# WebSocket handshakes (spec §7.0.5; docs/CONFORMANCE.md A-14)
# --------------------------------------------------------------------------

#: RFC 6455 policy violation. The close code for "you may not have this stream":
#: 1008 is the protocol's 403, and a browser surfaces it on the failed handshake.
SOCKET_POLICY_VIOLATION = 1008


async def authorise_socket(ws: WebSocket, *allowed: Role) -> Principal | None:
    """Authorise a socket handshake, or close it and return ``None``.

    ``require_role`` cannot do this job: it is an HTTP dependency whose ``Request``
    parameter FastAPI never binds on a WebSocket connection (declaring it on a socket
    route kills the handshake with a TypeError, auth on or off), and a socket's answer to
    "no" is a close frame rather than a status code. So this is the socket's own seam,
    reading exactly the same signed session as the HTTP one:

    * ``AUTH_DISABLED=true`` (the demo default) -- the role-switcher principal, never
      refused, so the demo and the whole suite are unchanged;
    * ``AUTH_DISABLED=false`` -- a valid signed cookie whose role is in ``allowed``, or
      the handshake is closed with 1008 and this returns ``None``.

    **Closed BEFORE ``accept()``**, which is the point: a refusal has to be a rejected
    handshake, not an accepted socket that is dropped a moment later, or the replay the
    caller asked for has already been written to a caller who was never authorised.
    The caller therefore returns immediately on ``None`` and accepts nothing.

    A missing ``NOC_SESSION_SECRET`` is refused too. The HTTP seam answers 503 there --
    "the server is misconfigured", not "you are not allowed" -- but a handshake has no
    such code, and serving the ops feed to everyone because the operator forgot a secret
    is the one outcome worth ruling out. The reason string says which it was.
    """
    unknown = [r for r in allowed if r not in ROLES]
    if unknown:  # a typo fails at import, not at 3am
        raise ValueError(f"unknown role(s) {unknown}; expected any of {list(ROLES)}")
    if auth_disabled():
        return _role_switcher_principal(ws)
    if not session_secret():
        await ws.close(
            code=SOCKET_POLICY_VIOLATION,
            reason="auth is enabled but NOC_SESSION_SECRET is not configured",
        )
        return None
    principal = current_principal(ws)
    if principal is None:
        await ws.close(code=SOCKET_POLICY_VIOLATION, reason="authentication required")
        return None
    allowed_set = frozenset(allowed) if allowed else frozenset(ROLES)
    if principal.role not in allowed_set:
        await ws.close(
            code=SOCKET_POLICY_VIOLATION,
            reason=f"role '{principal.role}' may not read this stream",
        )
        return None
    return principal


# --------------------------------------------------------------------------
# Production guard (spec §7.0.5)
# --------------------------------------------------------------------------


def production_guard_active() -> bool:
    """True when sensitive routes must NOT be registered at all.

    ``AUTH_DISABLED=true`` with ``NOC_ENV=production`` means an unauthenticated
    internet-facing API: the routes that export operational or personal data are
    then refused registration rather than served to anyone who finds the URL.
    """
    return auth_disabled() and is_production()


def log_production_guard(routes: tuple[str, ...] | list[str]) -> None:
    """Exactly one line saying what was refused and why."""
    log.warning(
        "PRODUCTION GUARD: refusing to register %d sensitive route(s) [%s] because "
        "AUTH_DISABLED=true and NOC_ENV=production — they export operational/personal "
        "data and there is no authentication; set AUTH_DISABLED=false to serve them.",
        len(routes),
        ", ".join(routes),
    )
