"""Photos (IMAGE-1): the patient's photo is described by the image analysis agent and triaged like
a voice note's transcript; the doctor sees the photo with the description; the doctor sends photos
and voice notes from the portal to the patient's WhatsApp."""
import io
import json
import math
import struct

import av
import httpx
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from PIL import Image

from app import pipeline
from app.agents import image_agent
from app.agents.image_agent import ImageAnalysis, ImageAnalyzer, photo_text, prepare_image
from app.main import app
from app.messaging import payloads
from app.messaging.inbound import parse_meta_webhook
from app.messaging.providers import EumSocialProvider, MediaUploadError, MetaCloudProvider, MockProvider
from app.models.schemas import ChatMessage, UserProfile
from app import case_messages
from app.routers import cases as cases_router
from app.services import media_convert
from app.services.audio_service import audio_service
from app.services.dynamo_service import dynamo_service
from app.services.whatsapp_service import WhatsAppService
from test_messaging_providers import EUM_PHONE_ID, META_PHONE_ID, TO, Recorder, eum_client, meta_ok
from test_pipeline_support import FakeWhatsApp, fakes, inbound, new_phone  # noqa: F401  (fixture import)

CONTACT = "+923331234567"


def recording(fmt: str, codec: str, layout: str = "mono", options=None, seconds: float = 1.0) -> bytes:
    """A short 440 Hz tone the way a browser records it: `fmt` container, `codec`, `layout`."""
    rate = 48000
    channels = 2 if layout == "stereo" else 1
    samples = int(rate * seconds)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate))) * channels
                   for n in range(samples))
    frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
    frame.planes[0].update(pcm)
    frame.sample_rate = rate
    out = io.BytesIO()
    with av.open(out, "w", format=fmt, options=options or {}) as container:
        stream = container.add_stream(codec, rate=rate, layout=layout)
        container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    return out.getvalue()


# Chrome / Safari record fragmented MP4 (AAC), Firefox OGG/Opus, older Chrome / Edge WebM.
CHROME_MP4 = {"movflags": "frag_keyframe+empty_moov+default_base_moof"}


def assert_voice_note(data: bytes):
    """Mono 48 kHz OGG/Opus: what WhatsApp plays as a voice note."""
    with av.open(io.BytesIO(data)) as container:
        assert container.format.name == "ogg"
        [stream] = container.streams.audio
        assert (stream.codec_context.name, stream.codec_context.layout.name, stream.rate) == ("opus", "mono", 48000)
        assert float(container.duration) / av.time_base > 0.5


def png_bytes(size=(4, 4)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (180, 20, 20)).save(out, "PNG")
    return out.getvalue()


def registered() -> str:
    phone = new_phone()
    dynamo_service.save_user(UserProfile(phone_number=phone, name="Ahmed Khan", emergency_contact_name="Brother Ali",
                                         emergency_contact_phone=CONTACT))
    return phone


def meta_body(message: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550001111", "phone_number_id": "1"},
        "contacts": [{"profile": {"name": "Ahmed"}, "wa_id": "923001234567"}],
        "messages": [{"from": "923001234567", "id": "wamid.IMG", "timestamp": "1791716400", **message}],
    }}]}]}


# ---------------------------------------------------------------- inbound parsing
def test_meta_image_message_is_parsed_with_caption():
    [msg] = parse_meta_webhook(meta_body({"type": "image", "image": {
        "id": "777", "mime_type": "image/jpeg", "sha256": "abc", "caption": "  my leg after the fall "}}))
    assert (msg.type, msg.media_id, msg.media_mime_type, msg.caption) == ("image", "777", "image/jpeg", "my leg after the fall")


def test_photo_sent_as_document_is_read_as_image():
    [msg] = parse_meta_webhook(meta_body({"type": "document", "document": {
        "id": "778", "mime_type": "image/png", "filename": "burn.png"}}))
    assert (msg.type, msg.media_id, msg.media_mime_type, msg.caption) == ("image", "778", "image/png", None)


# ---------------------------------------------------------------- image agent
@pytest.mark.asyncio
async def test_analyzer_in_mock_mode_never_invents_an_analysis():
    result = await ImageAnalyzer(use_models=False).analyze(png_bytes(), "image/png", "cut")
    assert result.ok is False and result.error == image_agent.ANALYSIS_UNAVAILABLE


