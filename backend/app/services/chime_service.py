"""Amazon Chime SDK Meetings: emergency video rooms between patient and doctor (VIDEO-1).

A meeting is created once per case and reused while it is live. The full Chime `Meeting`
object (which the browser client needs to join), the meeting ID and a secret patient token
are stored on the case. Attendees are created on demand by POST /api/v1/video/{id}/join.

Modes:
  - aws:  real `chime-sdk-meetings` calls (MOCK_AWS=False, no AWS_ENDPOINT_URL).
  - mock: a clearly simulated Meeting/Attendee (`"Simulated": True`). Used with MOCK_AWS=True
          and in Floci mode (Floci does not emulate Chime), or when the Chime client fails.
  CHIME_MODE=aws|mock overrides the automatic choice (e.g. real Chime next to Floci).
"""
import asyncio
import os
import re
import secrets
import uuid
from typing import Dict, Any, Optional
from urllib.parse import urlencode

import boto3
from botocore.exceptions import ClientError

from app.config import settings
from app.models.schemas import EmergencyCase
from app.services.dynamo_service import dynamo_service


class MeetingEndedError(Exception):
    """The case's Chime meeting no longer exists (ended or expired)."""


def is_simulated_meeting(meeting: Optional[Dict[str, Any]]) -> bool:
    return bool(meeting) and bool(meeting.get("Simulated"))


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


