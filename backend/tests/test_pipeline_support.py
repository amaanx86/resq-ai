"""Shared fakes for the pipeline / worker / webhook tests (stream B2). No tests here.

The pipeline's collaborators from other streams (triage agent, WhatsApp facade, speech) are
replaced with recording fakes, so these tests don't depend on those streams' internals.
"""
import random
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app import doctor_chat, doctor_offers, pipeline
from app.agents.image_agent import ImageAnalysis
from app.models.schemas import AudioObject, InboundWhatsAppMessage, TranscriptionResult, TriageAssessment


def new_phone() -> str:
    return "+92300" + "".join(random.choice("0123456789") for _ in range(7))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def inbound(phone: str, *, type: str = "text", text: Optional[str] = None, transcript: Optional[str] = None,
            media_id: Optional[str] = None, provider: str = "meta", message_id: Optional[str] = None,
            sender_name: Optional[str] = None, caption: Optional[str] = None) -> InboundWhatsAppMessage:
    return InboundWhatsAppMessage(
        provider=provider,
        provider_message_id=message_id or f"wamid.{uuid.uuid4().hex}",
        sender_phone=phone,
        sender_name=sender_name,
        timestamp=now_iso(),
        type=type,
        text=text,
        media_id=media_id or ("media-123" if type in ("audio", "image") and transcript is None else None),
        media_mime_type="image/jpeg" if type == "image" else None,
        caption=caption,
        simulated_transcript=transcript,
        simulated_language="Urdu" if transcript is not None else None,
    )


