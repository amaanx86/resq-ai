from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime, timezone

# ==========================================
# USER & ONBOARDING SCHEMAS
# ==========================================
class UserProfile(BaseModel):
    phone_number: str
    name: str
    emergency_contact_name: Optional[str] = None
    # None until the user replies to the onboarding prompt (FLOW-1). Never a placeholder number.
    emergency_contact_phone: Optional[str] = None
    address: Optional[str] = "Default Emergency Address"
    onboarding_status: Literal["PENDING_DETAILS", "COMPLETE"] = "COMPLETE"
    # Open case (ACTIVE / DOCTOR_HANDOVER) for handover routing (FLOW-2). Cleared on resolve.
    active_case_id: Optional[str] = None
    registered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class UserOnboardingRequest(BaseModel):
    phone_number: str
    name: str
    emergency_contact_name: str
    emergency_contact_phone: str
    address: Optional[str] = None

# ==========================================
# DOCTOR SCHEMAS
# ==========================================
class DoctorRegistration(BaseModel):
    name: str
    phone_number: str
    license_no: str
    specialty: str # Cardiology, Neurology, Emergency Medicine, Trauma, Pulmonology, Pediatrics
    bio: str
    is_active: bool = True
    # Portal sign-in (Cognito username). Required by POST /api/v1/doctors/register; doctors
    # registered before it existed have none.
    email: Optional[str] = None

