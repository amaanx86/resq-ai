"""WhatsApp facade: the message copy lives here, the transport is the provider chosen by
WHATSAPP_PROVIDER (see app/messaging/factory.py). Callers never branch on the provider."""
import re
from typing import Any, Dict, List, Optional

from app.config import settings
from app.messaging.factory import build_provider
from app.messaging.providers import WhatsAppProvider
from app.models.schemas import AudioObject


def with_title(doctor_name: str) -> str:
    """ "Sarah Khan" -> "Dr. Sarah Khan"; a name that already has the title ("Dr. Sarah Khan, MD",
    "DR SARAH") is kept as it is."""
    name = (doctor_name or "").strip()
    return name if re.match(r"(?i)^dr(\.|\s|$)", name) else f"Dr. {name}"


class WhatsAppService:
    def __init__(self, provider: Optional[WhatsAppProvider] = None):
        # Startup validation happens in build_provider: mock fallback locally, RuntimeError on AWS.
        self.provider: WhatsAppProvider = provider or build_provider()

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def mock_mode(self) -> bool:
        return self.provider.name == "mock"

    async def send_text_message(self, to_phone: str, text: str) -> Dict[str, Any]:
        return await self.provider.send_text(to_phone, text)

    async def send_video_call_invitation(
        self,
        to_phone: str,
        doctor_name: str,
        specialty: str,
        video_url: str
    ) -> Dict[str, Any]:
        """Interactive WhatsApp message with a "Join video call" URL button.

        The URL is also in the body text, so the message still works if the button doesn't render
        (and the provider resends it as plain text if WhatsApp rejects the interactive message).
        """
        body = (
            f"{with_title(doctor_name)} ({specialty}) has joined your emergency case and requested an immediate live video assessment.\n\n"
            f"Tap *Join video call* to enter the secure room on your phone (no app installation required).\n\n"
            f"If the button doesn't work, open this link:\n{video_url}\n\n"
            f"Stay calm. Your doctor is with you."
        )
        return await self.provider.send_video_invite(
            to_phone,
            body=body,
            button_text="Join video call",
            url=video_url,
            header="Emergency video consultation",
            footer="ResQ-AI Emergency Response",
        )

    async def send_location_request(self, to_phone: str, body: str) -> Dict[str, Any]:
        """Interactive "Send location" button (text instructions if WhatsApp rejects it)."""
        return await self.provider.send_location_request(to_phone, body)

    async def send_emergency_location_alert(self, to_phone: str, patient_name: str, maps_url: str) -> Dict[str, Any]:
        """Template message to the emergency contact with the patient's location. A template because
        the contact has usually never messaged this number (outside the 24-hour window)."""
        return await self.provider.send_template(
            to_phone,
            settings.WHATSAPP_LOCATION_TEMPLATE_NAME,
            settings.WHATSAPP_LOCATION_TEMPLATE_LANGUAGE,
            [patient_name, maps_url],
        )

    async def send_doctor_case_offer(self, to_phone: str, esi_level: int, specialty: str, summary: str,
                                     answer_within: str, accept_payload: str, decline_payload: str) -> Dict[str, Any]:
        """Offers a case to a doctor (OFFER-2): template `WHATSAPP_OFFER_TEMPLATE_NAME` with Accept /
        Decline quick replies (works outside the 24-hour window). If the template send fails (e.g. not
        approved yet), interactive reply buttons with the same payloads, which only reach a doctor who
        messaged ResQ in the last 24 hours. `channel` in the result says which one went out."""
        summary = " ".join((summary or "No summary yet.").split())[:300]
        result = await self.provider.send_template(
            to_phone, settings.WHATSAPP_OFFER_TEMPLATE_NAME, "en",
            [str(esi_level), specialty, summary, answer_within],
            quick_reply_payloads=[accept_payload, decline_payload],
        )
        if result.get("status") != "ERROR":
            return {**result, "channel": "template"}
        print(f"[WhatsApp] Offer template to {to_phone} failed ({result.get('detail')}); trying reply buttons.")
        fallback = await self.provider.send_reply_buttons(
            to_phone,
            body=(f"A ResQ-AI patient needs a doctor. ESI level {esi_level}, specialty {specialty}.\n\n"
                  f"Summary: {summary}\n\nPlease accept within {answer_within}, otherwise the case is "
                  "offered to the next doctor."),
            buttons=[{"id": accept_payload, "title": "Accept"}, {"id": decline_payload, "title": "Decline"}],
            header="New emergency case",
            footer="ResQ-AI Emergency Response",
        )
        return {**fallback, "channel": "interactive", "template_error": result.get("detail")}

    async def send_case_link(self, to_phone: str, body: str, button_text: str, url: str,
                             header: Optional[str] = None) -> Dict[str, Any]:
        """Text with one URL button, e.g. the portal link for a doctor (text with the link if the
        interactive message is rejected; `body` should contain the URL)."""
        return await self.provider.send_video_invite(to_phone, body=body, button_text=button_text, url=url,
                                                     header=header, footer="ResQ-AI Emergency Response")

    async def send_reply_buttons(self, to_phone: str, body: str, buttons: List[Dict[str, str]]) -> Dict[str, Any]:
        """Text with up to three reply buttons ({"id": payload, "title": ...}); a tap comes back as a
        `button` message with the id as payload. Only inside the 24-hour window."""
        return await self.provider.send_reply_buttons(to_phone, body=body, buttons=buttons,
                                                      footer="ResQ-AI Emergency Response")

    async def send_location(self, to_phone: str, latitude: float, longitude: float,
                            name: Optional[str] = None, address: Optional[str] = None) -> Dict[str, Any]:
        """A map pin (DOC-3: the patient's shared location, forwarded to their doctor)."""
        return await self.provider.send_location(to_phone, latitude, longitude, name=name, address=address)

    async def send_list_message(self, to_phone: str, body: str, button_text: str,
                                rows: List[Dict[str, str]], header: Optional[str] = None) -> Dict[str, Any]:
        """Interactive list of up to 10 choices ({"id": payload, "title", "description"?}); a tap comes
        back as a `button` message with the id as payload (DOC-4: the doctor's @resq menu). Only
        inside the 24-hour window; text with the choices if WhatsApp rejects it."""
        return await self.provider.send_list(to_phone, body=body, button=button_text, rows=rows,
                                             header=header, footer="ResQ-AI Emergency Response")

    async def send_doctor_patient_waiting(self, to_phone: str, patient_name: str, show_payload: str) -> Dict[str, Any]:
        """DOC-6: a patient wrote to a doctor whose 24-hour window is closed. Template
        `WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME` ({{1}} patient) with a **Show messages** quick reply;
        the tap opens the window and brings the messages."""
        return await self.provider.send_template(
            to_phone, settings.WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME, "en", [patient_name],
            quick_reply_payloads=[show_payload],
        )

    async def send_doctor_number_confirmation(self, to_phone: str, doctor_name: str, yes_payload: str,
                                              no_payload: str) -> Dict[str, Any]:
        """DOC-8: asks a newly registered doctor to confirm their number. Template
        `WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME` ({{1}} doctor) with **Yes, it's me** / **No, not me**
        quick replies; interactive reply buttons with the same payloads if the template send fails."""
        result = await self.provider.send_template(
            to_phone, settings.WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME, "en", [doctor_name],
            quick_reply_payloads=[yes_payload, no_payload],
        )
        if result.get("status") != "ERROR":
            return {**result, "channel": "template"}
        print(f"[WhatsApp] Number confirmation template to {to_phone} failed ({result.get('detail')}); "
              "trying reply buttons.")
        fallback = await self.provider.send_reply_buttons(
            to_phone,
            body=(f"This number was added to ResQ-AI as the WhatsApp number of {doctor_name}. Emergency case "
                  "offers and patients' messages will come here. Is this your number?"),
            buttons=[{"id": yes_payload, "title": "Yes, it's me"}, {"id": no_payload, "title": "No, not me"}],
            header="Confirm your number",
            footer="ResQ-AI Emergency Response",
        )
        return {**fallback, "channel": "interactive", "template_error": result.get("detail")}

    async def send_onboarding_prompt(self, to_phone: str) -> Dict[str, Any]:
        """Sends welcome and emergency contact capture prompt for first-time users"""
        prompt = (
            "🚨 *Welcome to ResQ-AI Emergency Response*\n\n"
            "Who should we call for you in an emergency? Tell us your emergency contact's name and phone "
            "number however you like: type it, or send a voice note in Urdu, Hindi or English.\n"
            "Example: _My name is Ahmed, my brother Ali's number is 0333 1234567_\n\n"
            "⚠️ *If you are having an emergency right now, send a voice note immediately!*"
        )
        return await self.send_text_message(to_phone, prompt)

    async def send_registration_confirmation(
        self, to_phone: str, name: str, contact_name: str, contact_phone: str
    ) -> Dict[str, Any]:
        text = (
            f"✅ *Registration complete, {name}.*\n\n"
            f"Emergency contact: *{contact_name}* ({contact_phone}). We will call them automatically if you report a critical emergency, and send them your location when you share it.\n\n"
            "⚠️ *In an emergency, send a voice note right away in Urdu, Hindi, or English.*"
        )
        return await self.send_text_message(to_phone, text)

    async def send_registration_format_hint(self, to_phone: str, problem: Optional[str] = None) -> Dict[str, Any]:
        """A registration we could not complete. `problem` (app.registration): "missing" (no number),
        "invalid" (not a full phone number) or "own_number" (the patient's own)."""
        ask = {
            "invalid": "That phone number looks incomplete. Please send your emergency contact's number "
                       "again with all its digits, e.g. _0333 1234567_ or _+92 333 1234567_.",
            "own_number": "That is your own number. Please send the number of someone we should call for "
                          "you, such as a family member.",
        }.get(problem, "We couldn't catch your emergency contact's phone number. Please send their name and "
                       "number again, typed or in a voice note, e.g. _My brother Ali, 0333 1234567_.")
        text = f"{ask}\n\n⚠️ *If this is an emergency, don't wait: send a voice note now.*"
        return await self.send_text_message(to_phone, text)

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject:
        """Stores an inbound voice note (or, with kind="image", a photo) in the S3 media vault.
        Raises MediaFetchError on failure."""
        return await self.provider.fetch_media_to_s3(media_id, user_phone, kind=kind, mime_type=mime_type)

    async def send_media_message(self, to_phone: str, kind: str, key: str, data: bytes, content_type: str,
                                 caption: Optional[str] = None) -> Dict[str, Any]:
        """A doctor's photo (kind="image", with an optional caption) or voice note (kind="audio")
        to the patient (IMAGE-1). The file must already be in the vault at `key`: it is uploaded to
        WhatsApp, then sent by media ID. Never raises: an ERROR result carries `detail`."""
        try:
            media_id = await self.provider.upload_media(data, key, content_type)
        except Exception as e:
            print(f"[WhatsApp] Media upload for {to_phone} failed: {e}")
            return {"status": "ERROR", "provider": self.provider_name, "recipient": to_phone, "detail": str(e)}
        if kind == "image":
            return await self.provider.send_image(to_phone, media_id, caption)
        return await self.provider.send_audio(to_phone, media_id)


whatsapp_service = WhatsAppService()
