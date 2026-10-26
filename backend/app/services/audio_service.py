import asyncio
import os
import re
import time
import boto3
import uuid
import httpx
from botocore.config import Config
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from app.config import settings
from app.models.schemas import AudioObject, TranscriptionResult

# Language identification candidates we ask Transcribe for (SPEECH-1). Urdu (ur-PK, ur-IN) is not
# offered by Amazon Transcribe batch today (it is missing from the service's LanguageCode enum), so
# the client filters this list against the codes its service model accepts. Spoken Urdu then comes
# back as hi-IN and is re-labelled from the transcript's vocabulary (see transcribe_audio).
# Override with TRANSCRIBE_LANGUAGE_OPTIONS="hi-IN,en-IN,en-US".
DEFAULT_LANGUAGE_OPTIONS = ["ur-PK", "ur-IN", "hi-IN", "en-US", "en-IN"]

# Transcribe language code -> language name used across ResQ-AI.
LANGUAGE_CODE_MAP: Dict[str, str] = {
    "ur-PK": "Urdu",
    "ur-IN": "Urdu",
    "hi-IN": "Hindi",
    "en-US": "English",
    "en-IN": "English",
    "en-GB": "English",
    "en-AU": "English",
}

TRANSCRIPTION_UNAVAILABLE = "transcription unavailable (Floci/mock)"

# Folders in the media vault (the "audio vault" bucket also holds photos, IMAGE-1).
VOICE_NOTE_FOLDER = "voice-notes"
IMAGE_FOLDER = "images"
# Photos and voice notes a doctor sends from the portal.
OUTBOUND_FOLDER = "outbound"

IMAGE_EXTENSIONS: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
}


def image_extension(mime_type: Optional[str]) -> str:
    base = (mime_type or "").split(";")[0].strip().lower()
    return IMAGE_EXTENSIONS.get(base, "jpg")


def language_from_code(code: Optional[str]) -> str:
    """Maps a Transcribe language code (ur-PK, hi-IN, en-US, ...) to Urdu | Hindi | English | Unknown."""
    if not code:
        return "Unknown"
    if code in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[code]
    prefix = code.split("-")[0].lower()
    return {"ur": "Urdu", "hi": "Hindi", "en": "English"}.get(prefix, "Unknown")


# ---------------------------------------------------------------------------
# Text language detection (WhatsApp text messages, and re-labelling hi-IN transcripts)
# ---------------------------------------------------------------------------
_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")
_ROMAN_TOKEN = re.compile(r"[a-z]+")
_DEVANAGARI_TOKEN = re.compile(r"[ऀ-ॿ]+")

