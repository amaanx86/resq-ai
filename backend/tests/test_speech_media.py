"""UI-1 backend: voice note playback URLs and the streaming endpoint."""
import uuid
from urllib.parse import urlparse, parse_qs

import boto3
import pytest
from botocore.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import settings
from app.models.schemas import EmergencyCase, ChatMessage
from app.routers import media
from app.services.audio_service import audio_service
from app.services.dynamo_service import dynamo_service

OGG = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead-test-bytes"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(media.router)
    return TestClient(app)


def _case(audio_key=None) -> EmergencyCase:
    case = EmergencyCase(
        case_id=f"CASE-MEDIA-{uuid.uuid4().hex[:8]}", user_phone="+923001110000", patient_name="Test Patient",
        transcript="test", detected_language="English", esi_level=2, severity="CRITICAL",
        status="ACTIVE", audio_s3_key=audio_key,
    )
    return dynamo_service.save_case(case)


def test_unknown_case_is_404(client):
    assert client.get("/api/v1/cases/CASE-NOPE/audio").status_code == 404


def test_case_without_audio_is_404(client):
    case = _case()
    assert client.get(f"/api/v1/cases/{case.case_id}/audio").status_code == 404
    assert client.get(f"/api/v1/cases/{case.case_id}/audio/stream").status_code == 404


def test_mock_mode_returns_stream_url_and_streams_bytes(client):
    audio = audio_service.upload_audio_to_s3(OGG, "+923001110000")
    case = _case(audio.key)

    res = client.get(f"/api/v1/cases/{case.case_id}/audio")
    assert res.status_code == 200
    assert res.json() == {"url": f"/api/v1/cases/{case.case_id}/audio/stream", "expires_in": 300}

    stream = client.get(res.json()["url"])
    assert stream.status_code == 200
    assert stream.content == OGG
    assert stream.headers["content-type"].startswith("audio/ogg")


def test_real_aws_returns_presigned_url(client, monkeypatch):
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="AKIATEST", aws_secret_access_key="secret",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    monkeypatch.setattr(audio_service, "s3", s3)
    monkeypatch.setattr(audio_service, "mock_mode", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    key = "voice-notes/20261011_120000_923001110000_abc123.ogg"
    case = _case(key)

    body = client.get(f"/api/v1/cases/{case.case_id}/audio").json()
    url = urlparse(body["url"])
    query = parse_qs(url.query)
    assert body["expires_in"] == 300
    assert url.scheme == "https"
    assert url.netloc.startswith(f"{settings.S3_AUDIO_BUCKET}.s3")
    assert url.path == f"/{key}"
    assert query["X-Amz-Expires"] == ["300"]
    assert "X-Amz-Signature" in query


def test_stream_from_moto_s3(client, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=settings.S3_AUDIO_BUCKET)
        monkeypatch.setattr(audio_service, "s3", s3)
        audio = audio_service.upload_audio_to_s3(OGG, "+923001110000")
        case = _case(audio.key)

        # Floci-style: no presign (endpoint set), the stream endpoint serves the object
        monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", "http://floci:4566")
        assert client.get(f"/api/v1/cases/{case.case_id}/audio").json()["url"] == f"/api/v1/cases/{case.case_id}/audio/stream"
        res = client.get(f"/api/v1/cases/{case.case_id}/audio/stream")
        assert res.status_code == 200
        assert res.content == OGG
        assert res.headers["content-type"] == "audio/ogg; codecs=opus"

        # Key on the case but object missing from the bucket
        missing = _case("voice-notes/missing.ogg")
        assert client.get(f"/api/v1/cases/{missing.case_id}/audio/stream").status_code == 404


def test_chat_message_audio(client):
    case = _case()
    audio = audio_service.upload_audio_to_s3(OGG, "+923001110000")
    with_audio = dynamo_service.save_chat_message(ChatMessage(
        message_id=f"MSG-{uuid.uuid4().hex[:8]}", case_id=case.case_id, sender="PATIENT",
        sender_name="Test Patient", content="(voice note)", audio_s3_key=audio.key))
    text_only = dynamo_service.save_chat_message(ChatMessage(
        message_id=f"MSG-{uuid.uuid4().hex[:8]}", case_id=case.case_id, sender="PATIENT",
        sender_name="Test Patient", content="hello"))

    base = f"/api/v1/cases/{case.case_id}/messages"
    res = client.get(f"{base}/{with_audio.message_id}/audio")
    assert res.status_code == 200
    assert res.json()["url"] == f"{base}/{with_audio.message_id}/audio/stream"
    assert client.get(res.json()["url"]).content == OGG

    assert client.get(f"{base}/{text_only.message_id}/audio").status_code == 404
    assert client.get(f"{base}/MSG-NOPE/audio").status_code == 404