class FakeWhatsApp:
    provider_name = "mock"

    def __init__(self):
        self.calls: List[tuple] = []

    def __getattr__(self, name: str):
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def _record(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            self.calls.append((name, args, kwargs))
            return {"status": "SENT_MOCK"}
        return _record

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject:
        self.calls.append(("fetch_media_to_s3", (media_id, user_phone), {"kind": kind, "mime_type": mime_type}))
        if kind == "image":
            key = f"images/{user_phone.lstrip('+')}/{media_id}.jpg"
        else:
            key = f"voice-notes/{user_phone.lstrip('+')}/{media_id}.ogg"
        return AudioObject(key=key, s3_uri=f"s3://resq-raw-audio-vault/{key}")

    def names(self) -> List[str]:
        return [c[0] for c in self.calls]

    def texts(self) -> List[str]:
        return [c[1][1] for c in self.calls if c[0] == "send_text_message"]


class FakeAudio:
    def __init__(self):
        self.transcription = TranscriptionResult(ok=True, transcript="seene me dard hai", language="Urdu",
                                                 language_code="ur-PK", confidence=0.9)
        self.transcribe_calls: List[str] = []

    async def transcribe_audio(self, s3_uri: str) -> TranscriptionResult:
        self.transcribe_calls.append(s3_uri)
        return self.transcription

    def read_media_bytes(self, key: str) -> bytes:
        return b"\xff\xd8 fake jpeg " + key.encode()

    def detect_language(self, text: str):
        return ("English", 0.8)


class FakeImageAnalyzer:
    """Stands in for the image analysis agent (IMAGE-1); records what it was shown."""

    def __init__(self):
        self.analysis = ImageAnalysis(
            ok=True,
            description="A deep cut about 5 cm long on the left forearm, bleeding heavily.",
            clinical_summary="Deep forearm laceration with active bleeding.",
            findings=["5 cm laceration, left forearm", "active bleeding"],
            red_flags=["heavy active bleeding"],
            shows_medical_concern=True,
            image_quality="clear",
            engine="bedrock:fake-vision",
        )
        self.calls: List[Dict[str, Any]] = []

    async def analyze(self, data: bytes, mime_type: Optional[str], caption: Optional[str] = None) -> ImageAnalysis:
        self.calls.append(dict(data=data, mime_type=mime_type, caption=caption))
        return self.analysis


class FakeTriage:
    """Returns a canned assessment for `esi`; records calls and the emergency tools it 'fired'."""
    mock_mode = True

    def __init__(self, esi: int = 1):
        self.esi = esi
        self.calls: List[Dict[str, Any]] = []
        self.tools_fired: List[str] = []
        self.fail = False

    async def evaluate_and_dispatch(self, transcript, user_phone, patient_name, emergency_contact_phone,
                                    detected_language="Urdu", case_id=None) -> TriageAssessment:
        self.calls.append(dict(transcript=transcript, user_phone=user_phone, patient_name=patient_name,
                               emergency_contact_phone=emergency_contact_phone,
                               detected_language=detected_language, case_id=case_id))
        if self.fail:
            raise RuntimeError("agent exploded")
        esi = self.esi
        emergency = esi <= 2
        tool_results: List[Dict[str, Any]] = []
        call_id = None
        if emergency:
            if emergency_contact_phone:
                call_id = f"CONN-{case_id[-6:]}"
                self.tools_fired.append("alert_emergency_contact")
                tool_results.append({"tool": "alert_emergency_contact", "status": "SUCCESS", "contact_id": call_id})
            else:
                tool_results.append({"tool": "alert_emergency_contact", "status": "SKIPPED_NO_CONTACT"})
            self.tools_fired.append("match_specialist_doctor")
            tool_results.append({"tool": "match_specialist_doctor", "status": "OFFER_PENDING",
                                 "candidates": [{"doctor_id": "DOC-CARDIO-01", "doctor_name": "Dr. Sarah Khan, MD"}]})
        return TriageAssessment(
            is_emergency=emergency,
            esi_level=esi,
            confidence_score=0.91,
            detected_language=detected_language,
            clinical_summary=f"summary for ESI {esi}",
            matched_specialty="Cardiology" if esi <= 3 else None,
            emergency_contact_called=bool(call_id),
            contact_call_id=call_id,
            reasoning=f"reasoning for ESI {esi}",
            degraded=False,
            triage_engine="heuristic",
            tool_results=tool_results,
        )


@pytest.fixture
def fakes(monkeypatch):
    wa, audio, triage, images = FakeWhatsApp(), FakeAudio(), FakeTriage(), FakeImageAnalyzer()
    doctor_matches: List[Dict[str, Any]] = []

    async def fake_match(required_specialty, patient_phone, clinical_summary, case_id=None):
        doctor_matches.append(dict(required_specialty=required_specialty, patient_phone=patient_phone))
        return {"tool": "match_specialist_doctor", "status": "OFFER_PENDING", "requested_specialty": required_specialty,
                "candidates": [{"doctor_id": "DOC-GEN-99", "doctor_name": "Dr. Review Doctor"}]}

    events: List[Dict[str, Any]] = []
    real_publish_chat = pipeline.events_service.publish_chat_message
    real_publish_case = pipeline.events_service.publish_case

    async def rec_chat(msg):
        events.append({"type": "chat_message", "case_id": msg.case_id, "message": msg})
        await real_publish_chat(msg)

    async def rec_case(case):
        events.append({"type": "case_upserted", "case_id": case.case_id, "case": case})
        await real_publish_case(case)

    monkeypatch.setattr(pipeline, "whatsapp_service", wa)
    # Doctor offers (OFFER-1) send to doctors and, on accept / exhaust, to the patient: recorded apart,
    # so the patient-flow tests see only the pipeline's own messages.
    offer_wa = FakeWhatsApp()
    monkeypatch.setattr(doctor_offers, "whatsapp_service", offer_wa)
    # Doctors' own WhatsApp (DOC-*): case cards, forwarded patient messages, command replies.
    monkeypatch.setattr(doctor_chat, "whatsapp_service", offer_wa)
    # Number confirmation (DOC-8): the question and its replies go to the doctor too.
    from app import doctor_verification
    monkeypatch.setattr(doctor_verification, "whatsapp_service", offer_wa)
    monkeypatch.setattr(pipeline, "audio_service", audio)
    monkeypatch.setattr(pipeline, "image_analyzer", images)
    monkeypatch.setattr(pipeline, "triage_agent", triage)
    monkeypatch.setattr(pipeline, "match_specialist_doctor_tool", fake_match)
    monkeypatch.setattr(pipeline.events_service, "publish_chat_message", rec_chat)
    monkeypatch.setattr(pipeline.events_service, "publish_case", rec_case)
    return SimpleNamespace(whatsapp=wa, offer_whatsapp=offer_wa, audio=audio, triage=triage, images=images,
                           doctor_matches=doctor_matches, events=events)


async def accept_as(case_id: str, doctor_id: str = None):
    """Accepts the case offer as a doctor of the current round (default: the first offered one)."""
    from app.services.dynamo_service import dynamo_service
    case = dynamo_service.get_case(case_id)
    doctor = dynamo_service.get_doctor(doctor_id or case.offered_doctor_ids[0])
    outcome, updated = await doctor_offers.accept_offer(case_id, case.offer_id, doctor)
    assert outcome == doctor_offers.ACCEPTED, outcome
    return updated