@pytest.mark.asyncio
async def test_analyzer_falls_back_to_the_next_model(monkeypatch):
    analyzer = ImageAnalyzer(use_models=True, model_factory=lambda model_id: model_id)
    analyzer.model_ids = ["primary", "fallback"]
    tried = []

    async def fake_with_model(model_id, image, fmt, caption):
        tried.append((model_id, fmt, caption))
        if model_id == "primary":
            raise RuntimeError("throttled")
        return image_agent.ImageFindings(shows_medical_concern=True, description=" Second-degree burn on the hand. ",
                                         clinical_summary="Partial-thickness hand burn.", findings=["blisters", " "],
                                         red_flags=[], image_quality="Clear")

    monkeypatch.setattr(analyzer, "_with_model", fake_with_model)
    result = await analyzer.analyze(png_bytes(), "image/png", "hot oil")
    assert tried == [("primary", "png", "hot oil"), ("fallback", "png", "hot oil")]
    assert result.ok and result.engine == "bedrock:fallback"
    assert result.description == "Second-degree burn on the hand."
    assert result.findings == ["blisters"] and result.image_quality == "clear"


@pytest.mark.asyncio
async def test_analyzer_reports_every_model_failing_and_unreadable_photos(monkeypatch):
    analyzer = ImageAnalyzer(use_models=True, model_factory=lambda model_id: model_id)

    async def boom(*args):
        raise RuntimeError("down")

    monkeypatch.setattr(analyzer, "_with_model", boom)
    assert (await analyzer.analyze(png_bytes(), "image/png")).error == "every image analysis model failed"
    unreadable = await analyzer.analyze(b"not an image", "image/jpeg")
    assert unreadable.ok is False and "could not be read" in unreadable.error


def test_prepare_image_scales_large_photos_down_for_the_model():
    data, fmt = prepare_image(png_bytes((3000, 1000)), "image/png")
    img = Image.open(io.BytesIO(data))
    assert fmt == "jpeg" and img.format == "JPEG" and max(img.size) == image_agent.MAX_MODEL_SIDE_PX
    small = png_bytes()
    assert prepare_image(small, "image/png") == (small, "png")
    # A wrong MIME type is corrected from the file itself.
    assert prepare_image(small, "application/octet-stream") == (small, "png")


def test_photo_text_with_and_without_analysis():
    ok = ImageAnalysis(ok=True, description="Swollen ankle.", findings=["swelling"], red_flags=["deformity"],
                       image_quality="poor")
    text = photo_text("fell down stairs", ok)
    assert text.splitlines() == ['[Photo sent by the patient]', 'Patient\'s caption: "fell down stairs"',
                                 "Image analysis: Swollen ankle.", "Visible findings: swelling",
                                 "Red flags visible: deformity", "Image quality: poor"]
    assert "Image analysis unavailable: down" in photo_text(None, ImageAnalysis(ok=False, error="down"))


# ---------------------------------------------------------------- pipeline
@pytest.mark.asyncio
async def test_photo_is_analysed_and_triaged_and_the_doctor_sees_it(fakes):
    phone = registered()
    res = await pipeline.process_inbound_message(inbound(phone, type="image", media_id="img-1", caption="mera haath"))
    assert res["action"] == "triaged"
    case = dynamo_service.get_case(res["case_id"])
    assert case.image_s3_key.endswith("img-1.jpg") and case.audio_s3_key is None
    assert case.image_analysis["clinical_summary"] == "Deep forearm laceration with active bleeding."
    assert case.transcript.startswith("[Photo sent by the patient]")

    [fetch] = [c for c in fakes.whatsapp.calls if c[0] == "fetch_media_to_s3"]
    assert fetch[2] == {"kind": "image", "mime_type": "image/jpeg"}
    [shown] = fakes.images.calls
    assert shown["caption"] == "mera haath" and shown["data"].startswith(b"\xff\xd8")
    [triaged] = fakes.triage.calls
    assert 'Patient\'s caption: "mera haath"' in triaged["transcript"]
    assert "Red flags visible: heavy active bleeding" in triaged["transcript"]

    first = next(m for m in dynamo_service.get_case_messages(case.case_id) if m.sender == "PATIENT")
    assert first.image_s3_key == case.image_s3_key
    assert first.content.startswith("📷 [Photo sent by the patient]")
    assert first.content.endswith("AI summary: Deep forearm laceration with active bleeding.")
    assert "EMERGENCY DETECTED" in fakes.whatsapp.texts()[0]


