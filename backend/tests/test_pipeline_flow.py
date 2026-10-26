"""Pipeline routing: onboarding (FLOW-1), handover (FLOW-2), ESI handling (AGENT-5),
transcription failure (SPEECH-1), idempotency (INFRA-6) and case fields (DATA-1/2/4)."""
import pytest

from app import pipeline
from app.services.dynamo_service import dynamo_service
from test_pipeline_support import fakes, inbound, new_phone  # noqa: F401  (fixture import)


def cases_for(phone):
    return [c for c in dynamo_service.list_all_cases() if c.user_phone == phone]


# Reading registrations (free-form text and voice notes): tests/test_registration.py.


# ---------------------------------------------------------------- FLOW-1
@pytest.mark.asyncio
async def test_new_user_registration_reply_updates_profile_and_creates_no_case(fakes):
    phone = new_phone()
    r1 = await pipeline.process_inbound_message(inbound(phone, text="Hi", sender_name="Ahmed"))
    assert r1["action"] == "greeting" and r1["case_id"] is None
    user = dynamo_service.get_user(phone)
    assert user.onboarding_status == "PENDING_DETAILS"
    assert user.emergency_contact_phone is None
    assert user.name == "Ahmed"
    assert "send_onboarding_prompt" in fakes.whatsapp.names()

    r2 = await pipeline.process_inbound_message(inbound(phone, text="Ahmed Khan, Brother Ali, +923331234567"))
    assert r2["status"] == "PROCESSED" and r2["action"] == "registered" and r2["case_id"] is None
    user = dynamo_service.get_user(phone)
    assert user.onboarding_status == "COMPLETE"
    assert (user.name, user.emergency_contact_name, user.emergency_contact_phone) == \
        ("Ahmed Khan", "Brother Ali", "+923331234567")
    assert "send_registration_confirmation" in fakes.whatsapp.names()
    assert cases_for(phone) == []
    assert fakes.triage.calls == []


@pytest.mark.asyncio
async def test_new_user_without_name_gets_caller_placeholder(fakes):
    phone = new_phone()
    await pipeline.process_inbound_message(inbound(phone, text="salam"))
    assert dynamo_service.get_user(phone).name == f"Caller {phone[-4:]}"


@pytest.mark.asyncio
async def test_voice_note_before_registration_is_still_triaged(fakes):
    phone = new_phone()
    res = await pipeline.process_inbound_message(
        inbound(phone, type="audio", transcript="Mere uncle ko dil ka daura par raha hai"))
    assert res["action"] == "triaged"
    assert len(fakes.triage.calls) == 1
    assert fakes.triage.calls[0]["emergency_contact_phone"] is None
    case = dynamo_service.get_case(res["case_id"])
    assert case.status == "DOCTOR_HANDOVER" and case.severity == "CRITICAL"
    assert case.contact_call_id is None
    assert "alert_emergency_contact" not in fakes.triage.tools_fired
    # Emergency reply first, then the onboarding prompt; the reply says no contact was called.
    names = fakes.whatsapp.names()
    assert names.index("send_text_message") < names.index("send_onboarding_prompt")
    reply = fakes.whatsapp.texts()[0]
    assert "none registered" in reply and "automated call placed" not in reply
    assert dynamo_service.get_user(phone).active_case_id == case.case_id


@pytest.mark.asyncio
async def test_incomplete_number_gets_hint(fakes):
    phone = new_phone()
    await pipeline.process_inbound_message(inbound(phone, text="hello"))
    res = await pipeline.process_inbound_message(inbound(phone, text="Ahmed Khan, Brother Ali, 0333 12"))
    assert res["action"] == "registration_hint"
    assert "send_registration_format_hint" in fakes.whatsapp.names()
    assert dynamo_service.get_user(phone).onboarding_status == "PENDING_DETAILS"
    assert fakes.triage.calls == [] and cases_for(phone) == []


@pytest.mark.asyncio
async def test_pending_user_emergency_text_is_triaged_not_hinted(fakes):
    phone = new_phone()
    await pipeline.process_inbound_message(inbound(phone, text="hello"))
    res = await pipeline.process_inbound_message(
        inbound(phone, text="Bhaiya highway par car accident ho gaya hai, sar se bahut khoon beh raha hai"))
    assert res["action"] == "triaged"
    assert "send_registration_format_hint" not in fakes.whatsapp.names()


# ---------------------------------------------------------------- FLOW-2
async def _open_case(fakes, phone):
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="dil ka daura"))
    return res["case_id"]


