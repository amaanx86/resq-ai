"""Portal authentication and authorization (UI-2).

The portal signs in through the Cognito Hosted UI and sends the Cognito **ID token** as
`Authorization: Bearer <token>` (WebSockets pass it as `?token=<token>`). This module verifies
it against the user pool's JWKS and applies the portal's access rules.

With AUTH_ENABLED=False (local mock mode) every function allows everything and the caller is
LOCAL_PRINCIPAL, an administrator.

Principal claims:
  - groups:    from `cognito:groups` ("Administrators", "Doctors")
  - doctor_id: from the `custom:doctor_id` user attribute (Doctors only)

Access rules (AUTH_ENABLED=True):
  - Everything under the portal routers needs a valid ID token from a user in the
    Administrators or Doctors group.
  - `/api/v1/analytics/**` (including the demo seed), `/api/v1/admins/**` (ADMIN-2), `POST /api/v1/doctors/register`,
    `POST /api/v1/doctors/{id}/invite`, `POST /api/v1/doctors/{id}/confirm-number`,
    `PATCH` / `DELETE /api/v1/doctors/{id}`, `DELETE /api/v1/cases/{id}`,
    `POST /api/v1/webhook/simulate-voice` and
    `GET /api/v1/webhook/messages/{id}` are Administrators-only. (The rest of the webhook router, Meta's verification and inbound POST,
    is not portal-authenticated; the inbound POST checks Meta's signature instead.)
  - `POST /api/v1/doctors/{id}/status`: Administrators, or the doctor toggling themself.
  - `GET /api/v1/doctors/{id}`: Administrators, or the doctor reading their own profile.
  - Doctors only see cases assigned to them (`assigned_doctor_id == custom:doctor_id`):
    case lists are filtered with `visible_cases`, and any `/api/v1/cases/{case_id}/...` route
    is refused with 403 for a case assigned to someone else.
"""
import asyncio
import logging
import re
import threading
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

import jwt
from fastapi import HTTPException, Request, WebSocket
from app.config import settings
from app.models.schemas import EmergencyCase

logger = logging.getLogger("resq.auth")

ADMIN_GROUP = "Administrators"
DOCTOR_GROUP = "Doctors"


@dataclass
class Principal:
    sub: str
    email: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    doctor_id: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return "Administrators" in self.groups

    @property
    def is_doctor(self) -> bool:
        return DOCTOR_GROUP in self.groups


# Used when AUTH_ENABLED is False: full access, like an administrator.
LOCAL_PRINCIPAL = Principal(sub="local-dev", email="local@resq.dev", groups=["Administrators"])

# Principal for CORS preflight (OPTIONS) requests, which carry no token.
ANONYMOUS_PRINCIPAL = Principal(sub="anonymous", groups=[])


# ==========================================
# TOKEN VERIFICATION
# ==========================================
class AuthError(Exception):
    """The token is missing, malformed, expired, or not a valid Cognito ID token for this app."""


def _issuer() -> str:
    return f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com/{settings.COGNITO_USER_POOL_ID}"


def _jwks_url() -> str:
    return f"{_issuer()}/.well-known/jwks.json"


_jwk_clients: Dict[str, jwt.PyJWKClient] = {}
_jwk_lock = threading.Lock()


def _get_jwk_client() -> jwt.PyJWKClient:
    """One cached PyJWKClient per JWKS URL. It caches the key set (5 min) and signing keys,
    and refetches when a token carries an unknown `kid` (key rotation)."""
    url = _jwks_url()
    with _jwk_lock:
        client = _jwk_clients.get(url)
        if client is None:
            client = jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=300, cache_keys=True, max_cached_keys=16, timeout=5)
            _jwk_clients[url] = client
        return client


def verify_id_token(token: str) -> Dict[str, Any]:
    """Verifies a Cognito ID token (signature via JWKS, iss, aud, token_use=id, exp) and returns its claims."""
    if not token:
        raise AuthError("missing token")
    if not settings.COGNITO_USER_POOL_ID or not settings.COGNITO_APP_CLIENT_ID:
        logger.error("[Auth] AUTH_ENABLED but COGNITO_USER_POOL_ID / COGNITO_APP_CLIENT_ID are not set")
        raise AuthError("authentication is not configured")
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.COGNITO_APP_CLIENT_ID,
            issuer=_issuer(),
            options={"require": ["exp", "iat", "iss", "aud", "sub", "token_use"]},
            leeway=30,
        )
    except jwt.ExpiredSignatureError as e:
        raise AuthError("token expired") from e
    except (jwt.PyJWKClientError, jwt.InvalidTokenError) as e:
        raise AuthError(f"invalid token: {e}") from e
    except Exception as e:  # network failure fetching JWKS, etc.
        logger.warning(f"[Auth] Token verification failed: {e}")
        raise AuthError("token could not be verified") from e
    if claims.get("token_use") != "id":
        raise AuthError("an ID token is required (got token_use=%s)" % claims.get("token_use"))
    return claims