class ChimeService:
    def __init__(self):
        override = os.getenv("CHIME_MODE", "auto").lower()
        if override == "mock":
            self.mock_mode, reason = True, "CHIME_MODE=mock"
        elif override == "aws":
            self.mock_mode, reason = False, "CHIME_MODE=aws"
        elif settings.MOCK_AWS:
            self.mock_mode, reason = True, "MOCK_AWS=True"
        elif settings.AWS_ENDPOINT_URL:
            self.mock_mode, reason = True, "Floci does not emulate Chime SDK Meetings"
        else:
            self.mock_mode, reason = False, "real AWS"

        self.chime = None
        if not self.mock_mode:
            try:
                # Never pass the Floci endpoint: Chime always goes to real AWS.
                self.chime = boto3.client('chime-sdk-meetings', region_name=settings.AWS_REGION)
            except Exception as e:
                if not settings.MOCK_AWS and not settings.AWS_ENDPOINT_URL:
                    raise RuntimeError(
                        f"[ChimeService] MOCK_AWS=False but the Chime SDK Meetings client failed ({e}). "
                        "Set CHIME_MODE=mock to run with simulated video rooms on purpose."
                    ) from e
                print(f"[ChimeService] Warning: Could not create Chime SDK Meetings client, using SIMULATED video rooms: {e}")
                self.mock_mode = True
                reason = "client creation failed"

        # Sessions created before their case was saved (the agent can run before the pipeline
        # persists the case). Flushed onto the case the next time it is read here.
        self._pending: Dict[str, Dict[str, Any]] = {}

        if self.mock_mode:
            print(f"[ChimeService] SIMULATED video rooms ({reason}). Patients and doctors see a labelled mock room.")
        else:
            print(f"[ChimeService] Amazon Chime SDK Meetings active in {settings.AWS_REGION}.")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def build_join_urls(meeting_id: str, case_id: str, token: str,
                        doctor_token: Optional[str] = None) -> Dict[str, str]:
        """Join links. The doctor's link needs a portal login, unless it carries `doctor_token` (the
        link sent to the doctor's own WhatsApp, DOC-4)."""
        base = settings.PORTAL_BASE_URL.rstrip("/")
        patient = f"{base}/call/{meeting_id}?" + urlencode({"role": "patient", "case": case_id, "token": token})
        doctor_params = {"role": "doctor", "case": case_id, **({"token": doctor_token} if doctor_token else {})}
        doctor = f"{base}/call/{meeting_id}?" + urlencode(doctor_params)
        return {"patient_join_url": patient, "doctor_join_url": doctor}

    def doctor_whatsapp_link(self, case: EmergencyCase, doctor_id: str) -> str:
        """The doctor's join link for their WhatsApp (DOC-4): no portal login, a secret token that only
        works for this doctor while they hold the open case (checked in app/routers/video.py). The
        case must already have a meeting (create_emergency_video_session)."""
        token = case.video_doctor_token if case.video_doctor_token_for == doctor_id else None
        if not token:
            token = secrets.token_urlsafe(24)
            dynamo_service.update_case(case.case_id, {"video_doctor_token": token, "video_doctor_token_for": doctor_id})
        return self.build_join_urls(case.video_meeting_id or "", case.case_id, case.video_patient_token or "",
                                    doctor_token=token)["doctor_join_url"]

    @staticmethod
    def _simulated_meeting(case_id: str) -> Dict[str, Any]:
        meeting_id = f"MEET-{uuid.uuid4().hex[:8].upper()}"
        return {
            "MeetingId": meeting_id,
            "ExternalMeetingId": case_id,
            "MediaRegion": "simulated",
            "MediaPlacement": {},
            "Simulated": True,
        }

    @staticmethod
    def _request_token(case_id: str) -> str:
        # ClientRequestToken: 2-64 chars of [-_a-zA-Z0-9]. Unique per attempt so a case can get
        # a fresh meeting after the previous one ended.
        safe = re.sub(r"[^-_a-zA-Z0-9]", "-", case_id)[:50]
        return f"{safe}-{uuid.uuid4().hex[:12]}"

    def _flush_pending(self, case: EmergencyCase) -> EmergencyCase:
        pending = self._pending.get(case.case_id)
        if pending and case.video_meeting_id != pending["video_meeting_id"]:
            updated = dynamo_service.update_case(case.case_id, pending)
            if updated:
                self._pending.pop(case.case_id, None)
                return updated
        elif pending:
            self._pending.pop(case.case_id, None)
        return case

    def get_case_with_video(self, case_id: str) -> Optional[EmergencyCase]:
        """The case, with any session created before the case existed persisted onto it."""
        case = dynamo_service.get_case(case_id)
        if case:
            case = self._flush_pending(case)
        return case

    async def _meeting_is_live(self, meeting: Dict[str, Any]) -> bool:
        if is_simulated_meeting(meeting):
            # A simulated room is only reused while we are still simulating.
            return self.mock_mode
        if self.mock_mode or not self.chime:
            return False
        try:
            await asyncio.to_thread(self.chime.get_meeting, MeetingId=meeting["MeetingId"])
            return True
        except Exception as e:
            print(f"[ChimeService] Stored meeting {meeting.get('MeetingId')} is not live ({_error_code(e) or e}); creating a new one.")
            return False

    async def _create_meeting(self, case_id: str) -> Dict[str, Any]:
        if not self.mock_mode and self.chime:
            try:
                res = await asyncio.to_thread(
                    self.chime.create_meeting,
                    ClientRequestToken=self._request_token(case_id),
                    MediaRegion=settings.AWS_REGION,
                    ExternalMeetingId=case_id[:64],
                )
                return res["Meeting"]
            except Exception as e:
                print(f"[ChimeService] ERROR: create_meeting failed, falling back to a SIMULATED room: {e}")
        return self._simulated_meeting(case_id)

    # ------------------------------------------------------------------ public API
    async def create_emergency_video_session(self, case_id: str, doctor_name: str) -> Dict[str, Any]:
        """
        Creates (or reuses) the Amazon Chime SDK meeting for a case and builds the 1-click
        browser join links. Persists the Meeting, its ID, the patient token and the patient
        link on the case.
        """
        case = self.get_case_with_video(case_id)
        stored = case.video_meeting if case else (self._pending.get(case_id) or {}).get("video_meeting")
        stored_token = case.video_patient_token if case else (self._pending.get(case_id) or {}).get("video_patient_token")

        reused = False
        if stored and stored_token and await self._meeting_is_live(stored):
            meeting, token, reused = stored, stored_token, True
        else:
            meeting = await self._create_meeting(case_id)
            token = secrets.token_urlsafe(24)

        meeting_id = meeting["MeetingId"]
        urls = self.build_join_urls(meeting_id, case_id, token)
        update = {
            "video_meeting_id": meeting_id,
            "video_meeting": meeting,
            "video_patient_token": token,
            "video_room_url": urls["patient_join_url"],
        }
        if not reused:
            if case and dynamo_service.update_case(case_id, update):
                self._pending.pop(case_id, None)
            else:
                print(f"[ChimeService] Case {case_id} not saved yet; video session kept pending until it is.")
                self._pending[case_id] = update

        return {
            "meeting_id": meeting_id,
            "patient_join_url": urls["patient_join_url"],
            "doctor_join_url": urls["doctor_join_url"],
            "status": "READY",
            "doctor_name": doctor_name,
            "mock": is_simulated_meeting(meeting),
            "reused": reused,
        }

    async def create_attendee(self, case: EmergencyCase, role: str, external_user_id: str) -> Dict[str, Any]:
        """Joins `role` to the case's meeting. Returns {"Meeting", "Attendee", "mock"}.

        Raises MeetingEndedError if Chime no longer knows the meeting; other Chime errors propagate.
        """
        meeting = case.video_meeting or {}
        if is_simulated_meeting(meeting):
            return {
                "Meeting": meeting,
                "Attendee": {
                    "AttendeeId": f"SIM-{role.upper()}-{uuid.uuid4().hex[:8]}",
                    "ExternalUserId": external_user_id,
                    "JoinToken": "simulated-join-token",
                    "Simulated": True,
                },
                "mock": True,
            }
        if self.mock_mode or not self.chime:
            # A real meeting stored earlier, but this process can't reach Chime.
            raise MeetingEndedError("Chime SDK Meetings is not available in this environment")
        try:
            res = await asyncio.to_thread(
                self.chime.create_attendee,
                MeetingId=meeting["MeetingId"],
                ExternalUserId=external_user_id[:64],
            )
        except ClientError as e:
            if _error_code(e) in ("NotFoundException", "NotFound"):
                raise MeetingEndedError(str(e)) from e
            raise
        return {"Meeting": meeting, "Attendee": res["Attendee"], "mock": False}


chime_service = ChimeService()
