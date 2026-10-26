"""WhatsApp Cloud API message bodies, shared by every provider.

Meta posts these to the Graph API; End User Messaging Social passes the same JSON (bytes) in
`send_whatsapp_message(message=...)`. Only the `to` format differs, and each provider formats it.
"""
from typing import Any, Dict, List, Optional

# Meta limits for interactive messages.
CTA_BODY_MAX = 1024
CTA_HEADER_MAX = 60
CTA_FOOTER_MAX = 60
CTA_BUTTON_MAX = 20
REPLY_BUTTON_TITLE_MAX = 20
REPLY_BUTTON_ID_MAX = 256
REPLY_BUTTONS_MAX = 3
TEXT_BODY_MAX = 4096
TEMPLATE_PARAM_MAX = 1024
CAPTION_MAX = 1024


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_message(to: str, text: str) -> Dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": _clip(text, TEXT_BODY_MAX)},
    }


def location_request_message(to: str, body: str) -> Dict[str, Any]:
    """Interactive "Send location" button (interactive.type=location_request_message). The reply is
    an inbound message of type "location"."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {"text": _clip(body, CTA_BODY_MAX)},
            "action": {"name": "send_location"},
        },
    }


def template_message(to: str, name: str, language: str, body_params: List[str],
                     quick_reply_payloads: Optional[List[str]] = None,
                     url_suffixes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Approved template message: the only kind WhatsApp delivers outside the 24-hour customer
    service window, e.g. to an emergency contact who never messaged the business number.

    Button parameters, by button index in the template: `quick_reply_payloads` set the payload a
    quick-reply tap sends back (inbound `button.payload`); `url_suffixes` fill the {{1}} at the end
    of dynamic URL buttons. Quick replies come first, so URL indexes follow them."""
    template: Dict[str, Any] = {"name": name, "language": {"code": language}}
    components: List[Dict[str, Any]] = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": _clip(str(p), TEMPLATE_PARAM_MAX)} for p in body_params],
        })
    replies = list(quick_reply_payloads or [])
    for i, payload in enumerate(replies):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": payload}]})
    for i, suffix in enumerate(url_suffixes or []):
        components.append({"type": "button", "sub_type": "url", "index": str(len(replies) + i),
                           "parameters": [{"type": "text", "text": suffix}]})
    if components:
        template["components"] = components
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }


def reply_buttons_message(to: str, body: str, buttons: List[Dict[str, str]],
                          header: Optional[str] = None, footer: Optional[str] = None) -> Dict[str, Any]:
    """Interactive reply buttons (at most 3): `buttons` are {"id", "title"}; a tap comes back as an
    inbound `interactive.button_reply` with that id. Only inside the 24-hour window."""
    interactive: Dict[str, Any] = {
        "type": "button",
        "body": {"text": _clip(body, CTA_BODY_MAX)},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"][:REPLY_BUTTON_ID_MAX],
                                        "title": _clip(b["title"], REPLY_BUTTON_TITLE_MAX)}}
            for b in buttons[:REPLY_BUTTONS_MAX]
        ]},
    }
    if header:
        interactive["header"] = {"type": "text", "text": _clip(header, CTA_HEADER_MAX)}
    if footer:
        interactive["footer"] = {"text": _clip(footer, CTA_FOOTER_MAX)}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }


def cta_url_message(
    to: str,
    body: str,
    button_text: str,
    url: str,
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> Dict[str, Any]:
    """Interactive call-to-action URL button message (type=interactive, interactive.type=cta_url)."""
    interactive: Dict[str, Any] = {
        "type": "cta_url",
        "body": {"text": _clip(body, CTA_BODY_MAX)},
        "action": {
            "name": "cta_url",
            "parameters": {"display_text": _clip(button_text, CTA_BUTTON_MAX), "url": url},
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": _clip(header, CTA_HEADER_MAX)}
    if footer:
        interactive["footer"] = {"text": _clip(footer, CTA_FOOTER_MAX)}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }


def image_message(to: str, media_id: str, caption: Optional[str] = None) -> Dict[str, Any]:
    """A photo uploaded to WhatsApp first (`media_id` from the provider's media upload)."""
    image: Dict[str, Any] = {"id": media_id}
    if caption:
        image["caption"] = _clip(caption, CAPTION_MAX)
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": image,
    }


def audio_message(to: str, media_id: str) -> Dict[str, Any]:
    """An audio file uploaded to WhatsApp first. OGG/Opus shows up as a voice note. Audio messages
    have no caption."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "audio",
        "audio": {"id": media_id},
    }


LIST_BUTTON_MAX = 20
LIST_ROW_TITLE_MAX = 24
LIST_ROW_DESCRIPTION_MAX = 72
LIST_ROWS_MAX = 10
LOCATION_TEXT_MAX = 1000


def location_message(to: str, latitude: float, longitude: float, name: Optional[str] = None,
                     address: Optional[str] = None) -> Dict[str, Any]:
    """A map pin (e.g. the patient's shared location forwarded to their doctor, DOC-3)."""
    location: Dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if name:
        location["name"] = _clip(name, LOCATION_TEXT_MAX)
    if address:
        location["address"] = _clip(address, LOCATION_TEXT_MAX)
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "location",
        "location": location,
    }


def list_message(to: str, body: str, button: str, rows: List[Dict[str, str]],
                 header: Optional[str] = None, footer: Optional[str] = None,
                 section_title: str = "Actions") -> Dict[str, Any]:
    """Interactive list (up to 10 rows: {"id", "title", "description"?}). A tap comes back as an
    inbound `interactive.list_reply` with the row id (parsed as a `button` message, payload = id)."""
    interactive: Dict[str, Any] = {
        "type": "list",
        "body": {"text": _clip(body, CTA_BODY_MAX)},
        "action": {
            "button": _clip(button, LIST_BUTTON_MAX),
            "sections": [{
                "title": _clip(section_title, LIST_ROW_TITLE_MAX),
                "rows": [
                    {"id": r["id"][:REPLY_BUTTON_ID_MAX], "title": _clip(r["title"], LIST_ROW_TITLE_MAX),
                     **({"description": _clip(r["description"], LIST_ROW_DESCRIPTION_MAX)}
                        if r.get("description") else {})}
                    for r in rows[:LIST_ROWS_MAX]
                ],
            }],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": _clip(header, CTA_HEADER_MAX)}
    if footer:
        interactive["footer"] = {"text": _clip(footer, CTA_FOOTER_MAX)}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
