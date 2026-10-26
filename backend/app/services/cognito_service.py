"""Portal users for doctors in the Cognito user pool, so nobody sets them up by hand.

Registering a doctor (POST /api/v1/doctors/register) creates their portal user: the email is the
username, `custom:doctor_id` links it to the DOCTOR#<id> record (backend/app/auth.py reads it),
`custom:role` = doctor, `custom:specialty`, and the user joins the Doctors group. Cognito emails
the invitation (ResQ-AI template: `invite_message_template` in terraform/cloudfront_cognito.tf)
with a temporary password; the doctor signs in through the portal's Hosted UI and chooses their
own password. POST /api/v1/doctors/{id}/invite sends it again (temporary passwords expire).

Cognito does not tell the API when a doctor replaces the temporary password, so the roster
(GET /api/v1/doctors) asks `portal_user_status` about doctors still shown as INVITED and saves
ACTIVE once they have.

An email that already has a portal user (e.g. one created by hand before this existed) is linked
instead of refused, unless it belongs to another registered doctor; it gets the invitation again
only if it never set a password.

Administrators (ADMIN-2): adding one in the portal (POST /api/v1/admins) creates their portal user
the same way, in the Administrators group with `custom:role` = admin, and Cognito emails the same
invitation. An existing user (e.g. a doctor who also administers) only joins the group; their
attributes stay. Removing an administrator takes them out of the group; the user stays.

Modes:
  - aws:  real `cognito-idp` calls (MOCK_AWS=False, no AWS_ENDPOINT_URL, COGNITO_USER_POOL_ID set).
  - mock: nothing is created and results say SKIPPED. Used with MOCK_AWS=True, in Floci mode
          (Floci does not emulate Cognito) and without a user pool ID.
"""
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from app.config import settings
from app.services.dynamo_service import dynamo_service

DOCTOR_GROUP = "Doctors"
ADMIN_GROUP = "Administrators"

INVITED = "INVITED"   # invitation email sent; the doctor has not set a password yet
ACTIVE = "ACTIVE"     # the user already had a password: linked, no email
SKIPPED = "SKIPPED"   # no Cognito (mock / Floci / no user pool)

# Cognito status of a user who never replaced the temporary password.
_PENDING_STATUS = "FORCE_CHANGE_PASSWORD"


class PortalUserConflict(Exception):
    """The email is already the portal user of another registered doctor."""


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


