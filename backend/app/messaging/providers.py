"""Outbound WhatsApp providers (send + media download).

Selected by `WHATSAPP_PROVIDER` through `app.messaging.factory.build_provider`; nothing outside
`app/messaging/` should know which one is active. Provider-specific details (recipient phone
format, API versions, message IDs, media download) stay in here.

Send methods never raise: they return a dict with `status` "SENT" | "SENT_MOCK" | "ERROR",
`provider`, `recipient` and `message_id` (or `detail` on error), as the old service did.
`fetch_media_to_s3` raises `MediaFetchError` when an inbound voice note or photo cannot be stored,
so the caller can take the "transcription / analysis failed" path instead of reading an empty
object. Outbound media (a doctor's photo or voice note, IMAGE-1) is uploaded to WhatsApp first with
`upload_media` (raises `MediaUploadError`), then sent by media ID with `send_image` / `send_audio`.
"""
import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import httpx

from app.config import settings
from app.messaging import payloads
from app.models.schemas import AudioObject
from app.services.audio_service import audio_service

# Minimal valid Ogg/Opus header, used by the mock provider as a stand-in voice note.
STUB_OGG = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead"
# 1x1 PNG, the mock provider's stand-in photo.
STUB_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS"
            b"\xde\x00\x00\x00\x0cIDATx\x9cc8!'\x07\x00\x02\xb6\x01\x054\xa6u\xaa\x00\x00\x00\x00IEND\xaeB`\x82")

MIME_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
}


class MediaFetchError(RuntimeError):
    """The provider could not download the media or store it in the S3 audio vault."""


class MediaUploadError(RuntimeError):
    """The provider could not upload outbound media to WhatsApp."""


