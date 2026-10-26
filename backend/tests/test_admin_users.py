"""ADMIN-2: administrators added in the portal (app/routers/admins.py), each with a phone number that
gets the no-doctor alert calls (app/admin_alerts.py), and their Cognito portal user
(app/services/cognito_service.py: Administrators group, invitation email)."""
import uuid

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app import admin_alerts
from app.config import settings
from app.main import app
from app.models.schemas import AdminProfile
from app.routers import admins as admins_router
from app.services import cognito_service as cognito
from app.services import telephony_service as telephony_module
from app.services.dynamo_service import dynamo_service
from test_admin_alerts import FakePhone, run_call, waiting_case
from test_auth_portal import auth_on, bearer, make_token  # noqa: F401  (auth_on is a fixture)
from test_doctor_offers import Offers

URL = "/api/v1/admins"


def unique_email():
    return f"admin.{uuid.uuid4().hex[:8]}@Example.com"


def body(email=None, phone="+923001110001", **extra):
    return {"name": "Ayesha Admin", "email": email or unique_email(), "phone_number": phone, **extra}


class FakeCognito:
    def __init__(self, result=cognito.INVITED):
        self.result, self.error, self.remove_error = result, None, None
        self.invites, self.removed, self.status_calls = [], [], []
        self.user_status = cognito.INVITED

    def invite_admin(self, email, name):
        self.invites.append((email, name))
        if self.error:
            raise self.error
        return self.result

    def remove_admin(self, email):
        self.removed.append(email)
        if self.remove_error:
            raise self.remove_error
        return True

    def portal_user_status(self, email):
        self.status_calls.append(email)
        return self.user_status


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake(monkeypatch):
    f = FakeCognito()
    monkeypatch.setattr(admins_router, "cognito_service", f)
    return f


# ---------------------------------------------------------------------------- adding
def test_add_an_admin_in_mock_mode(client):
    res = client.post(URL, json=body("New.Admin@Example.com", phone="0300 111 0001"))
    assert res.status_code == 200
    admin = res.json()
    assert admin["admin_id"].startswith("ADM-")
    assert admin["email"] == "new.admin@example.com"
    assert admin["phone_number"] == "+923001110001"                       # local number → E.164
    assert admin["receive_alert_calls"] is True
    assert admin["portal_status"] == cognito.SKIPPED and admin["portal_invited_at"] is None
    assert admin["added_by"] == "local@resq.dev"                         # the caller (local mode)
    assert [a["admin_id"] for a in client.get(URL).json()] == [admin["admin_id"]]
    assert dynamo_service.get_admin(admin["admin_id"]).phone_number == "+923001110001"


@pytest.mark.parametrize("override,status", [
    ({"email": "not-an-email"}, 422),
    ({"email": ""}, 422),
    ({"phone_number": "12"}, 422),
    ({"phone_number": "call me"}, 422),
    ({"phone_number": ""}, 422),
    ({"name": "   "}, 422),
])
def test_add_refuses_bad_input(client, override, status):
    res = client.post(URL, json={**body(), **override})
    assert res.status_code == status
    assert client.get(URL).json() == []


def test_add_refuses_a_known_email(client):
    email = unique_email()
    assert client.post(URL, json=body(email)).status_code == 200
    again = client.post(URL, json=body(email.upper(), phone="+923001110002"))
    assert again.status_code == 409
    assert len(client.get(URL).json()) == 1


def test_add_invites_through_cognito(client, fake):
    admin = client.post(URL, json=body("boss@example.com")).json()
    assert fake.invites == [("boss@example.com", "Ayesha Admin")]
    assert admin["portal_status"] == cognito.INVITED and admin["portal_invited_at"]


def test_nothing_is_saved_when_cognito_fails(client, fake):
    fake.error = RuntimeError("Cognito is down")
    res = client.post(URL, json=body())
    assert res.status_code == 502 and "Cognito is down" in res.json()["detail"]
    assert dynamo_service.list_admins() == []


