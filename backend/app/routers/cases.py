import asyncio
import uuid
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from typing import List, Optional
from app import admin_alerts, auth, case_messages, direct_calls, doctor_offers, doctor_verification
from app.config import settings
from app.models.schemas import DoctorProfile, EmergencyCase, ChatMessage, SendChatMessageRequest, VideoSessionResponse
from app.pipeline import mark_ambulance_called
from app.pipeline import resolve_case as resolve_case_in_pipeline
from app.services import events_service
from app.services.dynamo_service import dynamo_service, OPEN_CASE_STATUSES
from app.services.chime_service import chime_service
from app.services.whatsapp_service import whatsapp_service, with_title

router = APIRouter(prefix="/api/v1/cases", tags=["Emergency Cases"])


def _visible_case_or_404(request: Request, case_id: str) -> EmergencyCase:
    case = dynamo_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    auth.assert_case_visible(request, case)
    return case


@router.get("/active", response_model=List[EmergencyCase])
def get_active_cases(request: Request):
    """Retrieves all active emergency cases waiting for doctor intervention"""
    return auth.visible_cases(request, dynamo_service.list_active_cases())

@router.get("", response_model=List[EmergencyCase])
def get_all_cases(request: Request):
    """Retrieves all cases for executive auditing and conversation history"""
    return auth.visible_cases(request, dynamo_service.list_all_cases())

@router.get("/{case_id}", response_model=EmergencyCase)
def get_case_detail(case_id: str, request: Request):
    """Retrieves single emergency case details"""
    return _visible_case_or_404(request, case_id)

@router.get("/{case_id}/messages", response_model=List[ChatMessage])
def get_case_messages(case_id: str, request: Request):
    """Retrieves full conversation logs between doctor and patient"""
    _visible_case_or_404(request, case_id)
    return dynamo_service.get_case_messages(case_id)

@router.post("/{case_id}/messages", response_model=ChatMessage)
async def send_doctor_message(case_id: str, req: SendChatMessageRequest, request: Request):
    """Doctor sends a message from the Web Portal, bridging directly to patient's WhatsApp"""
    case = _visible_case_or_404(request, case_id)
    return await case_messages.send_doctor_text(case, req.sender_name, req.content)

@router.post("/{case_id}/messages/media", response_model=ChatMessage)
async def send_doctor_media(case_id: str, request: Request, file: UploadFile = File(...),
                            caption: str = Form(""), sender_name: str = Form("Dr. On-Duty")):
    """Doctor sends a photo or a voice note from the portal to the patient's WhatsApp (IMAGE-1).

    Multipart: `file` (image/* or audio/*), optional `caption` (shown under a photo; sent as a text
    just before a voice note, which has no caption), `sender_name`. Photos go out as JPEG / PNG,
    voice notes as mono OGG/Opus whatever the browser recorded (`app.services.media_convert`). The file is kept in the media vault
    (`outbound/<case>/`) and the chat message links it. 409 when the case is closed, 413 / 415 for
    a file that is too large / cannot be sent, 502 when WhatsApp refuses it (nothing is saved then)."""
    case = _visible_case_or_404(request, case_id)
    if case.status not in OPEN_CASE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Case is {case.status}; it is no longer open")
    data = await file.read(settings.MEDIA_UPLOAD_MAX_BYTES + 1)
    try:
        return await case_messages.send_doctor_media(case, sender_name, data, file.content_type, caption)
    except case_messages.MediaSendError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

@router.post("/{case_id}/resolve", response_model=EmergencyCase)
async def resolve_case(case_id: str, request: Request):
    """Doctor closes the case: status RESOLVED, resolved_at set, the patient's active_case_id
    cleared (their next message is triaged again), and the update is pushed to the portal."""
    _visible_case_or_404(request, case_id)
    principal = auth.get_principal(request)
    resolved = await resolve_case_in_pipeline(case_id, resolved_by=principal.email or principal.sub)
    if not resolved:
        raise HTTPException(status_code=404, detail="Case not found")
    return resolved

