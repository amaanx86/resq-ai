"""Data-layer regressions found in review, against a moto DynamoDB with the real ResQTable schema:

1. DynamoDB mode never answers from the stale in-memory mirror when DynamoDB says "nothing"
   (an off-duty doctor must not be assigned by a worker that cached him).
2. Scans and queries follow LastEvaluatedKey (a single page stops at 1 MB); open cases come from
   GSI_SpecialtyActive, not a filtered scan.
3. Startup seeding never overwrites existing doctor / demo-user records.
4. A failed S3 upload raises instead of returning an AudioObject for a missing object.
7. update_case writes only the changed fields (UpdateItem), so concurrent writers don't clobber.
"""
import uuid

import boto3
import httpx
import pytest
from moto import mock_aws

from app.agents import tools
from app.config import settings
from app.messaging.providers import MediaFetchError, MetaCloudProvider, MockProvider
from app.models.schemas import ChatMessage, DoctorProfile, EmergencyCase, UserProfile
from app.services import audio_service as audio_module
from app.services.audio_service import AudioUploadError, audio_service
from app.services.dynamo_service import DynamoDBService, _to_item, create_resq_table

DEMO_USER = "+923009988776"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", False)
    with mock_aws():
        # Real-AWS mode only checks the table (Terraform owns it), so create it first.
        create_resq_table(boto3.client("dynamodb", region_name=settings.AWS_REGION), settings.DYNAMODB_TABLE_NAME)
        yield monkeypatch


def _service(monkeypatch, seed: bool) -> DynamoDBService:
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", seed)
    svc = DynamoDBService()
    assert not svc.mock_mode and svc.table is not None
    return svc


def _case(case_id: str, status: str = "DOCTOR_HANDOVER", doctor: str = "DOC-1", transcript: str = "t") -> EmergencyCase:
    return EmergencyCase(case_id=case_id, user_phone="+92300", patient_name="X", transcript=transcript,
                         detected_language="Urdu", esi_level=2, severity="CRITICAL", status=status,
                         assigned_doctor_id=doctor, confidence_score=0.93)


# ---------------------------------------------------------------- 1. stale in-memory fallback
def test_worker_never_assigns_a_doctor_who_went_off_duty(aws):
    api = _service(aws, seed=True)       # API task: seeds the demo doctors into DynamoDB
    worker = _service(aws, seed=False)   # worker task: separate process, empty memory, no seed
    aws.setattr(tools, "dynamo_service", worker)

    first, tier = tools.rank_candidates("Cardiology")[0]
    assert first.doctor_id == "DOC-CARDIO-01" and tier == "specialty"
    assert worker.increment_doctor_cases_handled("DOC-CARDIO-01").cases_handled == 19
    assert worker._mock_doctors == {}  # the increment no longer caches doctors in memory

    api.update_doctor_status("DOC-CARDIO-01", False)  # doctor goes off duty in the portal
    raw = api.table.get_item(Key={"PK": "DOCTOR#DOC-CARDIO-01", "SK": "PROFILE"})["Item"]
    assert raw["GSI1SK"] == "STATUS#INACTIVE" and raw["data"]["cases_handled"] == 19  # increment kept

    worker._mock_doctors["DOC-CARDIO-01"] = DoctorProfile(  # even with a stale cached copy...
        doctor_id="DOC-CARDIO-01", name="stale", phone_number="+1", license_no="L", specialty="Cardiology",
        bio="b", is_active=True)
    assert worker.get_available_doctors_by_specialty("Cardiology") == []
    ranked = tools.rank_candidates("Cardiology")
    assert "DOC-CARDIO-01" not in [d.doctor_id for d, _ in ranked] and ranked[0][1] == "any_active"