# Romanized Hindustani words shared by Urdu and Hindi (grammar words and common emergency vocabulary).
_HINDUSTANI_COMMON = {
    "hai", "hain", "tha", "thi", "ho", "hoga", "gaya", "gayi", "gaye", "raha", "rahi", "rahe",
    "ka", "ki", "ke", "ko", "se", "me", "mein", "mai", "par", "pe", "aur", "ya", "nahi",
    "nahin", "na", "bhi", "kya", "kyun", "kaise", "kab", "kahan", "mera", "meri", "mere", "mujhe",
    "hum", "humein", "hamara", "aap", "aapka", "tum", "unko", "usko", "inko", "woh", "wo", "yeh", "ye",
    "bahut", "bohot", "bohat", "jaldi", "abhi", "koi", "kuch", "sab", "kal", "aaj", "sa", "si",
    "dard", "sar", "pet", "khoon", "saans", "sans", "bukhar", "chot", "behosh", "takleef", "dawa",
    "dawai", "bhejo", "bhej", "karo", "kar", "karein", "bata", "batao", "chal", "gir", "pada", "padi",
    "uncle", "beta", "beti", "bhai", "halka", "zor", "beh", "lag", "laga", "lagi", "ja", "jao",
}
# Words that lean Urdu (Perso-Arabic vocabulary, polite Urdu verb forms, family/religious terms).
_URDU_MARKERS = {
    "dil", "daura", "seena", "seene", "shadeed", "bhejein", "bhejien", "farmaein",
    "madad", "ammi", "abbu", "ammijan", "abbujan", "khala", "phuppo", "khuda", "allah", "inshallah",
    "mashallah", "shukriya", "meharbani", "meherbani", "fauran", "foran", "zukam", "zukaam",
    "mareez", "mariz", "zakhmi", "janab", "taklif", "tabiyat", "tabiyyat", "hazrat", "bilkul",
    "zaroor", "zaroorat",
}
# Words that lean Hindi (Sanskrit-derived vocabulary, Indian address terms).
_HINDI_MARKERS = {
    "bhaiya", "bhaiyya", "kripya", "krupya", "chhati", "chati", "dhanyavaad", "dhanyawad",
    "sahayata", "turant", "rogi", "durghatna", "ghayal", "upchaar", "didi", "pitaji",
    "mataji", "namaste", "shaayad", "samasya", "swasthya", "hriday",
}
# Urdu-leaning words written in Devanagari (Transcribe hi-IN output for Urdu speakers).
_URDU_MARKERS_DEVANAGARI = {
    "शदीद", "सीने", "सीना", "भेजें", "भेजिए", "ज़ुकाम", "जुकाम", "अम्मी", "अब्बू", "ख़ुदा", "खुदा",
    "अल्लाह", "शुक्रिया", "मेहरबानी", "फ़ौरन", "फौरन", "मरीज़", "मरीज", "ज़ख़्मी", "जख्मी", "जनाब",
    "इंशाअल्लाह", "माशाअल्लाह",
}
_HINDI_MARKERS_DEVANAGARI = {
    "कृपया", "छाती", "धन्यवाद", "सहायता", "तुरंत", "रोगी", "दुर्घटना", "घायल", "उपचार", "भैया",
    "हृदय", "दीदी", "नमस्ते",
}
# Very common English words, so English text is recognised positively rather than by elimination.
_ENGLISH_COMMON = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "my", "his", "her", "she", "he",
    "it", "they", "we", "you", "i", "and", "or", "but", "not", "no", "of", "to", "in", "on", "at",
    "for", "with", "from", "this", "that", "has", "have", "had", "can", "cannot", "can't", "could",
    "please", "help", "suddenly", "pain", "chest", "breathing", "breathe", "bleeding", "fell", "fall",
    "collapsed", "unconscious", "side", "one", "speak", "clearly", "face", "mother", "father", "son",
    "daughter", "brother", "sister", "wife", "husband", "need", "send", "now", "very", "bad", "hurt",
    "accident", "fever", "since", "yesterday", "today", "what", "do", "does", "did", "there",
}


