"""WhatsApp inbound adapters and the portal simulator (INFRA-6, INFRA-7).

The routes here only verify, normalize and enqueue. The pipeline itself lives in
`app/pipeline.py` and runs in the worker (`python -m app.worker`), or inline in pure mock mode.

The router is mounted without portal_auth (Meta must reach the webhook). The portal-only routes
here (the simulator and its status poll) call `auth.portal_auth` themselves: Administrators only
when AUTH_ENABLED.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app import auth
from app.config import settings
from app.messaging.inbound import parse_meta_webhook, verify_meta_signature
from app.models.schemas import InboundCallEvent, InboundWhatsAppMessage
from app.pipeline import idempotency_key
from app.services.dynamo_service import dynamo_service
from app.services.queue_service import queue_service

router = APIRouter(prefix="/api/v1/webhook", tags=["WhatsApp Webhook"])

PROVIDERS = ("simulator", "meta", "eum_social")


class SimulatedVoiceRequest(BaseModel):
    user_phone: str = "+923009988776"
    voice_text: str = "Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, please jaldi madad bhejein!"
    language: Optional[str] = "Urdu"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 1. Meta Webhook Verification
@router.get("")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    """Verifies WhatsApp Webhook token with Meta"""
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


# 2. Meta inbound webhook: verify, normalize, enqueue, return fast
@router.post("")
async def handle_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives Meta Cloud API webhook events and puts each message and call event on the inbound queue."""
    raw = await request.body()
    if not verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        body = await request.json()
    except Exception as e:
        print(f"[Webhook] Error parsing JSON body: {e}")
        return {"status": "IGNORED", "reason": "Empty or non-JSON payload"}

    events = parse_meta_webhook(body)
    received_at = _now_iso()
    calls = 0
    for event in events:
        if isinstance(event, InboundCallEvent):
            # Calls: never deferred to a background task; the answer window is short.
            await queue_service.publish_call_event(event)
            calls += 1
        elif queue_service.inline:
            # Mock mode: no queue/worker, so process after the response (keeps the ack fast).
            background_tasks.add_task(queue_service.publish, event, received_at)
        else:
            # A failed enqueue returns 500, so Meta redelivers the event.
            await queue_service.publish(event, received_at)
    print(f"[Webhook] {len(events) - calls} message(s) and {calls} call event(s) accepted "
          f"({queue_service.mode} mode).")
    return {"status": "RECEIVED", "messages": len(events) - calls, "calls": calls}


# 3. Portal / judge voice simulation
@router.post("/simulate-voice")
async def simulate_voice_note(sim_req: SimulatedVoiceRequest, request: Request):
    """Publishes a simulated voice note (with its own transcript) like a real inbound message.

    Inline mode (mock) processes it before returning, so `case` is filled. With a queue the
    response is QUEUED and `case` is null; poll GET /api/v1/webhook/messages/{provider_message_id}.
    Administrators only when AUTH_ENABLED (it triggers the real emergency tools).
    """
    await auth.portal_auth(request)
    phone = sim_req.user_phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = f"+{phone}"
    msg = InboundWhatsAppMessage(
        provider="simulator",
        provider_message_id=f"sim-{uuid.uuid4().hex}",
        sender_phone=phone,
        timestamp=_now_iso(),
        type="audio",
        simulated_transcript=sim_req.voice_text,
        simulated_language=sim_req.language or None,
    )
    result = await queue_service.publish(msg)
    case = None
    if result.get("case_id") and result.get("status") in ("PROCESSED", "DUPLICATE"):
        case = dynamo_service.get_case(result["case_id"])
    return {
        "status": "QUEUED" if result.get("status") == "QUEUED" else "PROCESSED",
        "provider_message_id": msg.provider_message_id,
        "action": result.get("action"),
        "case": case,
    }


# 4. Processing status of an inbound message (the simulate modal polls this)
@router.get("/messages/{provider_message_id}")
async def get_message_status(provider_message_id: str, request: Request, provider: Optional[str] = None):
    """{"status": "PENDING"|"IN_PROGRESS"|"PROCESSED", "case_id": str|null}. Administrators only
    when AUTH_ENABLED."""
    await auth.portal_auth(request)
    providers = [provider] if provider else PROVIDERS
    for p in providers:
        record = await asyncio.to_thread(dynamo_service.get_idempotency_record, idempotency_key(p, provider_message_id))
        if record:
            result = record.get("result") or {}
            status = "PROCESSED" if record.get("status") == "COMPLETE" else "IN_PROGRESS"
            return {"status": status, "case_id": result.get("case_id"), "action": result.get("action")}
    return {"status": "PENDING", "case_id": None}