def test_dynamodb_readers_return_dynamodb_answer_even_when_empty(aws):
    svc = _service(aws, seed=False)
    svc._mock_users["+1"] = UserProfile(phone_number="+1", name="ghost")
    svc._mock_doctors["DOC-X"] = DoctorProfile(doctor_id="DOC-X", name="ghost", phone_number="+1", license_no="L",
                                               specialty="Trauma", bio="b")
    svc._mock_cases["CASE-X"] = _case("CASE-X")
    svc._mock_messages["CASE-X"] = [ChatMessage(message_id="M", case_id="CASE-X", sender="BOT", sender_name="b", content="c")]
    svc._mock_idempotency["k"] = {"status": "COMPLETE", "claimed_at": "x", "result": {"a": 1}}

    assert svc.get_user("+1") is None
    assert svc.get_doctor("DOC-X") is None
    assert svc.list_doctors() == []
    assert svc.get_available_doctors_by_specialty("Trauma") == []
    assert svc.get_case("CASE-X") is None
    assert svc.list_all_cases() == [] and svc.list_active_cases() == []
    assert svc.count_open_cases_for_doctor("DOC-1") == 0
    assert svc.get_case_messages("CASE-X") == []
    assert svc.get_idempotency_record("k") is None
    assert svc.update_case("CASE-X", {"status": "RESOLVED"}) is None
    assert svc.increment_doctor_cases_handled("DOC-X") is None
    assert svc.update_doctor_status("DOC-X", False) is None


def test_memory_is_used_only_when_the_dynamodb_call_raises(aws):
    svc = _service(aws, seed=False)
    svc._mock_doctors["DOC-X"] = DoctorProfile(doctor_id="DOC-X", name="cached", phone_number="+1", license_no="L",
                                               specialty="Trauma", bio="b")

    def down(**kwargs):
        raise RuntimeError("DynamoDB unreachable")

    aws.setattr(svc.table, "query", down)
    aws.setattr(svc.table, "get_item", down)
    assert [d.doctor_id for d in svc.get_available_doctors_by_specialty("Trauma")] == ["DOC-X"]
    assert svc.get_doctor("DOC-X").name == "cached"


def test_mock_mode_behaviour_is_unchanged():
    svc = DynamoDBService()  # conftest: MOCK_AWS=True, SEED_DEMO_DATA=True
    assert svc.mock_mode and svc.table is None
    assert [d.doctor_id for d in svc.get_available_doctors_by_specialty("cardiology")] == ["DOC-CARDIO-01"]
    assert svc.increment_doctor_cases_handled("DOC-CARDIO-01").cases_handled == 19
    assert svc.update_doctor_status("DOC-CARDIO-01", False).is_active is False
    assert svc.get_available_doctors_by_specialty("Cardiology") == []
    svc.save_case(_case("CASE-M1"))
    svc.save_case(_case("CASE-M2", status="RESOLVED"))
    assert [c.case_id for c in svc.list_active_cases()] == ["CASE-M1"]
    assert svc.update_case("CASE-M1", {"status": "RESOLVED", "bogus": 1}).status == "RESOLVED"
    assert svc.list_active_cases() == [] and svc.update_case("CASE-NOPE", {"status": "X"}) is None