def test_an_admin_can_opt_out_of_calls_from_the_start(client):
    admin = client.post(URL, json=body(receive_alert_calls=False)).json()
    assert admin["receive_alert_calls"] is False
    assert admin_alerts.admin_numbers() == []


# ---------------------------------------------------------------------------- list, edit, invite, remove
def test_list_is_oldest_first_and_shows_portal_access_once_the_password_is_set(client, fake):
    first = client.post(URL, json=body()).json()
    second = client.post(URL, json=body(phone="+923001110002")).json()
    assert [a["admin_id"] for a in client.get(URL).json()] == [first["admin_id"], second["admin_id"]]
    fake.user_status = cognito.ACTIVE
    listed = client.get(URL).json()
    assert {a["portal_status"] for a in listed} == {cognito.ACTIVE}
    assert dynamo_service.get_admin(first["admin_id"]).portal_status == cognito.ACTIVE
    fake.status_calls.clear()
    client.get(URL)
    assert fake.status_calls == []                                      # ACTIVE is final


def test_edit_phone_name_and_calls(client):
    admin = client.post(URL, json=body()).json()
    url = f"{URL}/{admin['admin_id']}"
    res = client.patch(url, json={"phone_number": "+92 333 1234567", "name": "Dr. Ops"})
    assert res.status_code == 200
    assert res.json()["phone_number"] == "+923331234567" and res.json()["name"] == "Dr. Ops"
    assert res.json()["email"] == admin["email"]                         # never changed here
    assert client.patch(url, json={"receive_alert_calls": False}).json()["receive_alert_calls"] is False
    assert client.patch(url, json={"phone_number": "123"}).status_code == 422
    assert client.patch(url, json={"name": ""}).status_code == 422
    assert client.patch(url, json={}).status_code == 200                # nothing to change
    assert client.patch(f"{URL}/ADM-NOPE", json={"name": "X"}).status_code == 404
    assert dynamo_service.get_admin(admin["admin_id"]).phone_number == "+923331234567"


def test_resend_invitation(client, fake):
    admin = client.post(URL, json=body()).json()
    res = client.post(f"{URL}/{admin['admin_id']}/invite")
    assert res.status_code == 200 and res.json()["portal_status"] == cognito.INVITED
    resent_at = res.json()["portal_invited_at"]
    assert resent_at >= admin["portal_invited_at"]
    fake.result = cognito.ACTIVE                                        # they already set a password
    res = client.post(f"{URL}/{admin['admin_id']}/invite")
    assert res.json()["portal_status"] == cognito.ACTIVE
    assert res.json()["portal_invited_at"] == resent_at                 # no new email, date kept
    assert client.post(f"{URL}/ADM-NOPE/invite").status_code == 404
    fake.error = RuntimeError("throttled")
    assert client.post(f"{URL}/{admin['admin_id']}/invite").status_code == 502


def test_remove_an_admin(client, fake):
    admin = client.post(URL, json=body("leaver@example.com")).json()
    res = client.delete(f"{URL}/{admin['admin_id']}")
    assert res.status_code == 200 and res.json() == {"admin_id": admin["admin_id"], "removed": True}
    assert fake.removed == ["leaver@example.com"]
    assert client.get(URL).json() == [] and dynamo_service.get_admin(admin["admin_id"]) is None
    assert client.delete(f"{URL}/{admin['admin_id']}").status_code == 404


def test_removal_keeps_the_admin_when_cognito_fails(client, fake):
    admin = client.post(URL, json=body()).json()
    fake.remove_error = RuntimeError("Cognito is down")
    assert client.delete(f"{URL}/{admin['admin_id']}").status_code == 502
    assert dynamo_service.get_admin(admin["admin_id"]) is not None


def test_an_admin_cannot_remove_themself(client):
    me = client.post(URL, json=body("LOCAL@resq.dev")).json()           # the local-mode caller
    res = client.delete(f"{URL}/{me['admin_id']}")
    assert res.status_code == 409 and "yourself" in res.json()["detail"]
    assert dynamo_service.get_admin(me["admin_id"]) is not None


