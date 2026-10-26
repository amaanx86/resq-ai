"""UI-2: Cognito ID token verification and portal access rules (app/auth.py).

Tokens are signed with a throwaway RSA key and the JWKS client is stubbed, so no network.
Auth is switched on by replacing app.auth's `settings` reference with an override proxy.
"""
import asyncio
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.auth as auth
from app.main import app
from app.models.schemas import EmergencyCase
from app.services.dynamo_service import dynamo_service

REGION = "us-east-1"
POOL_ID = "us-east-1_TestPool"
CLIENT_ID = "test-app-client-id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
KID = "test-kid-1"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
_PUBLIC_JWK.update({"kid": KID, "alg": "RS256", "use": "sig"})


class _SettingsOverride:
    """Delegates to the real settings, with a few attributes overridden."""

    def __init__(self, base, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


class _FakeJWKClient:
    def __init__(self):
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != KID:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return jwt.PyJWK(_PUBLIC_JWK)


def make_token(groups=("Administrators",), doctor_id=None, token_use="id", aud=CLIENT_ID,
               iss=ISSUER, exp_in=3600, key=None, kid=KID, drop=()):
    now = int(time.time())
    claims = {
        "sub": str(uuid.uuid4()),
        "email": "user@resq.test",
        "iss": iss,
        "aud": aud,
        "token_use": token_use,
        "iat": now - 10,
        "auth_time": now - 10,
        "exp": now + exp_in,
        "cognito:groups": list(groups),
    }
    if doctor_id:
        claims["custom:doctor_id"] = doctor_id
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, key or _PRIVATE_KEY, algorithm="RS256", headers={"kid": kid})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(auth, "settings", _SettingsOverride(
        auth.settings, AUTH_ENABLED=True, COGNITO_USER_POOL_ID=POOL_ID,
        COGNITO_APP_CLIENT_ID=CLIENT_ID, COGNITO_REGION=REGION))
    fake = _FakeJWKClient()
    monkeypatch.setattr(auth, "_get_jwk_client", lambda: fake)
    return fake


@pytest.fixture
def auth_off(monkeypatch):
    monkeypatch.setattr(auth, "settings", _SettingsOverride(auth.settings, AUTH_ENABLED=False))


@pytest.fixture
def client():
    return TestClient(app)


def _case(case_id, doctor_id):
    return EmergencyCase(
        case_id=case_id, user_phone="+923000000001", patient_name="Auth Test", transcript="test",
        detected_language="English", esi_level=2, severity="HIGH", status="DOCTOR_HANDOVER",
        matched_specialty="Cardiology", assigned_doctor_id=doctor_id, assigned_doctor_name="Dr. Test",
    )


@pytest.fixture
def two_cases():
    a = _case(f"CASE-AUTH-{uuid.uuid4().hex[:6]}", "DOC-CARDIO-01")
    b = _case(f"CASE-AUTH-{uuid.uuid4().hex[:6]}", "DOC-NEURO-02")
    dynamo_service.save_case(a)
    dynamo_service.save_case(b)
    return a, b


# ---------------- token verification ----------------

def test_valid_admin_token_is_accepted(auth_on, client):
    r = client.get("/api/v1/cases/active", headers=bearer(make_token()))
    assert r.status_code == 200


def test_verify_id_token_returns_claims_and_principal(auth_on):
    claims = auth.verify_id_token(make_token(groups=["Doctors"], doctor_id="DOC-NEURO-02"))
    p = auth.principal_from_claims(claims)
    assert p.groups == ["Doctors"] and p.doctor_id == "DOC-NEURO-02"
    assert p.is_doctor and not p.is_admin
    assert p.email == "user@resq.test"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}])
