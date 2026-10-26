"""Doctor portal invitations (app/services/cognito_service.py): registering a doctor creates their
Cognito user (Doctors group, custom:doctor_id) and Cognito emails the invitation."""
import uuid

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import settings
from app.main import app
from app.routers import doctors as doctors_router
from app.services import cognito_service as cognito
from app.services.dynamo_service import dynamo_service
from test_pipeline_support import new_phone


def registration(email):
    return {"name": "Dr. Invite", "phone_number": new_phone(), "license_no": "PMC-1",
            "specialty": "Cardiology", "bio": "", "email": email}


def unique_email():
    return f"dr.{uuid.uuid4().hex[:8]}@Example.com"


# ---------------------------------------------------------------------------- HTTP (mock mode)
def test_register_requires_an_email_and_refuses_a_registered_one():
    with TestClient(app) as client:
        assert client.post("/api/v1/doctors/register", json=registration(None)).status_code == 422
        assert client.post("/api/v1/doctors/register", json=registration("not-an-email")).status_code == 422
        email = unique_email()
        res = client.post("/api/v1/doctors/register", json=registration(email))
        assert res.status_code == 200
        doc = res.json()
        assert doc["email"] == email.lower() and doc["portal_status"] == cognito.SKIPPED   # no Cognito locally
        assert doc["portal_invited_at"] is None
        again = client.post("/api/v1/doctors/register", json=registration(email.upper()))
        assert again.status_code == 409


