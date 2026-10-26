"""Outbound WhatsApp providers, the whatsapp_service facade and provider selection (INFRA-7).

Meta is tested with httpx.MockTransport, End User Messaging Social with botocore's Stubber.
Both must send the same WhatsApp Cloud API message body (only the `to` format differs).
"""
import json

import boto3
import httpx
import pytest
from botocore.stub import ANY, Stubber

from app.config import settings
from app.messaging import factory, providers
from app.messaging.factory import build_provider, missing_settings
from app.messaging.providers import EumSocialProvider, MediaFetchError, MetaCloudProvider, MockProvider
from app.models.schemas import AudioObject
from app.services.whatsapp_service import WhatsAppService, whatsapp_service

META_PHONE_ID = "106540352242922"
EUM_PHONE_ID = "phone-number-id-976c72a700aac43eaf573ae050example"
TO = "+923001234567"
VIDEO_URL = "https://portal.example.com/call/m-123?role=patient&case=c-1&token=t0k"


class JsonBytes:
    """Stubber matcher: the `message` blob must decode to this JSON object."""

    def __init__(self, expected: dict):
        self.expected = expected

    def __eq__(self, other):
        return isinstance(other, (bytes, bytearray)) and json.loads(other) == self.expected

    def __repr__(self):
        return f"JsonBytes({self.expected!r})"


class Recorder:
    """httpx.MockTransport handler that records requests and replays queued responses."""

    def __init__(self, *responses: httpx.Response):
        self.requests = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def meta_provider(recorder: Recorder) -> MetaCloudProvider:
    return MetaCloudProvider("EAAG-test-token", META_PHONE_ID, "v20.0", transport=httpx.MockTransport(recorder))


def meta_ok(message_id="wamid.OUT1") -> httpx.Response:
    return httpx.Response(200, json={
        "messaging_product": "whatsapp",
        "contacts": [{"input": "923001234567", "wa_id": "923001234567"}],
        "messages": [{"id": message_id}],
    })


def eum_client():
    return boto3.client(
        "socialmessaging", region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing",
    )


def captured_meta_json(recorder: Recorder, index: int = 0) -> dict:
    return json.loads(recorder.requests[index].content)


# ---------------------------------------------------------------------------
# Meta Cloud API provider
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_meta_send_text_exact_request():
    rec = Recorder(meta_ok())
    result = await meta_provider(rec).send_text(TO, "Ambulance dispatched.")

    assert result == {"status": "SENT", "provider": "meta", "recipient": TO, "message_id": "wamid.OUT1"}
    [req] = rec.requests
    assert req.method == "POST"
    assert str(req.url) == f"https://graph.facebook.com/v20.0/{META_PHONE_ID}/messages"
    assert req.headers["Authorization"] == "Bearer EAAG-test-token"
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "923001234567",
        "type": "text",
        "text": {"body": "Ambulance dispatched."},
    }


@pytest.mark.asyncio
async def test_meta_video_invite_is_interactive_cta_url():
    rec = Recorder(meta_ok("wamid.VID"))
    service = WhatsAppService(provider=meta_provider(rec))
    result = await service.send_video_call_invitation(TO, "Sarah Khan", "Cardiology", VIDEO_URL)

    assert result["status"] == "SENT"
    body = captured_meta_json(rec)
    assert body["type"] == "interactive"
    assert body["to"] == "923001234567"
    interactive = body["interactive"]
    assert interactive["type"] == "cta_url"
    assert interactive["action"] == {
        "name": "cta_url",
        "parameters": {"display_text": "Join video call", "url": VIDEO_URL},
    }
    assert interactive["header"] == {"type": "text", "text": "Emergency video consultation"}
    assert "Dr. Sarah Khan (Cardiology)" in interactive["body"]["text"]
    assert VIDEO_URL in interactive["body"]["text"]  # text fallback inside the body
    assert len(interactive["body"]["text"]) <= 1024
    assert len(interactive["action"]["parameters"]["display_text"]) <= 20


