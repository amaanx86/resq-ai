"""Video consultation join endpoints (VIDEO-1).

The router is registered without portal_auth: the patient joins with the secret token from
their WhatsApp link; the doctor's join runs portal_auth itself, unless the doctor opened the link
sent to their own WhatsApp (DOC-4), which carries the doctor's own token.
"""
import hmac
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request
from botocore.exceptions import ClientError

from app import auth
from app.services.chime_service import chime_service, MeetingEndedError
from app.services.dynamo_service import OPEN_CASE_STATUSES

router = APIRouter(prefix="/api/v1/video", tags=["Video Consultation"])

ROLES = ("patient", "doctor")


@router.post("/{meeting_id}/join")
async def join_video_meeting(
    meeting_id: str,
    request: Request,
    role: str = Query(..., description="patient | doctor"),
    case_id: str = Query(..., alias="case"),
    token: Optional[str] = Query(None, description="Link token: the patient's, or the doctor's WhatsApp link token"),
):
    """
    Creates a Chime attendee for the patient or the doctor and returns what the browser
    client needs to join: `{Meeting, Attendee, mock, case_id, role, ...}`.
    """
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="role must be 'patient' or 'doctor'")

    case = chime_service.get_case_with_video(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if role == "patient":
        expected = case.video_patient_token or ""
        if not expected or not token or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(status_code=403, detail="Invalid or expired video link")
    elif token:
        # The doctor's link from their own WhatsApp (DOC-4): valid only for the doctor it was made for,
        # while they still hold the open case.
        expected = case.video_doctor_token or ""
        if (not expected or not hmac.compare_digest(token.encode(), expected.encode())
                or case.video_doctor_token_for != case.assigned_doctor_id
                or case.status not in OPEN_CASE_STATUSES):
            raise HTTPException(status_code=403, detail="Invalid or expired video link")
    else:
        await auth.portal_auth(request)
        auth.assert_case_visible(request, case)

    if not case.video_meeting or case.video_meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="Meeting not found for this case")

    try:
        joined = await chime_service.create_attendee(case, role, f"{role}-{case.case_id}")
    except MeetingEndedError:
        raise HTTPException(
            status_code=410,
            detail="This video call has ended. Ask the doctor to start a new call.",
        )
    except ClientError as e:
        print(f"[Video] create_attendee failed for case {case.case_id}: {e}")
        raise HTTPException(status_code=502, detail="Could not join the video call (Chime error)")

    return {
        "Meeting": joined["Meeting"],
        "Attendee": joined["Attendee"],
        "mock": joined["mock"],
        "case_id": case.case_id,
        "role": role,
        "patient_name": case.patient_name,
        "doctor_name": case.assigned_doctor_name,
        "specialty": case.matched_specialty,
    }