class FakeCognito:
    def __init__(self, result=cognito.INVITED, error=None):
        self.result, self.error, self.calls = result, error, []
        self.user_status, self.status_error, self.status_calls = cognito.INVITED, None, []

    def invite_doctor(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result

    def portal_user_status(self, email):
        self.status_calls.append(email)
        if self.status_error:
            raise self.status_error
        return self.user_status


def test_register_invites_and_saves_nothing_when_cognito_fails(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(doctors_router, "cognito_service", fake)
    with TestClient(app) as client:
        doc = client.post("/api/v1/doctors/register", json=registration(unique_email())).json()
        assert doc["portal_status"] == cognito.INVITED and doc["portal_invited_at"]
        assert fake.calls == [(doc["email"], "Dr. Invite", doc["doctor_id"], "Cardiology")]

        fake.error = cognito.PortalUserConflict("taken")
        email = unique_email()
        assert client.post("/api/v1/doctors/register", json=registration(email)).status_code == 409
        fake.error = RuntimeError("Cognito is down")
        res = client.post("/api/v1/doctors/register", json=registration(email))
        assert res.status_code == 502 and "Cognito is down" in res.json()["detail"]
        assert not [d for d in dynamo_service.list_doctors() if d.email == email.lower()]


def test_resend_invitation(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(doctors_router, "cognito_service", fake)
    with TestClient(app) as client:
        doc = client.post("/api/v1/doctors/register", json=registration(unique_email())).json()
        res = client.post(f"/api/v1/doctors/{doc['doctor_id']}/invite")
        assert res.status_code == 200 and res.json()["portal_status"] == cognito.INVITED
        assert res.json()["portal_invited_at"] > doc["portal_invited_at"]
        fake.result = cognito.ACTIVE                          # they already set a password
        res = client.post(f"/api/v1/doctors/{doc['doctor_id']}/invite")
        assert res.json()["portal_status"] == cognito.ACTIVE
        assert dynamo_service.get_doctor(doc["doctor_id"]).portal_status == cognito.ACTIVE
        assert client.post("/api/v1/doctors/DOC-NOPE/invite").status_code == 404
        res = client.post(f"/api/v1/doctors/{doc['doctor_id']}/invite", json={"email": "other@example.com"})
        assert res.status_code == 409                         # an email is never swapped here


def test_roster_shows_portal_access_once_the_password_is_set(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(doctors_router, "cognito_service", fake)
    with TestClient(app) as client:
        doc = client.post("/api/v1/doctors/register", json=registration(unique_email())).json()

        def listed():
            return next(d for d in client.get("/api/v1/doctors").json() if d["doctor_id"] == doc["doctor_id"])

        assert listed()["portal_status"] == cognito.INVITED   # still on the temporary password
        fake.status_error = RuntimeError("Cognito is down")
        assert listed()["portal_status"] == cognito.INVITED   # roster still loads
        fake.status_error, fake.user_status = None, cognito.ACTIVE
        now = listed()
        assert now["portal_status"] == cognito.ACTIVE and now["portal_invited_at"] == doc["portal_invited_at"]
        assert dynamo_service.get_doctor(doc["doctor_id"]).portal_status == cognito.ACTIVE
        fake.status_calls.clear()
        listed()
        assert doc["email"] not in fake.status_calls          # ACTIVE is final: no more lookups


def test_invite_a_doctor_registered_without_an_email(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(doctors_router, "cognito_service", fake)
    with TestClient(app) as client:
        taken = client.post("/api/v1/doctors/register", json=registration(unique_email())).json()["email"]
        old = next(d for d in dynamo_service.list_doctors() if not d.email)   # e.g. demo doctors
        url = f"/api/v1/doctors/{old.doctor_id}/invite"
        assert client.post(url).status_code == 422
        assert client.post(url, json={"email": "nope"}).status_code == 422
        assert client.post(url, json={"email": taken}).status_code == 409
        email = unique_email()
        res = client.post(url, json={"email": email})
        assert res.status_code == 200
        assert res.json()["email"] == email.lower() and res.json()["portal_status"] == cognito.INVITED
        assert fake.calls[-1] == (email.lower(), old.name, old.doctor_id, old.specialty)
        saved = dynamo_service.get_doctor(old.doctor_id)
        assert saved.email == email.lower() and saved.cases_handled == old.cases_handled


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
        monkeypatch.setattr(settings, "MOCK_AWS", False)
        monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
        monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", pool_id)
        svc = cognito.CognitoService()
        assert not svc.mock_mode
        yield idp, pool_id, svc


def attributes(idp, pool_id, email):
    user = idp.admin_get_user(UserPoolId=pool_id, Username=email)
    return user["UserStatus"], {a["Name"]: a["Value"] for a in user["UserAttributes"]}


def groups(idp, pool_id, email):
    return [g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool_id, Username=email)["Groups"]]


def test_invite_creates_the_doctor_user(pool):
    idp, pool_id, svc = pool
    assert svc.invite_doctor("new@example.com", "Dr. New", "DOC-CARD-0001", "Cardiology") == cognito.INVITED
    status, attrs = attributes(idp, pool_id, "new@example.com")
    assert status == "FORCE_CHANGE_PASSWORD"
    assert attrs["custom:doctor_id"] == "DOC-CARD-0001" and attrs["custom:role"] == "doctor"
    assert attrs["custom:specialty"] == "Cardiology" and attrs["name"] == "Dr. New"
    assert attrs["email_verified"] == "true"
    assert groups(idp, pool_id, "new@example.com") == ["Doctors"]
    # Again for the same doctor (resend): still pending, invitation sent again.
    assert svc.invite_doctor("new@example.com", "Dr. New", "DOC-CARD-0001", "Cardiology") == cognito.INVITED


def test_existing_users_are_linked(pool):
    idp, pool_id, svc = pool
    # Created by hand before this existed, and already signed in once.
    idp.admin_create_user(UserPoolId=pool_id, Username="old@example.com", MessageAction="SUPPRESS",
                          UserAttributes=[{"Name": "email", "Value": "old@example.com"}])
    idp.admin_set_user_password(UserPoolId=pool_id, Username="old@example.com", Password="Passw0rd!x", Permanent=True)
    assert svc.invite_doctor("old@example.com", "Dr. Old", "DOC-NEUR-0001", "Neurology") == cognito.ACTIVE
    status, attrs = attributes(idp, pool_id, "old@example.com")
    assert status == "CONFIRMED" and attrs["custom:doctor_id"] == "DOC-NEUR-0001"
    assert groups(idp, pool_id, "old@example.com") == ["Doctors"]


def test_a_user_of_another_registered_doctor_is_refused(pool, monkeypatch):
    idp, pool_id, svc = pool
    svc.invite_doctor("taken@example.com", "Dr. A", "DOC-A", "Cardiology")
    # DOC-A was never saved (failed registration): another registration may take the user over.
    assert svc.invite_doctor("taken@example.com", "Dr. B", "DOC-B", "Cardiology") == cognito.INVITED
    monkeypatch.setattr(cognito.dynamo_service, "get_doctor", lambda doctor_id: object())
    with pytest.raises(cognito.PortalUserConflict):
        svc.invite_doctor("taken@example.com", "Dr. C", "DOC-C", "Cardiology")
    assert attributes(idp, pool_id, "taken@example.com")[1]["custom:doctor_id"] == "DOC-B"


def test_portal_user_status(pool):
    idp, pool_id, svc = pool
    svc.invite_doctor("status@example.com", "Dr. S", "DOC-S", "Cardiology")
    assert svc.portal_user_status("status@example.com") == cognito.INVITED
    idp.admin_set_user_password(UserPoolId=pool_id, Username="status@example.com", Password="Passw0rd!x", Permanent=True)
    assert svc.portal_user_status("status@example.com") == cognito.ACTIVE
    assert svc.portal_user_status("nobody@example.com") is None
    assert cognito.CognitoService().portal_user_status("status@example.com") == cognito.ACTIVE


def test_no_half_created_user_when_the_group_is_missing(pool):
    idp, pool_id, svc = pool
    idp.delete_group(GroupName="Doctors", UserPoolId=pool_id)
    with pytest.raises(Exception):
        svc.invite_doctor("nogroup@example.com", "Dr. X", "DOC-X", "Cardiology")
    with pytest.raises(idp.exceptions.UserNotFoundException):
        idp.admin_get_user(UserPoolId=pool_id, Username="nogroup@example.com")


# ---------------------------------------------------------------------------- DynamoDB branch
def test_portal_fields_in_dynamodb(monkeypatch):
    from app.models.schemas import DoctorProfile
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
        svc.register_doctor(DoctorProfile(doctor_id="DOC-1", name="Dr. One", phone_number="+923001112222",
                                          license_no="L", specialty="Cardiology", bio="", cases_handled=4))
        doc = svc.update_doctor_portal("DOC-1", cognito.INVITED, "2026-10-17T10:00:00+00:00", email="one@example.com")
        assert (doc.email, doc.portal_status, doc.cases_handled) == ("one@example.com", cognito.INVITED, 4)
        doc = svc.update_doctor_portal("DOC-1", cognito.ACTIVE, "2026-10-17T10:00:00+00:00")
        assert doc.email == "one@example.com" and doc.portal_status == cognito.ACTIVE
        svc._mock_doctors.clear()
        assert svc.get_doctor("DOC-1").portal_status == cognito.ACTIVE      # read back from the table
        assert svc.update_doctor_portal("DOC-NOPE", cognito.ACTIVE, None) is None