@pytest.mark.asyncio
async def test_meta_video_invite_falls_back_to_text_when_rejected():
    rejected = httpx.Response(400, json={"error": {"message": "(#131009) Parameter value is not valid", "code": 131009}})
    rec = Recorder(rejected, meta_ok("wamid.TXT"))
    result = await WhatsAppService(provider=meta_provider(rec)).send_video_call_invitation(
        TO, "Sarah Khan", "Cardiology", VIDEO_URL
    )

    assert result["status"] == "SENT"
    assert result["fallback_from"] == "interactive"
    assert [captured_meta_json(rec, i)["type"] for i in range(2)] == ["interactive", "text"]
    assert VIDEO_URL in captured_meta_json(rec, 1)["text"]["body"]


@pytest.mark.asyncio
async def test_meta_location_request_is_interactive_send_location():
    rec = Recorder(meta_ok("wamid.LOC"))
    result = await WhatsAppService(provider=meta_provider(rec)).send_location_request(TO, "Share your location.")

    assert result["status"] == "SENT"
    assert captured_meta_json(rec) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "923001234567",
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {"text": "Share your location."},
            "action": {"name": "send_location"},
        },
    }


@pytest.mark.asyncio
async def test_meta_location_request_falls_back_to_text_when_rejected():
    rejected = httpx.Response(400, json={"error": {"message": "(#131009) Parameter value is not valid"}})
    rec = Recorder(rejected, meta_ok("wamid.TXT"))
    result = await WhatsAppService(provider=meta_provider(rec)).send_location_request(TO, "Share your location.")

    assert result["status"] == "SENT" and result["fallback_from"] == "interactive"
    text = captured_meta_json(rec, 1)["text"]["body"]
    assert text.startswith("Share your location.") and "*Location*" in text


@pytest.mark.asyncio
async def test_meta_emergency_location_alert_is_the_configured_template(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_LOCATION_TEMPLATE_NAME", "resq_emergency_location")
    monkeypatch.setattr(settings, "WHATSAPP_LOCATION_TEMPLATE_LANGUAGE", "en")
    rec = Recorder(meta_ok("wamid.TPL"))
    link = "https://maps.google.com/?q=24.860700,67.001100"
    result = await WhatsAppService(provider=meta_provider(rec)).send_emergency_location_alert(
        "+923331234567", "Ahmed Khan", link)

    assert result["status"] == "SENT"
    assert captured_meta_json(rec) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "923331234567",
        "type": "template",
        "template": {
            "name": "resq_emergency_location",
            "language": {"code": "en"},
            "components": [{"type": "body", "parameters": [
                {"type": "text", "text": "Ahmed Khan"}, {"type": "text", "text": link}]}],
        },
    }


@pytest.mark.asyncio
async def test_meta_send_error_is_returned_not_raised():
    rec = Recorder(httpx.Response(401, json={"error": {"message": "Invalid OAuth access token"}}))
    result = await meta_provider(rec).send_text(TO, "hi")
    assert result["status"] == "ERROR"
    assert result["http_status"] == 401
    assert "Invalid OAuth" in result["detail"]


@pytest.mark.asyncio
async def test_meta_fetch_media_downloads_from_graph_then_uploads(monkeypatch):
    lookaside = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1048234539876154&ext=1"
    rec = Recorder(
        httpx.Response(200, json={"url": lookaside, "mime_type": "audio/ogg; codecs=opus", "id": "1048234539876154"}),
        httpx.Response(200, content=b"OggS-real-voice-note"),
    )
    uploads = []

    def fake_upload(audio_bytes, phone):
        uploads.append((audio_bytes, phone))
        return AudioObject(key="voice-notes/x.ogg", s3_uri="s3://bucket/voice-notes/x.ogg")

    monkeypatch.setattr(providers.audio_service, "upload_audio_to_s3", fake_upload)
    audio = await meta_provider(rec).fetch_media_to_s3("1048234539876154", TO)

    assert audio.key == "voice-notes/x.ogg"
    assert uploads == [(b"OggS-real-voice-note", TO)]
    assert str(rec.requests[0].url) == "https://graph.facebook.com/v20.0/1048234539876154"
    assert str(rec.requests[1].url) == lookaside
    assert all(r.headers["Authorization"] == "Bearer EAAG-test-token" for r in rec.requests)


@pytest.mark.asyncio
async def test_meta_fetch_media_failure_raises():
    rec = Recorder(httpx.Response(404, json={"error": {"message": "Unsupported get request"}}))
    with pytest.raises(MediaFetchError):
        await meta_provider(rec).fetch_media_to_s3("123", TO)


