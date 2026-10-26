import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    # True: in-memory stores and simulated AWS calls. Real deployments must set MOCK_AWS=False
    # explicitly (the ECS task definitions do). Tests force True in tests/conftest.py.
    MOCK_AWS: bool = _env_bool("MOCK_AWS", "True")
    AWS_ENDPOINT_URL: Optional[str] = os.getenv("AWS_ENDPOINT_URL", None)
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "test")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "test")

    # Process role: the API service seeds demo data on startup, the worker must not.
    SEED_DEMO_DATA: bool = _env_bool("SEED_DEMO_DATA", "True")
    # Pads analytics with demo numbers. Off by default so KPIs reflect real data only.
    DEMO_MODE: bool = _env_bool("DEMO_MODE", "False")

    # Foundation Models
    BEDROCK_MODEL_ID: str = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-5-5-sonnet-2026")
    BEDROCK_FALLBACK_MODEL_ID: str = os.getenv("BEDROCK_FALLBACK_MODEL_ID", "amazon.nova-pro-v1:0")

    # Triage agent backend: "local" runs the Strands agent in-process (mock, Floci, tests);
    # "agentcore" invokes the agent deployed on Bedrock AgentCore Runtime.
    AGENT_BACKEND: str = os.getenv("AGENT_BACKEND", "local")
    AGENTCORE_RUNTIME_ARN: Optional[str] = os.getenv("AGENTCORE_RUNTIME_ARN", None)
    AGENTCORE_GATEWAY_URL: Optional[str] = os.getenv("AGENTCORE_GATEWAY_URL", None)
    # Long-term patient memory (AgentCore Memory, app/agents/memory.py). Unset, mock or Floci: off.
    AGENTCORE_MEMORY_ID: Optional[str] = os.getenv("AGENTCORE_MEMORY_ID", None)
    AGENTCORE_MEMORY_TOP_K: int = int(os.getenv("AGENTCORE_MEMORY_TOP_K", "5"))
    # Per memory call; a slower call is skipped (triage runs without history).
    AGENTCORE_MEMORY_TIMEOUT_SECONDS: float = float(os.getenv("AGENTCORE_MEMORY_TIMEOUT_SECONDS", "2.0"))
    # Upper bound for the Strands classification step before falling back (AGENT-3).
    AGENT_CLASSIFY_TIMEOUT_SECONDS: float = float(os.getenv("AGENT_CLASSIFY_TIMEOUT_SECONDS", "45"))

    # Image analysis agent (IMAGE-1, app/agents/image_agent.py): a vision model on Bedrock describes
    # the patient's photo for triage and the doctor. Unset: BEDROCK_MODEL_ID; then the fallback model.
    IMAGE_ANALYSIS_MODEL_ID: Optional[str] = os.getenv("IMAGE_ANALYSIS_MODEL_ID", None)
    IMAGE_ANALYSIS_TIMEOUT_SECONDS: float = float(os.getenv("IMAGE_ANALYSIS_TIMEOUT_SECONDS", "40"))
    # Largest photo or voice note a doctor may send from the portal (WhatsApp allows 5 MB images,
    # 16 MB audio; bigger photos are scaled down first).
    MEDIA_UPLOAD_MAX_BYTES: int = int(os.getenv("MEDIA_UPLOAD_MAX_BYTES", str(16 * 1024 * 1024)))

    # AWS Storage & Database
    DYNAMODB_TABLE_NAME: str = os.getenv("DYNAMODB_TABLE_NAME", "ResQTable")
    S3_AUDIO_BUCKET: str = os.getenv("S3_AUDIO_BUCKET", "resq-raw-audio-vault")

    # Speech (Amazon Transcribe batch jobs)
    TRANSCRIBE_TIMEOUT_SECONDS: int = int(os.getenv("TRANSCRIBE_TIMEOUT_SECONDS", "60"))
    TRANSCRIBE_POLL_INTERVAL_SECONDS: float = float(os.getenv("TRANSCRIBE_POLL_INTERVAL_SECONDS", "2.0"))
    # Comma-separated Transcribe LanguageOptions override. Unset: the defaults in audio_service,
    # filtered to codes the installed botocore accepts (Transcribe has no Urdu model today).
    TRANSCRIBE_LANGUAGE_OPTIONS: Optional[str] = os.getenv("TRANSCRIBE_LANGUAGE_OPTIONS", None)

    # WhatsApp provider: "meta" (Meta Cloud API, local dev) or "eum_social" (AWS End User
    # Messaging Social, production). Nothing outside app/messaging/ should branch on this.
    WHATSAPP_PROVIDER: str = os.getenv("WHATSAPP_PROVIDER", "meta")
    WHATSAPP_API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v20.0")
    # Meta Cloud API provider
    WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_ACCESS_TOKEN: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "resq_local_verify_token")
    # Meta app secret: when set, POST /api/v1/webhook requires a valid X-Hub-Signature-256.
    WHATSAPP_APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")
    # End User Messaging Social provider (AWS phone number ID, "phone-number-id-...")
    EUM_SOCIAL_PHONE_NUMBER_ID: str = os.getenv("EUM_SOCIAL_PHONE_NUMBER_ID", "")
    # Approved WhatsApp template that sends the patient's shared location to the emergency contact
    # (business-initiated, so it must be a template). Body: {{1}} patient name, {{2}} map link.
    # Defined in terraform/whatsapp_templates.tf.
    WHATSAPP_LOCATION_TEMPLATE_NAME: str = os.getenv("WHATSAPP_LOCATION_TEMPLATE_NAME", "resq_emergency_location")
    WHATSAPP_LOCATION_TEMPLATE_LANGUAGE: str = os.getenv("WHATSAPP_LOCATION_TEMPLATE_LANGUAGE", "en")
    # Doctor templates (OFFER-2, CALL-3), English. Defined in terraform/whatsapp_templates.tf.
    WHATSAPP_OFFER_TEMPLATE_NAME: str = os.getenv("WHATSAPP_OFFER_TEMPLATE_NAME", "resq_doctor_case_offer")
    WHATSAPP_CALL_TEMPLATE_NAME: str = os.getenv("WHATSAPP_CALL_TEMPLATE_NAME", "resq_incoming_patient_call")
    WHATSAPP_CALL_PORTAL_TEMPLATE_NAME: str = os.getenv("WHATSAPP_CALL_PORTAL_TEMPLATE_NAME", "resq_incoming_patient_call_portal")

    # Inbound message queue (WhatsApp -> worker). WEBHOOK_QUEUE_URL wins over the name lookup.
    WEBHOOK_QUEUE_NAME: str = os.getenv("WEBHOOK_QUEUE_NAME", "resq-webhook-queue")
    WEBHOOK_QUEUE_URL: Optional[str] = os.getenv("WEBHOOK_QUEUE_URL", None)
    WEBHOOK_DLQ_NAME: str = os.getenv("WEBHOOK_DLQ_NAME", "resq-webhook-dlq")

    # Worker -> API fan-out for the WebSocket bridge (SNS topic, one SQS queue per API task).
    PORTAL_EVENTS_TOPIC_NAME: str = os.getenv("PORTAL_EVENTS_TOPIC_NAME", "resq-portal-events")
    PORTAL_EVENTS_TOPIC_ARN: Optional[str] = os.getenv("PORTAL_EVENTS_TOPIC_ARN", None)

    # Telephony (Amazon Connect)
    CONNECT_INSTANCE_ID: str = os.getenv("CONNECT_INSTANCE_ID", "00000000-0000-0000-0000-000000000000")
    CONNECT_FLOW_ID: str = os.getenv("CONNECT_FLOW_ID", "00000000-0000-0000-0000-000000000000")
    CONNECT_SOURCE_PHONE_NUMBER: str = os.getenv("CONNECT_SOURCE_PHONE_NUMBER", "+18005550199")

    # Chime video: "auto" = real Chime only with MOCK_AWS=False and no AWS_ENDPOINT_URL (Floci has no
    # Chime); "mock" forces the simulated room; "aws" forces real Chime.
    CHIME_MODE: str = os.getenv("CHIME_MODE", "auto")

    # Doctor case offers (OFFER-1, OFFER-3): seconds a round waits for an answer, and free doctors
    # offered at once for ESI 1-2 (ESI 3: one at a time). Rounds go on until no free doctor who has
    # not been asked is left; then the case waits for one (OFFER-5).
    DOCTOR_OFFER_TIMEOUT_ESI1: int = int(os.getenv("DOCTOR_OFFER_TIMEOUT_ESI1", "60"))
    DOCTOR_OFFER_TIMEOUT_ESI2: int = int(os.getenv("DOCTOR_OFFER_TIMEOUT_ESI2", "60"))
    DOCTOR_OFFER_TIMEOUT_ESI3: int = int(os.getenv("DOCTOR_OFFER_TIMEOUT_ESI3", "300"))
    DOCTOR_OFFER_FANOUT_URGENT: int = int(os.getenv("DOCTOR_OFFER_FANOUT_URGENT", "3"))
    # A case nobody could take (OFFER-5) is checked again this often until a doctor is free.
    WAITING_CASE_RECHECK_SECONDS: int = int(os.getenv("WAITING_CASE_RECHECK_SECONDS", "60"))

    # No-doctor alerts (ADMIN-1): administrators called through Amazon Connect when an ESI 1-2 case
    # has no doctor (comma-separated E.164; empty: the portal alert only), repeated every
    # ADMIN_ALERT_REPEAT_SECONDS until an administrator acknowledges, at most ADMIN_ALERT_MAX_CALLS.
    ADMIN_ALERT_PHONE_NUMBERS: str = os.getenv("ADMIN_ALERT_PHONE_NUMBERS", "")
    ADMIN_ALERT_REPEAT_SECONDS: int = int(os.getenv("ADMIN_ALERT_REPEAT_SECONDS", "300"))
    ADMIN_ALERT_MAX_CALLS: int = int(os.getenv("ADMIN_ALERT_MAX_CALLS", "3"))

    # Doctors on WhatsApp (DOC-*): a doctor's messages to the ResQ number go to their patient,
    # "@resq ..." messages are commands. Off: doctors get a "use the portal" reply instead.
    DOCTOR_WHATSAPP_RELAY_ENABLED: bool = _env_bool("DOCTOR_WHATSAPP_RELAY_ENABLED", "True")
    # Free-form "@resq" commands (DOC-5). Unset: BEDROCK_MODEL_ID; then the fallback model.
    DOCTOR_AGENT_MODEL_ID: Optional[str] = os.getenv("DOCTOR_AGENT_MODEL_ID", None)
    DOCTOR_AGENT_TIMEOUT_SECONDS: float = float(os.getenv("DOCTOR_AGENT_TIMEOUT_SECONDS", "20"))
    # A doctor whose last case closed this recently gets "case closed" instead of being triaged.
    DOCTOR_CASE_CLOSED_GRACE_HOURS: int = int(os.getenv("DOCTOR_CASE_CLOSED_GRACE_HOURS", "12"))
    # Reminder to a doctor whose case has had no messages for this long ("still treating ...?").
    DOCTOR_IDLE_REMINDER_SECONDS: int = int(os.getenv("DOCTOR_IDLE_REMINDER_SECONDS", "7200"))
    # Patient messages to a doctor outside their 24-hour WhatsApp window (DOC-6): this template
    # instead, at most once per DOCTOR_WAITING_TEMPLATE_INTERVAL_SECONDS per case. Body: {{1}} patient.
    WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME: str = os.getenv("WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME",
                                                           "resq_doctor_patient_waiting")
    DOCTOR_WAITING_TEMPLATE_INTERVAL_SECONDS: int = int(os.getenv("DOCTOR_WAITING_TEMPLATE_INTERVAL_SECONDS", "1800"))
    # DOC-8: a newly registered doctor (or a changed number) confirms the WhatsApp number with this
    # template's quick replies before it gets offers or patients' messages. {{1}} doctor name.
    # False: numbers are trusted as entered (no question, no gate).
    DOCTOR_NUMBER_CONFIRMATION_ENABLED: bool = _env_bool("DOCTOR_NUMBER_CONFIRMATION_ENABLED", "True")
    WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME: str = os.getenv("WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME",
                                                       "resq_doctor_confirm_number")

    # Direct WhatsApp calls (app/direct_calls.py: the doctor calls the patient from their own
    # WhatsApp while calls cannot ring in the portal): how long an Open chat & call link works.
    DIRECT_CALL_LINK_TTL_SECONDS: int = int(os.getenv("DIRECT_CALL_LINK_TTL_SECONDS", "3600"))

    # Web Portal Base URL
    PORTAL_BASE_URL: str = os.getenv("PORTAL_BASE_URL", "http://localhost:5173")

    # Portal authentication (Amazon Cognito). Disabled for local mock mode.
    AUTH_ENABLED: bool = _env_bool("AUTH_ENABLED", "False")
    COGNITO_USER_POOL_ID: str = os.getenv("COGNITO_USER_POOL_ID", "")
    COGNITO_APP_CLIENT_ID: str = os.getenv("COGNITO_APP_CLIENT_ID", "")
    COGNITO_REGION: str = os.getenv("COGNITO_REGION", os.getenv("AWS_REGION", "us-east-1"))

settings = Settings()