def detect_language(text: str) -> Tuple[str, float]:
    """Deterministic, dependency-free language guess for a short message: (Urdu|Hindi|English, confidence).

    1. Script: Arabic-script letters mean Urdu, Devanagari means Hindi (Urdu-leaning vocabulary in
       Devanagari, as Transcribe hi-IN produces for Urdu speakers, is re-labelled Urdu).
    2. Romanized text: score tokens against Hindustani grammar words, Urdu-leaning and Hindi-leaning
       vocabulary, and common English words. Mostly-Hindustani text is Urdu or Hindi by the marker
       balance (ties go to Urdu, the primary market); otherwise English.
    """
    if not text or not text.strip():
        return "English", 0.0

    arabic = len(_ARABIC_SCRIPT.findall(text))
    devanagari = len(_DEVANAGARI.findall(text))
    latin = len(_LATIN.findall(text))
    letters = arabic + devanagari + latin
    if letters == 0:
        return "English", 0.0

    if arabic >= devanagari and arabic / letters >= 0.5:
        return "Urdu", round(min(0.99, 0.75 + 0.24 * arabic / letters), 2)

    if devanagari / letters >= 0.5:
        tokens = _DEVANAGARI_TOKEN.findall(text)
        urdu = sum(1 for t in tokens if t in _URDU_MARKERS_DEVANAGARI)
        hindi = sum(1 for t in tokens if t in _HINDI_MARKERS_DEVANAGARI)
        if urdu >= 2 and urdu > hindi:
            return "Urdu", round(min(0.9, 0.6 + 0.1 * (urdu - hindi)), 2)
        return "Hindi", round(min(0.99, 0.75 + 0.24 * devanagari / letters), 2)

    tokens = _ROMAN_TOKEN.findall(text.lower())
    if not tokens:
        return "English", 0.5
    urdu = sum(1 for t in tokens if t in _URDU_MARKERS)
    hindi = sum(1 for t in tokens if t in _HINDI_MARKERS)
    common = sum(1 for t in tokens if t in _HINDUSTANI_COMMON)
    english = sum(1 for t in tokens if t in _ENGLISH_COMMON)
    hindustani = common + urdu + hindi

    if hindustani >= 2 and hindustani > english:
        share = hindustani / (hindustani + english)
        if urdu == hindi:
            # No lexical lean: Hindustani either way. Default to Urdu with low confidence.
            return "Urdu", round(min(0.7, 0.5 + 0.2 * share), 2)
        lean = abs(urdu - hindi) / (urdu + hindi)
        conf = round(min(0.97, 0.6 + 0.25 * lean + 0.12 * share), 2)
        return ("Urdu" if urdu > hindi else "Hindi"), conf

    if english == 0 and hindustani == 0:
        return "English", 0.5
    share = english / max(1, english + hindustani)
    return "English", round(min(0.99, 0.55 + 0.44 * share), 2)


class AudioUploadError(RuntimeError):
    """The voice note could not be stored in the S3 audio vault."""


