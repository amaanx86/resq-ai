"""Administrators added in the portal (ADMIN-2). Administrators only (app/auth.py).

Adding one creates their Cognito portal user in the Administrators group and Cognito emails the
invitation (app/services/cognito_service.py), like a doctor's. Their phone number gets the no-doctor
alert calls for ESI 1-2 cases (app/admin_alerts.py) while `receive_alert_calls` is on, together with
the numbers in ADMIN_ALERT_PHONE_NUMBERS. Administrators created by hand in Cognito still work; they
just have no phone on file until someone adds them here.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import phonenumbers
from fastapi import APIRouter, HTTPException, Request

from app.auth import get_principal
from app.models.schemas import AdminProfile, AdminRegistration, AdminUpdate
from app.services import cognito_service as cognito
from app.services.cognito_service import cognito_service
from app.services.dynamo_service import dynamo_service

router = APIRouter(prefix="/api/v1/admins", tags=["Administrators"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _checked_phone(raw: Optional[str]) -> str:
    """E.164; local numbers are read as Pakistani ("0300 1234567"). 422 if it is not a valid number."""
    try:
        number = phonenumbers.parse((raw or "").strip(), "PK")
    except phonenumbers.NumberParseException:
        number = None
    if number is None or not phonenumbers.is_valid_number(number):
        raise HTTPException(status_code=422, detail="A valid phone number is required (e.g. +923001234567); "
                                                    "it gets the no-doctor alert calls")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _checked_name(raw: Optional[str]) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="A name is required")
    return name[:120]


def _checked_email(raw: Optional[str]) -> str:
    """Normalised email; 422 if invalid, 409 if an administrator already has it."""
    email = (raw or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="A valid email address is required for the portal invitation")
    if any(a.email.lower() == email for a in dynamo_service.list_admins()):
        raise HTTPException(status_code=409, detail=f"An administrator with the email {email} already exists")
    return email


def _invite(email: str, name: str) -> str:
    try:
        return cognito_service.invite_admin(email, name)
    except Exception as e:
        print(f"[Admins] Portal user for {email} failed: {e}")
        raise HTTPException(status_code=502,
                            detail=f"Could not create the administrator's portal sign-in: {cognito.error_message(e)}")


def _invited_at(status: str) -> Optional[str]:
    return datetime.now(timezone.utc).isoformat() if status == cognito.INVITED else None


def _get(admin_id: str) -> AdminProfile:
    admin = dynamo_service.get_admin(admin_id)
    if not admin:
        raise HTTPException(status_code=404, detail="Administrator not found")
    return admin


def _with_current_portal_status(admin: AdminProfile) -> AdminProfile:
    """An invited administrator who has since set their password is saved as ACTIVE (as for doctors)."""
    if admin.portal_status != cognito.INVITED:
        return admin
    try:
        status = cognito_service.portal_user_status(admin.email)
    except Exception as e:
        print(f"[Admins] Portal status of {admin.email} unknown: {cognito.error_message(e)}")
        return admin
    if status != cognito.ACTIVE:
        return admin
    return dynamo_service.update_admin_fields(admin.admin_id, {"portal_status": cognito.ACTIVE}) or admin


@router.get("", response_model=List[AdminProfile])
def list_admins():
    """The administrators added in the portal, oldest first."""
    return [_with_current_portal_status(a) for a in dynamo_service.list_admins()]


@router.post("", response_model=AdminProfile)
def add_admin(req: AdminRegistration, request: Request):
    """Adds an administrator: Cognito user in the Administrators group + invitation email, and their
    phone for the no-doctor alert calls. 422 for a bad email or phone, 409 for a known email, 502 if
    Cognito refuses (nothing is saved then)."""
    name, phone, email = _checked_name(req.name), _checked_phone(req.phone_number), _checked_email(req.email)
    portal_status = _invite(email, name)   # portal user first, like a doctor's registration
    admin = AdminProfile(
        admin_id=f"ADM-{uuid.uuid4().hex[:6].upper()}", name=name, email=email, phone_number=phone,
        receive_alert_calls=req.receive_alert_calls, added_by=get_principal(request).email,
        portal_status=portal_status, portal_invited_at=_invited_at(portal_status))
    saved = dynamo_service.save_admin(admin)
    print(f"[Admins] {saved.added_by} added administrator {saved.email} ({saved.admin_id}).")
    return saved


@router.patch("/{admin_id}", response_model=AdminProfile)
def update_admin(admin_id: str, req: AdminUpdate):
    """Changes an administrator's name, phone or whether they get the alert calls."""
    _get(admin_id)
    fields = {}
    if req.name is not None:
        fields["name"] = _checked_name(req.name)
    if req.phone_number is not None:
        fields["phone_number"] = _checked_phone(req.phone_number)
    if req.receive_alert_calls is not None:
        fields["receive_alert_calls"] = req.receive_alert_calls
    updated = dynamo_service.update_admin_fields(admin_id, fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Administrator not found")
    return updated


@router.post("/{admin_id}/invite", response_model=AdminProfile)
def resend_invitation(admin_id: str):
    """Emails the portal invitation again (temporary passwords expire). ACTIVE: they already set a
    password, no email was sent."""
    admin = _get(admin_id)
    portal_status = _invite(admin.email, admin.name)
    fields = {"portal_status": portal_status, "portal_invited_at": _invited_at(portal_status) or admin.portal_invited_at}
    return dynamo_service.update_admin_fields(admin_id, fields) or admin


@router.delete("/{admin_id}")
def remove_admin(admin_id: str, request: Request):
    """Removes an administrator: out of the Administrators group in Cognito, no more alert calls.
    409 for yourself (another administrator has to do it)."""
    admin = _get(admin_id)
    me = (get_principal(request).email or "").lower()
    if me and me == admin.email.lower():
        raise HTTPException(status_code=409, detail="You cannot remove yourself; ask another administrator")
    try:
        cognito_service.remove_admin(admin.email)
    except Exception as e:
        print(f"[Admins] Removing {admin.email} from the Administrators group failed: {e}")
        raise HTTPException(status_code=502,
                            detail=f"Could not remove the portal access: {cognito.error_message(e)}")
    dynamo_service.delete_admin(admin_id)
    print(f"[Admins] {me or 'local'} removed administrator {admin.email} ({admin_id}).")
    return {"admin_id": admin_id, "removed": True}