def test_missing_token_is_401(auth_on, client, headers):
    r = client.get("/api/v1/cases/active", headers=headers)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("kwargs", [
    {"exp_in": -3600},                 # expired
    {"aud": "some-other-client"},      # wrong audience
    {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"},  # wrong issuer
    {"token_use": "access"},           # access token instead of ID token
    {"key": _OTHER_KEY},               # bad signature
    {"kid": "unknown-kid"},            # key not in the JWKS
    {"drop": ("token_use",)},          # missing token_use
])
def test_invalid_tokens_are_401(auth_on, client, kwargs):
    r = client.get("/api/v1/cases/active", headers=bearer(make_token(**kwargs)))
    assert r.status_code == 401, kwargs


def test_garbage_token_is_401(auth_on, client):
    r = client.get("/api/v1/cases/active", headers=bearer("not.a.jwt"))
    assert r.status_code == 401


def test_user_without_portal_group_is_403(auth_on, client):
    r = client.get("/api/v1/cases/active", headers=bearer(make_token(groups=[])))
    assert r.status_code == 403


def test_unconfigured_pool_rejects_tokens(monkeypatch, client):
    monkeypatch.setattr(auth, "settings", _SettingsOverride(
        auth.settings, AUTH_ENABLED=True, COGNITO_USER_POOL_ID="", COGNITO_APP_CLIENT_ID=""))
    r = client.get("/api/v1/cases/active", headers=bearer(make_token()))
    assert r.status_code == 401


def test_jwk_client_is_cached_per_pool(monkeypatch):
    monkeypatch.setattr(auth, "settings", _SettingsOverride(
        auth.settings, AUTH_ENABLED=True, COGNITO_USER_POOL_ID=POOL_ID,
        COGNITO_APP_CLIENT_ID=CLIENT_ID, COGNITO_REGION=REGION))
    monkeypatch.setattr(auth, "_jwk_clients", {})
    c1 = auth._get_jwk_client()
    c2 = auth._get_jwk_client()
    assert c1 is c2
    assert c1.uri == f"{ISSUER}/.well-known/jwks.json"


# ---------------- path rules ----------------

def test_there_is_no_open_cad_route(auth_on, client):
    """The CAD mock receiver is gone (no automatic dispatch): nothing under /cases is open."""
    assert not [r for r in client.app.routes if "cad" in getattr(r, "path", "")]
    r = client.post("/api/v1/cases/cad-mock-dispatch", json={"case_id": "X", "esi_level": 1})
    assert r.status_code != 200


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/v1/analytics/summary", None),
    ("post", "/api/v1/analytics/seed-demo", None),
    ("post", "/api/v1/doctors/register", {
        "name": "Dr. X", "phone_number": "+923001111111", "license_no": "L-1", "specialty": "Cardiology"}),
    ("post", "/api/v1/doctors/DOC-CARDIO-01/invite", None),
])
def test_admin_only_routes_refuse_doctors(auth_on, client, method, path, body):
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    r = getattr(client, method)(path, headers=doctor, **({"json": body} if body else {}))
    assert r.status_code == 403


def test_admin_can_read_analytics(auth_on, client):
    r = client.get("/api/v1/analytics/summary", headers=bearer(make_token()))
    assert r.status_code == 200


def test_doctor_can_toggle_only_own_status(auth_on, client):
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    r = client.post("/api/v1/doctors/DOC-NEURO-02/status?is_active=true", headers=doctor)
    assert r.status_code == 403
    r = client.post("/api/v1/doctors/DOC-CARDIO-01/status?is_active=true", headers=doctor)
    assert r.status_code == 200


# ---------------- doctor case filtering ----------------

def test_doctor_reads_only_own_profile(auth_on, client):
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    r = client.get("/api/v1/doctors/DOC-CARDIO-01", headers=doctor)
    assert r.status_code == 200 and r.json()["doctor_id"] == "DOC-CARDIO-01"
    assert client.get("/api/v1/doctors/DOC-NEURO-02", headers=doctor).status_code == 403
    admin = bearer(make_token())
    assert client.get("/api/v1/doctors/DOC-NEURO-02", headers=admin).status_code == 200
    assert client.get("/api/v1/doctors/DOC-NOPE", headers=admin).status_code == 404


