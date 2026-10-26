"""Real AWS (MOCK_AWS=False, no AWS_ENDPOINT_URL): a service whose AWS client cannot be set up
must stop the task instead of silently switching to its in-memory / simulated mode."""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import audio_service as audio_mod
from app.services import chime_service as chime_mod
from app.services import dynamo_service as dynamo_mod
from app.services import telephony_service as telephony_mod


@pytest.fixture
def real_aws(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    monkeypatch.delenv("CHIME_MODE", raising=False)


def _boom(*args, **kwargs):
    raise Exception("AccessDenied")


def test_dynamo_fails_fast_when_table_is_unreadable(real_aws, monkeypatch):
    client = SimpleNamespace(describe_table=_boom, list_tables=_boom)
    resource = SimpleNamespace(meta=SimpleNamespace(client=client), Table=lambda name: None)
    monkeypatch.setattr(dynamo_mod.boto3, "resource", lambda *a, **k: resource)
    with pytest.raises(RuntimeError, match="DynamoDB"):
        dynamo_mod.DynamoDBService()


def test_dynamo_checks_the_table_without_listing_or_creating(real_aws, monkeypatch):
    calls = []
    client = SimpleNamespace(describe_table=lambda **k: calls.append(k), list_tables=_boom, create_table=_boom)
    resource = SimpleNamespace(meta=SimpleNamespace(client=client), Table=lambda name: object())
    monkeypatch.setattr(dynamo_mod.boto3, "resource", lambda *a, **k: resource)
    svc = dynamo_mod.DynamoDBService()
    assert svc.mock_mode is False
    assert calls == [{"TableName": svc.table_name}]


def test_audio_fails_fast_when_clients_fail(real_aws, monkeypatch):
    monkeypatch.setattr(audio_mod.boto3, "client", _boom)
    with pytest.raises(RuntimeError, match="AudioService"):
        audio_mod.AudioService()


def test_audio_does_not_list_buckets_in_real_aws(real_aws, monkeypatch):
    s3 = SimpleNamespace(list_buckets=_boom, create_bucket=_boom)
    monkeypatch.setattr(audio_mod.boto3, "client", lambda name, **k: s3 if name == "s3" else SimpleNamespace())
    monkeypatch.setattr(audio_mod.AudioService, "_resolve_language_options", lambda self: ["hi-IN", "en-US"])
    assert audio_mod.AudioService().mock_mode is False


def test_chime_fails_fast_when_client_fails(real_aws, monkeypatch):
    monkeypatch.setattr(chime_mod.boto3, "client", _boom)
    with pytest.raises(RuntimeError, match="ChimeService"):
        chime_mod.ChimeService()


def test_connect_fails_fast_when_client_fails(real_aws, monkeypatch):
    monkeypatch.setattr(telephony_mod.boto3, "client", _boom)
    with pytest.raises(RuntimeError, match="TelephonyService"):
        telephony_mod.TelephonyService()


def test_floci_still_falls_back_to_mock(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", "http://floci:4566")
    monkeypatch.setattr(dynamo_mod.boto3, "resource", _boom)
    assert dynamo_mod.DynamoDBService().mock_mode is True