@router.delete("/{case_id}")
async def delete_case(case_id: str, request: Request):
    """Administrators delete a closed case (RESOLVED or DISMISSED): the case, its chat, its photos
    and voice notes in S3, and its place in the month's analytics. 409 for an open case: close it
    first, so no patient is left waiting on a case that no longer exists."""
    case = _visible_case_or_404(request, case_id)
    if case.status in OPEN_CASE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Case is {case.status}; close it before deleting it")
    try:
        deleted = await asyncio.to_thread(dynamo_service.delete_case, case_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"The case could not be deleted: {e}")
    if deleted is None:
        raise HTTPException(status_code=404, detail="Case not found")
    case, messages = deleted
    from app.services.audio_service import audio_service  # lazy: boto3 client
    keys = [case.audio_s3_key, case.image_s3_key] + [k for m in messages for k in (m.audio_s3_key, m.image_s3_key)]
    media = await asyncio.to_thread(audio_service.delete_media, keys)
    principal = auth.get_principal(request)
    print(f"[Cases] {principal.email or principal.sub} deleted case {case_id} ({len(messages)} messages, "
          f"{media} media object(s)).")
    return {"case_id": case_id, "deleted": True, "messages": len(messages), "media": media}

@router.post("/{case_id}/ambulance-called", response_model=EmergencyCase)
async def record_ambulance_called(case_id: str, request: Request):
    """The doctor called an ambulance for the patient (there is no automatic dispatch). Records
    who and when, tells the patient on WhatsApp and pushes the update to the portal. Idempotent."""
    case = _visible_case_or_404(request, case_id)
    if case.status not in OPEN_CASE_STATUSES and not case.ambulance_called_at:
        raise HTTPException(status_code=409, detail=f"Case is {case.status}; it is no longer open")
    principal = auth.get_principal(request)
    updated = await mark_ambulance_called(case_id, called_by=principal.email or principal.sub)
    if not updated:
        raise HTTPException(status_code=404, detail="Case not found")
    return updated

class OfferAnswerRequest(BaseModel):
    # The round being answered (from the case); omitted: the current round.
    offer_id: Optional[str] = None
    # Only for administrators / local mode without a doctor identity: answer as this doctor.
    doctor_id: Optional[str] = None


class AssignRequest(BaseModel):
    doctor_id: str


def _answering_doctor(request: Request, requested_id: Optional[str]) -> DoctorProfile:
    """Doctors answer as themselves; an administrator (or local mode) names the doctor."""
    principal = auth.get_principal(request)
    doctor_id = principal.doctor_id if principal.is_doctor and principal.doctor_id else requested_id
    if not doctor_id:
        raise HTTPException(status_code=400, detail="doctor_id is required")
    doctor = dynamo_service.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    return doctor


@router.post("/{case_id}/offer/accept", response_model=EmergencyCase)
async def accept_case_offer(case_id: str, req: OfferAnswerRequest, request: Request):
    """An offered doctor accepts the case (OFFER-1). The first accept of the round wins;
    409 if the offer is no longer open to this doctor (taken, reassigned, expired or closed)."""
    _visible_case_or_404(request, case_id)
    outcome, case = await doctor_offers.accept_offer(case_id, req.offer_id, _answering_doctor(request, req.doctor_id))
    if outcome == doctor_offers.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Case not found")
    if outcome == doctor_offers.BUSY:
        doctor = _answering_doctor(request, req.doctor_id)
        raise HTTPException(status_code=409, detail=doctor_offers.busy_text(doctor))
    if outcome not in (doctor_offers.ACCEPTED, doctor_offers.ALREADY_YOURS):
        raise HTTPException(status_code=409, detail="This case has already been taken, reassigned or closed")
    return case


@router.post("/{case_id}/offer/decline", response_model=EmergencyCase)
async def decline_case_offer(case_id: str, req: OfferAnswerRequest, request: Request):
    """An offered doctor declines the case; when the whole round has declined, the next round starts.
    409 if the offer is no longer pending for this doctor."""
    _visible_case_or_404(request, case_id)
    outcome, case = await doctor_offers.decline_offer(case_id, req.offer_id, _answering_doctor(request, req.doctor_id))
    if outcome == doctor_offers.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Case not found")
    if outcome != doctor_offers.DECLINED:
        raise HTTPException(status_code=409, detail="This offer is no longer pending")
    return case


