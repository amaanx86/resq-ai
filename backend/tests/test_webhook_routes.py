"""HTTP + WebSocket surface of stream B2: webhook verification and enqueue, simulator,
message status, case resolve, and in-process fan-out to /ws/bridge and /ws/cases."""
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.main import app
from app.models.schemas import InboundWhatsAppMessage
from app import case_messages
from app.routers import cases as cases_router
from app.routers import webhook as webhook_router
from app.services.dynamo_service import dynamo_service
from test_pipeline_support import FakeWhatsApp, fakes, inbound, new_phone  # noqa: F401


@pytest.fixture
def client(fakes, monkeypatch):
    _wa = FakeWhatsApp()
    monkeypatch.setattr(cases_router, "whatsapp_service", _wa)
    monkeypatch.setattr(case_messages, "whatsapp_service", _wa)  # the portal send code
    with TestClient(app) as c:
        yield c


class RecordingQueue:
    """Stands in for queue_service in SQS mode."""
    inline = False
    mode = "sqs"

    def __init__(self):
        self.published = []

    async def publish(self, msg, received_at=None):
        self.published.append((msg, received_at))
        return {"status": "QUEUED", "provider_message_id": msg.provider_message_id, "sqs_message_id": "m-1"}


def _fake_inbound_module(monkeypatch, messages, signature_ok=True):
    mod = types.ModuleType("app.messaging.inbound")
    mod.parse_meta_webhook = lambda body: list(messages)
    mod.verify_meta_signature = lambda raw, header: signature_ok
    mod.parse_queue_message = lambda body: [InboundWhatsAppMessage.model_validate_json(body)]
    monkeypatch.setitem(sys.modules, "app.messaging.inbound", mod)
    # The router imports these names at module load, so patch them there too.
    monkeypatch.setattr(webhook_router, "parse_meta_webhook", mod.parse_meta_webhook)
    monkeypatch.setattr(webhook_router, "verify_meta_signature", mod.verify_meta_signature)