class DoctorProfile(DoctorRegistration):
    doctor_id: str
    cases_handled: int = 0
    registered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Portal user (app/services/cognito_service.py): INVITED (invitation email sent, password not
    # set yet), ACTIVE (the user already had a password), SKIPPED (no Cognito: local/mock mode).
    portal_status: Optional[str] = None
    portal_invited_at: Optional[str] = None
    # One case per doctor (OFFER-4): the case the doctor holds, as a pending offer (OFFER, until
    # `busy_until`) or as the assigned doctor (CASE). Set and cleared with conditional writes
    # (`dynamo_service.claim_doctor` / `release_doctor`); None: free.
    busy_case_id: Optional[str] = None
    busy_kind: Optional[str] = None
    busy_until: Optional[str] = None
    # Last message from the doctor's WhatsApp to the ResQ number (DOC-0): WhatsApp delivers free-form
    # messages to them only within 24 hours of it.
    last_inbound_at: Optional[str] = None
    # Last case the doctor held, and when it closed (DOC-1: a late message gets "case closed").
    last_case_id: Optional[str] = None
    last_case_closed_at: Optional[str] = None
    # Number confirmation (DOC-8, app/doctor_verification.py): None (registered before it existed:
    # trusted), PENDING, CONFIRMED or DECLINED (the number's owner said "not me"). Only None and
    # CONFIRMED get offers and patients' messages.
    phone_status: Optional[str] = None
    phone_confirmed_at: Optional[str] = None
    phone_confirmation_sent_at: Optional[str] = None
    phone_confirmation_status: Optional[str] = None  # SENT | FAILED (the last request)

class DoctorUpdate(BaseModel):
    """PATCH /api/v1/doctors/{id} (administrators): any of these; the email (the sign-in) is not
    changed here."""
    name: Optional[str] = None
    phone_number: Optional[str] = None
    license_no: Optional[str] = None
    specialty: Optional[str] = None
    bio: Optional[str] = None

# ==========================================
# ADMINISTRATOR SCHEMAS (ADMIN-2)
# ==========================================
class AdminRegistration(BaseModel):
    """An administrator added in the portal (POST /api/v1/admins): their Cognito portal user joins
    the Administrators group, and their phone gets the no-doctor alert calls (ADMIN-1)."""
    name: str
    email: str
    phone_number: str
    receive_alert_calls: bool = True

class AdminUpdate(BaseModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    receive_alert_calls: Optional[bool] = None

class AdminProfile(AdminRegistration):
    admin_id: str
    added_by: Optional[str] = None
    registered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Portal user (app/services/cognito_service.py), as for doctors: INVITED, ACTIVE or SKIPPED.
    portal_status: Optional[str] = None
    portal_invited_at: Optional[str] = None

# ==========================================
# TRIAGE & EMERGENCY CASE SCHEMAS
# ==========================================
class TriageAssessment(BaseModel):
    # is_emergency means ESI 1-2, the only levels that run the emergency tools (AGENT-5).
    is_emergency: bool
    esi_level: int = Field(ge=1, le=5) # 1 = Immediate Resuscitation, 2 = Emergent, 3 = Urgent, 4 = Less Urgent, 5 = Non-urgent
    confidence_score: float
    detected_language: str # Urdu, Hindi, English
    clinical_summary: str
    matched_specialty: Optional[str] = None
    emergency_contact_called: bool = False
    contact_call_id: Optional[str] = None
    reasoning: str
    # True when both Bedrock models failed and the keyword classifier decided (AGENT-3).
    degraded: bool = False
    # Which engine produced the assessment: "bedrock:<model>", "agentcore", "heuristic".
    triage_engine: Optional[str] = None
    # One dict per tool call: {"tool", "status", ...tool output, "started_at", "finished_at"} (DATA-2).
    tool_results: List[Dict[str, Any]] = Field(default_factory=list)
    # Facts recalled from AgentCore Memory for this patient (MEM-1); empty when memory is off.
    patient_history: List[str] = Field(default_factory=list)

class EmergencyCase(BaseModel):
    case_id: str
    user_phone: str
    patient_name: str
    emergency_contact_phone: Optional[str] = None
    # Legacy display URL. New code stores audio_s3_key and serves playback via a presigned URL (UI-1).
    audio_s3_url: Optional[str] = None
    audio_s3_key: Optional[str] = None
    transcript: str
    detected_language: str
    esi_level: int
    severity: str # CRITICAL, HIGH, MODERATE, LOW
    status: str # ACTIVE, DOCTOR_HANDOVER, RESOLVED, DISMISSED
    matched_specialty: Optional[str] = None
    # Set when a doctor accepts the case offer (OFFER-1); never assigned without the doctor's yes.
    assigned_doctor_id: Optional[str] = None
    assigned_doctor_name: Optional[str] = None
    # Doctor case offer (OFFER-1, app/doctor_offers.py). PENDING: offered to `offered_doctor_ids`
    # in round `offer_id` until `offer_expires_at`; ACCEPTED: `assigned_doctor_*` took it;
    # EXHAUSTED: no free doctor took it; the case waits and is offered again as soon as a doctor is
    # free (OFFER-5), and admins are alerted (ADMIN-1). None: no doctor needed (ESI 4-5).
    doctor_offer_status: Optional[str] = None
    offer_id: Optional[str] = None
    offered_doctor_ids: List[str] = Field(default_factory=list)
    offer_expires_at: Optional[str] = None
    declined_doctor_ids: List[str] = Field(default_factory=list)  # declined or let a round time out
    # One entry per round: {offer_id, doctor_ids, sent_at, expires_at, outcome, at}; outcome is
    # PENDING, ACCEPTED (+ doctor_id), DECLINED, TIMED_OUT or SUPERSEDED.
    offer_history: List[Dict[str, Any]] = Field(default_factory=list)
    offer_started_at: Optional[str] = None  # first offer sent (time-to-accept KPI)
    accepted_at: Optional[str] = None
    # Waiting for a doctor (OFFER-5): since when (one waiting period; kept after a doctor accepts, for
    # the waiting time) and why: NO_DOCTOR_FREE (nobody on duty or everyone busy) or ALL_DECLINED.
    no_doctor_since: Optional[str] = None
    no_doctor_reason: Optional[str] = None
    # No-doctor alert (ADMIN-1): the administrator who acknowledged the current waiting period (stops
    # the repeat calls), the waiting period whose calls are queued, and one entry per call placed
    # ({since, attempt, phone, status, contact_id, error, at}).
    admin_alert_ack_by: Optional[str] = None
    admin_alert_ack_at: Optional[str] = None
    admin_call_period: Optional[str] = None
    admin_alert_calls: List[Dict[str, Any]] = Field(default_factory=list)
    contact_call_id: Optional[str] = None
    video_room_url: Optional[str] = None
    # Location the patient shared on WhatsApp (ESI 1-2 cases ask for it), latest share wins.
    location_requested_at: Optional[str] = None
    location_latitude: Optional[float] = None
    location_longitude: Optional[float] = None
    location_name: Optional[str] = None
    location_address: Optional[str] = None
    location_shared_at: Optional[str] = None
    # WhatsApp template alert with the location to the emergency contact: SENT, FAILED, SKIPPED_NO_CONTACT.
    location_alert_status: Optional[str] = None
    # No automatic dispatch: the assigned doctor calls the ambulance and records it in the portal.
    ambulance_called_at: Optional[str] = None
    ambulance_called_by: Optional[str] = None
    # Direct WhatsApp call (app/direct_calls.py, used while calls cannot reach the portal): the
    # patient asked for a call; the assigned doctor opened the patient's WhatsApp chat to call them
    # from their own number (only the choice is recorded, the call itself is outside ResQ).
    call_requested_at: Optional[str] = None
    direct_call_at: Optional[str] = None
    direct_call_by: Optional[str] = None
    # Agent audit trail (DATA-2)
    clinical_summary: Optional[str] = None
    reasoning: Optional[str] = None
    confidence_score: Optional[float] = None
    triage_engine: Optional[str] = None
    degraded: bool = False
    tool_results: List[Dict[str, Any]] = Field(default_factory=list)
    # Patient history the triage agent recalled from AgentCore Memory (MEM-1), shown to the doctor.
    patient_history: List[str] = Field(default_factory=list)
    # Photo the patient sent (IMAGE-1): S3 key in the media vault and what the image agent saw
    # (`app.agents.image_agent.ImageAnalysis.as_dict()`: description, findings, red flags, ...).
    image_s3_key: Optional[str] = None
    image_analysis: Optional[Dict[str, Any]] = None
    # Transcription (or image analysis) failed or was unavailable: a human must review (SPEECH-1).
    needs_human_review: bool = False
    # Inbound provenance (INFRA-7)
    provider: Optional[str] = None # meta, eum_social, simulator
    provider_message_id: Optional[str] = None
    # Timing for the real average response time (DATA-1). dispatched_at: when the ESI 1-2 response
    # (contact call + doctor handover) went out.
    received_at: Optional[str] = None
    dispatched_at: Optional[str] = None
    # Video (VIDEO-1): full Chime Meeting object and the secret token in the patient link.
    video_meeting_id: Optional[str] = None
    video_meeting: Optional[Dict[str, Any]] = None
    video_patient_token: Optional[str] = None
    # Secret token in the doctor's video link sent on WhatsApp (DOC-4): the doctor joins without the
    # portal login. Only valid for the doctor it was made for while they hold the open case.
    video_doctor_token: Optional[str] = None
    video_doctor_token_for: Optional[str] = None
    # Patient messages that could not reach the doctor's WhatsApp (24-hour window closed, DOC-6):
    # when the template was last sent, and the first message the doctor has not seen yet.
    doctor_waiting_template_at: Optional[str] = None
    doctor_missed_since: Optional[str] = None
    resolved_at: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ==========================================
# CHAT & WEBSOCKET SCHEMAS
# ==========================================
class ChatMessage(BaseModel):
    message_id: str
    case_id: str
    sender: str # PATIENT, DOCTOR, BOT
    sender_name: str
    content: str
    audio_s3_key: Optional[str] = None
    # A photo in the message (patient's photo, or one the doctor sent from the portal).
    image_s3_key: Optional[str] = None
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class SendChatMessageRequest(BaseModel):
    content: str
    sender: str = "DOCTOR"
    sender_name: str = "Dr. On-Duty"

# ==========================================
# VIDEO CONSULTATION SCHEMAS
# ==========================================
class VideoSessionRequest(BaseModel):
    case_id: str
    doctor_id: str
    doctor_name: str

class VideoSessionResponse(BaseModel):
    meeting_id: str
    patient_join_url: str
    doctor_join_url: str
    status: str

# ==========================================
# ANALYTICS SCHEMAS
# ==========================================
class AnalyticsSummary(BaseModel):
    total_registered_users: int
    total_emergency_cases: int # ESI 1-2
    total_urgent_cases: int = 0 # ESI 3: reviewed by a doctor, no dispatch
    total_false_alarms: int # ESI 4-5
    monthly_doctor_calls: Dict[str, int]
    cases_by_specialty: Dict[str, int]
    # None when no case has both received_at and dispatched_at yet.
    avg_response_time_seconds: Optional[float] = None
    # OFFER-1: first doctor offer sent -> a doctor accepted. None until a case was accepted.
    avg_time_to_accept_seconds: Optional[float] = None
    languages_handled: Dict[str, int]
    demo_mode: bool = False
    month: Optional[str] = None # YYYY-MM the KPIs cover

# ==========================================
# WHATSAPP WEBHOOK SCHEMAS
# ==========================================
class WhatsAppMedia(BaseModel):
    id: str
    mime_type: Optional[str] = None
    sha256: Optional[str] = None

class WhatsAppMessage(BaseModel):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    type: str # text, audio, voice, location, interactive
    text: Optional[Dict[str, str]] = None
    audio: Optional[WhatsAppMedia] = None
    voice: Optional[WhatsAppMedia] = None

class WhatsAppWebhookEntry(BaseModel):
    id: str
    changes: List[Dict[str, Any]]

class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: List[WhatsAppWebhookEntry]

# ==========================================
# PROVIDER-NEUTRAL INBOUND MESSAGE (INFRA-7)
# ==========================================
class InboundWhatsAppMessage(BaseModel):
    """One inbound WhatsApp message, identical whichever provider delivered it.

    Produced by the Meta webhook adapter, the End User Messaging Social adapter and the
    simulator; consumed by the worker pipeline. This is the body of every queue message.
    """
    provider: Literal["meta", "eum_social", "simulator"]
    provider_message_id: str # wamid... ; idempotency key for the worker
    sender_phone: str # E.164 with leading "+"
    sender_name: Optional[str] = None # WhatsApp profile name, if the provider sent it
    timestamp: str # ISO 8601 UTC
    type: Literal["text", "audio", "image", "location", "button", "unsupported"]
    text: Optional[str] = None
    # Image messages: the caption the patient typed under the photo, if any.
    caption: Optional[str] = None
    # Button taps (WA-0): a template quick-reply button (`type: "button"`) or an interactive reply
    # button / list row (`type: "interactive"`). The payload is the ID we set when sending, e.g.
    # "OFFER:ACCEPT:<case_id>:<offer_id>"; the text is the button title the user saw.
    button_payload: Optional[str] = None
    button_text: Optional[str] = None
    # Location messages (the patient tapped "Send location", or attached a location).
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_name: Optional[str] = None
    location_address: Optional[str] = None
    # Provider media ID for audio and images (Meta media ID / EUM Social media ID). None for text.
    media_id: Optional[str] = None
    media_mime_type: Optional[str] = None
    # Simulator only: transcript to use instead of downloading and transcribing audio.
    simulated_transcript: Optional[str] = None
    simulated_language: Optional[str] = None


class InboundCallEvent(BaseModel):
    """One WhatsApp Calling API webhook event (`field: "calls"`, WA-0), from either provider.

    `connect`: a user called the business number; `sdp` holds the SDP offer the answering side
    must reply to. `terminate`: the call ended (`status` Completed / Failed, `duration_seconds`).
    `status`: progress of a business-initiated call (`status` RINGING / ACCEPTED / REJECTED).
    Call events are not messages: the worker handles them outside the message pipeline.
    """
    kind: Literal["call_event"] = "call_event"  # queue-body discriminator
    provider: Literal["meta", "eum_social", "simulator"]
    call_id: str  # wacid...
    event: Literal["connect", "terminate", "status"]
    # The WhatsApp user on the call (the caller for USER_INITIATED, the callee otherwise), E.164.
    user_phone: str
    user_name: Optional[str] = None
    business_phone: Optional[str] = None  # E.164 of our number, when the payload has it
    phone_number_id: Optional[str] = None  # Meta phone number ID of our number
    direction: Optional[str] = None  # USER_INITIATED / BUSINESS_INITIATED
    timestamp: str  # ISO 8601 UTC
    sdp_type: Optional[str] = None
    sdp: Optional[str] = None
    status: Optional[str] = None
    duration_seconds: Optional[int] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    biz_opaque_callback_data: Optional[str] = None
    error: Optional[str] = None  # first error message Meta attached, if any


class InternalTask(BaseModel):
    """Work the backend schedules for itself on the inbound queue (WA-0), e.g. an offer timeout.

    Published with `queue_service.publish_internal(kind, payload, delay_seconds)`. SQS delays a
    message at most 900 s; a longer delay is carried in `run_at` and the worker re-queues the
    task until it is due. Delivery is at-least-once, so every handler must be idempotent (e.g. an
    offer timeout checks that its `offer_id` is still the current one).
    """
    task_id: str
    kind: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_at: str  # ISO 8601 UTC: the task does not run before this

# ==========================================
# SPEECH (SPEECH-1)
# ==========================================
class AudioObject(BaseModel):
    key: str # S3 object key in the audio vault
    s3_uri: str # s3://bucket/key, for Transcribe
    url: Optional[str] = None # HTTPS URL for display; use a presigned URL for playback

class TranscriptionResult(BaseModel):
    ok: bool
    transcript: str = "" # empty when ok is False
    language: str = "Unknown" # Urdu, Hindi, English, Unknown
    language_code: Optional[str] = None # e.g. ur-PK
    confidence: float = 0.0
    error: Optional[str] = None