# ---------------------------------------------------------------------------- access rules
@pytest.mark.parametrize("method,path,payload", [
    ("get", URL, None),
    ("post", URL, body("x@example.com")),
    ("patch", f"{URL}/ADM-1", {"name": "X"}),
    ("post", f"{URL}/ADM-1/invite", None),
    ("delete", f"{URL}/ADM-1", None),
])
def test_doctors_cannot_manage_admins(auth_on, client, method, path, payload):  # noqa: F811
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    res = getattr(client, method)(path, headers=doctor, **({"json": payload} if payload else {}))
    assert res.status_code == 403
    assert client.get(URL).status_code == 401                           # no token at all


def test_admins_can_manage_admins_with_auth_on(auth_on, client):  # noqa: F811
    admin_headers = bearer(make_token())
    res = client.post(URL, json=body(), headers=admin_headers)
    assert res.status_code == 200
    assert client.get(URL, headers=admin_headers).status_code == 200


# ---------------------------------------------------------------------------- alert calls
def add(name, phone, calls=True, **kw):
    return dynamo_service.save_admin(AdminProfile(admin_id=f"ADM-{uuid.uuid4().hex[:6]}", name=name,
                                                  email=f"{uuid.uuid4().hex[:6]}@example.com",
                                                  phone_number=phone, receive_alert_calls=calls, **kw))


def test_admin_numbers_merge_portal_admins_and_the_setting(monkeypatch):
    add("A", "+923001110001")
    add("B", "+92 300 1110002")
    add("Off", "+923001110009", calls=False)                            # opted out
    add("Dup", "+923001110001")                                         # same phone twice
    monkeypatch.setattr(settings, "ADMIN_ALERT_PHONE_NUMBERS", "+923001110002, +923001110003")
    assert admin_alerts.admin_numbers() == ["+923001110001", "+923001110002", "+923001110003"]


