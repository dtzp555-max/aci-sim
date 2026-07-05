"""Token store + auth routes for aaaLogin, aaaRefresh, aaaLogout.

Session tokens are minted on aaaLogin and validated (via `require_session`) by
every protected data route in app.py. Real APIC semantics emulated here:
  - aaaLogin checks credentials (default admin/cisco, overridable via
    SIM_USERNAME/SIM_PASSWORD env vars) → 401 APIC envelope on mismatch. The
    login name may be plain ("admin") or domain-qualified
    ("apic:local\\admin", "local\\admin") — real APIC accepts both for its
    local login domain; see `_bare_username()`.
  - Tokens expire after SESSION_LIFETIME_SECONDS (default 600s); aaaRefresh
    requires a live token and renews it to a full lifetime.
  - Queries/writes without a valid, unexpired APIC-cookie → 403 APIC envelope
    "Token was invalid (Error: Token timeout)".
  - Malformed aaaLogin bodies never leak FastAPI's {"detail": ...} shape —
    the route parses the request body manually and returns a 400 APIC
    envelope instead of letting FastAPI raise RequestValidationError.

PR-9 addition — certificate signature auth (accept-mode). cisco.aci's
module_utils/aci.py supports password-less auth: when `private_key` is set,
every request's `cert_auth()` sets a single `Cookie` header carrying four
APIC-Certificate-* fields instead of (or alongside) the aaaLogin flow —
verified read-only against upstream `plugins/module_utils/aci.py`:

    sig_dn = "uni/userext/user-{username}/usercert-{certificate_name}"
    self.headers["Cookie"] = (
        "APIC-Certificate-Algorithm=v1.0; "
        "APIC-Certificate-DN={sig_dn}; "
        "APIC-Certificate-Fingerprint=fingerprint; "
        "APIC-Request-Signature={base64(RSA-SHA256(method+path+body))}"
    )

This means a cert-auth playbook never calls aaaLogin at all — every single
request (GET/POST/DELETE) carries the Cookie header standalone. See
`require_session` below for the accept-mode implementation and
docs/DESIGN.md's "Certificate accept-mode trust model" note for what is
and is not verified.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

# Default lab credentials — overridable per CONTRACT.md §1 / README §Configuration.
SIM_USERNAME: str = os.environ.get("SIM_USERNAME", "admin")
SIM_PASSWORD: str = os.environ.get("SIM_PASSWORD", "cisco")

# Real APIC default session lifetime is 600s (refreshTimeoutSeconds).
SESSION_LIFETIME_SECONDS: float = 600.0

# Certificate-DN shape cisco.aci's cert_auth() builds:
#   uni/userext/user-{username}/usercert-{certificate_name}
# Captured group 1 = the username, checked against SIM_USERNAME below.
_CERT_DN_RE = re.compile(r"^uni/userext/user-([^/]+)/usercert-[^/]+$")

# PR-15 addition — domain-qualified aaaLogin name. Real APIC accepts login
# names of the shape "apic:<loginDomain>\<username>" (e.g. "apic:local\admin")
# in addition to a bare username, per APIC's local/remote login-domain
# handling. aci-py's client posts the domain-qualified form by default.
# Captured group 1 = the domain (unused/ignored, any domain is accepted —
# this sim has no concept of configured login domains), group 2 = the bare
# username, which is what gets compared against SIM_USERNAME below.
_DOMAIN_QUALIFIED_NAME_RE = re.compile(r"^(?:apic:)?[^\\]+\\(.+)$")


def _bare_username(name: str) -> str:
    """Strip an optional `apic:<domain>\\` or `<domain>\\` prefix from a login name.

    "apic:local\\admin" -> "admin"; "local\\admin" -> "admin"; "admin" -> "admin"
    (no prefix present, returned unchanged).
    """
    m = _DOMAIN_QUALIFIED_NAME_RE.match(name)
    return m.group(1) if m else name

# SIM_CERT_STRICT — documented placeholder, NOT YET IMPLEMENTED (see
# docs/DESIGN.md). When unset/false (the only mode this sim supports today),
# a well-formed cert-cookie set authenticates its claimed user WITHOUT any
# RSA signature verification (accept-mode / trust-the-claimed-identity). A
# future strict mode would verify APIC-Request-Signature against a
# registered public key for that user — out of scope for PR-9.
SIM_CERT_STRICT: bool = os.environ.get("SIM_CERT_STRICT", "false").lower() in ("1", "true", "yes")


@dataclass
class _Session:
    user: str
    expires_at: float


# Module-level session store: token -> _Session. Shared across all APIC apps
# in this process (mirrors the pre-existing module-level behavior); each test
# builds a fresh FastAPI app/state per fixture but the token namespace is
# process-wide, which is fine since tokens are unguessable 128-hex-char values.
_sessions: dict[str, _Session] = {}


def _new_token() -> str:
    return secrets.token_hex(64)


def _now() -> float:
    return time.monotonic()


def _prune_expired() -> None:
    """Opportunistically drop expired tokens. Called on every store access."""
    now = _now()
    expired = [tok for tok, sess in _sessions.items() if sess.expires_at <= now]
    for tok in expired:
        del _sessions[tok]


def _create_session(user: str) -> str:
    _prune_expired()
    token = _new_token()
    _sessions[token] = _Session(user=user, expires_at=_now() + SESSION_LIFETIME_SECONDS)
    return token


def _lookup_session(token: str | None) -> _Session | None:
    """Return the live session for `token`, or None if missing/unknown/expired."""
    _prune_expired()
    if not token:
        return None
    return _sessions.get(token)


def _touch_session(token: str) -> bool:
    """Renew an existing session's expiry to a full lifetime. False if invalid."""
    sess = _lookup_session(token)
    if sess is None:
        return False
    sess.expires_at = _now() + SESSION_LIFETIME_SECONDS
    return True