# ---------------------------------------------------------------- 2. pagination
def test_scans_and_queries_paginate_past_one_megabyte(aws):
    svc = _service(aws, seed=False)
    n = 260  # ~1.3 MB of cases (and of messages): one Scan/Query page stops at 1 MB
    big = "t" * 5000
    with svc.table.batch_writer() as batch:
        for i in range(n):
            c = _case(f"CASE-{i:05d}", status="ACTIVE" if i % 10 == 0 else "DOCTOR_HANDOVER", transcript=big)
            batch.put_item(Item={"PK": f"CASE#{c.case_id}", "SK": "METADATA", "GSI1PK": f"STATUS#{c.status}",
                                 "GSI1SK": f"DATE#{c.created_at[:10]}", "GSI2PK": f"MONTH#{c.created_at[:7]}",
                                 "GSI2SK": c.created_at, "data": _to_item(c)})
        for i in range(n):
            m = ChatMessage(message_id=f"M{i:05d}", case_id="CASE-00000", sender="PATIENT", sender_name="p",
                            content=big, timestamp=f"2026-10-11T00:00:00.{i:06d}+00:00")
            batch.put_item(Item={"PK": "CASE#CASE-00000", "SK": f"MSG#{m.timestamp}#{m.message_id}",
                                 "data": m.model_dump(mode="json")})
    for i in range(30):
        svc.register_doctor(DoctorProfile(doctor_id=f"DOC-P{i:02d}", name=f"D{i}", phone_number="+1",
                                          license_no="L", specialty="Trauma", bio="b"))
    svc._mock_cases.clear(); svc._mock_doctors.clear(); svc._mock_messages.clear()
    # Prove the data spans several pages for each access path.
    from boto3.dynamodb.conditions import Key
    assert "LastEvaluatedKey" in svc.table.scan()
    assert "LastEvaluatedKey" in svc.table.query(IndexName="GSI_SpecialtyActive",
                                                 KeyConditionExpression=Key("GSI1PK").eq("STATUS#DOCTOR_HANDOVER"))
    assert "LastEvaluatedKey" in svc.table.query(KeyConditionExpression=Key("PK").eq("CASE#CASE-00000"))

    scans = []
    real_scan = svc.table.scan

    def spy_scan(**kwargs):
        scans.append(kwargs)
        return real_scan(**kwargs)

    aws.setattr(svc.table, "scan", spy_scan)
    assert len(svc.list_active_cases()) == n
    assert scans == []  # open cases come from GSI_SpecialtyActive queries, not a scan
    assert svc.count_open_cases_for_doctor("DOC-1") == n
    assert len(svc.list_all_cases()) == n
    assert len(svc.list_doctors()) == 30
    assert len(svc.get_case_messages("CASE-00000")) == n


def test_list_active_cases_follows_status_changes_on_the_gsi(aws):
    svc = _service(aws, seed=False)
    svc.save_case(_case("CASE-A", status="ACTIVE"))
    svc.save_case(_case("CASE-H", status="DOCTOR_HANDOVER"))
    svc.save_case(_case("CASE-D", status="DISMISSED"))
    assert {c.case_id for c in svc.list_active_cases()} == {"CASE-A", "CASE-H"}
    svc.update_case("CASE-A", {"status": "RESOLVED"})
    assert [c.case_id for c in svc.list_active_cases()] == ["CASE-H"]


# ---------------------------------------------------------------- 3. seeding never overwrites
def test_api_restart_keeps_doctor_and_demo_user_state(aws):
    first = _service(aws, seed=True)
    first.update_doctor_status("DOC-CARDIO-01", False)
    for _ in range(3):
        first.increment_doctor_cases_handled("DOC-NEURO-02")
    user = first.get_user(DEMO_USER)
    first.save_user(user.model_copy(update={"active_case_id": "CASE-OPEN1", "emergency_contact_phone": "+923339999999"}))

    restarted = _service(aws, seed=True)  # new API task, seeds again
    cardio = restarted.get_doctor("DOC-CARDIO-01")
    assert cardio.is_active is False
    assert restarted.table.get_item(Key={"PK": "DOCTOR#DOC-CARDIO-01", "SK": "PROFILE"})["Item"]["GSI1SK"] == "STATUS#INACTIVE"
    assert restarted.get_doctor("DOC-NEURO-02").cases_handled == 15
    user = restarted.get_user(DEMO_USER)
    assert user.active_case_id == "CASE-OPEN1" and user.emergency_contact_phone == "+923339999999"
    assert {d.doctor_id for d in restarted.list_doctors()} == {"DOC-CARDIO-01", "DOC-NEURO-02", "DOC-TRAUMA-03", "DOC-PULMO-04"}


def test_seed_fills_an_empty_table(aws):
    svc = _service(aws, seed=True)
    assert svc.get_doctor("DOC-TRAUMA-03").name.startswith("Dr. James")
    assert [d.doctor_id for d in svc.get_available_doctors_by_specialty("Cardiology")] == ["DOC-CARDIO-01"]
    assert svc.get_user(DEMO_USER).name == "Zubair Ahmed"