def error_message(exc: Exception) -> str:
    """Cognito's own message for a ClientError (e.g. an invalid email), else str(exc)."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Message") or str(exc)
    return str(exc)


class CognitoService:
    def __init__(self):
        self.pool_id = settings.COGNITO_USER_POOL_ID
        if settings.MOCK_AWS:
            self.mock_mode, reason = True, "MOCK_AWS=True"
        elif settings.AWS_ENDPOINT_URL:
            self.mock_mode, reason = True, "Floci does not emulate Cognito"
        elif not self.pool_id:
            self.mock_mode, reason = True, "COGNITO_USER_POOL_ID is not set"
        else:
            self.mock_mode, reason = False, f"user pool {self.pool_id}"
        self.client = None if self.mock_mode else boto3.client("cognito-idp", region_name=settings.COGNITO_REGION)
        print(f"[Cognito] Doctor portal invitations: {'off (' + reason + ')' if self.mock_mode else reason}")

    def invite_doctor(self, email: str, name: str, doctor_id: str, specialty: str) -> str:
        """Creates (or links) the doctor's portal user and emails the invitation when they have no
        password yet. Returns INVITED, ACTIVE or SKIPPED. Raises PortalUserConflict, or the
        Cognito ClientError (e.g. InvalidParameterException for a bad email)."""
        if self.mock_mode:
            return SKIPPED
        attributes = self._doctor_attributes(name, doctor_id, specialty)
        try:
            self.client.admin_create_user(
                UserPoolId=self.pool_id, Username=email, DesiredDeliveryMediums=["EMAIL"],
                UserAttributes=[{"Name": "email", "Value": email},
                                {"Name": "email_verified", "Value": "true"}, *attributes])
        except ClientError as e:
            if _error_code(e) != "UsernameExistsException":
                raise
            return self._link_existing(email, doctor_id, attributes)
        try:
            self._add_to_doctors(email)
        except Exception:
            # Without the group the user can sign in but sees nothing; let the admin retry cleanly.
            self._delete_quietly(email)
            raise
        return INVITED

    def _link_existing(self, email: str, doctor_id: str, attributes: List[Dict[str, str]]) -> str:
        user = self.client.admin_get_user(UserPoolId=self.pool_id, Username=email)
        current = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
        linked_to = current.get("custom:doctor_id")
        # A link to a doctor that was never saved (a failed registration) may be taken over.
        if linked_to and linked_to != doctor_id and dynamo_service.get_doctor(linked_to):
            raise PortalUserConflict(f"{email} is already the portal user of doctor {linked_to}.")
        self.client.admin_update_user_attributes(UserPoolId=self.pool_id, Username=email,
                                                 UserAttributes=attributes)
        self._add_to_doctors(email)
        if user.get("UserStatus") != _PENDING_STATUS:
            return ACTIVE
        # A new temporary password and the invitation email again.
        self.client.admin_create_user(UserPoolId=self.pool_id, Username=email,
                                      MessageAction="RESEND", DesiredDeliveryMediums=["EMAIL"])
        return INVITED

    def invite_admin(self, email: str, name: str) -> str:
        """Creates the administrator's portal user (or adds an existing one to the group) and emails
        the invitation when they have no password yet. Returns INVITED, ACTIVE or SKIPPED. Raises the
        Cognito ClientError (e.g. InvalidParameterException for a bad email)."""
        if self.mock_mode:
            return SKIPPED
        try:
            self.client.admin_create_user(
                UserPoolId=self.pool_id, Username=email, DesiredDeliveryMediums=["EMAIL"],
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                                {"Name": "name", "Value": name}, {"Name": "custom:role", "Value": "admin"}])
        except ClientError as e:
            if _error_code(e) != "UsernameExistsException":
                raise
            user = self.client.admin_get_user(UserPoolId=self.pool_id, Username=email)
            self._add_to_group(email, ADMIN_GROUP)
            if user.get("UserStatus") != _PENDING_STATUS:
                return ACTIVE
            self.client.admin_create_user(UserPoolId=self.pool_id, Username=email,
                                          MessageAction="RESEND", DesiredDeliveryMediums=["EMAIL"])
            return INVITED
        try:
            self._add_to_group(email, ADMIN_GROUP)
        except Exception:
            self._delete_quietly(email)
            raise
        return INVITED

    def update_doctor(self, email: str, name: str, doctor_id: str, specialty: str) -> bool:
        """Keeps the doctor's portal user in step with an administrator's edit (name, specialty).
        False in mock mode or when there is no such user."""
        if self.mock_mode:
            return False
        try:
            self.client.admin_update_user_attributes(UserPoolId=self.pool_id, Username=email,
                                                     UserAttributes=self._doctor_attributes(name, doctor_id, specialty))
            return True
        except ClientError as e:
            if _error_code(e) == "UserNotFoundException":
                return False
            raise

    def remove_doctor(self, email: str) -> bool:
        """An unregistered doctor loses portal access: out of the Doctors group (the user stays: it
        may be an administrator too). False in mock mode or when there is no such user."""
        if self.mock_mode:
            return False
        try:
            self.client.admin_remove_user_from_group(UserPoolId=self.pool_id, Username=email, GroupName=DOCTOR_GROUP)
            return True
        except ClientError as e:
            if _error_code(e) == "UserNotFoundException":
                return False
            raise

    def remove_admin(self, email: str) -> bool:
        """Takes the user out of the Administrators group (the user itself stays: it may be a
        doctor too). False in mock mode or when there is no such user."""
        if self.mock_mode:
            return False
        try:
            self.client.admin_remove_user_from_group(UserPoolId=self.pool_id, Username=email, GroupName=ADMIN_GROUP)
            return True
        except ClientError as e:
            if _error_code(e) == "UserNotFoundException":
                return False
            raise

    def portal_user_status(self, email: str) -> Optional[str]:
        """INVITED while the user still has the temporary password, ACTIVE once they set their own,
        None if there is no such user or no Cognito (mock mode). Raises other Cognito errors."""
        if self.mock_mode:
            return None
        try:
            user = self.client.admin_get_user(UserPoolId=self.pool_id, Username=email)
        except ClientError as e:
            if _error_code(e) == "UserNotFoundException":
                return None
            raise
        return INVITED if user.get("UserStatus") == _PENDING_STATUS else ACTIVE

    @staticmethod
    def _doctor_attributes(name: str, doctor_id: str, specialty: str) -> List[Dict[str, str]]:
        return [{"Name": "name", "Value": name},
                {"Name": "custom:doctor_id", "Value": doctor_id},
                {"Name": "custom:role", "Value": "doctor"},
                {"Name": "custom:specialty", "Value": specialty}]

    def _add_to_doctors(self, email: str) -> None:
        self._add_to_group(email, DOCTOR_GROUP)

    def _add_to_group(self, email: str, group: str) -> None:
        self.client.admin_add_user_to_group(UserPoolId=self.pool_id, Username=email, GroupName=group)

    def _delete_quietly(self, email: str) -> None:
        try:
            self.client.admin_delete_user(UserPoolId=self.pool_id, Username=email)
        except Exception as e:
            print(f"[Cognito] Could not remove the half-created portal user {email}: {e}")


cognito_service = CognitoService()