def test_admin_numbers_survive_a_storage_error(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_ALERT_PHONE_NUMBERS", "+923001110003")

    def boom():
        raise RuntimeError("DynamoDB is down")
    monkeypatch.setattr(dynamo_service, "list_admins", boom)
    assert admin_alerts.admin_numbers() == ["+923001110003"]


@pytest.mark.asyncio
async def test_the_no_doctor_call_rings_the_portal_admins(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_ALERT_PHONE_NUMBERS", "")
    phone = FakePhone()
    monkeypatch.setattr(telephony_module, "telephony_service", phone)
    offers = Offers(monkeypatch)
    add("On call", "+923001110001")
    add("Off", "+923001110009", calls=False)
    case = await waiting_case(offers, esi=1)
    tasks = [t for t in offers.tasks if t[0] == admin_alerts.ADMIN_CALL_TASK]
    assert len(tasks) == 1
    result = await run_call(tasks[0][1])
    assert result["outcome"] == "called"
    assert [c[0] for c in phone.calls] == ["+923001110001"]
    assert "ESI level 1" in phone.calls[0][1]


@pytest.mark.asyncio
async def test_without_any_admin_phone_only_the_portal_alert_runs(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_ALERT_PHONE_NUMBERS", "")
    offers = Offers(monkeypatch)
    add("Off", "+923001110009", calls=False)
    await waiting_case(offers, esi=1)
    assert [t for t in offers.tasks if t[0] == admin_alerts.ADMIN_CALL_TASK] == []


# ---------------------------------------------------------------------------- DynamoDB branch
def test_admins_in_dynamodb(monkeypatch):
    from app.services.dynamo_service import DynamoDBService, create_resq_table
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", False)
    with mock_aws():
        create_resq_table(boto3.client("dynamodb", region_name=settings.AWS_REGION), settings.DYNAMODB_TABLE_NAME)
        svc = DynamoDBService()
        assert not svc.mock_mode
        a = svc.save_admin(AdminProfile(admin_id="ADM-1", name="One", email="one@example.com",
                                        phone_number="+923001110001", registered_at="2026-10-21T10:00:00+00:00"))
        svc.save_admin(AdminProfile(admin_id="ADM-0", name="Zero", email="zero@example.com",
                                    phone_number="+923001110000", registered_at="2026-10-20T10:00:00+00:00"))
        svc._mock_admins.clear()                                        # read back from the table
        assert [x.admin_id for x in svc.list_admins()] == ["ADM-0", "ADM-1"]
        assert svc.get_admin("ADM-1") == a
        updated = svc.update_admin_fields("ADM-1", {"phone_number": "+923331234567", "receive_alert_calls": False,
                                                    "admin_id": "HACK", "unknown": 1})
        assert updated.admin_id == "ADM-1" and updated.phone_number == "+923331234567"
        assert updated.receive_alert_calls is False and updated.email == "one@example.com"
        assert svc.update_admin_fields("ADM-NOPE", {"name": "X"}) is None
        assert svc.delete_admin("ADM-1") is True and svc.get_admin("ADM-1") is None
        assert svc.delete_admin("ADM-1") is False
        assert [x.admin_id for x in svc.list_admins()] == ["ADM-0"]


# ---------------------------------------------------------------------------- Cognito (moto)
@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        idp = boto3.client("cognito-idp", region_name=settings.COGNITO_REGION)
        pool_id = idp.create_user_pool(
            PoolName="resq-test", UsernameAttributes=["email"],
            Schema=[{"Name": n, "AttributeDataType": "String", "Mutable": True}
                    for n in ("doctor_id", "role", "specialty")])["UserPool"]["Id"]
        idp.create_group(GroupName="Doctors", UserPoolId=pool_id)
        idp.create_group(GroupName="Administrators", UserPoolId=pool_id)
        monkeypatch.setattr(settings, "MOCK_AWS", False)
        monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
        monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", pool_id)
        svc = cognito.CognitoService()
        assert not svc.mock_mode
        yield idp, pool_id, svc


def user(idp, pool_id, email):
    u = idp.admin_get_user(UserPoolId=pool_id, Username=email)
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool_id, Username=email)["Groups"]]
    return u["UserStatus"], {a["Name"]: a["Value"] for a in u["UserAttributes"]}, sorted(groups)


def test_invite_creates_the_admin_user(pool):
    idp, pool_id, svc = pool
    assert svc.invite_admin("boss@example.com", "The Boss") == cognito.INVITED
    status, attrs, groups = user(idp, pool_id, "boss@example.com")
    assert status == "FORCE_CHANGE_PASSWORD" and groups == ["Administrators"]
    assert attrs["custom:role"] == "admin" and attrs["name"] == "The Boss" and attrs["email_verified"] == "true"
    assert svc.invite_admin("boss@example.com", "The Boss") == cognito.INVITED      # resend while pending


def test_a_doctor_who_administers_keeps_their_doctor_user(pool):
    idp, pool_id, svc = pool
    svc.invite_doctor("doc@example.com", "Dr. Doc", "DOC-1", "Cardiology")
    idp.admin_set_user_password(UserPoolId=pool_id, Username="doc@example.com", Password="Passw0rd!x", Permanent=True)
    assert svc.invite_admin("doc@example.com", "Dr. Doc") == cognito.ACTIVE          # no email: has a password
    status, attrs, groups = user(idp, pool_id, "doc@example.com")
    assert groups == ["Administrators", "Doctors"]
    assert attrs["custom:doctor_id"] == "DOC-1" and attrs["custom:role"] == "doctor"
    assert svc.remove_admin("doc@example.com") is True
    assert user(idp, pool_id, "doc@example.com")[2] == ["Doctors"]                   # still a doctor


def test_remove_an_unknown_user(pool):
    _, _, svc = pool
    assert svc.remove_admin("nobody@example.com") is False


def test_mock_cognito_skips(monkeypatch):
    svc = cognito.CognitoService()                                      # MOCK_AWS=True in tests
    assert svc.invite_admin("a@example.com", "A") == cognito.SKIPPED
    assert svc.remove_admin("a@example.com") is False