@pytest.mark.asyncio
async def test_unanalysed_photo_without_caption_opens_a_human_review_case(fakes):
    fakes.images.analysis = ImageAnalysis(ok=False, error=image_agent.ANALYSIS_UNAVAILABLE)
    phone = registered()
    res = await pipeline.process_inbound_message(inbound(phone, type="image", media_id="img-2"))
    assert res["action"] == "human_review"
    case = dynamo_service.get_case(res["case_id"])
    assert case.needs_human_review and case.esi_level == 2 and case.status == "ACTIVE"
    assert case.transcript.startswith("[Image analysis failed: image analysis unavailable")
    assert case.triage_engine == "none (image analysis failed)"
    assert case.image_s3_key.endswith("img-2.jpg")
    assert fakes.triage.calls == [] and case.contact_call_id is None
    reply = fakes.whatsapp.texts()[0]
    assert "received your photo but could not analyse it" in reply and "to look at it" in reply
    chat = next(m for m in dynamo_service.get_case_messages(case.case_id) if m.sender == "PATIENT")
    assert chat.image_s3_key and "Image analysis unavailable" in chat.content


@pytest.mark.asyncio
async def test_unanalysed_photo_with_caption_triages_the_caption(fakes):
    fakes.images.analysis = ImageAnalysis(ok=False, error="every image analysis model failed")
    phone = registered()
    res = await pipeline.process_inbound_message(inbound(phone, type="image", caption="bohat khoon beh raha hai"))
    assert res["action"] == "triaged"
    [triaged] = fakes.triage.calls
    assert '"bohat khoon beh raha hai"' in triaged["transcript"]
    assert "Image analysis unavailable: every image analysis model failed" in triaged["transcript"]


@pytest.mark.asyncio
async def test_photo_on_an_open_case_goes_to_the_doctor_chat(fakes):
    phone = registered()
    first = await pipeline.process_inbound_message(inbound(phone, text="seene me dard"))
    res = await pipeline.process_inbound_message(inbound(phone, type="image", media_id="img-3", caption="ECG report"))
    assert (res["action"], res["case_id"]) == ("handover", first["case_id"])
    assert len(fakes.triage.calls) == 1   # the ESI 1 protocol ran already: no re-triage
    photo = [m for m in dynamo_service.get_case_messages(first["case_id"]) if m.image_s3_key]
    assert len(photo) == 1 and photo[0].sender == "PATIENT" and "ECG report" in photo[0].content


@pytest.mark.asyncio
async def test_photo_follow_up_escalates_an_esi3_case_and_lands_on_the_case(fakes):
    fakes.triage.esi = 3
    phone = registered()
    first = await pipeline.process_inbound_message(inbound(phone, text="haath pe chot lagi"))
    fakes.triage.esi = 1
    res = await pipeline.process_inbound_message(inbound(phone, type="image", media_id="img-4"))
    assert (res["action"], res["case_id"]) == ("escalated", first["case_id"])
    latest = fakes.triage.calls[-1]["transcript"]
    assert latest.startswith("[Earlier message] haath pe chot lagi")
    assert "[Latest message] [Photo sent by the patient]" in latest
    case = dynamo_service.get_case(first["case_id"])
    assert case.image_s3_key.endswith("img-4.jpg") and case.image_analysis["ok"] is True


def test_patient_said_reads_photo_lines_for_follow_up_triage():
    def said(content):
        return pipeline._patient_said(ChatMessage(message_id="m", case_id="c", sender="PATIENT", sender_name="p",
                                                  content=content))
    assert said("📷 [Photo sent by the patient]\nImage analysis: Burn.\nAI summary: Burn.") == \
        "[Photo sent by the patient]\nImage analysis: Burn."
    assert said("📷 [Photo sent by the patient]\nImage analysis unavailable: down") is None


# ---------------------------------------------------------------- outbound payloads and providers
def test_media_payloads():
    assert payloads.image_message("923", "m1", "Dr. A: keep pressure") == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": "923", "type": "image",
        "image": {"id": "m1", "caption": "Dr. A: keep pressure"}}
    assert payloads.image_message("923", "m1")["image"] == {"id": "m1"}
    assert payloads.audio_message("923", "m2")["audio"] == {"id": "m2"}


@pytest.mark.asyncio
async def test_meta_uploads_media_then_sends_it_by_id():
    rec = Recorder(httpx.Response(200, json={"id": "4490709327384033"}), meta_ok("wamid.IMG-OUT"))
    service = WhatsAppService(provider=MetaCloudProvider("EAAG-test-token", META_PHONE_ID, "v20.0",
                                                         transport=httpx.MockTransport(rec)))
    result = await service.send_media_message(TO, "image", "outbound/c/x.png", b"PNGDATA", "image/png", caption="hi")
    assert result["status"] == "SENT" and result["message_id"] == "wamid.IMG-OUT"
    upload, send = rec.requests
    assert str(upload.url) == f"https://graph.facebook.com/v20.0/{META_PHONE_ID}/media"
    assert b'name="messaging_product"' in upload.content and b"PNGDATA" in upload.content
    assert json.loads(send.content)["image"] == {"id": "4490709327384033", "caption": "hi"}