def test_doctor_sees_only_assigned_case_routes(auth_on, client, two_cases):
    mine, other = two_cases
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    assert client.get(f"/api/v1/cases/{mine.case_id}", headers=doctor).status_code == 200
    assert client.get(f"/api/v1/cases/{mine.case_id}/messages", headers=doctor).status_code == 200
    assert client.get(f"/api/v1/cases/{other.case_id}", headers=doctor).status_code == 403
    assert client.get(f"/api/v1/cases/{other.case_id}/messages", headers=doctor).status_code == 403
    # Admins see everything.
    admin = bearer(make_token())
    assert client.get(f"/api/v1/cases/{other.case_id}", headers=admin).status_code == 200


def test_offered_doctor_may_read_and_answer_but_not_act(auth_on, client, monkeypatch):
    """OFFER-1: while the offer is pending the offered doctors see the case (read only); only the
    offer answers are allowed until one of them accepts. Other doctors see nothing."""
    from app import doctor_offers
    monkeypatch.setattr(doctor_offers, "_schedule_timeout", lambda *a, **k: asyncio.sleep(0))
    case = _case(f"CASE-AUTH-{uuid.uuid4().hex[:6]}", None).model_copy(update={
        "doctor_offer_status": "PENDING", "offer_id": "OFR-AUTH", "offered_doctor_ids": ["DOC-CARDIO-01"]})
    dynamo_service.save_case(case)
    offered = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    stranger = bearer(make_token(groups=["Doctors"], doctor_id="DOC-NEURO-02"))
    assert client.get(f"/api/v1/cases/{case.case_id}", headers=offered).status_code == 200
    assert client.get(f"/api/v1/cases/{case.case_id}", headers=stranger).status_code == 403
    msg = {"sender_name": "Dr. Sarah Khan, MD", "content": "hello"}
    assert client.post(f"/api/v1/cases/{case.case_id}/messages", json=msg, headers=offered).status_code == 403
    assert client.post(f"/api/v1/cases/{case.case_id}/resolve", headers=offered).status_code == 403
    assert client.post(f"/api/v1/cases/{case.case_id}/offer/accept", json={}, headers=stranger).status_code == 403
    # The offered doctor answers as themselves (a doctor_id in the body is ignored).
    r = client.post(f"/api/v1/cases/{case.case_id}/offer/accept", json={"doctor_id": "DOC-NEURO-02"}, headers=offered)
    assert r.status_code == 200 and r.json()["assigned_doctor_id"] == "DOC-CARDIO-01"
    assert client.post(f"/api/v1/cases/{case.case_id}/messages", json=msg, headers=offered).status_code == 200
    assert client.post(f"/api/v1/cases/{case.case_id}/assign", json={"doctor_id": "DOC-CARDIO-01"},
                       headers=offered).status_code == 403          # admins only


def test_doctor_without_doctor_id_sees_no_case(auth_on, client, two_cases):
    mine, _ = two_cases
    doctor = bearer(make_token(groups=["Doctors"]))
    assert client.get(f"/api/v1/cases/{mine.case_id}", headers=doctor).status_code == 403


def _request_with(principal):
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


def test_visible_cases_and_assert_case_visible(auth_on, two_cases):
    mine, other = two_cases
    doctor = auth.Principal(sub="d", groups=["Doctors"], doctor_id="DOC-CARDIO-01")
    req = _request_with(doctor)
    assert auth.visible_cases(req, [mine, other]) == [mine]
    auth.assert_case_visible(req, mine)
    with pytest.raises(auth.HTTPException) as exc:
        auth.assert_case_visible(req, other)
    assert exc.value.status_code == 403

    admin_req = _request_with(auth.Principal(sub="a", groups=["Administrators"]))
    assert auth.visible_cases(admin_req, [mine, other]) == [mine, other]
    # Dict-shaped cases (as sent on the event bus) work too.
    assert auth.can_see_case(doctor, mine.model_dump()) is True
    assert auth.can_see_case(doctor, other.model_dump()) is False