@pytest.mark.asyncio
async def test_second_message_goes_to_open_case_chat_without_triage(fakes):
    phone = new_phone()
    case_id = await _open_case(fakes, phone)
    tools_before = list(fakes.triage.tools_fired)

    res = await pipeline.process_inbound_message(inbound(phone, text="He is now unconscious, please hurry"))
    assert res["action"] == "handover" and res["case_id"] == case_id
    assert len(fakes.triage.calls) == 1                 # no second triage
    assert fakes.triage.tools_fired == tools_before     # no tools fired
    assert len(cases_for(phone)) == 1                   # no new case
    msgs = dynamo_service.get_case_messages(case_id)
    assert any(m.sender == "PATIENT" and "unconscious" in m.content for m in msgs)
    assert any(e["type"] == "chat_message" and "unconscious" in e["message"].content for e in fakes.events)


@pytest.mark.asyncio
async def test_handover_voice_note_is_transcribed_and_keeps_audio_key(fakes):
    phone = new_phone()
    case_id = await _open_case(fakes, phone)
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", media_id="media-777"))
    assert res["action"] == "handover"
    msg = [m for m in dynamo_service.get_case_messages(case_id) if m.provider_message_id][-1]
    assert msg.audio_s3_key.endswith("media-777.ogg")
    assert "seene me dard hai" in msg.content
    assert len(fakes.triage.calls) == 1


@pytest.mark.asyncio
async def test_resolve_clears_active_case_and_next_message_is_triaged(fakes):
    phone = new_phone()
    case_id = await _open_case(fakes, phone)
    resolved = await pipeline.resolve_case(case_id, resolved_by="dr@example.com")
    assert resolved.status == "RESOLVED" and resolved.resolved_at
    assert dynamo_service.get_user(phone).active_case_id is None
    assert dynamo_service.get_open_case_for_user(phone) is None
    assert any(e["type"] == "case_upserted" and e["case"].status == "RESOLVED" for e in fakes.events)

    res = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="dil ka daura again"))
    assert res["action"] == "triaged" and res["case_id"] != case_id


@pytest.mark.asyncio
async def test_registration_while_case_open_updates_profile_and_notes_case(fakes):
    phone = new_phone()
    case_id = await _open_case(fakes, phone)
    res = await pipeline.process_inbound_message(inbound(phone, text="Ahmed Khan, Brother Ali, +923331234567"))
    assert res["action"] == "registered" and res["case_id"] == case_id
    assert dynamo_service.get_user(phone).emergency_contact_phone == "+923331234567"
    assert dynamo_service.get_user(phone).active_case_id == case_id
    assert dynamo_service.get_case(case_id).emergency_contact_phone == "+923331234567"


# ---------------------------------------------------------------- AGENT-5
@pytest.mark.asyncio
async def test_esi3_creates_active_moderate_case_without_emergency_protocol(fakes):
    fakes.triage.esi = 3
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="tez bukhar hai"))
    case = dynamo_service.get_case(res["case_id"])
    assert case.status == "ACTIVE" and case.severity == "MODERATE" and case.esi_level == 3
    assert fakes.triage.tools_fired == []
    assert case.contact_call_id is None and case.dispatched_at is None
    assert case.location_requested_at is None
    assert not [c for c in fakes.whatsapp.calls if c[0] == "send_location_request"]
    # A doctor still reviews it: offered with the ESI 3 timeout, assigned on accept (OFFER-1).
    assert case.assigned_doctor_id is None and case.doctor_offer_status == "PENDING"
    assert case.offered_doctor_ids and case.offer_history[0]["esi_level"] == 3
    assert dynamo_service.get_user(phone).active_case_id == case.case_id
    reply = fakes.whatsapp.texts()[0]
    assert "urgent care" in reply and "stood down" not in reply


@pytest.mark.asyncio
async def test_esi5_is_dismissed_and_not_left_open(fakes):
    fakes.triage.esi = 5
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="halka sar dard"))
    case = dynamo_service.get_case(res["case_id"])
    assert case.status == "DISMISSED" and case.severity == "LOW"
    assert dynamo_service.get_user(phone).active_case_id is None
    assert fakes.triage.tools_fired == []


# ---------------------------------------------------------------- SPEECH-1
@pytest.mark.asyncio
async def test_transcription_failure_creates_human_review_case_and_calls_nobody(fakes):
    from app.models.schemas import TranscriptionResult
    fakes.audio.transcription = TranscriptionResult(ok=False, error="transcription unavailable (Floci/mock)")
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", media_id="media-x"))
    assert res["action"] == "human_review"
    case = dynamo_service.get_case(res["case_id"])
    assert case.needs_human_review is True
    assert (case.esi_level, case.severity, case.status) == (2, "HIGH", "ACTIVE")
    assert "transcription unavailable" in case.transcript
    assert case.audio_s3_key.endswith("media-x.ogg")
    assert fakes.triage.calls == [] and fakes.triage.tools_fired == []
    assert case.contact_call_id is None and case.dispatched_at is None
    assert case.location_requested_at  # the reviewing doctor may need to send an ambulance
    assert fakes.doctor_matches[0]["required_specialty"] == "General Emergency"
    assert case.doctor_offer_status == "PENDING" and len(case.offered_doctor_ids) == 3   # ESI 2: three doctors
    assert "A doctor is being alerted" in fakes.whatsapp.texts()[0]
    assert "send the voice note again" in fakes.whatsapp.texts()[0]