@pytest.mark.asyncio
async def test_meta_upload_failure_is_an_error_result_not_an_exception():
    rec = Recorder(httpx.Response(400, json={"error": {"message": "Param file must be a file with one of the following types"}}))
    service = WhatsAppService(provider=MetaCloudProvider("t", META_PHONE_ID, transport=httpx.MockTransport(rec)))
    result = await service.send_media_message(TO, "audio", "outbound/c/x.webm", b"x", "audio/webm")
    assert result["status"] == "ERROR" and "Param file" in result["detail"]


@pytest.mark.asyncio
async def test_eum_social_uploads_from_the_vault_then_sends():
    client = eum_client()
    provider = EumSocialProvider(EUM_PHONE_ID, "v20.0", client=client, bucket="resq-raw-audio-vault")
    with Stubber(client) as stub:
        stub.add_response("post_whatsapp_message_media", {"mediaId": "eum-media-1"}, {
            "originationPhoneNumberId": EUM_PHONE_ID,
            "sourceS3File": {"bucketName": "resq-raw-audio-vault", "key": "outbound/c/v.ogg"}})
        assert await provider.upload_media(b"ignored", "outbound/c/v.ogg", "audio/ogg") == "eum-media-1"
        stub.add_client_error("post_whatsapp_message_media", "AccessDeniedException")
        with pytest.raises(MediaUploadError):
            await provider.upload_media(b"", "outbound/c/v.ogg", "audio/ogg")


@pytest.mark.asyncio
async def test_mock_provider_stores_a_stub_photo():
    obj = await MockProvider().fetch_media_to_s3("m-9", "+923001110000", kind="image", mime_type="image/jpeg")
    assert obj.key.startswith("images/") and obj.key.endswith(".png")
    assert Image.open(io.BytesIO(audio_service.read_media_bytes(obj.key))).size == (1, 1)


# ---------------------------------------------------------------- portal: doctor sends media
class ErrorWhatsApp(FakeWhatsApp):
    async def send_media_message(self, *args, **kwargs):
        return {"status": "ERROR", "detail": "131053 Media upload error"}


def open_case(client) -> str:
    return client.post("/api/v1/webhook/simulate-voice",
                       json={"user_phone": new_phone(), "voice_text": "dil ka daura"}).json()["case"]["case_id"]


def test_doctor_sends_a_photo_and_it_is_viewable(monkeypatch):
    wa = FakeWhatsApp()
    _wa = wa
    monkeypatch.setattr(cases_router, "whatsapp_service", _wa)
    monkeypatch.setattr(case_messages, "whatsapp_service", _wa)  # the portal send code
    with TestClient(app) as client:
        case_id = open_case(client)
        r = client.post(f"/api/v1/cases/{case_id}/messages/media",
                        files={"file": ("wound-care.png", png_bytes(), "image/png")},
                        data={"caption": "Clean it like this", "sender_name": "Dr. Sarah Khan"})
        assert r.status_code == 200, r.text
        msg = r.json()
        assert msg["sender"] == "DOCTOR" and msg["content"] == "📷 [Photo]: Clean it like this"
        assert msg["image_s3_key"].startswith(f"outbound/{case_id}/") and msg["audio_s3_key"] is None

        [(name, args, kwargs)] = wa.calls
        assert name == "send_media_message" and args[1] == "image" and args[4] == "image/png"
        assert kwargs["caption"] == "👨‍⚕️ *Dr. Sarah Khan*: Clean it like this"

        meta = client.get(f"/api/v1/cases/{case_id}/messages/{msg['message_id']}/image").json()
        assert meta["url"] == f"/api/v1/cases/{case_id}/messages/{msg['message_id']}/image/stream"
        stream = client.get(meta["url"])
        assert stream.status_code == 200 and stream.headers["content-type"] == "image/png"
        assert client.get(f"/api/v1/cases/{case_id}/image").status_code == 404   # the case has no patient photo