class AudioService:
    def __init__(self):
        self.mock_mode = settings.MOCK_AWS
        self.s3 = None
        self.transcribe = None
        self.language_options = list(DEFAULT_LANGUAGE_OPTIONS)
        # Bytes uploaded while there is no S3 client (pure mock), so playback still works.
        self._mock_objects: Dict[str, Tuple[bytes, str]] = {}
        # Tests inject an httpx.MockTransport here to stub the transcript download.
        self._http_transport: Optional[httpx.AsyncBaseTransport] = None

        if not self.mock_mode or settings.AWS_ENDPOINT_URL:
            try:
                boto_kwargs = {"region_name": settings.AWS_REGION}
                if settings.AWS_ENDPOINT_URL:
                    boto_kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
                    boto_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                    boto_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
                else:
                    # SigV4 + virtual-hosted addressing: presigned URLs work in every region.
                    boto_kwargs["config"] = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})

                self.s3 = boto3.client('s3', **boto_kwargs)
                self._ensure_bucket_exists()

                # Amazon Transcribe only in real AWS (Floci does not emulate it)
                if not settings.AWS_ENDPOINT_URL and not self.mock_mode:
                    self.transcribe = boto3.client('transcribe', region_name=settings.AWS_REGION)
                    self.language_options = self._resolve_language_options()
                    print(f"[AudioService] Amazon Transcribe enabled (language options: {self.language_options})")
                else:
                    print(f"[AudioService] Amazon Transcribe disabled: {TRANSCRIPTION_UNAVAILABLE}")
                print(f"[AudioService] Connected to S3 bucket '{settings.S3_AUDIO_BUCKET}' via {settings.AWS_ENDPOINT_URL or 'AWS Cloud'}")
            except Exception as e:
                if not settings.AWS_ENDPOINT_URL:
                    raise RuntimeError(
                        f"[AudioService] MOCK_AWS=False but S3/Transcribe is unavailable ({e}). "
                        "Voice notes would be kept in memory and never transcribed."
                    ) from e
                print(f"[AudioService] Warning: Could not connect to Floci S3: {e}")
                self.mock_mode = True
        else:
            print(f"[AudioService] Mock mode: audio kept in memory, {TRANSCRIPTION_UNAVAILABLE}")

    def _resolve_language_options(self) -> list:
        """Requested language options, minus codes this Transcribe API version does not accept."""
        raw = os.getenv("TRANSCRIBE_LANGUAGE_OPTIONS")
        requested = [c.strip() for c in raw.split(",") if c.strip()] if raw else list(DEFAULT_LANGUAGE_OPTIONS)
        try:
            valid = set(self.transcribe.meta.service_model.shape_for("LanguageCode").enum)
        except Exception:
            return requested
        accepted = [c for c in requested if c in valid]
        dropped = [c for c in requested if c not in valid]
        if dropped:
            print(f"[AudioService] Transcribe does not support {dropped}; identifying among {accepted}")
        return accepted if len(accepted) >= 2 else ["hi-IN", "en-IN", "en-US"]

    def _ensure_bucket_exists(self):
        """Ensures the S3 audio vault bucket exists on Floci. In real AWS Terraform owns the bucket
        (and the task roles have no s3:ListAllMyBuckets)."""
        if not self.s3 or not settings.AWS_ENDPOINT_URL:
            return
        try:
            buckets = [b["Name"] for b in self.s3.list_buckets().get("Buckets", [])]
            if settings.S3_AUDIO_BUCKET not in buckets:
                print(f"[AudioService] Bucket '{settings.S3_AUDIO_BUCKET}' not found. Auto-creating bucket on Floci/AWS...")
                self.s3.create_bucket(Bucket=settings.S3_AUDIO_BUCKET)
                print(f"[AudioService] Created bucket '{settings.S3_AUDIO_BUCKET}' successfully.")
        except Exception as e:
            print(f"[AudioService] Error checking/creating S3 bucket: {e}")

    def build_media_key(self, folder: str, user_phone: str, ext: str) -> str:
        """S3 key for a new media object: `<folder>/<utc time>_<phone digits>_<random>.<ext>`."""
        return f"{folder}/{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{user_phone.replace('+', '')}_{uuid.uuid4().hex[:6]}.{ext}"

    def build_audio_key(self, user_phone: str, ext: str = "ogg") -> str:
        """S3 key for a new voice note. Shared by every upload path (Meta download, EUM Social media)."""
        return self.build_media_key(VOICE_NOTE_FOLDER, user_phone, ext)

    def build_image_key(self, user_phone: str, mime_type: Optional[str] = None) -> str:
        """S3 key for a patient's photo (IMAGE-1)."""
        return self.build_media_key(IMAGE_FOLDER, user_phone, image_extension(mime_type))

    def audio_object_for_key(self, key: str) -> AudioObject:
        """s3:// URI (for Transcribe) and HTTPS display URL for an object in the audio vault."""
        if settings.AWS_ENDPOINT_URL:
            url = f"{settings.AWS_ENDPOINT_URL}/{settings.S3_AUDIO_BUCKET}/{key}"
        else:
            url = f"https://{settings.S3_AUDIO_BUCKET}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
        return AudioObject(key=key, s3_uri=f"s3://{settings.S3_AUDIO_BUCKET}/{key}", url=url)

    def upload_media_to_s3(self, data: bytes, key: str, content_type: str) -> AudioObject:
        """Stores any media object (voice note, photo) in the vault under `key`. Raises
        AudioUploadError if S3 rejects it; with no S3 client (pure mock) the bytes are kept in memory."""
        if self.s3:
            # A failed upload must not look like a stored object (the caller would transcribe or
            # analyse a missing object): raise, and the caller takes the "could not retrieve" path.
            try:
                self.s3.put_object(
                    Bucket=settings.S3_AUDIO_BUCKET,
                    Key=key,
                    Body=data,
                    ContentType=content_type
                )
            except Exception as e:
                print(f"[AudioService] Error uploading to S3: {e}")
                raise AudioUploadError(f"S3 upload of {key} to {settings.S3_AUDIO_BUCKET} failed: {e}") from e
        else:
            self._mock_objects[key] = (data, content_type)

        return self.audio_object_for_key(key)

    def upload_audio_to_s3(self, audio_bytes: bytes, user_phone: str) -> AudioObject:
        """Uploads raw OGG audio to the S3 audio vault. Raises AudioUploadError if S3 rejects it;
        with no S3 client (pure mock) the bytes are kept in memory."""
        return self.upload_media_to_s3(audio_bytes, self.build_audio_key(user_phone), "audio/ogg; codecs=opus")

    def upload_image_to_s3(self, image_bytes: bytes, user_phone: str, mime_type: Optional[str] = None) -> AudioObject:
        """Uploads a patient's photo to the vault (`images/`). Raises AudioUploadError on failure."""
        content_type = (mime_type or "image/jpeg").split(";")[0].strip() or "image/jpeg"
        return self.upload_media_to_s3(image_bytes, self.build_image_key(user_phone, content_type), content_type)

    def delete_media(self, keys) -> int:
        """Deletes vault objects (a deleted case's voice notes and photos). Best effort: returns how
        many were deleted, logs failures, never raises."""
        keys = sorted({k for k in keys if k})
        if not keys:
            return 0
        if not self.s3:
            return sum(self._mock_objects.pop(k, None) is not None for k in keys)
        deleted = 0
        for i in range(0, len(keys), 1000):  # DeleteObjects takes at most 1000 keys
            try:
                res = self.s3.delete_objects(Bucket=settings.S3_AUDIO_BUCKET,
                                             Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": False})
                deleted += len(res.get("Deleted", []))
                for err in res.get("Errors", []):
                    print(f"[AudioService] Could not delete {err.get('Key')}: {err.get('Message')}")
            except Exception as e:
                print(f"[AudioService] Error deleting {len(keys[i:i + 1000])} media object(s): {e}")
        return deleted

    def read_media_bytes(self, key: str) -> bytes:
        """The whole object (a photo for the image agent). Raises if it is missing or unreadable."""
        obj = self.get_audio_object(key)
        if obj is None:
            raise AudioUploadError(f"{key} is not in the media vault")
        body = obj[0]
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    # ================= PLAYBACK (UI-1) =================
    def presign_audio_url(self, key: str, expires_seconds: int = 300) -> Optional[str]:
        """Short-lived presigned GET URL for a voice note. Real AWS only: in Floci/mock mode the
        browser cannot reach the object store, so callers use the streaming endpoint instead (None)."""
        if not key or not self.s3 or settings.AWS_ENDPOINT_URL or self.mock_mode:
            return None
        try:
            return self.s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.S3_AUDIO_BUCKET, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except Exception as e:
            print(f"[AudioService] Error presigning {key}: {e}")
            return None

    def get_audio_object(self, key: str) -> Optional[Tuple[object, str, Optional[int]]]:
        """(body, content_type, content_length) for streaming a voice note, or None if it is missing.
        body is a botocore StreamingBody (S3/Floci) or bytes (pure mock)."""
        if not key:
            return None
        if self.s3:
            try:
                obj = self.s3.get_object(Bucket=settings.S3_AUDIO_BUCKET, Key=key)
                return obj["Body"], obj.get("ContentType") or "audio/ogg", obj.get("ContentLength")
            except Exception as e:
                print(f"[AudioService] Error reading {key} from S3: {e}")
                return None
        stored = self._mock_objects.get(key)
        if stored:
            return stored[0], stored[1], len(stored[0])
        return None

    # ================= TRANSCRIPTION (SPEECH-1) =================
    async def transcribe_audio(self, s3_uri: str) -> TranscriptionResult:
        """Transcribes an OGG/Opus voice note in S3 with Amazon Transcribe automatic language identification.

        Starts a batch job on the s3:// URI, polls until COMPLETED/FAILED (non-blocking), downloads the
        transcript JSON from TranscriptFileUri and maps the identified language. Never invents text:
        any failure, timeout or missing client returns TranscriptionResult(ok=False, error=...).
        """
        if self.transcribe is None:
            return TranscriptionResult(ok=False, error=TRANSCRIPTION_UNAVAILABLE)
        if not s3_uri or not s3_uri.startswith("s3://"):
            return TranscriptionResult(ok=False, error=f"invalid media URI for Transcribe (need s3://bucket/key): {s3_uri!r}")

        job_name = f"resq-triage-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        try:
            await asyncio.to_thread(
                self.transcribe.start_transcription_job,
                TranscriptionJobName=job_name,
                Media={"MediaFileUri": s3_uri},
                MediaFormat="ogg",
                IdentifyLanguage=True,
                LanguageOptions=self.language_options,
            )
        except Exception as e:
            print(f"[AudioService] Transcribe start_transcription_job failed: {e}")
            return TranscriptionResult(ok=False, error=f"start_transcription_job failed: {e}")

        timeout = float(settings.TRANSCRIBE_TIMEOUT_SECONDS)
        interval = float(settings.TRANSCRIBE_POLL_INTERVAL_SECONDS)
        deadline = time.monotonic() + timeout
        while True:
            try:
                res = await asyncio.to_thread(self.transcribe.get_transcription_job, TranscriptionJobName=job_name)
            except Exception as e:
                print(f"[AudioService] Transcribe get_transcription_job failed for {job_name}: {e}")
                return TranscriptionResult(ok=False, error=f"get_transcription_job failed: {e}")
            job = res.get("TranscriptionJob", {})
            status = job.get("TranscriptionJobStatus")
            if status == "COMPLETED":
                break
            if status == "FAILED":
                reason = job.get("FailureReason", "unknown reason")
                print(f"[AudioService] Transcribe job {job_name} FAILED: {reason}")
                return TranscriptionResult(ok=False, error=f"transcription job failed: {reason}")
            if time.monotonic() >= deadline:
                print(f"[AudioService] Transcribe job {job_name} timed out after {timeout:.0f}s (status {status})")
                return TranscriptionResult(ok=False, error=f"transcription timed out after {timeout:.0f}s (status {status})")
            await asyncio.sleep(interval)

        uri = (job.get("Transcript") or {}).get("TranscriptFileUri")
        if not uri:
            return TranscriptionResult(ok=False, error="transcription completed without a TranscriptFileUri")
        try:
            async with httpx.AsyncClient(transport=self._http_transport, timeout=15.0) as client:
                resp = await client.get(uri)
                resp.raise_for_status()
                data = resp.json()
            transcripts = data.get("results", {}).get("transcripts", [])
            transcript = " ".join(t.get("transcript", "") for t in transcripts).strip()
        except Exception as e:
            print(f"[AudioService] Could not fetch transcript for {job_name}: {e}")
            return TranscriptionResult(ok=False, error=f"could not fetch transcript: {e}")

        code = job.get("LanguageCode")
        score = job.get("IdentifiedLanguageScore")
        confidence = float(score) if score is not None else 0.0
        if not transcript:
            return TranscriptionResult(ok=False, language=language_from_code(code), language_code=code,
                                       confidence=confidence, error="no speech detected (empty transcript)")

        language = language_from_code(code)
        if language == "Hindi":
            # Transcribe has no Urdu model: Urdu speech comes back as hi-IN. Re-label from vocabulary.
            guess, _ = detect_language(transcript)
            if guess == "Urdu":
                language = "Urdu"
        print(f"[AudioService] Transcribe job {job_name} COMPLETED ({code}, score {confidence:.2f}) -> {language}")
        return TranscriptionResult(ok=True, transcript=transcript, language=language,
                                   language_code=code, confidence=confidence)

    def detect_language(self, text: str) -> Tuple[str, float]:
        """Language of a text message: (Urdu | Hindi | English, confidence). See module-level detect_language."""
        return detect_language(text)


audio_service = AudioService()
