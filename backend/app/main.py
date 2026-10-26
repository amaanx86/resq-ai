from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.config import settings
from app.auth import portal_auth
from app.routers import webhook, doctors, admins, cases, analytics, bridge, video, media, direct_call
from app.services.dynamo_service import dynamo_service
from app.services.audio_service import audio_service
from app.services.telephony_service import telephony_service
from app.services.chime_service import chime_service
from app.services import events_service
from app.agents.triage_agent import triage_agent
from app.agents.image_agent import image_analyzer


def service_modes() -> dict:
    """Effective mode per service. Each service can fall back to mock on its own (INFRA-1)."""
    def mode(svc) -> str:
        return "mock" if getattr(svc, "mock_mode", True) else "aws"
    return {
        "dynamodb": mode(dynamo_service),
        "s3_transcribe": mode(audio_service),
        "connect": mode(telephony_service),
        "chime": mode(chime_service),
        "triage_agent": mode(triage_agent),
        "image_analysis": mode(image_analyzer),
        "agent_backend": settings.AGENT_BACKEND,
        "whatsapp_provider": settings.WHATSAPP_PROVIDER,
        "auth_enabled": settings.AUTH_ENABLED,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[ResQ-AI Engine] Initializing backend in {settings.ENVIRONMENT} mode (MOCK_AWS={settings.MOCK_AWS})...")
    print(f"[ResQ-AI Engine] Effective service modes: {service_modes()}")
    if settings.SEED_DEMO_DATA:
        # Seed demo cases for hackathon judging & developer walkthrough
        analytics.seed_demo_cases()
        print("[ResQ-AI Engine] Seeded initial doctors, patients, and clinical demo cases successfully.")
    await events_service.start_api_consumer()
    yield
    await events_service.stop_api_consumer()
    print("[ResQ-AI Engine] Shutting down cleanly.")

app = FastAPI(
    title="ResQ-AI Autonomous Emergency Response API",
    description="AWS-native emergency medical triage backend: Strands triage agent on Amazon Bedrock (AgentCore in production), multilingual speech, Amazon Connect telephony and Chime video.",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware for Web Portal
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API Routers.
# Open: WhatsApp webhook (Meta verification + inbound events), WebSocket bridge (checks its own
# token), video (the patient join uses a per-case token; the doctor join checks auth itself).
# Portal routes go through portal_auth (Cognito JWT when AUTH_ENABLED).
portal = [Depends(portal_auth)]
app.include_router(webhook.router)
app.include_router(doctors.router, dependencies=portal)
app.include_router(admins.router, dependencies=portal)
app.include_router(cases.router, dependencies=portal)
app.include_router(media.router, dependencies=portal)
app.include_router(analytics.router, dependencies=portal)
app.include_router(video.router)
# Open chat & call links come from the doctor's WhatsApp: no portal login, the token is the secret.
app.include_router(direct_call.router)
app.include_router(bridge.router)

@app.get("/")
def root():
    return {
        "project": "ResQ-AI",
        "status": "OPERATIONAL",
        "version": "1.0.0",
        "model": settings.BEDROCK_MODEL_ID,
        "region": settings.AWS_REGION,
        "mock_aws": settings.MOCK_AWS,
        "services": service_modes(),
    }

@app.get("/healthz")
def health_check():
    return {"status": "HEALTHY", "service": "resq-backend-api"}