def test_doctor_voice_note_is_sent_as_ogg_with_a_label_first(monkeypatch):
    wa = FakeWhatsApp()
    _wa = wa
    monkeypatch.setattr(cases_router, "whatsapp_service", _wa)
    monkeypatch.setattr(case_messages, "whatsapp_service", _wa)  # the portal send code
    with TestClient(app) as client:
        case_id = open_case(client)
        r = client.post(f"/api/v1/cases/{case_id}/messages/media",
                        files={"file": ("voice-note.m4a", recording("mp4", "aac", "stereo", CHROME_MP4),
                                        "audio/mp4;codecs=mp4a.40.2")},
                        data={"sender_name": "Dr. Sarah Khan"})
        assert r.status_code == 200, r.text
        assert r.json()["audio_s3_key"].endswith(".ogg") and r.json()["content"] == "🎤 [Voice note]"
        assert [c[0] for c in wa.calls] == ["send_text_message", "send_media_message"]
        assert wa.calls[0][1][1] == "👨‍⚕️ *Dr. Sarah Khan*: Voice note from your doctor"
        assert wa.calls[1][1][1] == "audio" and wa.calls[1][1][4] == "audio/ogg"
        assert_voice_note(wa.calls[1][1][3])
        assert_voice_note(audio_service.read_media_bytes(r.json()["audio_s3_key"]))
        assert client.get(f"/api/v1/cases/{case_id}/messages/{r.json()['message_id']}/audio").status_code == 200


def test_doctor_media_is_refused_when_it_cannot_be_sent(monkeypatch):
    _wa = FakeWhatsApp()
    monkeypatch.setattr(cases_router, "whatsapp_service", _wa)
    monkeypatch.setattr(case_messages, "whatsapp_service", _wa)  # the portal send code
    with TestClient(app) as client:
        case_id = open_case(client)
        url = f"/api/v1/cases/{case_id}/messages/media"
        broken = client.post(url, files={"file": ("n.webm", b"\x1aE\xdf\xa3", "audio/webm;codecs=opus")})
        assert broken.status_code == 415 and "audio/webm" in broken.json()["detail"]
        assert client.post(url, files={"file": ("a.pdf", b"%PDF", "application/pdf")}).status_code == 415
        assert client.post(url, files={"file": ("x.png", b"not a png at all", "image/webp")}).status_code == 415
        assert client.post(url, files={"file": ("e.png", b"", "image/png")}).status_code == 400

        _wa = ErrorWhatsApp()
        monkeypatch.setattr(cases_router, "whatsapp_service", _wa)
        monkeypatch.setattr(case_messages, "whatsapp_service", _wa)  # the portal send code
        failed = client.post(url, files={"file": ("p.png", png_bytes(), "image/png")})
        assert failed.status_code == 502 and "131053" in failed.json()["detail"]
        assert not [m for m in dynamo_service.get_case_messages(case_id) if m.image_s3_key]

        client.post(f"/api/v1/cases/{case_id}/resolve")
        closed = client.post(url, files={"file": ("p.png", png_bytes(), "image/png")})
        assert closed.status_code == 409


def test_webp_photo_from_the_doctor_is_converted_to_jpeg():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(out, "WEBP")
    data, content_type, ext = media_convert.prepare_image(out.getvalue(), "image/webp")
    assert (content_type, ext) == ("image/jpeg", "jpg") and Image.open(io.BytesIO(data)).format == "JPEG"


@pytest.mark.parametrize("recorded, content_type", [
    (lambda: recording("mp4", "aac", "stereo", CHROME_MP4), "audio/mp4;codecs=mp4a.40.2"),   # Chrome, Safari
    (lambda: recording("mp4", "libopus", "mono", CHROME_MP4), "audio/mp4"),                  # Chrome without AAC
    (lambda: recording("ogg", "libopus", "stereo"), "audio/ogg;codecs=opus"),                # Firefox
    (lambda: recording("webm", "libopus", "mono"), "audio/webm;codecs=opus"),                # older Chrome / Edge
])
def test_every_recording_becomes_a_mono_ogg_opus_voice_note(recorded, content_type):
    data, out_type, ext = media_convert.prepare_audio(recorded(), content_type)
    assert (out_type, ext) == ("audio/ogg", "ogg")
    assert_voice_note(data)


def test_unreadable_audio_is_refused():
    for data in (b"\x1aE", b"OggS-not-really"):
        with pytest.raises(media_convert.UnsupportedMedia):
            media_convert.prepare_audio(data, "audio/ogg")
    with pytest.raises(media_convert.UnsupportedMedia, match="no audio"):
        media_convert.prepare_audio(png_bytes(), "audio/mp4")
