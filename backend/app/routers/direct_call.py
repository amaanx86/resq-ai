"""Open chat & call links (`app.direct_calls`): opened from the doctor's WhatsApp, so there is no
portal login; the token in the URL is the secret."""
import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app import direct_calls
from app.config import settings

router = APIRouter(prefix="/api/v1/direct-call", tags=["Direct WhatsApp calls"])

_REASONS = {
    direct_calls.UNKNOWN: "This link is not valid.",
    direct_calls.EXPIRED: "This link has expired.",
    direct_calls.NOT_ASSIGNED: "This case is no longer assigned to you.",
    direct_calls.CLOSED: "This case is closed.",
}


@router.get("/{token}", include_in_schema=False)
async def open_direct_call(token: str):
    """Records that the doctor is calling the patient, tells the patient, and redirects to the
    patient's WhatsApp chat (wa.me). A small page explains a dead link instead."""
    outcome, url = await direct_calls.use_link(token)
    if outcome == direct_calls.OK and url:
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
    portal = html.escape(settings.PORTAL_BASE_URL)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'><title>ResQ-AI</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#0f172a'>"
        f"<h1 style='font-size:1.25rem'>{html.escape(_REASONS.get(outcome, 'This link does not work.'))}</h1>"
        f"<p>Open the case in the <a href='{portal}'>ResQ-AI portal</a> to contact the patient.</p>"
        "</body></html>"
    )
    return HTMLResponse(page, status_code=410 if outcome == direct_calls.EXPIRED else 404,
                        headers={"Cache-Control": "no-store"})