def _pop_session(token: str | None) -> None:
    _prune_expired()
    if token:
        _sessions.pop(token, None)


def _cert_auth_user(request: Request) -> str | None:
    """Return the authenticated username for a well-formed cert-cookie request.

    Accept-mode (documented trust model, see docs/DESIGN.md): if all four
    APIC-Certificate-* cookies are present and APIC-Certificate-DN parses to
    "uni/userext/user-{user}/usercert-{name}" with user == SIM_USERNAME, the
    request is treated as authenticated WITHOUT verifying
    APIC-Request-Signature's RSA-SHA256 bytes. Any of: a missing cookie, an
    unparsable DN, or a DN whose user != SIM_USERNAME → returns None (caller
    falls through to the normal cookie-session check, which will also fail
    for a request that never called aaaLogin, yielding the standard 403).

    This intentionally does NOT check SIM_CERT_STRICT — that flag is a
    documented placeholder for a future signature-verifying mode and has no
    effect yet (see auth.py module docstring + DESIGN.md).
    """
    cookies = request.cookies
    required = (
        "APIC-Certificate-Algorithm",
        "APIC-Certificate-Fingerprint",
        "APIC-Certificate-DN",
        "APIC-Request-Signature",
    )
    if not all(cookies.get(name) for name in required):
        return None

    cert_dn = cookies["APIC-Certificate-DN"]
    m = _CERT_DN_RE.match(cert_dn)
    if not m:
        return None

    user = m.group(1)
    if user != SIM_USERNAME:
        return None

    return user


def require_session(request: Request) -> _Session | None:
    """Dependency-style helper: return the caller's live session or None.

    Callers (app.py routes) turn a None into a 403 APIC envelope. Kept as a
    plain function (not a FastAPI Depends) so routes can return the 403 body
    in the exact APIC envelope shape rather than a generic exception page.

    PR-9: also accepts certificate-signature auth (accept-mode) — a request
    carrying a well-formed, matching-user cert-cookie set authenticates
    without ever having called aaaLogin. A synthetic, non-expiring _Session
    is returned for that request only (not stored — cheap to recompute per
    request, and there is no real token to leak/reuse).
    """
    token = request.cookies.get("APIC-cookie")
    session = _lookup_session(token)
    if session is not None:
        return session

    cert_user = _cert_auth_user(request)
    if cert_user is not None:
        return _Session(user=cert_user, expires_at=_now() + SESSION_LIFETIME_SECONDS)

    return None


def _apic_error(text: str, code: str, status_code: int) -> JSONResponse:
    body = {
        "imdata": [{"error": {"attributes": {"text": text, "code": code}}}],
        "totalCount": "1",
    }
    return JSONResponse(status_code=status_code, content=body)


def _auth_error_403() -> JSONResponse:
    return _apic_error("Token was invalid (Error: Token timeout)", code="403", status_code=403)


def _login_response(token: str) -> dict:
    return {
        "imdata": [
            {
                "aaaLogin": {
                    "attributes": {
                        "token": token,
                        "refreshTimeoutSeconds": str(int(SESSION_LIFETIME_SECONDS)),
                    }
                }
            }
        ],
        "totalCount": "1",
    }


def make_auth_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/aaaLogin.json")
    async def aaa_login(request: Request, response: Response):
        # Parse the body manually (not via a FastAPI `body: dict` parameter)
        # so a malformed/non-dict body never triggers FastAPI's automatic
        # RequestValidationError -> {"detail": [...]} leak. We want a 400
        # APIC envelope for every malformed shape instead.
        try:
            raw = await request.json()
        except Exception:
            return _apic_error("Malformed request body", code="400", status_code=400)
        if not isinstance(raw, dict):
            return _apic_error("Malformed request body", code="400", status_code=400)

        aaa_user = raw.get("aaaUser")
        if not isinstance(aaa_user, dict):
            return _apic_error("Malformed aaaUser body", code="400", status_code=400)
        attrs = aaa_user.get("attributes")
        if not isinstance(attrs, dict):
            return _apic_error("Malformed aaaUser body", code="400", status_code=400)

        username = attrs.get("name")
        password = attrs.get("pwd")
        bare_username = _bare_username(username) if isinstance(username, str) else username
        if bare_username != SIM_USERNAME or password != SIM_PASSWORD:
            return _apic_error(
                "Authentication failed: invalid username or password",
                code="401",
                status_code=401,
            )

        token = _create_session(bare_username)
        response.set_cookie("APIC-cookie", token)
        return _login_response(token)

    @router.get("/api/aaaRefresh.json")
    async def aaa_refresh(request: Request, response: Response):
        token = request.cookies.get("APIC-cookie")
        if not token or not _touch_session(token):
            return _auth_error_403()
        response.set_cookie("APIC-cookie", token)
        return _login_response(token)

    @router.post("/api/aaaLogout.json")
    async def aaa_logout(request: Request, response: Response):
        token = request.cookies.get("APIC-cookie")
        _pop_session(token)
        response.delete_cookie("APIC-cookie")
        return {"imdata": [], "totalCount": "0"}

    return router