def principal_from_claims(claims: Dict[str, Any]) -> Principal:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return Principal(
        sub=str(claims.get("sub")),
        email=claims.get("email"),
        groups=list(groups),
        doctor_id=claims.get("custom:doctor_id") or None,
    )


def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


# ==========================================
# ACCESS RULES
# ==========================================
_ADMIN_PREFIXES = ("/api/v1/analytics", "/api/v1/admins")
_ADMIN_ROUTES = {
    ("POST", "/api/v1/doctors/register"),
    # The portal's voice-note simulator (it triggers real dispatch tools) and its status poll.
    # The webhook router is mounted without portal_auth (Meta must reach it), so these two
    # handlers call portal_auth themselves.
    ("POST", "/api/v1/webhook/simulate-voice"),
}
_ADMIN_PATH_RES = (re.compile(r"^/api/v1/webhook/messages/[^/]+$"),
                   re.compile(r"^/api/v1/cases/[^/]+/assign$"),
                   re.compile(r"^/api/v1/cases/[^/]+/no-doctor-alert/ack$"),
                   re.compile(r"^/api/v1/doctors/[^/]+/invite$"),
                   re.compile(r"^/api/v1/doctors/[^/]+/confirm-number$"))
# Administrators only for these methods: editing / unregistering a doctor, deleting a case.
_ADMIN_METHOD_RES = ((("PATCH", "DELETE"), re.compile(r"^/api/v1/doctors/[^/]+$")),
                     (("DELETE",), re.compile(r"^/api/v1/cases/[^/]+$")))
_DOCTOR_STATUS_RE = re.compile(r"^/api/v1/doctors/([^/]+)/status/?$")
_DOCTOR_PROFILE_RE = re.compile(r"^/api/v1/doctors/([^/]+)/?$")
_CASE_PATH_RE = re.compile(r"^/api/v1/cases/([^/]+)(?:/.*)?$")
# Per-case writes an offered (not yet assigned) doctor may make.
_OFFER_ANSWER_RE = re.compile(r"^/api/v1/cases/[^/]+/offer/(accept|decline)$")
# Path segments under /api/v1/cases/ that are not case IDs.
_CASE_COLLECTION_SEGMENTS = {"active"}


def _case_field(case: Any, name: str) -> Any:
    if isinstance(case, dict):
        return case.get(name)
    return getattr(case, name, None)


def can_see_case(principal: Principal, case: Any) -> bool:
    """True if this principal may see this case (EmergencyCase or its dict form): admins; the
    assigned doctor; a doctor the case is currently offered to (OFFER-1, read only until accepted)."""
    if not settings.AUTH_ENABLED or principal.is_admin:
        return True
    if principal.is_doctor and principal.doctor_id:
        return can_act_on_case(principal, case) or (
            _case_field(case, "doctor_offer_status") == "PENDING"
            and principal.doctor_id in (_case_field(case, "offered_doctor_ids") or []))
    return False


def can_act_on_case(principal: Principal, case: Any) -> bool:
    """True if this principal may change this case (chat, video, resolve, ambulance): admins and
    the assigned doctor. An offered doctor may only accept or decline."""
    if not settings.AUTH_ENABLED or principal.is_admin:
        return True
    return bool(principal.is_doctor and principal.doctor_id
                and _case_field(case, "assigned_doctor_id") == principal.doctor_id)