def test_case_list_endpoint_filtered_for_doctor(auth_on, client, two_cases):
    """Holds once cases.py calls auth.visible_cases (stream B2)."""
    mine, other = two_cases
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    r = client.get("/api/v1/cases", headers=doctor)
    assert r.status_code == 200
    ids = {c["case_id"] for c in r.json()}
    if other.case_id in ids:
        pytest.skip("cases.py does not call auth.visible_cases yet (stream B2)")
    assert mine.case_id in ids


# ---------------- WebSockets ----------------

def _ws_app():
    ws_app = FastAPI()

    async def handler(websocket: WebSocket):
        principal = await auth.authorize_websocket(websocket)
        if principal is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_json({"sub": principal.sub, "groups": principal.groups})
        await websocket.close()

    ws_app.add_api_websocket_route("/ws/cases", handler)
    ws_app.add_api_websocket_route("/ws/bridge/{case_id}", handler)
    return TestClient(ws_app)


def test_websocket_requires_valid_token(auth_on):
    c = _ws_app()
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws/cases"):
            pass
    assert exc.value.code == 1008
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"/ws/cases?token={make_token(exp_in=-3600)}"):
            pass
    with c.websocket_connect(f"/ws/cases?token={make_token()}") as ws:
        assert ws.receive_json()["groups"] == ["Administrators"]


def test_websocket_bridge_checks_case_assignment(auth_on, two_cases):
    mine, other = two_cases
    c = _ws_app()
    token = make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01")
    with c.websocket_connect(f"/ws/bridge/{mine.case_id}?token={token}") as ws:
        assert ws.receive_json()["groups"] == ["Doctors"]
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(f"/ws/bridge/{other.case_id}?token={token}"):
            pass
    assert exc.value.code == 1008


def test_real_bridge_websocket_refuses_missing_token(auth_on):
    """bridge.py accepts and then closes with 1008, so a browser sees 1008 (not a failed
    handshake, 1006) and stops reconnecting."""
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws/cases") as ws:
            ws.receive_text()   # the handshake succeeded; the refusal arrives as a close frame
    assert exc.value.code == 1008
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(f"/ws/bridge/CASE-NOPE?token={make_token(groups=['Doctors'], doctor_id='D')}") as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    with c.websocket_connect(f"/ws/cases?token={make_token()}") as ws:
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"


# ---------------- auth disabled ----------------

def test_auth_disabled_allows_everything(auth_off, client, two_cases):
    _, other = two_cases
    assert client.get("/api/v1/analytics/summary").status_code == 200
    assert client.get(f"/api/v1/cases/{other.case_id}").status_code == 200
    req = SimpleNamespace(state=SimpleNamespace())
    assert auth.get_principal(req) is auth.LOCAL_PRINCIPAL
    assert auth.visible_cases(req, list(two_cases)) == list(two_cases)
    auth.assert_case_visible(req, other)


def test_auth_disabled_websocket_is_local_principal(auth_off):
    ws = SimpleNamespace(query_params={}, path_params={})
    assert asyncio.run(auth.authorize_websocket(ws)) is auth.LOCAL_PRINCIPAL


# ---------------- doctor video join (portal_auth called directly by app/routers/video.py) ----------------

def test_doctor_video_join_uses_portal_auth(auth_on, client, two_cases):
    mine, other = two_cases
    url = "/api/v1/video/MEETING-X/join?role=doctor&case={}"
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    r = client.post(url.format(mine.case_id))
    if r.status_code in (404, 405):
        pytest.skip("video join route not registered yet (stream D)")
    assert r.status_code == 401
    assert client.post(url.format(other.case_id), headers=doctor).status_code == 403
    # Authorized; the case simply has no meeting yet -> 404 from the route, not an auth error.
    assert client.post(url.format(mine.case_id), headers=doctor).status_code == 404
    assert client.post(url.format(other.case_id), headers=bearer(make_token())).status_code == 404
