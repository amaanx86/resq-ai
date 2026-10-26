"""SPEECH-1: Amazon Transcribe batch flow with a stubbed boto3 client and a stubbed transcript download."""
import boto3
import httpx
import pytest
from botocore.stub import Stubber, ANY

from app.config import settings
from app.services.audio_service import (
    AudioService, LANGUAGE_CODE_MAP, TRANSCRIPTION_UNAVAILABLE, language_from_code,
)

S3_URI = "s3://resq-raw-audio-vault/voice-notes/20261011_120000_923009988776_abc123.ogg"
TRANSCRIPT_URI = "https://s3.us-east-1.amazonaws.com/aws-transcribe-us-east-1-prod/123/resq-triage/asrOutput.json?X-Amz-Signature=x"


def _transcribe_client():
    return boto3.client("transcribe", region_name="us-east-1",
                        aws_access_key_id="testing", aws_secret_access_key="testing")


def _job(status, **extra):
    job = {"TranscriptionJobName": "resq-triage-x", "TranscriptionJobStatus": status}
    job.update(extra)
    return {"TranscriptionJob": job}


def _transcript_transport(text, status_code=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if status_code != 200:
            return httpx.Response(status_code, text="denied")
        return httpx.Response(200, json={
            "jobName": "resq-triage-x", "accountId": "123",
            "results": {"transcripts": [{"transcript": text}], "items": []},
            "status": "COMPLETED",
        })
    return httpx.MockTransport(handler)


@pytest.fixture
def svc():
    service = AudioService()  # mock mode: no Transcribe client of its own
    client = _transcribe_client()
    service.transcribe = client
    service.language_options = ["hi-IN", "en-US", "en-IN"]
    stubber = Stubber(client)
    stubber.activate()
    yield service, stubber
    stubber.deactivate()


def _expect_start(stubber, options=("hi-IN", "en-US", "en-IN")):
    stubber.add_response(
        "start_transcription_job",
        _job("IN_PROGRESS"),
        expected_params={
            "TranscriptionJobName": ANY,
            "Media": {"MediaFileUri": S3_URI},
            "MediaFormat": "ogg",
            "IdentifyLanguage": True,
            "LanguageOptions": list(options),
        },
    )


@pytest.mark.asyncio
async def test_polls_until_completed_and_fetches_transcript(svc):
    service, stubber = svc
    _expect_start(stubber)
    stubber.add_response("get_transcription_job", _job("IN_PROGRESS"), {"TranscriptionJobName": ANY})
    stubber.add_response("get_transcription_job", _job("IN_PROGRESS"), {"TranscriptionJobName": ANY})
    stubber.add_response("get_transcription_job", _job(
        "COMPLETED", LanguageCode="en-US", IdentifiedLanguageScore=0.87,
        Transcript={"TranscriptFileUri": TRANSCRIPT_URI}), {"TranscriptionJobName": ANY})
    seen = []
    text = "My father has severe chest pain and cannot breathe, please send an ambulance."
    service._http_transport = _transcript_transport(text, seen=seen)

    result = await service.transcribe_audio(S3_URI)

    assert result.ok is True
    assert result.transcript == text
    assert result.language == "English"
    assert result.language_code == "en-US"
    assert result.confidence == pytest.approx(0.87)
    assert result.error is None
    assert seen == [TRANSCRIPT_URI]
    stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_hindi_code_with_urdu_vocabulary_is_labelled_urdu(svc):
    """Transcribe has no Urdu model; Urdu speech returns hi-IN and is re-labelled from the words."""
    service, stubber = svc
    _expect_start(stubber)
    stubber.add_response("get_transcription_job", _job(
        "COMPLETED", LanguageCode="hi-IN", IdentifiedLanguageScore=0.81,
        Transcript={"TranscriptFileUri": TRANSCRIPT_URI}), {"TranscriptionJobName": ANY})
    service._http_transport = _transcript_transport("मेरे चाचा को सीने में शदीद दर्द है, फौरन एम्बुलेंस भेजें")

    result = await service.transcribe_audio(S3_URI)
    assert result.ok and result.language == "Urdu" and result.language_code == "hi-IN"


@pytest.mark.asyncio
async def test_failed_job_returns_explicit_failure(svc):
    service, stubber = svc
    _expect_start(stubber)
    stubber.add_response("get_transcription_job", _job(
        "FAILED", FailureReason="The media format provided does not match the detected media format."),
        {"TranscriptionJobName": ANY})

    result = await service.transcribe_audio(S3_URI)
    assert result.ok is False
    assert result.transcript == ""
    assert "media format" in result.error


@pytest.mark.asyncio
async def test_timeout_returns_failure(svc, monkeypatch):
    service, stubber = svc
    monkeypatch.setattr(settings, "TRANSCRIBE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(settings, "TRANSCRIBE_POLL_INTERVAL_SECONDS", 0)
    _expect_start(stubber)
    stubber.add_response("get_transcription_job", _job("IN_PROGRESS"), {"TranscriptionJobName": ANY})

    result = await service.transcribe_audio(S3_URI)
    assert result.ok is False
    assert "timed out" in result.error
    assert result.transcript == ""


@pytest.mark.asyncio
async def test_start_error_returns_failure(svc):
    service, stubber = svc
    stubber.add_client_error("start_transcription_job", service_error_code="BadRequestException",
                             service_message="Invalid language option ur-PK")
    result = await service.transcribe_audio(S3_URI)
    assert result.ok is False and "start_transcription_job failed" in result.error


@pytest.mark.asyncio
async def test_transcript_download_error_returns_failure(svc):
    service, stubber = svc
    _expect_start(stubber)
    stubber.add_response("get_transcription_job", _job(
        "COMPLETED", LanguageCode="en-US", IdentifiedLanguageScore=0.9,
        Transcript={"TranscriptFileUri": TRANSCRIPT_URI}), {"TranscriptionJobName": ANY})
    service._http_transport = _transcript_transport("", status_code=403)

    result = await service.transcribe_audio(S3_URI)
    assert result.ok is False and "could not fetch transcript" in result.error


@pytest.mark.asyncio
async def test_empty_transcript_is_a_failure(svc):
    service, stubber = svc
    _expect_start(stubber)
    stubber.add_response("get_transcription_job", _job(
        "COMPLETED", LanguageCode="hi-IN", IdentifiedLanguageScore=0.4,
        Transcript={"TranscriptFileUri": TRANSCRIPT_URI}), {"TranscriptionJobName": ANY})
    service._http_transport = _transcript_transport("   ")

    result = await service.transcribe_audio(S3_URI)
    assert result.ok is False and "no speech" in result.error


@pytest.mark.asyncio
async def test_no_client_returns_unavailable():
    service = AudioService()
    assert service.transcribe is None  # mock mode never creates a Transcribe client
    result = await service.transcribe_audio(S3_URI)
    assert result.ok is False
    assert result.error == TRANSCRIPTION_UNAVAILABLE
    assert result.transcript == ""


@pytest.mark.asyncio
async def test_https_url_is_rejected(svc):
    service, _ = svc
    result = await service.transcribe_audio("https://resq-raw-audio-vault.s3.amazonaws.com/x.ogg")
    assert result.ok is False and "s3://" in result.error


@pytest.mark.parametrize("code,language", [
    ("ur-PK", "Urdu"), ("ur-IN", "Urdu"), ("hi-IN", "Hindi"),
    ("en-US", "English"), ("en-IN", "English"), ("en-GB", "English"),
    (None, "Unknown"), ("fr-FR", "Unknown"),
])
def test_language_code_mapping(code, language):
    assert language_from_code(code) == language


def test_mapping_covers_every_requested_option():
    for code in ("ur-PK", "ur-IN", "hi-IN", "en-US", "en-IN"):
        assert code in LANGUAGE_CODE_MAP


def test_language_options_filtered_to_supported_codes(monkeypatch):
    """Urdu is not in Transcribe's LanguageCode enum; the client must not send codes it rejects."""
    monkeypatch.delenv("TRANSCRIBE_LANGUAGE_OPTIONS", raising=False)
    service = AudioService()
    service.transcribe = _transcribe_client()
    options = service._resolve_language_options()
    valid = set(service.transcribe.meta.service_model.shape_for("LanguageCode").enum)
    assert len(options) >= 2 and all(c in valid for c in options)
    assert "hi-IN" in options and "en-US" in options and "en-IN" in options


def test_no_fake_sentences_in_source():
    import inspect
    import app.services.audio_service as mod
    src = inspect.getsource(mod)
    assert "Transcript from Amazon Transcribe" not in src
    assert "Emergency speech audio processed" not in src
    assert "dil ka daura par raha hai" not in src