# ---------------------------------------------------------------------------
# End User Messaging Social provider
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "video", "location_request", "template"])
async def test_eum_social_sends_same_body_as_meta(kind):
    rec = Recorder(meta_ok())
    meta_service = WhatsAppService(provider=meta_provider(rec))

    client = eum_client()
    eum_service = WhatsAppService(provider=EumSocialProvider(EUM_PHONE_ID, "v20.0", client=client, s3_client=object()))

    if kind == "text":
        send = lambda svc: svc.send_text_message(TO, "Help is on the way.")
    elif kind == "video":
        send = lambda svc: svc.send_video_call_invitation(TO, "Sarah Khan", "Cardiology", VIDEO_URL)
    elif kind == "location_request":
        send = lambda svc: svc.send_location_request(TO, "Share your location.")
    else:
        send = lambda svc: svc.send_emergency_location_alert(TO, "Ahmed Khan", "https://maps.google.com/?q=1,2")

    await send(meta_service)
    expected = dict(captured_meta_json(rec), to=TO)  # EUM Social keeps the '+'

    with Stubber(client) as stub:
        stub.add_response(
            "send_whatsapp_message",
            {"messageId": "eum-msg-1"},
            {"originationPhoneNumberId": EUM_PHONE_ID, "message": JsonBytes(expected), "metaApiVersion": "v20.0"},
        )
        result = await send(eum_service)
        stub.assert_no_pending_responses()

    assert result == {"status": "SENT", "provider": "eum_social", "recipient": TO, "message_id": "eum-msg-1"}


@pytest.mark.asyncio
async def test_eum_social_send_error_is_returned_not_raised():
    client = eum_client()
    provider = EumSocialProvider(EUM_PHONE_ID, "v20.0", client=client, s3_client=object())
    with Stubber(client) as stub:
        stub.add_client_error("send_whatsapp_message", "AccessDeniedByMetaException", "Re-engagement window closed")
        result = await provider.send_text(TO, "hi")
    assert result["status"] == "ERROR"
    assert "AccessDeniedByMetaException" in result["detail"]


class FakeS3:
    def __init__(self, keys=None, fail=False):
        self.keys = keys or []
        self.fail = fail
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("AccessDenied")
        prefix = kwargs["Prefix"]
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(prefix)]}


@pytest.mark.asyncio
async def test_eum_social_fetch_media_writes_straight_to_s3(monkeypatch):
    fixed_key = "voice-notes/20261011_110000_923001234567_ab12cd.ogg"
    prefix = "voice-notes/20261011_110000_923001234567_ab12cd_"
    monkeypatch.setattr(providers.audio_service, "build_audio_key", lambda phone, ext="ogg": fixed_key)
    stored = f"{prefix}1048234539876154.ogg"
    s3 = FakeS3(keys=[stored])
    client = eum_client()
    provider = EumSocialProvider(EUM_PHONE_ID, "v20.0", client=client, s3_client=s3, bucket="resq-raw-audio-vault")

    with Stubber(client) as stub:
        stub.add_response(
            "get_whatsapp_message_media",
            {"mimeType": "audio/ogg", "fileSize": 14},
            {
                "mediaId": "1048234539876154",
                "originationPhoneNumberId": EUM_PHONE_ID,
                "destinationS3File": {"bucketName": "resq-raw-audio-vault", "key": prefix},
            },
        )
        audio = await provider.fetch_media_to_s3("1048234539876154", TO)
        stub.assert_no_pending_responses()

    assert audio.key == stored
    assert audio.s3_uri == f"s3://{settings.S3_AUDIO_BUCKET}/{stored}"
    assert s3.calls[0]["Prefix"] == prefix


@pytest.mark.asyncio
async def test_eum_social_fetch_media_guesses_key_when_listing_fails():
    client = eum_client()
    provider = EumSocialProvider(EUM_PHONE_ID, "v20.0", client=client, s3_client=FakeS3(fail=True))
    with Stubber(client) as stub:
        stub.add_response(
            "get_whatsapp_message_media",
            {"mimeType": "audio/ogg; codecs=opus", "fileSize": 14},
            {"mediaId": "555", "originationPhoneNumberId": EUM_PHONE_ID, "destinationS3File": ANY},
        )
        audio = await provider.fetch_media_to_s3("555", TO)
    assert audio.key.startswith("voice-notes/")
    assert audio.key.endswith("_555.ogg")


