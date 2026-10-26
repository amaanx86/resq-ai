"""Prepares a doctor's photo or voice note from the portal for WhatsApp (IMAGE-1).

WhatsApp takes JPEG / PNG photos up to 5 MB. Photos in another format (WebP, GIF, ...) or over
5 MB are re-encoded as JPEG with Pillow.

Voice notes are always re-encoded as mono OGG/Opus with PyAV (bundled ffmpeg), the only format
WhatsApp plays as a voice note. Browser recordings sent as they were (Chrome's fragmented MP4,
Firefox's possibly stereo OGG) were accepted but never reached the patient, who only got the
text sent before them. Any audio ffmpeg reads is accepted (OGG, MP4/AAC, WebM, MP3, ...);
unreadable audio raises `UnsupportedMedia`.
"""
import io
from typing import Optional, Tuple

WHATSAPP_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
WHATSAPP_IMAGE_MAX_BYTES = 5 * 1024 * 1024
VOICE_NOTE_BITRATE = 32000   # mono speech
OUTBOUND_IMAGE_MAX_SIDE_PX = 2560


class UnsupportedMedia(ValueError):
    """The file cannot be sent on WhatsApp (wrong type, or unreadable)."""


def base_type(content_type: Optional[str]) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def prepare_image(data: bytes, content_type: Optional[str]) -> Tuple[bytes, str, str]:
    """(bytes, content type, extension) of a photo WhatsApp accepts."""
    ct = base_type(content_type)
    if ct == "image/jpg":
        ct = "image/jpeg"
    if ct in WHATSAPP_IMAGE_TYPES and len(data) <= WHATSAPP_IMAGE_MAX_BYTES:
        return data, ct, WHATSAPP_IMAGE_TYPES[ct]
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
        img.thumbnail((OUTBOUND_IMAGE_MAX_SIDE_PX, OUTBOUND_IMAGE_MAX_SIDE_PX))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=85)
    except Exception as e:
        raise UnsupportedMedia(f"the photo could not be read or converted to JPEG ({e})") from e
    if out.tell() > WHATSAPP_IMAGE_MAX_BYTES:
        raise UnsupportedMedia("the photo is still over 5 MB after compression")
    return out.getvalue(), "image/jpeg", "jpg"


def prepare_audio(data: bytes, content_type: Optional[str]) -> Tuple[bytes, str, str]:
    """(bytes, content type, extension) of the recording as a WhatsApp voice note: mono OGG/Opus."""
    try:
        import av
        out = io.BytesIO()
        with av.open(io.BytesIO(data)) as src:
            audio = next((s for s in src.streams if s.type == "audio"), None)
            if audio is None:
                raise UnsupportedMedia("the file has no audio")
            with av.open(out, "w", format="ogg") as dst:
                # The encoder resamples each frame to 48 kHz mono itself.
                opus = dst.add_stream("libopus", rate=48000, layout="mono")
                opus.bit_rate = VOICE_NOTE_BITRATE
                for frame in src.decode(audio):
                    frame.pts = None
                    dst.mux(opus.encode(frame))
                dst.mux(opus.encode(None))
    except UnsupportedMedia:
        raise
    except Exception as e:
        raise UnsupportedMedia(f"the {base_type(content_type) or 'audio'} recording could not be "
                               f"converted to a voice note ({e})") from e
    if out.tell() == 0:
        raise UnsupportedMedia("the recording is empty")
    return out.getvalue(), "audio/ogg", "ogg"