@router.post("/{case_id}/assign", response_model=EmergencyCase)
async def assign_case(case_id: str, req: AssignRequest, request: Request):
    """Administrators assign a doctor by hand, e.g. when no doctor accepted the offer (EXHAUSTED)."""
    case = _visible_case_or_404(request, case_id)
    if case.status not in OPEN_CASE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Case is {case.status}; it is no longer open")
    doctor = dynamo_service.get_doctor(req.doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if not doctor_verification.number_confirmed(doctor):
        # DOC-8: the patient's messages would go to a number nobody confirmed.
        raise HTTPException(status_code=409, detail=f"{doctor.name} has not confirmed their WhatsApp number yet")
    principal = auth.get_principal(request)
    try:
        updated = await doctor_offers.assign_by_admin(case_id, doctor, principal.email or principal.sub)
    except doctor_offers.DoctorBusyError as e:
        # One case per doctor (OFFER-4): pick someone else, or close their case first.
        held = dynamo_service.get_case(e.busy_case_id) if e.busy_case_id else None
        detail = f"{doctor.name} is busy with case {e.busy_case_id}" + (f" ({held.patient_name})" if held else "")
        raise HTTPException(status_code=409, detail=detail)
    if not updated:
        raise HTTPException(status_code=404, detail="Case not found")
    return updated


@router.post("/{case_id}/no-doctor-alert/ack", response_model=EmergencyCase)
async def acknowledge_no_doctor_alert(case_id: str, request: Request):
    """An administrator takes care of a case that has no doctor (ADMIN-1): stops the repeat calls to
    the administrators and the portal alarm for this waiting period. Administrators only; 409 if the
    case is not waiting for a doctor."""
    principal = auth.get_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can acknowledge no-doctor alerts")
    _visible_case_or_404(request, case_id)
    updated = await admin_alerts.acknowledge(case_id, principal.email or principal.sub)
    if updated is None:
        raise HTTPException(status_code=409, detail="This case is not waiting for a doctor")
    return updated


class DirectCallResponse(BaseModel):
    url: str  # https://wa.me/<patient>: opens the patient's WhatsApp chat


@router.post("/{case_id}/direct-call", response_model=DirectCallResponse)
async def start_direct_call(case_id: str, request: Request):
    """The assigned doctor calls the patient from their own WhatsApp (while calls cannot ring in the
    portal, app.direct_calls): records it, tells the patient and returns the wa.me URL to open.
    Administrators and local mode act as the assigned doctor."""
    case = _visible_case_or_404(request, case_id)
    if case.status not in OPEN_CASE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Case is {case.status}; it is no longer open")
    doctor = dynamo_service.get_doctor(case.assigned_doctor_id) if case.assigned_doctor_id else None
    if doctor is None:
        raise HTTPException(status_code=409, detail="No doctor has accepted this case yet")
    url = await direct_calls.start_direct_call(case, doctor, via="portal")
    return DirectCallResponse(url=url)


@router.post("/{case_id}/video-call", response_model=VideoSessionResponse)
async def initiate_doctor_video_call(case_id: str, request: Request):
    """
    Doctor triggers 1-Click Video Consultation:
    1. Creates Amazon Chime SDK meeting room
    2. Sends interactive invitation button straight to patient's WhatsApp
    """
    case = _visible_case_or_404(request, case_id)

    doctor_name = case.assigned_doctor_name or "Emergency Specialist"
    specialty = case.matched_specialty or "Emergency Medicine"

    # 1. Provision Amazon Chime SDK Meeting
    session = await chime_service.create_emergency_video_session(case_id, doctor_name)

    # 2. Update case status
    updated = dynamo_service.update_case(case_id, {
        "video_room_url": session["patient_join_url"]
    })

    # 3. Transmit WhatsApp 1-Click Action to Patient
    await whatsapp_service.send_video_call_invitation(
        to_phone=case.user_phone,
        doctor_name=doctor_name,
        specialty=specialty,
        video_url=session["patient_join_url"]
    )

    # 4. Save audit log message
    audit = ChatMessage(
        message_id=f"MSG-{uuid.uuid4().hex[:6]}",
        case_id=case_id,
        sender="BOT",
        sender_name="System",
        content=f"📹 [Video Room Launched]: {with_title(doctor_name)} sent 1-Click video consultation link to patient."
    )
    dynamo_service.save_chat_message(audit)
    await events_service.publish_chat_message(audit)
    if updated:
        await events_service.publish_case(updated)

    return VideoSessionResponse(
        meeting_id=session["meeting_id"],
        patient_join_url=session["patient_join_url"],
        doctor_join_url=session["doctor_join_url"],
        status="ACTIVE"
    )