@pytest.mark.asyncio
async def test_eum_social_fetch_media_failure_raises():
    client = eum_client()
    provider = EumSocialProvider(EUM_PHONE_ID, "v20.0", client=client, s3_client=FakeS3())
    with Stubber(client) as stub:
        stub.add_client_error("get_whatsapp_message_media", "ResourceNotFoundException", "media expired")
        with pytest.raises(MediaFetchError):
            await provider.fetch_media_to_s3("555", TO)


# ---------------------------------------------------------------------------
# Mock provider + facade
# ---------------------------------------------------------------------------
def test_test_suite_singleton_is_mock():
    # conftest: MOCK_AWS=True, WHATSAPP_PROVIDER=meta with no token -> loud fallback to mock.
    assert whatsapp_service.provider_name == "mock"
    assert whatsapp_service.mock_mode is True


@pytest.mark.asyncio
async def test_facade_methods_via_mock_provider(capsys):
    mock = MockProvider()
    service = WhatsAppService(provider=mock)

    results = [
        await service.send_text_message(TO, "hello"),
        await service.send_onboarding_prompt(TO),
        await service.send_registration_confirmation(TO, "Ahmed Khan", "Brother Ali", "+923331234567"),
        await service.send_registration_format_hint(TO),
        await service.send_video_call_invitation(TO, "Sarah Khan", "Cardiology", VIDEO_URL),
        await service.send_location_request(TO, "Share your location."),
        await service.send_emergency_location_alert(TO, "Ahmed Khan", "https://maps.google.com/?q=1,2"),
    ]

    assert all(r["status"] == "SENT_MOCK" and r["provider"] == "mock" for r in results)
    assert [p["type"] for p in mock.sent] == ["text", "text", "text", "text", "interactive", "interactive", "template"]
    assert "Ahmed Khan" in mock.sent[2]["text"]["body"] and "+923331234567" in mock.sent[2]["text"]["body"]
    assert "phone number" in mock.sent[3]["text"]["body"] and "voice note" in mock.sent[3]["text"]["body"]
    out = capsys.readouterr().out
    assert f"[WhatsApp-Outbound to {TO}]" in out
    assert VIDEO_URL in out
    assert "[Button: send_location]" in out
    assert "Ahmed Khan | https://maps.google.com/?q=1,2" in out


@pytest.mark.asyncio
async def test_mock_fetch_media_uploads_stub_ogg(monkeypatch):
    uploads = []

    def fake_upload(audio_bytes, phone):
        uploads.append((audio_bytes, phone))
        return AudioObject(key="voice-notes/stub.ogg", s3_uri="s3://b/voice-notes/stub.ogg")

    monkeypatch.setattr(providers.audio_service, "upload_audio_to_s3", fake_upload)
    audio = await WhatsAppService(provider=MockProvider()).fetch_media_to_s3("mock_media_1", TO)
    assert audio.key == "voice-notes/stub.ogg"
    assert uploads[0][0].startswith(b"OggS")


# ---------------------------------------------------------------------------
# Provider selection and startup validation
# ---------------------------------------------------------------------------
def cfg(**overrides):
    base = {
        "MOCK_AWS": False,
        "AWS_ENDPOINT_URL": None,
        "WHATSAPP_PROVIDER": "meta",
        "WHATSAPP_API_VERSION": "v21.0",
        "WHATSAPP_ACCESS_TOKEN": "EAAG-real-token",
        "WHATSAPP_PHONE_NUMBER_ID": META_PHONE_ID,
        "WHATSAPP_VERIFY_TOKEN": "verify-me",
        "EUM_SOCIAL_PHONE_NUMBER_ID": EUM_PHONE_ID,
    }
    base.update(overrides)
    return settings.model_copy(update=base)


def test_select_meta_when_configured():
    provider = build_provider(cfg())
    assert isinstance(provider, MetaCloudProvider)
    assert provider.messages_url == f"https://graph.facebook.com/v21.0/{META_PHONE_ID}/messages"