def _digits(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def extension_for_mime(mime_type: Optional[str]) -> str:
    base = (mime_type or "").split(";")[0].strip().lower()
    return MIME_EXTENSIONS.get(base, "ogg")


@runtime_checkable
class WhatsAppProvider(Protocol):
    name: str

    def format_recipient(self, phone: str) -> str: ...

    async def send_payload(self, to_phone: str, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    async def send_text(self, to_phone: str, text: str) -> Dict[str, Any]: ...

    async def send_video_invite(
        self, to_phone: str, body: str, button_text: str, url: str,
        header: Optional[str] = None, footer: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    async def send_location_request(self, to_phone: str, body: str) -> Dict[str, Any]: ...

    async def send_template(self, to_phone: str, name: str, language: str, body_params: List[str],
                            quick_reply_payloads: Optional[List[str]] = None,
                            url_suffixes: Optional[List[str]] = None) -> Dict[str, Any]: ...

    async def send_reply_buttons(self, to_phone: str, body: str, buttons: List[Dict[str, str]],
                                 header: Optional[str] = None, footer: Optional[str] = None) -> Dict[str, Any]: ...

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject: ...

    async def upload_media(self, data: bytes, key: str, content_type: str) -> str: ...

    async def send_image(self, to_phone: str, media_id: str, caption: Optional[str] = None) -> Dict[str, Any]: ...

    async def send_audio(self, to_phone: str, media_id: str) -> Dict[str, Any]: ...

    async def send_location(self, to_phone: str, latitude: float, longitude: float,
                            name: Optional[str] = None, address: Optional[str] = None) -> Dict[str, Any]: ...

    async def send_list(self, to_phone: str, body: str, button: str, rows: List[Dict[str, str]],
                        header: Optional[str] = None, footer: Optional[str] = None) -> Dict[str, Any]: ...


def _vault_key(kind: str, user_phone: str, mime_type: Optional[str]) -> str:
    """New vault key for inbound media: `images/...` for a photo, `voice-notes/...` otherwise."""
    if kind == "image":
        return audio_service.build_image_key(user_phone, mime_type)
    return audio_service.build_audio_key(user_phone)


class _BaseProvider:
    name = "base"

    def format_recipient(self, phone: str) -> str:
        return f"+{_digits(phone)}"

    async def send_payload(self, to_phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def _result(self, status: str, to_phone: str, **extra: Any) -> Dict[str, Any]:
        return {"status": status, "provider": self.name, "recipient": to_phone, **extra}

    async def send_text(self, to_phone: str, text: str) -> Dict[str, Any]:
        payload = payloads.text_message(self.format_recipient(to_phone), text)
        return await self.send_payload(to_phone, payload)

    async def send_video_invite(
        self, to_phone: str, body: str, button_text: str, url: str,
        header: Optional[str] = None, footer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Interactive CTA URL button. If WhatsApp rejects it (e.g. a non-public URL), resend as text.

        `body` should already contain the URL so the text fallback still works.
        """
        payload = payloads.cta_url_message(
            self.format_recipient(to_phone), body, button_text, url, header=header, footer=footer
        )
        result = await self.send_payload(to_phone, payload)
        if result.get("status") == "ERROR":
            print(f"[WhatsApp:{self.name}] Interactive video invite rejected ({result.get('detail')}); sending as text.")
            fallback_text = f"*{header}*\n\n{body}" if header else body
            fallback = await self.send_text(to_phone, fallback_text)
            fallback["fallback_from"] = "interactive"
            return fallback
        return result

    async def send_location_request(self, to_phone: str, body: str) -> Dict[str, Any]:
        """Interactive "Send location" button. If WhatsApp rejects it, resend the body as text (the
        patient can still attach a location by hand)."""
        result = await self.send_payload(to_phone, payloads.location_request_message(self.format_recipient(to_phone), body))
        if result.get("status") == "ERROR":
            print(f"[WhatsApp:{self.name}] Location request rejected ({result.get('detail')}); sending as text.")
            fallback = await self.send_text(
                to_phone, f"{body}\n\nTap 📎 (attach) > *Location* > *Send your current location*.")
            fallback["fallback_from"] = "interactive"
            return fallback
        return result

    async def send_template(self, to_phone: str, name: str, language: str, body_params: List[str],
                            quick_reply_payloads: Optional[List[str]] = None,
                            url_suffixes: Optional[List[str]] = None) -> Dict[str, Any]:
        payload = payloads.template_message(self.format_recipient(to_phone), name, language, body_params,
                                            quick_reply_payloads=quick_reply_payloads, url_suffixes=url_suffixes)
        return await self.send_payload(to_phone, payload)

    async def send_reply_buttons(self, to_phone: str, body: str, buttons: List[Dict[str, str]],
                                 header: Optional[str] = None, footer: Optional[str] = None) -> Dict[str, Any]:
        payload = payloads.reply_buttons_message(self.format_recipient(to_phone), body, buttons,
                                                 header=header, footer=footer)
        return await self.send_payload(to_phone, payload)

    async def send_image(self, to_phone: str, media_id: str, caption: Optional[str] = None) -> Dict[str, Any]:
        return await self.send_payload(to_phone, payloads.image_message(self.format_recipient(to_phone), media_id, caption))

    async def send_audio(self, to_phone: str, media_id: str) -> Dict[str, Any]:
        return await self.send_payload(to_phone, payloads.audio_message(self.format_recipient(to_phone), media_id))

    async def send_location(self, to_phone: str, latitude: float, longitude: float,
                            name: Optional[str] = None, address: Optional[str] = None) -> Dict[str, Any]:
        return await self.send_payload(to_phone, payloads.location_message(
            self.format_recipient(to_phone), latitude, longitude, name=name, address=address))

    async def send_list(self, to_phone: str, body: str, button: str, rows: List[Dict[str, str]],
                        header: Optional[str] = None, footer: Optional[str] = None) -> Dict[str, Any]:
        """Interactive list. If WhatsApp rejects it, the rows are resent as text (typed commands)."""
        result = await self.send_payload(to_phone, payloads.list_message(
            self.format_recipient(to_phone), body, button, rows, header=header, footer=footer))
        if result.get("status") == "ERROR":
            print(f"[WhatsApp:{self.name}] List message rejected ({result.get('detail')}); sending as text.")
            lines = "\n".join(f"• {r['title']}" + (f": {r['description']}" if r.get("description") else "")
                              for r in rows)
            fallback = await self.send_text(to_phone, f"{body}\n\n{lines}")
            fallback["fallback_from"] = "interactive"
            return fallback
        return result

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject:
        raise NotImplementedError

    async def upload_media(self, data: bytes, key: str, content_type: str) -> str:
        raise NotImplementedError


class MetaCloudProvider(_BaseProvider):
    """Meta WhatsApp Cloud API over the Graph API (local development, rollback path)."""

    name = "meta"
    GRAPH_BASE_URL = "https://graph.facebook.com"

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v20.0",
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 15.0,
    ):
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self._transport = transport
        self._timeout = timeout

    @property
    def messages_url(self) -> str:
        return f"{self.GRAPH_BASE_URL}/{self.api_version}/{self.phone_number_id}/messages"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    def format_recipient(self, phone: str) -> str:
        # The Graph API expects the number without the leading '+'.
        return _digits(phone)

    async def send_payload(self, to_phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with self._client() as client:
                res = await client.post(self.messages_url, json=payload, headers=self._headers())
            try:
                data = res.json()
            except ValueError:
                data = {"raw": res.text}
            if res.status_code >= 400 or (isinstance(data, dict) and "error" in data):
                detail = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
                print(f"[WhatsApp:meta] Send to {to_phone} failed: HTTP {res.status_code} {data}")
                return self._result("ERROR", to_phone, http_status=res.status_code, detail=detail or str(data))
            message_id = ((data.get("messages") or [{}])[0] or {}).get("id")
            print(f"[WhatsApp:meta] Sent {payload.get('type')} to {to_phone} ({message_id})")
            return self._result("SENT", to_phone, message_id=message_id)
        except Exception as e:
            print(f"[WhatsApp:meta] Error sending message to {to_phone}: {e}")
            return self._result("ERROR", to_phone, detail=str(e))

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject:
        """Graph API: GET /{media_id} for a short-lived URL, download it, upload to the audio vault
        (`voice-notes/`, or `images/` for a photo)."""
        try:
            async with self._client() as client:
                info_res = await client.get(
                    f"{self.GRAPH_BASE_URL}/{self.api_version}/{media_id}", headers=self._headers()
                )
                info_res.raise_for_status()
                info = info_res.json()
                media_url = info.get("url")
                mime_type = mime_type or info.get("mime_type")
                if not media_url:
                    raise MediaFetchError(f"Graph API returned no URL for media {media_id}")
                audio_res = await client.get(media_url, headers=self._headers(), follow_redirects=True)
                audio_res.raise_for_status()
                audio_bytes = audio_res.content
        except MediaFetchError:
            raise
        except Exception as e:
            raise MediaFetchError(f"Meta media download failed for {media_id}: {e}") from e
        if not audio_bytes:
            raise MediaFetchError(f"Meta media {media_id} is empty")
        try:
            if kind == "image":
                audio = await asyncio.to_thread(audio_service.upload_image_to_s3, audio_bytes, user_phone, mime_type)
            else:
                audio = await asyncio.to_thread(audio_service.upload_audio_to_s3, audio_bytes, user_phone)
        except Exception as e:
            raise MediaFetchError(f"Storing Meta media {media_id} in S3 failed: {e}") from e
        print(f"[WhatsApp:meta] Stored media {media_id} ({len(audio_bytes)} bytes) at {audio.s3_uri}")
        return audio

    async def upload_media(self, data: bytes, key: str, content_type: str) -> str:
        """Graph API: POST /{phone_number_id}/media (multipart) -> media ID for a send."""
        filename = key.rsplit("/", 1)[-1]
        try:
            async with self._client() as client:
                res = await client.post(
                    f"{self.GRAPH_BASE_URL}/{self.api_version}/{self.phone_number_id}/media",
                    headers=self._headers(),
                    data={"messaging_product": "whatsapp", "type": content_type},
                    files={"file": (filename, data, content_type)},
                )
            body = res.json() if res.content else {}
        except Exception as e:
            raise MediaUploadError(f"Meta media upload of {filename} failed: {e}") from e
        if res.status_code >= 400 or not isinstance(body, dict) or not body.get("id"):
            detail = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
            raise MediaUploadError(f"Meta media upload of {filename} failed: HTTP {res.status_code} {detail or body}")
        print(f"[WhatsApp:meta] Uploaded {filename} ({len(data)} bytes, {content_type}) as media {body['id']}")
        return str(body["id"])


class EumSocialProvider(_BaseProvider):
    """AWS End User Messaging Social (boto3 `socialmessaging`), used in production on AWS."""

    name = "eum_social"

    def __init__(
        self,
        phone_number_id: str,
        api_version: str = "v20.0",
        client: Any = None,
        s3_client: Any = None,
        bucket: Optional[str] = None,
    ):
        if client is None:
            import boto3
            client = boto3.client("socialmessaging", region_name=settings.AWS_REGION)
        self.client = client
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self._s3_client = s3_client
        self.bucket = bucket or settings.S3_AUDIO_BUCKET

    @property
    def s3(self) -> Any:
        return self._s3_client if self._s3_client is not None else audio_service.s3

    async def send_payload(self, to_phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                self.client.send_whatsapp_message,
                originationPhoneNumberId=self.phone_number_id,
                message=json.dumps(payload).encode("utf-8"),
                metaApiVersion=self.api_version,
            )
            message_id = response.get("messageId")
            print(f"[WhatsApp:eum_social] Sent {payload.get('type')} to {to_phone} ({message_id})")
            return self._result("SENT", to_phone, message_id=message_id)
        except Exception as e:
            print(f"[WhatsApp:eum_social] Error sending message to {to_phone}: {e}")
            return self._result("ERROR", to_phone, detail=str(e))

    def _locate_object(self, prefix: str, media_id: str, mime_type: Optional[str]) -> str:
        """The service stores the file at `<key prefix><mediaId>[.<ext>]`; find the exact key."""
        s3 = self.s3
        if s3 is not None:
            try:
                listing = s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=10)
                keys = sorted((obj["Key"] for obj in listing.get("Contents", [])), key=len)
                if keys:
                    return keys[0]
            except Exception as e:
                print(f"[WhatsApp:eum_social] Could not list s3://{self.bucket}/{prefix}* ({e}); guessing the key.")
        return f"{prefix}{media_id}.{extension_for_mime(mime_type)}"

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject:
        """get_whatsapp_message_media writes the file straight to the audio vault (no bytes pass through us)."""
        key = _vault_key(kind, user_phone, mime_type)
        prefix = key.rsplit(".", 1)[0] + "_"
        try:
            response = await asyncio.to_thread(
                self.client.get_whatsapp_message_media,
                mediaId=media_id,
                originationPhoneNumberId=self.phone_number_id,
                destinationS3File={"bucketName": self.bucket, "key": prefix},
            )
        except Exception as e:
            raise MediaFetchError(f"End User Messaging Social media download failed for {media_id}: {e}") from e
        stored_key = await asyncio.to_thread(self._locate_object, prefix, media_id,
                                             response.get("mimeType") or mime_type)
        print(f"[WhatsApp:eum_social] Stored media {media_id} ({response.get('fileSize')} KB) at s3://{self.bucket}/{stored_key}")
        return audio_service.audio_object_for_key(stored_key)

    async def upload_media(self, data: bytes, key: str, content_type: str) -> str:
        """post_whatsapp_message_media reads the object the caller already stored at `key` in the
        vault (with the caller's credentials) -> media ID for a send. `data` is not used."""
        try:
            response = await asyncio.to_thread(
                self.client.post_whatsapp_message_media,
                originationPhoneNumberId=self.phone_number_id,
                sourceS3File={"bucketName": self.bucket, "key": key},
            )
        except Exception as e:
            raise MediaUploadError(f"End User Messaging Social media upload of s3://{self.bucket}/{key} failed: {e}") from e
        media_id = response.get("mediaId")
        if not media_id:
            raise MediaUploadError(f"End User Messaging Social returned no mediaId for s3://{self.bucket}/{key}")
        print(f"[WhatsApp:eum_social] Uploaded s3://{self.bucket}/{key} as media {media_id}")
        return media_id


class MockProvider(_BaseProvider):
    """Prints outbound messages; stores a stub OGG for media. Tests and offline demos."""

    name = "mock"

    def __init__(self):
        self.sent: list = []  # payloads, newest last (handy in tests)

    async def send_payload(self, to_phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.sent.append(payload)
        if payload.get("type") == "interactive":
            interactive = payload["interactive"]
            params = interactive["action"].get("parameters") or {}
            if params:
                button = f"{params['display_text']} -> {params['url']}"
            elif interactive["action"].get("buttons"):
                button = " | ".join(b["reply"]["title"] for b in interactive["action"]["buttons"])
            elif interactive["action"].get("sections"):
                button = " | ".join(r["title"] for s in interactive["action"]["sections"] for r in s["rows"])
            else:
                button = interactive["action"].get("name", "")
            text = f"{interactive['body']['text']}\n[Button: {button}]"
        elif payload.get("type") in ("image", "audio"):
            media = payload[payload["type"]]
            label = "Photo" if payload["type"] == "image" else "Voice note"
            text = f"[{label} {media.get('id')}] {media.get('caption') or ''}".rstrip()
        elif payload.get("type") == "location":
            loc = payload["location"]
            text = f"[Location {loc['latitude']},{loc['longitude']}] {loc.get('name') or ''}".rstrip()
        elif payload.get("type") == "template":
            template = payload["template"]
            params = [p.get("text") or p.get("payload") or "" for c in template.get("components", [])
                      for p in c.get("parameters", [])]
            text = f"[Template {template['name']}] {' | '.join(params)}"
        else:
            text = (payload.get("text") or {}).get("body", "")
        print(f"[WhatsApp-Outbound to {to_phone}]: {text}")
        return self._result("SENT_MOCK", to_phone, message=text)

    async def fetch_media_to_s3(self, media_id: str, user_phone: str, kind: str = "audio",
                                mime_type: Optional[str] = None) -> AudioObject:
        try:
            if kind == "image":
                audio = await asyncio.to_thread(audio_service.upload_image_to_s3, STUB_PNG, user_phone, "image/png")
            else:
                audio = await asyncio.to_thread(audio_service.upload_audio_to_s3, STUB_OGG, user_phone)
        except Exception as e:
            raise MediaFetchError(f"Storing stub media {media_id} in S3 failed: {e}") from e
        print(f"[WhatsApp:mock] Stored stub {kind} for media {media_id} at {audio.s3_uri}")
        return audio

    async def upload_media(self, data: bytes, key: str, content_type: str) -> str:
        media_id = f"mock-media-{uuid.uuid4().hex[:8]}"
        print(f"[WhatsApp:mock] Uploaded {key} ({len(data)} bytes, {content_type}) as {media_id}")
        return media_id
