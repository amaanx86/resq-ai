import asyncio
import boto3
import uuid
from typing import Dict, Any
from app.config import settings


class TelephonyService:
    """Amazon Connect outbound calls to the emergency contact.

    Real only when MOCK_AWS=False and no AWS_ENDPOINT_URL (Floci does not emulate Connect, so Floci
    mode simulates the call and says so in the logs and the result). There is no ambulance dispatch
    here: the assigned doctor calls the ambulance.
    """

    def __init__(self):
        self.local_mode = settings.MOCK_AWS or bool(settings.AWS_ENDPOINT_URL)
        self.mock_mode = self.local_mode
        self.connect = None
        if not self.mock_mode:
            try:
                self.connect = boto3.client('connect', region_name=settings.AWS_REGION)
            except Exception as e:
                # Not local here, so this is real AWS: a simulated call would tell the patient their
                # emergency contact was phoned when nobody was. Fail fast instead.
                raise RuntimeError(
                    f"[TelephonyService] MOCK_AWS=False but the Amazon Connect client failed ({e})."
                ) from e
        if self.mock_mode:
            reason = "Floci does not emulate Connect" if settings.AWS_ENDPOINT_URL else "MOCK_AWS / no client"
            print(f"[TelephonyService] Amazon Connect calls are SIMULATED ({reason}).")
        else:
            print(f"[TelephonyService] Amazon Connect live (instance {settings.CONNECT_INSTANCE_ID}).")

    @staticmethod
    def spoken_message(patient_name: str, emergency_details: str, language: str) -> str:
        """Polly prompt in the caller's language. Polly has no Urdu voice, so the Urdu message is
        written in Devanagari (spoken Hindustani) for the Hindi voice the Connect flow picks."""
        if language == "Urdu":
            return f"इमरजेंसी अलर्ट! आपके अज़ीज़ {patient_name} के लिए एक हंगामी मेडिकल सूरत-ए-हाल रिपोर्ट हुई है: {emergency_details}। एक स्पेशलिस्ट डॉक्टर केस देख रहा है और एम्बुलेंस का इंतज़ाम करेगा। मरीज़ की लोकेशन मिलते ही आपको व्हाट्सऐप पर भेज दी जाएगी।"
        if language == "Hindi":
            return f"आपातकालीन चेतावनी! आपके परिजन {patient_name} के लिए एक आपातकालीन स्थिति दर्ज हुई है: {emergency_details}। एक विशेषज्ञ डॉक्टर मामला देख रहे हैं और एम्बुलेंस की व्यवस्था करेंगे। मरीज़ का लोकेशन मिलते ही आपको व्हाट्सऐप पर भेज दिया जाएगा।"
        return f"Emergency Alert! A critical medical situation has been reported for your contact {patient_name}: {emergency_details}. A specialist doctor is handling the case and will arrange an ambulance. The patient's location will be sent to you on WhatsApp once they share it."

    async def call_emergency_contact(
        self,
        contact_phone: str,
        patient_name: str,
        emergency_details: str,
        language: str = "Urdu"
    ) -> Dict[str, Any]:
        """
        Places an automated call to the registered emergency contact through Amazon Connect
        (Polly neural TTS in Urdu/Hindi/English). Returns status CALL_INITIATED or FAILED.
        """
        spoken_message = self.spoken_message(patient_name, emergency_details, language)
        return await self._place_call(contact_phone, spoken_message, language, {
            "PatientName": patient_name,
            "EmergencyDetails": emergency_details,
        })

    async def place_announcement_call(self, phone: str, spoken_message: str, language: str = "English") -> Dict[str, Any]:
        """Calls `phone` and reads `spoken_message` (e.g. the no-doctor alert to an administrator,
        ADMIN-1). Same contact flow as the emergency-contact call: it only reads `Language` (voice)
        and `SpokenMessage`. Returns status CALL_INITIATED or FAILED, never raises."""
        return await self._place_call(phone, spoken_message, language, {"Purpose": "admin_alert"})

    async def _place_call(self, phone: str, spoken_message: str, language: str,
                          extra_attributes: Dict[str, str]) -> Dict[str, Any]:
        if self.mock_mode:
            return {
                "status": "CALL_INITIATED",
                "provider": "Amazon Connect (Simulated)",
                "simulated": True,
                "contact_phone": phone,
                "contact_id": f"CONN-{uuid.uuid4().hex[:8].upper()}",
                "spoken_message": spoken_message,
                "language": language
            }

        try:
            # boto3 is synchronous: run it in a thread so the other emergency tools overlap (AGENT-4).
            res = await asyncio.to_thread(
                self.connect.start_outbound_voice_contact,
                DestinationPhoneNumber=phone,
                ContactFlowId=settings.CONNECT_FLOW_ID,
                InstanceId=settings.CONNECT_INSTANCE_ID,
                SourcePhoneNumber=settings.CONNECT_SOURCE_PHONE_NUMBER,
                Attributes={
                    **extra_attributes,
                    "Language": language,
                    "SpokenMessage": spoken_message
                }
            )
            return {
                "status": "CALL_INITIATED",
                "provider": "Amazon Connect",
                "simulated": False,
                "contact_phone": phone,
                "contact_id": res.get("ContactId"),
                "language": language
            }
        except Exception as e:
            print(f"[TelephonyService] Connect StartOutboundVoiceContact FAILED: {e}")
            return {
                "status": "FAILED",
                "provider": "Amazon Connect",
                "contact_phone": phone,
                "contact_id": None,
                "error": str(e)
            }

telephony_service = TelephonyService()
