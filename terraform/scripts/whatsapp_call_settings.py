"""Applies the WhatsApp calling settings of the linked business phone number in End User Messaging
Social (run by the `terraform_data.whatsapp_call_settings` local-exec in whatsapp_calling.tf; the AWS
provider has no resource for them).

Environment: PHONE_NUMBER_ID (phone-number-id-...), AWS_REGION, CALL_SETTINGS (JSON in the
UpdateLinkedWhatsAppBusinessAccountPhoneNumber `callSettings` shape). Uses boto3 rather than the AWS
CLI: the call settings API needs botocore >= 1.43.111, newer than many installed CLIs. Credentials as
for terraform (AWS_PROFILE); for `aws login` profiles without botocore[crt] they come from
`aws configure export-credentials`.

Prints the current settings, then updates them if they differ. On failure it prints the error EUM and
Meta returned (the console only shows a generic "couldn't update your call settings").
DRY_RUN=1 only reports what it would do.
"""
import json
import os
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError, MissingDependencyException


def session() -> boto3.Session:
    """Default credential chain; `aws login` sessions need botocore[crt], so fall back to the CLI's."""
    try:
        boto3.Session().get_credentials()
        return boto3.Session()
    except MissingDependencyException:
        res = subprocess.run(["aws", "configure", "export-credentials", "--format", "process"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            sys.exit(f"aws configure export-credentials failed: {res.stderr.strip()}")
        creds = json.loads(res.stdout)
        return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                             aws_session_token=creds.get("SessionToken"))


def main() -> None:
    phone_number_id = os.environ["PHONE_NUMBER_ID"]
    wanted = json.loads(os.environ["CALL_SETTINGS"])
    client = session().client("socialmessaging", region_name=os.environ["AWS_REGION"])

    current = client.get_linked_whatsapp_business_account_phone_number(id=phone_number_id)
    number = current.get("phoneNumber", {}).get("displayPhoneNumber", phone_number_id)
    print(f"WhatsApp call settings of {number} now: {json.dumps(current.get('callSettings'), default=str)}")
    never_configured = current.get("callSettings") is None
    if current.get("callSettings") == wanted or (never_configured and not wanted.get("callEnabled")):
        print("Call settings are up to date.")
        return
    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry run] would set: {json.dumps(wanted)}")
        return
    try:
        client.update_linked_whatsapp_business_account_phone_number(id=phone_number_id, callSettings=wanted)
    except ClientError as exc:
        err = exc.response.get("Error", {})
        sys.exit(f"Updating the call settings of {number} failed: {err.get('Code')}: {err.get('Message')}")
    print(f"Set WhatsApp call settings of {number}: {json.dumps(wanted)}")


if __name__ == "__main__":
    main()
