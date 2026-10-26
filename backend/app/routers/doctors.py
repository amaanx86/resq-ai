import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel
from app import doctor_offers, doctor_verification
from app.auth import get_principal
from app.models.schemas import DoctorProfile, DoctorRegistration, DoctorUpdate
from app.routers.admins import _checked_phone
from app.services import cognito_service as cognito
from app.services.cognito_service import cognito_service
from app.services.dynamo_service import OPEN_CASE_STATUSES, dynamo_service, phone_key

router = APIRouter(prefix="/api/v1/doctors", tags=["Doctors"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _invite(email: str, name: str, doctor_id: str, specialty: str) -> str:
    """Creates or links the doctor's Cognito portal user (app/services/cognito_service.py)."""
    try:
        return cognito_service.invite_doctor(email, name, doctor_id, specialty)
    except cognito.PortalUserConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        print(f"[Doctors] Portal user for {email} failed: {e}")
        raise HTTPException(status_code=502,
                            detail=f"Could not create the doctor's portal sign-in: {cognito.error_message(e)}")


def _invited_at(status: str) -> Optional[str]:
    return datetime.now(timezone.utc).isoformat() if status == cognito.INVITED else None


def _checked_email(raw: Optional[str]) -> str:
    """Normalised email; 422 if invalid, 409 if another doctor already has it."""
    email = (raw or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="A valid email address is required for the portal invitation")
    if any((d.email or "").lower() == email for d in dynamo_service.list_doctors()):
        raise HTTPException(status_code=409, detail=f"A doctor with the email {email} is already registered")
    return email


class PortalInvite(BaseModel):
    # Only for a doctor registered without an email; it becomes their sign-in.
    email: Optional[str] = None


def _with_current_portal_status(doctor: DoctorProfile) -> DoctorProfile:
    """An invited doctor who has since set their password is saved as ACTIVE. Cognito does not
    report that moment, so it is checked here, once per roster load while they are INVITED."""
    if doctor.portal_status != cognito.INVITED or not doctor.email:
        return doctor
    try:
        status = cognito_service.portal_user_status(doctor.email)
    except Exception as e:
        print(f"[Doctors] Portal status of {doctor.email} unknown: {cognito.error_message(e)}")
        return doctor
    if status != cognito.ACTIVE:
        return doctor
    return dynamo_service.update_doctor_portal(doctor.doctor_id, cognito.ACTIVE, doctor.portal_invited_at) or doctor


@router.get("", response_model=List[DoctorProfile])
def list_doctors():
    """Lists all registered doctors and their availability"""
    return [_with_current_portal_status(d) for d in dynamo_service.list_doctors()]

@router.get("/{doctor_id}", response_model=DoctorProfile)
def get_doctor(doctor_id: str):
    """One doctor's profile. Doctors may only read their own (the portal's availability switch)."""
    doctor = dynamo_service.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    return doctor

@router.post("/register", response_model=DoctorProfile)
def register_doctor(req: DoctorRegistration, background_tasks: BackgroundTasks, request: Request):
    """Registers a new doctor via the Web Portal and invites them to it: their Cognito user
    (Doctors group, custom:doctor_id) is created and Cognito emails the invitation. The doctor then
    confirms their WhatsApp number (DOC-8, after the response); until they do they get no offers.
    Without confirmation (DOCTOR_NUMBER_CONFIRMATION_ENABLED=False) an on-duty doctor is offered a
    case that is waiting for a doctor (OFFER-5) right away."""
    email = _checked_email(req.email)
    doctor_id = f"DOC-{req.specialty[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
    # Portal user first: a Cognito failure leaves no doctor without a way to sign in.
    portal_status = _invite(email, req.name, doctor_id, req.specialty)
    new_doc = DoctorProfile(
        doctor_id=doctor_id,
        name=req.name,
        phone_number=req.phone_number,
        license_no=req.license_no,
        specialty=req.specialty,
        bio=req.bio,
        is_active=req.is_active,
        email=email,
        cases_handled=0,
        portal_status=portal_status,
        portal_invited_at=_invited_at(portal_status),
        phone_status=doctor_verification.PENDING if doctor_verification.enabled() else None,
    )
    saved = dynamo_service.register_doctor(new_doc)
    if doctor_verification.enabled():
        background_tasks.add_task(doctor_verification.send_confirmation, saved, get_principal(request).email)
    elif saved.is_active:
        background_tasks.add_task(doctor_offers.doctor_came_on_duty, saved.doctor_id)
    return saved


def _open_case_of(doctor_id: str):
    """The open case this doctor is assigned to, or None (one case per doctor)."""
    return next((c for c in dynamo_service.list_active_cases()
                 if c.assigned_doctor_id == doctor_id and c.status in OPEN_CASE_STATUSES), None)


@router.patch("/{doctor_id}", response_model=DoctorProfile)
async def update_doctor(doctor_id: str, req: DoctorUpdate, background_tasks: BackgroundTasks, request: Request):
    """Administrators edit a doctor: name, phone, licence, specialty, bio (the email, their sign-in,
    stays). A new number has to be confirmed again (DOC-8), so it cannot change while the doctor
    holds a case (409). 409 also when another doctor has that number."""
    doctor = dynamo_service.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    fields = {}
    for name in ("name", "license_no", "specialty"):
        value = getattr(req, name)
        if value is not None:
            if not value.strip():
                raise HTTPException(status_code=422, detail=f"{name.replace('_', ' ').capitalize()} cannot be empty")
            fields[name] = value.strip()
    if req.bio is not None:
        fields["bio"] = req.bio.strip()
    new_phone = None
    if req.phone_number is not None and phone_key(req.phone_number) != phone_key(doctor.phone_number):
        new_phone = _checked_phone(req.phone_number)
        if phone_key(new_phone) == phone_key(doctor.phone_number):
            new_phone = None
        else:
            other = dynamo_service.get_doctor_by_phone(new_phone)
            if other and other.doctor_id != doctor_id:
                raise HTTPException(status_code=409, detail=f"{other.name} is registered with {new_phone}")
            held = _open_case_of(doctor_id)
            if held:
                raise HTTPException(status_code=409, detail=(
                    f"{doctor.name} is handling case {held.case_id} ({held.patient_name}); change the number "
                    "after the case is closed or handed over"))
            fields["phone_number"] = new_phone
    updated = dynamo_service.update_doctor_profile(doctor_id, fields) if fields else doctor
    if updated is None:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if new_phone and doctor_verification.enabled():
        updated = dynamo_service.update_doctor_fields(doctor_id, {"phone_status": doctor_verification.PENDING,
                                                                  "phone_confirmed_at": None}) or updated
        background_tasks.add_task(doctor_verification.send_confirmation, updated, get_principal(request).email)
    if updated.email and ({"name", "specialty"} & fields.keys()):
        try:
            cognito_service.update_doctor(updated.email, updated.name, doctor_id, updated.specialty)
        except Exception as e:
            print(f"[Doctors] Portal user of {updated.email} not updated: {cognito.error_message(e)}")
    print(f"[Doctors] {get_principal(request).email or 'local'} edited {doctor_id}: {sorted(fields)}.")
    return updated


@router.delete("/{doctor_id}")
async def unregister_doctor(doctor_id: str, request: Request):
    """Administrators unregister a doctor: no more offers, the WhatsApp number is a stranger's again,
    and the portal user leaves the Doctors group. 409 while the doctor holds an open case (assign it
    to another doctor or close it first); a pending offer is declined for them. Their past cases
    keep their name."""
    doctor = dynamo_service.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    held = _open_case_of(doctor_id)
    if held:
        raise HTTPException(status_code=409, detail=(
            f"{doctor.name} is handling case {held.case_id} ({held.patient_name}); assign it to another doctor "
            "or close it first"))
    for case in dynamo_service.list_active_cases():
        if (case.doctor_offer_status == "PENDING" and doctor_id in (case.offered_doctor_ids or [])
                and doctor_id not in (case.declined_doctor_ids or [])):
            await doctor_offers.decline_offer(case.case_id, case.offer_id, doctor)
    if doctor.email:
        try:
            cognito_service.remove_doctor(doctor.email)
        except Exception as e:
            print(f"[Doctors] Removing {doctor.email} from the Doctors group failed: {e}")
            raise HTTPException(status_code=502, detail=f"Could not remove the portal access: {cognito.error_message(e)}")
    dynamo_service.delete_doctor(doctor_id)
    print(f"[Doctors] {get_principal(request).email or 'local'} unregistered {doctor_id} ({doctor.name}).")
    return {"doctor_id": doctor_id, "removed": True}


@router.post("/{doctor_id}/confirm-number", response_model=DoctorProfile)
async def resend_number_confirmation(doctor_id: str, request: Request):
    """Administrators ask the doctor again to confirm their WhatsApp number (DOC-8), e.g. once the
    template is approved or after a "not me" answer was sorted out. 409 if it is confirmed."""
    doctor = dynamo_service.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if doctor_verification.number_confirmed(doctor):
        raise HTTPException(status_code=409, detail=f"{doctor.name}'s number is already confirmed")
    updated = await doctor_verification.start(doctor, get_principal(request).email)
    return updated or doctor

@router.post("/{doctor_id}/invite", response_model=DoctorProfile)
def invite_to_portal(doctor_id: str, req: Optional[PortalInvite] = None):
    """Sends the portal invitation again (temporary passwords expire), or a first one to a doctor
    registered without an email (`email` in the body). A doctor who already set a password gets
    no email: the result says portal_status ACTIVE."""
    doctor = dynamo_service.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    new_email = None
    if not doctor.email:
        if not (req and req.email):
            raise HTTPException(status_code=422, detail="This doctor has no email address yet: send one")
        new_email = _checked_email(req.email)
    elif req and req.email and req.email.strip().lower() != doctor.email.lower():
        raise HTTPException(status_code=409, detail=f"This doctor already signs in as {doctor.email}")
    email = new_email or doctor.email
    portal_status = _invite(email, doctor.name, doctor.doctor_id, doctor.specialty)
    invited_at = _invited_at(portal_status) or doctor.portal_invited_at
    return dynamo_service.update_doctor_portal(doctor_id, portal_status, invited_at, email=new_email) or doctor

@router.post("/{doctor_id}/status", response_model=DoctorProfile)
async def toggle_doctor_availability(doctor_id: str, is_active: bool):
    """Toggles doctor on-duty availability status. A doctor coming on duty is offered a case that
    is waiting for a doctor (OFFER-5) right away."""
    updated = dynamo_service.update_doctor_status(doctor_id, is_active)
    if not updated:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if is_active:
        await doctor_offers.doctor_came_on_duty(doctor_id)
    return updated

@router.get("/specialty/{specialty}", response_model=List[DoctorProfile])
def get_by_specialty(specialty: str):
    """Retrieves active on-duty specialists"""
    return dynamo_service.get_available_doctors_by_specialty(specialty)
