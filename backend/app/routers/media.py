"""Voice note playback and photo endpoints (UI-1, IMAGE-1).

GET .../audio returns a playable URL: a short-lived presigned S3 URL in real AWS, or the relative
.../audio/stream endpoint in Floci/mock mode (the browser cannot reach Floci's S3 or memory).
The same pair exists for voice notes attached to chat messages (handover mode, FLOW-2), and
.../image for photos: the patient's photo on the case, and photos in chat messages (the patient's,
or ones the doctor sent).
"""
from typing import Iterator, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from app import auth
from app.models.schemas import EmergencyCase
from app.services.audio_service import audio_service
from app.services.dynamo_service import dynamo_service

router = APIRouter(prefix="/api/v1/cases", tags=["Voice Note and Photo Media"])

AUDIO_URL_TTL_SECONDS = 300


def _visible_case(request: Request, case_id: str) -> EmergencyCase:
    case = dynamo_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Emergency case not found")
    auth.assert_case_visible(request, case)
    return case


_WHAT = {"audio_s3_key": "voice note", "image_s3_key": "photo"}


def _case_media_key(request: Request, case_id: str, field: str = "audio_s3_key") -> str:
    case = _visible_case(request, case_id)
    key = getattr(case, field)
    if not key:
        raise HTTPException(status_code=404, detail=f"This case has no {_WHAT[field]}")
    return key


def _message_media_key(request: Request, case_id: str, message_id: str, field: str = "audio_s3_key") -> str:
    _visible_case(request, case_id)
    msg = next((m for m in dynamo_service.get_case_messages(case_id) if m.message_id == message_id), None)
    if not msg:
        raise HTTPException(status_code=404, detail="Chat message not found")
    key = getattr(msg, field)
    if not key:
        raise HTTPException(status_code=404, detail=f"This message has no {_WHAT[field]}")
    return key


def _playable(key: str, stream_path: str) -> dict:
    url: Optional[str] = audio_service.presign_audio_url(key, AUDIO_URL_TTL_SECONDS)
    return {"url": url or stream_path, "expires_in": AUDIO_URL_TTL_SECONDS}


def _iter_body(body, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    if isinstance(body, (bytes, bytearray)):
        yield bytes(body)
        return
    try:
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def _stream(key: str) -> StreamingResponse:
    obj = audio_service.get_audio_object(key)
    if not obj:
        raise HTTPException(status_code=404, detail="Media not found in the vault")
    body, content_type, length = obj
    headers = {"Cache-Control": "private, max-age=300"}
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(_iter_body(body), media_type=content_type, headers=headers)


@router.get("/{case_id}/audio")
def get_case_audio(case_id: str, request: Request):
    """Playable URL for the case's original voice note, or 404 if it has none (e.g. simulated notes)."""
    key = _case_media_key(request, case_id)
    return _playable(key, f"/api/v1/cases/{case_id}/audio/stream")


@router.get("/{case_id}/audio/stream")
def stream_case_audio(case_id: str, request: Request):
    """Streams the case's voice note from S3 (Floci) or memory (mock) with its stored content type."""
    return _stream(_case_media_key(request, case_id))


@router.get("/{case_id}/messages/{message_id}/audio")
def get_message_audio(case_id: str, message_id: str, request: Request):
    """Playable URL for a voice note sent as a chat message after handover."""
    key = _message_media_key(request, case_id, message_id)
    return _playable(key, f"/api/v1/cases/{case_id}/messages/{message_id}/audio/stream")


@router.get("/{case_id}/messages/{message_id}/audio/stream")
def stream_message_audio(case_id: str, message_id: str, request: Request):
    return _stream(_message_media_key(request, case_id, message_id))


@router.get("/{case_id}/image")
def get_case_image(case_id: str, request: Request):
    """Viewable URL for the patient's photo on the case (the one that opened it, or the first sent on it)."""
    key = _case_media_key(request, case_id, "image_s3_key")
    return _playable(key, f"/api/v1/cases/{case_id}/image/stream")


@router.get("/{case_id}/image/stream")
def stream_case_image(case_id: str, request: Request):
    return _stream(_case_media_key(request, case_id, "image_s3_key"))


@router.get("/{case_id}/messages/{message_id}/image")
def get_message_image(case_id: str, message_id: str, request: Request):
    """Viewable URL for a photo in a chat message (the patient's, or one the doctor sent)."""
    key = _message_media_key(request, case_id, message_id, "image_s3_key")
    return _playable(key, f"/api/v1/cases/{case_id}/messages/{message_id}/image/stream")


@router.get("/{case_id}/messages/{message_id}/image/stream")
def stream_message_image(case_id: str, message_id: str, request: Request):
    return _stream(_message_media_key(request, case_id, message_id, "image_s3_key"))