async def portal_auth(request: Request) -> Principal:
    """Router-level dependency for every portal route under /api/v1 (registered in main.py).

    Verifies the bearer token, applies the path rules (admin-only routes, doctors' own cases) and
    stores the principal on request.state.principal.
    """
    if not settings.AUTH_ENABLED:
        request.state.principal = LOCAL_PRINCIPAL
        return LOCAL_PRINCIPAL

    method = request.method.upper()
    path = request.url.path.rstrip("/") or "/"

    if method == "OPTIONS":
        request.state.principal = ANONYMOUS_PRINCIPAL
        return ANONYMOUS_PRINCIPAL

    token = _bearer_token(request)
    if not token:
        raise _unauthorized("Missing bearer token")
    try:
        # PyJWKClient may fetch the JWKS over the network: keep it off the event loop.
        claims = await asyncio.to_thread(verify_id_token, token)
    except AuthError as e:
        raise _unauthorized(str(e))
    principal = principal_from_claims(claims)
    request.state.principal = principal

    if not (principal.is_admin or principal.is_doctor):
        raise HTTPException(status_code=403, detail="User is not in the Administrators or Doctors group")
    if principal.is_admin:
        return principal

    # From here on: a doctor.
    if (path.startswith(_ADMIN_PREFIXES) or (method, path) in _ADMIN_ROUTES
            or any(r.match(path) for r in _ADMIN_PATH_RES)
            or any(method in methods and r.match(path) for methods, r in _ADMIN_METHOD_RES)):
        raise HTTPException(status_code=403, detail="Administrators only")

    m = _DOCTOR_STATUS_RE.match(path)
    if m and method == "POST" and m.group(1) != principal.doctor_id:
        raise HTTPException(status_code=403, detail="Doctors may only change their own availability")
    m = _DOCTOR_PROFILE_RE.match(path)
    if m and method == "GET" and m.group(1) != principal.doctor_id:
        raise HTTPException(status_code=403, detail="Doctors may only read their own profile")

    m = _CASE_PATH_RE.match(path)
    if m and m.group(1) not in _CASE_COLLECTION_SEGMENTS:
        # Defence in depth for every per-case route (detail, messages, resolve, audio, video):
        # an unknown case falls through so the route itself returns 404.
        from app.services.dynamo_service import dynamo_service
        case = await asyncio.to_thread(dynamo_service.get_case, m.group(1))
        if case is not None and not can_see_case(principal, case):
            raise HTTPException(status_code=403, detail="This case is assigned to another doctor")
        if (case is not None and method not in ("GET", "HEAD") and not _OFFER_ANSWER_RE.match(path)
                and not can_act_on_case(principal, case)):
            raise HTTPException(status_code=403, detail="Accept the case before acting on it")

    return principal


def get_principal(request: Request) -> Principal:
    """Principal set by portal_auth, or LOCAL_PRINCIPAL when auth is disabled."""
    return getattr(request.state, "principal", LOCAL_PRINCIPAL)


def visible_cases(request: Request, cases: List[EmergencyCase]) -> List[EmergencyCase]:
    """Filters a case list to what the caller may see (doctors: only cases assigned to them)."""
    if not settings.AUTH_ENABLED:
        return cases
    principal = get_principal(request)
    if principal.is_admin:
        return cases
    return [c for c in cases if can_see_case(principal, c)]


def assert_case_visible(request: Request, case: EmergencyCase) -> None:
    """Raises HTTPException(403) if the caller may not see this case."""
    if not settings.AUTH_ENABLED:
        return None
    if not can_see_case(get_principal(request), case):
        raise HTTPException(status_code=403, detail="This case is assigned to another doctor")
    return None


async def authorize_websocket(websocket: WebSocket) -> Optional[Principal]:
    """Checks the `token` query parameter of a WebSocket before it is accepted.
    Returns the principal, or None if the connection must be refused (close code 1008).

    For `/ws/bridge/{case_id}` a doctor must be assigned to that case. For `/ws/cases` the
    caller should filter `case_upserted` events with `can_see_case(principal, case)`.
    """
    if not settings.AUTH_ENABLED:
        return LOCAL_PRINCIPAL
    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        claims = await asyncio.to_thread(verify_id_token, token)
    except AuthError as e:
        logger.info(f"[Auth] WebSocket refused: {e}")
        return None
    principal = principal_from_claims(claims)
    if not (principal.is_admin or principal.is_doctor):
        return None
    case_id = (websocket.path_params or {}).get("case_id")
    if case_id and not principal.is_admin:
        from app.services.dynamo_service import dynamo_service
        case = await asyncio.to_thread(dynamo_service.get_case, case_id)
        if case is None or not can_see_case(principal, case):
            return None
    return principal