def test_select_eum_social_when_configured():
    client = eum_client()
    provider = build_provider(cfg(WHATSAPP_PROVIDER="EUM_SOCIAL"), credentials_check=lambda: True, eum_client=client)
    assert isinstance(provider, EumSocialProvider)
    assert provider.client is client
    assert provider.api_version == "v21.0"


def test_eum_social_accepts_phone_number_arn():
    arn = "arn:aws:social-messaging:us-east-1:123456789012:phone-number-id/976c72a700aac43eaf573ae050example"
    provider = build_provider(
        cfg(WHATSAPP_PROVIDER="eum_social", EUM_SOCIAL_PHONE_NUMBER_ID=arn),
        credentials_check=lambda: True, eum_client=eum_client(),
    )
    assert provider.phone_number_id == arn


@pytest.mark.parametrize("overrides, expected", [
    ({"WHATSAPP_ACCESS_TOKEN": ""}, "WHATSAPP_ACCESS_TOKEN"),
    ({"WHATSAPP_ACCESS_TOKEN": "mock_token"}, "WHATSAPP_ACCESS_TOKEN"),
    ({"WHATSAPP_PHONE_NUMBER_ID": ""}, "WHATSAPP_PHONE_NUMBER_ID"),
    ({"WHATSAPP_VERIFY_TOKEN": ""}, "WHATSAPP_VERIFY_TOKEN"),
    ({"WHATSAPP_PROVIDER": "eum_social", "EUM_SOCIAL_PHONE_NUMBER_ID": ""}, "EUM_SOCIAL_PHONE_NUMBER_ID"),
    ({"WHATSAPP_PROVIDER": "eum_social", "EUM_SOCIAL_PHONE_NUMBER_ID": "106540352242922"}, "EUM_SOCIAL_PHONE_NUMBER_ID"),
    ({"WHATSAPP_PROVIDER": "whatsapp"}, "unknown WHATSAPP_PROVIDER"),
])
def test_real_aws_fails_fast_on_missing_settings(overrides, expected):
    with pytest.raises(RuntimeError, match=expected):
        build_provider(cfg(**overrides), credentials_check=lambda: True, eum_client=eum_client())


def test_real_aws_eum_social_without_credentials_fails_fast():
    with pytest.raises(RuntimeError, match="AWS credentials"):
        build_provider(cfg(WHATSAPP_PROVIDER="eum_social"), credentials_check=lambda: False)


@pytest.mark.parametrize("local", [{"MOCK_AWS": True}, {"AWS_ENDPOINT_URL": "http://floci:4566"}])
def test_local_runs_fall_back_to_mock_with_loud_warning(local, capsys):
    provider = build_provider(cfg(WHATSAPP_ACCESS_TOKEN="", **local))
    assert isinstance(provider, MockProvider)
    out = capsys.readouterr().out
    assert "WARNING" in out and "WHATSAPP_ACCESS_TOKEN" in out and "MOCK provider" in out


def test_explicit_mock_only_allowed_locally():
    assert isinstance(build_provider(cfg(WHATSAPP_PROVIDER="mock", MOCK_AWS=True)), MockProvider)
    with pytest.raises(RuntimeError, match="only allowed for local"):
        build_provider(cfg(WHATSAPP_PROVIDER="mock"))


def test_missing_settings_reports_everything():
    assert missing_settings(cfg(), "meta") == []
    assert missing_settings(cfg(WHATSAPP_ACCESS_TOKEN="", WHATSAPP_PHONE_NUMBER_ID=""), "meta") == [
        "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    ]
    assert len(missing_settings(cfg(EUM_SOCIAL_PHONE_NUMBER_ID=""), "eum_social", lambda: False)) == 2
    assert factory.is_local_runtime(cfg()) is False


def test_the_doctor_title_is_added_once():
    from app.services.whatsapp_service import with_title
    assert with_title("Sarah Khan") == "Dr. Sarah Khan"
    assert with_title("Dr. Ayesha Siddiqui, FCPS") == "Dr. Ayesha Siddiqui, FCPS"
    assert with_title("dr Ali") == "dr Ali"
    assert with_title("Drake Ali") == "Dr. Drake Ali"
