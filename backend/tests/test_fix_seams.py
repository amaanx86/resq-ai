"""Regression tests for the integration seams found in review:

1. Portal-only webhook routes (simulator, status poll) need Administrators when AUTH_ENABLED;
   the Meta signature check refuses unsigned events against real AWS when no secret is set.
2. A pending user's first emergency text with commas is triaged, never answered with a hint.
3. Two messages from one sender processed at the same time open ONE case (worker grouping and
   the pipeline's per-sender lease); the second is a handover into the first case.
4. Follow-ups on open cases where the emergency protocol never ran are triaged with the SAME
   case ID and escalate the case in place for ESI 1-2.
5. Message lease > SQS visibility timeout, visibility heartbeat, deterministic bot reply.
6. WebSocket refusals reach the browser as 1008 (accept, then close).
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import pipeline, worker as worker_mod
from app.config import settings
from app.main import app
from app.messaging.inbound import verify_meta_signature
from app.models.schemas import InboundWhatsAppMessage, TranscriptionResult
from app.services.dynamo_service import dynamo_service
from app.worker import Worker
from test_auth_portal import auth_on, bearer, make_token  # noqa: F401  (fixture import)
from test_pipeline_support import fakes, inbound, new_phone  # noqa: F401  (fixture import)


def cases_for(phone):
    return [c for c in dynamo_service.list_all_cases() if c.user_phone == phone]


# ====================================================================== 1. auth
def test_simulator_and_status_require_admin_when_auth_enabled(auth_on, fakes):
    c = TestClient(app)
    body = {"user_phone": new_phone(), "voice_text": "dil ka daura", "language": "Urdu"}
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    admin = bearer(make_token())

    assert c.post("/api/v1/webhook/simulate-voice", json=body).status_code == 401
    assert c.post("/api/v1/webhook/simulate-voice", json=body, headers=doctor).status_code == 403
    assert fakes.triage.calls == []          # nothing ran for the refused callers
    r = c.post("/api/v1/webhook/simulate-voice", json=body, headers=admin)
    assert r.status_code == 200 and r.json()["case"]["case_id"]

    status_url = f"/api/v1/webhook/messages/{r.json()['provider_message_id']}"
    assert c.get(status_url).status_code == 401
    assert c.get(status_url, headers=doctor).status_code == 403
    s = c.get(status_url, headers=admin)
    assert s.status_code == 200 and s.json()["status"] == "PROCESSED"


def test_meta_verification_handshake_stays_open_with_auth(auth_on):
    c = TestClient(app)
    r = c.get("/api/v1/webhook", params={"hub.mode": "subscribe", "hub.challenge": "42",
                                         "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN})
    assert r.status_code == 200 and r.text == "42"


def test_signature_refused_without_secret_on_real_aws(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    assert verify_meta_signature(b"{}", None) is False
    assert verify_meta_signature(b"{}", "sha256=deadbeef") is False
    r = TestClient(app).post("/api/v1/webhook", content=b'{"object": "whatsapp_business_account", "entry": []}',
                             headers={"Content-Type": "application/json"})
    assert r.status_code == 401


@pytest.mark.parametrize("mock_aws,endpoint", [(True, None), (False, "http://floci:4566"), (True, "http://floci:4566")])
def test_signature_permissive_without_secret_locally(monkeypatch, mock_aws, endpoint):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    monkeypatch.setattr(settings, "MOCK_AWS", mock_aws)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", endpoint)
    assert verify_meta_signature(b"{}", None) is True


# ====================================================================== 2. registration hint
# Emergency texts are never treated as only a registration: tests/test_registration.py.


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Help, my father collapsed, please hurry", "Chest pain, can't breathe, please help"])
async def test_new_user_first_emergency_text_with_commas_is_triaged(fakes, text):
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(phone, text=text))
    assert res["action"] == "triaged" and res["case_id"]
    assert "send_registration_format_hint" not in fakes.whatsapp.names()
    assert len(fakes.triage.calls) == 1


# ====================================================================== 3. same-sender race
class FakeSQSClient:
    def __init__(self):
        self.visibility = []

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.visibility.append((ReceiptHandle, VisibilityTimeout))


class FakeQueue:
    inline = False
    queue_url = "fake-queue"

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.deleted = []
        self.client = FakeSQSClient()

    def receive(self, n, w):
        b, self.bodies = self.bodies, []
        return [{"MessageId": str(i), "ReceiptHandle": f"rh{i}", "Body": body,
                 "Attributes": {"SentTimestamp": "1760000000000"}} for i, body in enumerate(b)]

    def delete(self, rh):
        self.deleted.append(rh)

    def release(self, rh):
        pass


def _parse(body):
    return [InboundWhatsAppMessage.model_validate_json(body)]


@pytest.fixture
def slow_triage(fakes, monkeypatch):
    """Makes the fake agent yield, so concurrent pipelines really interleave."""
    real = fakes.triage.evaluate_and_dispatch

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await real(*args, **kwargs)
    monkeypatch.setattr(fakes.triage, "evaluate_and_dispatch", slow)
    return fakes


@pytest.mark.asyncio
async def test_worker_batch_same_sender_opens_one_case_second_is_handover(slow_triage):
    fakes = slow_triage
    phone = new_phone()
    m1 = inbound(phone, type="audio", transcript="Mere abbu ko dil ka daura par raha hai")
    m2 = inbound(phone, type="audio", transcript="Jaldi karo, saans nahi aa rahi")
    q = FakeQueue([m1.model_dump_json(), m2.model_dump_json()])
    w = Worker(queue=q, process=pipeline.process_inbound_message, parse=_parse)
    await w.poll_once(wait_seconds=0)

    cases = cases_for(phone)
    assert len(cases) == 1
    assert fakes.triage.tools_fired.count("match_specialist_doctor") == 1
    r2 = dynamo_service.get_idempotency_record(pipeline.idempotency_key(m2.provider, m2.provider_message_id))
    assert r2["status"] == "COMPLETE"
    assert r2["result"] == {"case_id": cases[0].case_id, "action": "handover"}
    assert sorted(q.deleted) == ["rh0", "rh1"]


@pytest.mark.asyncio
async def test_worker_runs_different_senders_concurrently_same_sender_in_order():
    a, b = new_phone(), new_phone()
    msgs = [inbound(a, text="a1"), inbound(b, text="b1"), inbound(a, text="a2")]
    q = FakeQueue([m.model_dump_json() for m in msgs])
    running, peak, order = set(), [0], []

    async def proc(msg, received_at=None):
        running.add(msg.provider_message_id)
        peak[0] = max(peak[0], len(running))
        order.append(msg.text)
        await asyncio.sleep(0.02)
        running.discard(msg.provider_message_id)
        return {"status": "PROCESSED", "action": "x", "case_id": None}

    w = Worker(queue=q, process=proc, parse=_parse)
    groups = w.group_by_sender([{"Body": m.model_dump_json()} for m in msgs] + [{"Body": "garbage"}])
    assert [[_parse(g["Body"])[0].text if g["Body"] != "garbage" else "?" for g in grp] for grp in groups] == \
        [["a1", "a2"], ["b1"], ["?"]]
    await w.poll_once(wait_seconds=0)
    assert peak[0] == 2                              # a and b overlapped
    assert order.index("a1") < order.index("a2")     # a's messages in order
    assert len(q.deleted) == 3


@pytest.mark.asyncio
async def test_concurrent_pipelines_same_sender_inline_wait_then_handover(slow_triage):
    phone = new_phone()
    m1 = inbound(phone, type="audio", transcript="dil ka daura")
    m2 = inbound(phone, text="please hurry, he is not responding")
    r1, r2 = await asyncio.gather(pipeline.process_inbound_message(m1), pipeline.process_inbound_message(m2))
    assert r1["status"] == r2["status"] == "PROCESSED"
    assert r1["action"] == "triaged"
    assert r2["action"] == "handover" and r2["case_id"] == r1["case_id"]
    assert len(cases_for(phone)) == 1
    assert slow_triage.triage.tools_fired.count("match_specialist_doctor") == 1
    assert dynamo_service.get_idempotency_record(pipeline.sender_lease_key(phone)) is None   # released


@pytest.mark.asyncio
async def test_sender_busy_in_queue_mode_returns_in_progress_and_releases_message_claim(fakes, monkeypatch):
    monkeypatch.setattr(pipeline, "_inline_mode", lambda: False)
    phone = new_phone()
    lease = pipeline.sender_lease_key(phone)
    assert dynamo_service.claim_idempotency_key(lease, lease_seconds=300)
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    res = await pipeline.process_inbound_message(msg)
    assert res["status"] == "IN_PROGRESS" and res["reason"] == "sender_busy" and res["retry_after_seconds"] > 0
    assert dynamo_service.get_idempotency_record(pipeline.idempotency_key(msg.provider, msg.provider_message_id)) is None
    assert fakes.triage.calls == [] and cases_for(phone) == []

    # The worker leaves it on the queue and makes it visible again soon.
    q = FakeQueue([msg.model_dump_json()])
    await Worker(queue=q, process=pipeline.process_inbound_message, parse=_parse).poll_once(wait_seconds=0)
    assert q.deleted == [] and q.client.visibility == [("rh0", pipeline.SENDER_BUSY_RETRY_SECONDS)]

    dynamo_service.release_idempotency_key(lease)
    res = await pipeline.process_inbound_message(msg)
    assert res["status"] == "PROCESSED" and res["action"] == "triaged"


@pytest.mark.asyncio
async def test_sender_lease_released_when_pipeline_fails(fakes):
    phone = new_phone()
    fakes.triage.fail = True
    with pytest.raises(RuntimeError):
        await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="dil ka daura"))
    assert dynamo_service.get_idempotency_record(pipeline.sender_lease_key(phone)) is None


# ====================================================================== 4. follow-up triage
async def _human_review_case(fakes, phone):
    fakes.audio.transcription = TranscriptionResult(ok=False, error="transcription unavailable (mock)")
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", media_id="media-hr"))
    assert res["action"] == "human_review"
    fakes.audio.transcription = TranscriptionResult(ok=True, transcript="seene me dard hai", language="Urdu",
                                                    language_code="ur-PK", confidence=0.9)
    return res["case_id"]


@pytest.mark.asyncio
async def test_resend_after_human_review_escalates_same_case(fakes):
    phone = new_phone()
    case_id = await _human_review_case(fakes, phone)
    fakes.triage.esi = 1
    texts_before = len(fakes.whatsapp.texts())

    res = await pipeline.process_inbound_message(
        inbound(phone, text="Mere abbu ko dil ka daura para hai, seene me shadeed dard, saans nahi aa rahi"))
    assert res["action"] == "escalated" and res["case_id"] == case_id
    assert fakes.triage.calls[-1]["case_id"] == case_id
    assert len(cases_for(phone)) == 1
    case = dynamo_service.get_case(case_id)
    assert (case.esi_level, case.severity, case.status) == (1, "CRITICAL", "DOCTOR_HANDOVER")
    assert case.needs_human_review is False
    assert case.dispatched_at and case.location_requested_at
    assert case.triage_engine == "heuristic" and case.reasoning == "reasoning for ESI 1"
    assert "dil ka daura" in case.transcript and "Transcription failed" not in case.transcript
    assert any(t.get("tool") == "alert_emergency_contact" for t in case.tool_results)
    assert any(t.get("tool") == "match_specialist_doctor" and t.get("candidates", [{}])[0].get("doctor_id") == "DOC-GEN-99"
               for t in case.tool_results)                       # the original audit trail is kept
    # OFFER-1: the human-review round (ESI 2 timing) is superseded by an ESI 1 round.
    assert [r["outcome"] for r in case.offer_history] == ["SUPERSEDED", "PENDING"]
    assert case.offer_history[-1]["esi_level"] == 1 and case.doctor_offer_status == "PENDING"
    msgs = dynamo_service.get_case_messages(case_id)
    assert any(m.sender == "PATIENT" and "shadeed dard" in m.content for m in msgs)
    assert "EMERGENCY DETECTED" in fakes.whatsapp.texts()[texts_before]
    assert any(e["type"] == "case_upserted" and e["case"].status == "DOCTOR_HANDOVER" for e in fakes.events)

    # The emergency protocol ran: further messages are a plain handover.
    calls = len(fakes.triage.calls)
    res = await pipeline.process_inbound_message(inbound(phone, text="ambulance kab tak aayegi?"))
    assert res["action"] == "handover" and len(fakes.triage.calls) == calls


@pytest.mark.asyncio
async def test_followup_on_esi3_case_escalates_when_it_gets_worse(fakes):
    fakes.triage.esi = 3
    phone = new_phone()
    first = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="tez bukhar hai"))
    fakes.triage.esi = 2
    res = await pipeline.process_inbound_message(inbound(phone, text="ab woh behosh ho gaye hain"))
    assert res["action"] == "escalated" and res["case_id"] == first["case_id"]
    case = dynamo_service.get_case(first["case_id"])
    assert case.status == "DOCTOR_HANDOVER" and case.esi_level == 2 and case.dispatched_at
    assert case.location_requested_at
    assert "tez bukhar" in case.transcript and "[Follow-up]" in case.transcript
    assert len(cases_for(phone)) == 1


@pytest.mark.asyncio
async def test_followup_is_triaged_with_everything_the_patient_said_on_the_case(fakes):
    fakes.triage.esi = 3
    phone = new_phone()
    first = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="tez bukhar hai"))
    await pipeline.process_inbound_message(inbound(phone, text="gardan akar gayi hai"))
    await pipeline.process_inbound_message(inbound(phone, text="ab woh uljhan me hain"))

    triaged = fakes.triage.calls[-1]
    assert triaged["case_id"] == first["case_id"]
    assert triaged["transcript"] == ("[Earlier message] tez bukhar hai\n"
                                     "[Earlier message] gardan akar gayi hai\n"
                                     "[Latest message] ab woh uljhan me hain")
    # The stored transcript keeps its own format (original + follow-ups that changed the case).
    assert "[Earlier message]" not in dynamo_service.get_case(first["case_id"]).transcript


@pytest.mark.asyncio
async def test_followup_after_untranscribable_voice_note_is_triaged_alone(fakes):
    phone = new_phone()
    await _human_review_case(fakes, phone)
    fakes.triage.esi = 3
    await pipeline.process_inbound_message(inbound(phone, text="ulti aur tez bukhar hai"))
    assert fakes.triage.calls[-1]["transcript"] == "ulti aur tez bukhar hai"


@pytest.mark.asyncio
async def test_followup_esi3_updates_human_review_case_and_keeps_it_active(fakes):
    phone = new_phone()
    case_id = await _human_review_case(fakes, phone)
    fakes.triage.esi = 3
    res = await pipeline.process_inbound_message(inbound(phone, text="ulti aur tez bukhar hai"))
    assert res["action"] == "followup_triaged" and res["case_id"] == case_id
    case = dynamo_service.get_case(case_id)
    assert (case.esi_level, case.severity, case.status) == (3, "MODERATE", "ACTIVE")
    assert case.needs_human_review is False and case.clinical_summary == "summary for ESI 3"
    assert case.dispatched_at is None


@pytest.mark.asyncio
async def test_followup_esi5_never_downgrades_human_review_case(fakes):
    phone = new_phone()
    case_id = await _human_review_case(fakes, phone)
    before = dynamo_service.get_case(case_id)
    fakes.triage.esi = 5
    res = await pipeline.process_inbound_message(inbound(phone, text="thank you"))
    assert res["action"] == "handover" and res["case_id"] == case_id
    after = dynamo_service.get_case(case_id)
    assert (after.esi_level, after.status, after.needs_human_review) == (before.esi_level, "ACTIVE", True)
    assert any(m.content == "thank you" for m in dynamo_service.get_case_messages(case_id))


@pytest.mark.asyncio
async def test_untranscribable_followup_is_kept_without_triage(fakes):
    phone = new_phone()
    case_id = await _human_review_case(fakes, phone)
    fakes.audio.transcription = TranscriptionResult(ok=False, error="still unavailable")
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", media_id="media-2"))
    assert res["action"] == "handover" and res["case_id"] == case_id
    assert fakes.triage.calls == []


# ====================================================================== 5. leases, heartbeat, bot reply
@pytest.mark.asyncio
async def test_message_lease_outlives_sqs_visibility_timeout(fakes):
    """A redelivery after the 300 s visibility timeout, while the first attempt still runs,
    must see IN_PROGRESS rather than re-claim the message."""
    assert pipeline.MESSAGE_LEASE_SECONDS > 300
    msg = inbound(new_phone(), type="audio", transcript="dil ka daura")
    key = pipeline.idempotency_key(msg.provider, msg.provider_message_id)
    started = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    dynamo_service._mock_idempotency[key] = {"status": "IN_PROGRESS", "claimed_at": started, "result": None}
    res = await pipeline.process_inbound_message(msg)
    assert res["status"] == "IN_PROGRESS" and fakes.triage.calls == []


@pytest.mark.asyncio
async def test_worker_heartbeat_extends_visibility_while_processing(monkeypatch):
    monkeypatch.setattr(worker_mod, "HEARTBEAT_SECONDS", 0.01)
    msg = inbound(new_phone(), text="hi")
    q = FakeQueue([msg.model_dump_json()])

    async def slow(m, received_at=None):
        await asyncio.sleep(0.08)
        return {"status": "PROCESSED", "action": "x", "case_id": None}

    await Worker(queue=q, process=slow, parse=_parse).poll_once(wait_seconds=0)
    assert q.deleted == ["rh0"]
    assert q.client.visibility and all(v == ("rh0", worker_mod.VISIBILITY_EXTENSION_SECONDS) for v in q.client.visibility)
    n = len(q.client.visibility)
    await asyncio.sleep(0.05)
    assert len(q.client.visibility) == n       # heartbeat stopped with the message


@pytest.mark.asyncio
async def test_retry_stores_the_same_bot_reply_item(fakes):
    """A retry after the reply was stored (the claim was lost before COMPLETE) re-runs the triage
    of its own case and writes the bot reply under the same SK (MSG#<timestamp>#<id>)."""
    phone = new_phone()
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    first = await pipeline.process_inbound_message(msg)
    dynamo_service.release_idempotency_key(pipeline.idempotency_key(msg.provider, msg.provider_message_id))
    again = await pipeline.process_inbound_message(msg)
    assert again["action"] == "triaged" and again["case_id"] == first["case_id"]
    assert len(cases_for(phone)) == 1
    bots = [m for m in dynamo_service.get_case_messages(first["case_id"])
            if m.sender == "BOT" and m.sender_name == "ResQ-AI"]
    assert len(bots) == 2                                    # the reply and the location request, once each:
    assert len({(m.message_id, m.timestamp) for m in bots}) == 2   # same message_ids, replaced by key
    patient = [m for m in dynamo_service.get_case_messages(first["case_id"]) if m.sender == "PATIENT"]
    assert min(m.timestamp for m in bots) > patient[0].timestamp


# ====================================================================== 6. websocket refusal
def test_bridge_refusal_is_accept_then_1008(monkeypatch):
    from app import auth
    from starlette.websockets import WebSocketDisconnect

    async def refuse(websocket):
        return None
    monkeypatch.setattr(auth, "authorize_websocket", refuse)
    c = TestClient(app)
    for path in ("/ws/cases", "/ws/bridge/CASE-ANY"):
        with c.websocket_connect(path) as ws:            # handshake succeeds (no 1006 in a browser)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008