# ---------------------------------------------------------------- GET verification
def test_webhook_verification_good_token(client):
    r = client.get("/api/v1/webhook", params={"hub.mode": "subscribe", "hub.challenge": "12345",
                                               "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN})
    assert r.status_code == 200 and r.text == "12345"


def test_webhook_verification_bad_token(client):
    r = client.get("/api/v1/webhook", params={"hub.mode": "subscribe", "hub.challenge": "12345",
                                               "hub.verify_token": "wrong"})
    assert r.status_code == 403


# ---------------------------------------------------------------- POST webhook
def test_post_webhook_publishes_each_message_and_returns_fast(client, monkeypatch):
    msgs = [inbound(new_phone(), text="hi"), inbound(new_phone(), type="audio", media_id="m1")]
    _fake_inbound_module(monkeypatch, msgs)
    queue = RecordingQueue()
    monkeypatch.setattr(webhook_router, "queue_service", queue)
    started = time.perf_counter()
    r = client.post("/api/v1/webhook", json={"object": "whatsapp_business_account", "entry": []})
    elapsed = time.perf_counter() - started
    assert r.status_code == 200 and r.json() == {"status": "RECEIVED", "messages": 2, "calls": 0}
    assert [m.provider_message_id for m, _ in queue.published] == [m.provider_message_id for m in msgs]
    assert all(received_at for _, received_at in queue.published)
    assert elapsed < 2.0


def test_post_webhook_rejects_bad_signature(client, monkeypatch):
    _fake_inbound_module(monkeypatch, [inbound(new_phone(), text="hi")], signature_ok=False)
    queue = RecordingQueue()
    monkeypatch.setattr(webhook_router, "queue_service", queue)
    r = client.post("/api/v1/webhook", json={"entry": []}, headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401 and queue.published == []


def test_post_webhook_inline_mode_processes_in_background(client, fakes, monkeypatch):
    phone = new_phone()
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    _fake_inbound_module(monkeypatch, [msg])
    r = client.post("/api/v1/webhook", json={"entry": []})
    assert r.status_code == 200
    status = client.get(f"/api/v1/webhook/messages/{msg.provider_message_id}").json()
    assert status["status"] == "PROCESSED" and status["case_id"]
    assert dynamo_service.get_case(status["case_id"]).user_phone == phone


# ---------------------------------------------------------------- simulator + status
def test_simulate_voice_inline_returns_case_and_status_endpoint(client, fakes):
    phone = new_phone()
    r = client.post("/api/v1/webhook/simulate-voice",
                    json={"user_phone": phone, "voice_text": "seene me dard", "language": "Urdu"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "PROCESSED"
    assert body["provider_message_id"].startswith("sim-")
    assert body["case"]["user_phone"] == phone and body["case"]["provider"] == "simulator"
    assert fakes.triage.calls[0]["transcript"] == "seene me dard"
    assert fakes.triage.calls[0]["detected_language"] == "Urdu"

    s = client.get(f"/api/v1/webhook/messages/{body['provider_message_id']}").json()
    assert s["status"] == "PROCESSED" and s["case_id"] == body["case"]["case_id"]


def test_simulate_voice_queued_mode_returns_null_case(client, monkeypatch):
    queue = RecordingQueue()
    monkeypatch.setattr(webhook_router, "queue_service", queue)
    r = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": "923001112223", "voice_text": "x"})
    body = r.json()
    assert body["status"] == "QUEUED" and body["case"] is None
    msg = queue.published[0][0]
    assert msg.provider == "simulator" and msg.type == "audio" and msg.simulated_transcript == "x"
    assert msg.sender_phone == "+923001112223"
    s = client.get(f"/api/v1/webhook/messages/{body['provider_message_id']}").json()
    assert s == {"status": "PENDING", "case_id": None}


# ---------------------------------------------------------------- resolve
def test_resolve_endpoint_clears_active_case(client):
    phone = new_phone()
    case = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": phone, "voice_text": "dil"}).json()["case"]
    assert dynamo_service.get_user(phone).active_case_id == case["case_id"]
    r = client.post(f"/api/v1/cases/{case['case_id']}/resolve")
    assert r.status_code == 200
    assert r.json()["status"] == "RESOLVED" and r.json()["resolved_at"]
    assert dynamo_service.get_user(phone).active_case_id is None
    assert client.post("/api/v1/cases/CASE-NOPE/resolve").status_code == 404


def test_case_endpoints_apply_visibility(client, monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "visible_cases", lambda request, cases: calls.append("list") or [])

    def deny(request, case):
        from fastapi import HTTPException
        calls.append("detail")
        raise HTTPException(status_code=403, detail="not yours")
    monkeypatch.setattr(auth, "assert_case_visible", deny)
    assert client.get("/api/v1/cases").json() == []
    assert client.get("/api/v1/cases/active").json() == []
    case_id = dynamo_service.list_all_cases()[0].case_id
    assert client.get(f"/api/v1/cases/{case_id}").status_code == 403
    assert client.get(f"/api/v1/cases/{case_id}/messages").status_code == 403
    assert client.post(f"/api/v1/cases/{case_id}/resolve").status_code == 403
    assert calls.count("list") == 2 and calls.count("detail") == 3


# ---------------------------------------------------------------- WebSockets (in-process fan-out)
def test_bridge_receives_doctor_message_in_process(client):
    phone = new_phone()
    case = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": phone, "voice_text": "dil"}).json()["case"]
    with client.websocket_connect(f"/ws/bridge/{case['case_id']}") as ws:
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}
        r = client.post(f"/api/v1/cases/{case['case_id']}/messages",
                        json={"content": "Keep him lying down", "sender": "DOCTOR", "sender_name": "Dr. X"})
        assert r.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "chat_message" and event["case_id"] == case["case_id"]
        assert event["message"]["content"] == "Keep him lying down"
        assert event["message"]["message_id"] == r.json()["message_id"]


def test_bridge_receives_patient_handover_message(client):
    phone = new_phone()
    case = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": phone, "voice_text": "dil"}).json()["case"]
    with client.websocket_connect(f"/ws/bridge/{case['case_id']}") as ws:
        r = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": phone, "voice_text": "ab saans ruk rahi hai"})
        assert r.json()["action"] == "handover"
        event = ws.receive_json()
        assert event["type"] == "chat_message" and "ab saans ruk rahi hai" in event["message"]["content"]
        assert event["message"]["sender"] == "PATIENT"


def test_case_list_socket_gets_case_upserts(client):
    phone = new_phone()
    with client.websocket_connect("/ws/cases") as ws:
        case = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": phone, "voice_text": "dil"}).json()["case"]
        event = ws.receive_json()
        assert event["type"] == "case_upserted" and event["case_id"] == case["case_id"]
        client.post(f"/api/v1/cases/{case['case_id']}/resolve")
        # The doctor offer also updates the case (OFFER-1); read on until the resolve arrives.
        for _ in range(5):
            if ws.receive_json()["case"]["status"] == "RESOLVED":
                break
        else:
            pytest.fail("no RESOLVED case_upserted event")


def test_websocket_refused_when_unauthorized(client, monkeypatch):
    async def refuse(websocket):
        return None
    monkeypatch.setattr(auth, "authorize_websocket", refuse)
    from starlette.websockets import WebSocketDisconnect
    for path in ("/ws/cases", "/ws/bridge/CASE-ANY"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path) as ws:
                ws.receive_text()
        assert exc.value.code == 1008
