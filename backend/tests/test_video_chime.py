"""VIDEO-1: Chime video sessions, the join endpoint, and real-mode Chime calls (Stubber)."""
import asyncio
import uuid
from urllib.parse import urlparse, parse_qs

import boto3
import pytest
from botocore.stub import Stubber, ANY
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.models.schemas import EmergencyCase
from app.routers import video
from app.services.chime_service import ChimeService, chime_service, is_simulated_meeting
from app.services.dynamo_service import dynamo_service

REAL_MEETING_ID = "11111111-2222-3333-4444-555555555555"


def run(coro):
    return asyncio.run(coro)


def make_case(**overrides) -> EmergencyCase:
    case = EmergencyCase(
        case_id=f"CASE-{uuid.uuid4().hex[:8].upper()}",
        user_phone="+923001234567",
        patient_name="Video Test Patient",
        transcript="seene mein dard",
        detected_language="Urdu",
        esi_level=1,
        severity="CRITICAL",
        status="DOCTOR_HANDOVER",
        matched_specialty="Cardiology",
        assigned_doctor_name="Dr. Test",
        **overrides,
    )
    dynamo_service.save_case(case)
    return case


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(video.router)
    return TestClient(app)


def real_meeting(meeting_id=REAL_MEETING_ID, case_id="CASE-REAL"):
    return {
        "MeetingId": meeting_id,
        "ExternalMeetingId": case_id,
        "MediaRegion": "us-east-1",
        "MediaPlacement": {
            "AudioHostUrl": "abc.k.m1.ue1.app.chime.aws:3478",
            "AudioFallbackUrl": "wss://haxrp.m1.ue1.app.chime.aws:443/calls/x",
            "SignalingUrl": "wss://signal.m1.ue1.app.chime.aws/control/x",
            "TurnControlUrl": "https://2713.cell.us-east-1.meetings.chime.aws/v2/turn_sessions",
        },
    }


def real_service():
    svc = ChimeService()
    svc.mock_mode = False
    svc.chime = boto3.client(
        "chime-sdk-meetings", region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
    )
    return svc


# ---------------------------------------------------------------- session creation
def test_session_persists_meeting_and_token_and_builds_urls():
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))

    assert set(["meeting_id", "patient_join_url", "doctor_join_url", "status", "doctor_name"]) <= set(session)
    assert session["status"] == "READY" and session["doctor_name"] == "Dr. Test"
    assert session["mock"] is True

    saved = dynamo_service.get_case(case.case_id)
    assert saved.video_meeting_id == session["meeting_id"]
    assert saved.video_meeting["MeetingId"] == session["meeting_id"]
    assert is_simulated_meeting(saved.video_meeting)
    assert saved.video_patient_token and len(saved.video_patient_token) >= 24
    assert saved.video_room_url == session["patient_join_url"]

    base = settings.PORTAL_BASE_URL.rstrip("/")
    token = saved.video_patient_token
    assert session["patient_join_url"] == (
        f"{base}/call/{session['meeting_id']}?role=patient&case={case.case_id}&token={token}"
    )
    assert session["doctor_join_url"] == f"{base}/call/{session['meeting_id']}?role=doctor&case={case.case_id}"
    assert "token" not in session["doctor_join_url"]


def test_session_reuses_live_meeting():
    case = make_case()
    first = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    second = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    assert second["meeting_id"] == first["meeting_id"]
    assert second["patient_join_url"] == first["patient_join_url"]
    assert second["reused"] is True


def test_session_before_case_exists_is_flushed_later():
    case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
    session = run(chime_service.create_emergency_video_session(case_id, "Dr. Test"))
    assert dynamo_service.get_case(case_id) is None
    dynamo_service.save_case(EmergencyCase(
        case_id=case_id, user_phone="+923001234567", patient_name="Late", transcript="x",
        detected_language="Urdu", esi_level=1, severity="CRITICAL", status="DOCTOR_HANDOVER",
    ))
    case = chime_service.get_case_with_video(case_id)
    assert case.video_meeting_id == session["meeting_id"]
    assert case.video_patient_token in session["patient_join_url"]


# ---------------------------------------------------------------- join endpoint
def _join(client, session, case_id, role, token=None):
    params = {"role": role, "case": case_id}
    if token is not None:
        params["token"] = token
    return client.post(f"/api/v1/video/{session['meeting_id']}/join", params=params)


def test_patient_join_with_valid_token(client):
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    token = parse_qs(urlparse(session["patient_join_url"]).query)["token"][0]

    res = _join(client, session, case.case_id, "patient", token)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mock"] is True
    assert body["role"] == "patient" and body["case_id"] == case.case_id
    assert body["Meeting"]["MeetingId"] == session["meeting_id"]
    assert body["Attendee"]["AttendeeId"] and body["Attendee"]["JoinToken"]
    assert body["Attendee"]["ExternalUserId"] == f"patient-{case.case_id}"


def test_patient_join_with_bad_or_missing_token(client):
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    assert _join(client, session, case.case_id, "patient", "wrong-token").status_code == 403
    assert _join(client, session, case.case_id, "patient").status_code == 403


def test_join_unknown_case_or_meeting(client):
    res = client.post("/api/v1/video/MEET-NOPE/join", params={"role": "doctor", "case": "CASE-DOES-NOT-EXIST"})
    assert res.status_code == 404

    case = make_case()
    run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    res = client.post("/api/v1/video/MEET-OTHER/join", params={"role": "doctor", "case": case.case_id})
    assert res.status_code == 404