def test_in_memory_seed_only_adds_missing_keys():
    svc = DynamoDBService()  # mock mode, seeded
    svc.update_doctor_status("DOC-CARDIO-01", False)
    svc.save_user(svc.get_user(DEMO_USER).model_copy(update={"active_case_id": "CASE-1"}))
    svc._seed_mock_data()
    assert svc.get_doctor("DOC-CARDIO-01").is_active is False
    assert svc.get_user(DEMO_USER).active_case_id == "CASE-1"


# ---------------------------------------------------------------- 7. update_case writes only changed fields
def test_update_case_is_a_field_level_update_item(aws):
    svc = _service(aws, seed=False)
    svc.save_case(_case("CASE-U"))
    api, worker = svc, _service(aws, seed=False)  # two tasks, each with its own (stale) view

    puts = []
    real_put = svc.table.put_item
    aws.setattr(svc.table, "put_item", lambda **kw: puts.append(kw) or real_put(**kw))
    api.update_case("CASE-U", {"video_meeting_id": "MEET-1", "video_meeting": {"MediaRegion": "us-east-1", "score": 0.5}})
    updated = worker.update_case("CASE-U", {"status": "RESOLVED", "resolved_at": "2026-10-11T10:00:00+00:00",
                                            "esi_level": "1", "not_a_field": "ignored", "case_id": "CASE-EVIL"})
    assert puts == []  # no full-item overwrite
    assert updated.case_id == "CASE-U" and updated.status == "RESOLVED" and updated.esi_level == 1
    assert updated.video_meeting_id == "MEET-1"  # the other writer's field survived
    raw = svc.table.get_item(Key={"PK": "CASE#CASE-U", "SK": "METADATA"})["Item"]
    assert raw["GSI1PK"] == "STATUS#RESOLVED" and raw["data"]["video_meeting"]["score"] == pytest.approx(0.5)
    assert "not_a_field" not in raw["data"]
    assert float(raw["data"]["confidence_score"]) == pytest.approx(0.93)
    assert svc.get_case("CASE-U") == updated
    assert svc.update_case("CASE-MISSING", {"status": "RESOLVED"}) is None
    assert svc.table.get_item(Key={"PK": "CASE#CASE-MISSING", "SK": "METADATA"}).get("Item") is None
    with pytest.raises(Exception):
        svc.update_case("CASE-U", {"esi_level": "not-a-number"})


# ---------------------------------------------------------------- 4. failed S3 upload
def test_failed_s3_upload_raises(aws):
    s3 = boto3.client("s3", region_name="us-east-1")  # bucket never created: put_object fails
    aws.setattr(audio_service, "s3", s3)
    with pytest.raises(AudioUploadError):
        audio_service.upload_audio_to_s3(b"OggS", "+923001110000")


def test_successful_s3_upload_and_pure_mock_store(aws):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=settings.S3_AUDIO_BUCKET)
    aws.setattr(audio_service, "s3", s3)
    audio = audio_service.upload_audio_to_s3(b"OggS", "+923001110000")
    assert s3.get_object(Bucket=settings.S3_AUDIO_BUCKET, Key=audio.key)["Body"].read() == b"OggS"

    aws.setattr(audio_service, "s3", None)  # pure mock: in-memory store keeps working
    audio = audio_service.upload_audio_to_s3(b"OggS-mem", "+923001110000")
    assert audio_service.get_audio_object(audio.key)[0] == b"OggS-mem"


@pytest.mark.asyncio
async def test_providers_do_not_report_a_failed_upload_as_stored(aws):
    aws.setattr(audio_service, "s3", boto3.client("s3", region_name="us-east-1"))  # no bucket
    with pytest.raises((AudioUploadError, MediaFetchError)):
        await MockProvider().fetch_media_to_s3("mock_media_1", "+923001110000")

    def graph(request: httpx.Request) -> httpx.Response:
        if "lookaside" in str(request.url):
            return httpx.Response(200, content=b"OggS-voice")
        return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/x", "id": "1"})

    meta = MetaCloudProvider("token", "106540352242922", "v20.0", transport=httpx.MockTransport(graph))
    with pytest.raises((AudioUploadError, MediaFetchError)):
        await meta.fetch_media_to_s3("1", "+923001110000")