# ---------------------------------------------------------------- INFRA-6 idempotency
@pytest.mark.asyncio
async def test_duplicate_provider_message_id_is_processed_once(fakes):
    phone = new_phone()
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    first = await pipeline.process_inbound_message(msg)
    second = await pipeline.process_inbound_message(msg)
    assert first["status"] == "PROCESSED"
    assert second["status"] == "DUPLICATE" and second["case_id"] == first["case_id"]
    assert len(fakes.triage.calls) == 1
    assert fakes.triage.tools_fired.count("match_specialist_doctor") == 1
    assert len(cases_for(phone)) == 1


@pytest.mark.asyncio
async def test_failure_releases_claim_and_retry_reuses_case_id(fakes):
    phone = new_phone()
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    fakes.triage.fail = True
    with pytest.raises(RuntimeError):
        await pipeline.process_inbound_message(msg)
    assert dynamo_service.get_idempotency_record(pipeline.idempotency_key(msg.provider, msg.provider_message_id)) is None
    fakes.triage.fail = False
    res = await pipeline.process_inbound_message(msg)
    assert res["status"] == "PROCESSED"
    # Same case ID on both attempts: the agent's per-case tool idempotency covers partial failures.
    assert fakes.triage.calls[0]["case_id"] == fakes.triage.calls[1]["case_id"] == res["case_id"]


# ---------------------------------------------------------------- DATA-1 / DATA-2 / DATA-4
@pytest.mark.asyncio
async def test_case_copies_assessment_audit_fields_and_tool_ids(fakes):
    phone = new_phone()
    dynamo_service.save_user(dynamo_service.get_user("+923009988776").model_copy(
        update={"phone_number": phone, "active_case_id": None}))
    msg = inbound(phone, type="audio", transcript="dil ka daura", provider="simulator")
    res = await pipeline.process_inbound_message(msg, received_at="2026-10-11T10:00:00+00:00")
    case = dynamo_service.get_case(res["case_id"])
    call = fakes.triage.calls[0]
    assert call["case_id"] == case.case_id and call["emergency_contact_phone"] == "+923331122334"
    assert case.clinical_summary == "summary for ESI 1" and case.reasoning == "reasoning for ESI 1"
    assert case.confidence_score == 0.91 and case.triage_engine == "heuristic" and case.degraded is False
    assert case.contact_call_id == f"CONN-{case.case_id[-6:]}"          # the tool's ID, not a new one
    assert case.assigned_doctor_id is None and case.doctor_offer_status == "PENDING"   # offered, not assigned
    assert [t["tool"] for t in case.tool_results] == ["alert_emergency_contact", "match_specialist_doctor"]
    assert case.provider == "simulator" and case.provider_message_id == msg.provider_message_id
    assert case.received_at == "2026-10-11T10:00:00+00:00" and case.dispatched_at is not None
    assert "automated call placed" in fakes.whatsapp.texts()[0]
    assert any(e["type"] == "case_upserted" and e["case_id"] == case.case_id for e in fakes.events)


def test_pipeline_does_not_touch_private_doctor_store():
    import inspect
    assert "_mock_doctors" not in inspect.getsource(pipeline)


@pytest.mark.asyncio
async def test_unsupported_message_gets_reply_and_no_case(fakes):
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(phone, type="unsupported"))
    assert res["action"] == "unsupported" and cases_for(phone) == []
    assert any("voice note" in t for t in fakes.whatsapp.texts())


@pytest.mark.asyncio
async def test_retry_after_case_saved_updates_in_place_and_keeps_video_fields(fakes, monkeypatch):
    """A retry must not overwrite fields other code paths wrote (VIDEO-1) with a fresh model."""
    phone = new_phone()
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    real_save_message = dynamo_service.save_chat_message
    calls = {"n": 0}

    def flaky_save_message(m):
        if m.sender == "PATIENT":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DynamoDB hiccup after the case was saved")
        return real_save_message(m)

    monkeypatch.setattr(dynamo_service, "save_chat_message", flaky_save_message)
    with pytest.raises(RuntimeError):
        await pipeline.process_inbound_message(msg)
    case_id = pipeline.case_id_for(msg)
    dynamo_service.update_case(case_id, {"video_meeting_id": "MTG-1", "video_room_url": "https://x/call/MTG-1"})

    res = await pipeline.process_inbound_message(msg)
    assert res["status"] == "PROCESSED" and res["case_id"] == case_id
    case = dynamo_service.get_case(case_id)
    assert case.video_meeting_id == "MTG-1" and case.video_room_url == "https://x/call/MTG-1"
    assert len(cases_for(phone)) == 1