def test_join_rejects_unknown_role(client):
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    assert _join(client, session, case.case_id, "admin").status_code == 400


def test_doctor_join_uses_portal_auth(client, monkeypatch):
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))
    calls = []

    async def fake_portal_auth(request):
        calls.append("auth")
        return auth.LOCAL_PRINCIPAL

    def fake_visible(request, c):
        calls.append(("visible", c.case_id))

    monkeypatch.setattr(auth, "portal_auth", fake_portal_auth)
    monkeypatch.setattr(auth, "assert_case_visible", fake_visible)

    res = _join(client, session, case.case_id, "doctor")
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "doctor"
    assert res.json()["Attendee"]["ExternalUserId"] == f"doctor-{case.case_id}"
    assert calls == ["auth", ("visible", case.case_id)]


def test_doctor_join_denied_when_case_not_visible(client, monkeypatch):
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))

    def deny(request, c):
        raise HTTPException(status_code=403, detail="Not your case")

    monkeypatch.setattr(auth, "assert_case_visible", deny)
    assert _join(client, session, case.case_id, "doctor").status_code == 403


def test_doctor_join_requires_login(client, monkeypatch):
    case = make_case()
    session = run(chime_service.create_emergency_video_session(case.case_id, "Dr. Test"))

    async def unauth(request):
        raise HTTPException(status_code=401, detail="Login required")

    monkeypatch.setattr(auth, "portal_auth", unauth)
    assert _join(client, session, case.case_id, "doctor").status_code == 401


# ---------------------------------------------------------------- real mode (Stubber)
def test_real_mode_create_meeting_and_attendee(monkeypatch):
    svc = real_service()
    case = make_case()
    meeting = real_meeting(case_id=case.case_id)

    with Stubber(svc.chime) as stub:
        stub.add_response(
            "create_meeting", {"Meeting": meeting},
            {"ClientRequestToken": ANY, "MediaRegion": settings.AWS_REGION, "ExternalMeetingId": case.case_id},
        )
        session = run(svc.create_emergency_video_session(case.case_id, "Dr. Test"))
        assert session["meeting_id"] == REAL_MEETING_ID
        assert session["mock"] is False

        saved = dynamo_service.get_case(case.case_id)
        assert saved.video_meeting == meeting

        attendee = {
            "ExternalUserId": f"patient-{case.case_id}",
            "AttendeeId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "JoinToken": "join-token-value",
        }
        stub.add_response(
            "create_attendee", {"Attendee": attendee},
            {"MeetingId": REAL_MEETING_ID, "ExternalUserId": f"patient-{case.case_id}"},
        )
        joined = run(svc.create_attendee(saved, "patient", f"patient-{case.case_id}"))
        assert joined == {"Meeting": meeting, "Attendee": attendee, "mock": False}
        stub.assert_no_pending_responses()


def test_real_mode_reuses_live_meeting_and_replaces_ended_one():
    svc = real_service()
    case = make_case()
    meeting = real_meeting(case_id=case.case_id)
    new_meeting = real_meeting(meeting_id="99999999-2222-3333-4444-555555555555", case_id=case.case_id)

    with Stubber(svc.chime) as stub:
        stub.add_response("create_meeting", {"Meeting": meeting})
        first = run(svc.create_emergency_video_session(case.case_id, "Dr. Test"))

        # Live: get_meeting succeeds, nothing new is created.
        stub.add_response("get_meeting", {"Meeting": meeting}, {"MeetingId": REAL_MEETING_ID})
        again = run(svc.create_emergency_video_session(case.case_id, "Dr. Test"))
        assert again["meeting_id"] == first["meeting_id"] and again["reused"] is True

        # Ended: get_meeting 404 -> a new meeting and a new patient token.
        old_token = dynamo_service.get_case(case.case_id).video_patient_token
        stub.add_client_error("get_meeting", service_error_code="NotFoundException", http_status_code=404)
        stub.add_response("create_meeting", {"Meeting": new_meeting})
        fresh = run(svc.create_emergency_video_session(case.case_id, "Dr. Test"))
        assert fresh["meeting_id"] == new_meeting["MeetingId"] and fresh["reused"] is False
        saved = dynamo_service.get_case(case.case_id)
        assert saved.video_meeting_id == new_meeting["MeetingId"]
        assert saved.video_patient_token != old_token
        stub.assert_no_pending_responses()


def test_real_mode_ended_meeting_returns_410(client, monkeypatch):
    svc = real_service()
    case = make_case()
    with Stubber(svc.chime) as stub:
        stub.add_response("create_meeting", {"Meeting": real_meeting(case_id=case.case_id)})
        session = run(svc.create_emergency_video_session(case.case_id, "Dr. Test"))
        stub.add_client_error("create_attendee", service_error_code="NotFoundException", http_status_code=404)
        monkeypatch.setattr(video, "chime_service", svc)
        res = _join(client, session, case.case_id, "doctor")
        assert res.status_code == 410


def test_real_mode_create_meeting_failure_falls_back_to_simulated():
    svc = real_service()
    case = make_case()
    with Stubber(svc.chime) as stub:
        stub.add_client_error("create_meeting", service_error_code="ForbiddenException", http_status_code=403)
        session = run(svc.create_emergency_video_session(case.case_id, "Dr. Test"))
    assert session["mock"] is True
    assert is_simulated_meeting(dynamo_service.get_case(case.case_id).video_meeting)
