# =============================================================================
# WhatsApp calling on the business phone number (End User Messaging Social, CALL-0)
# =============================================================================
# Lets patients call the ResQ business number from WhatsApp (CALL-1). The AWS provider has no
# resource for the phone number's call settings, so a terraform_data runs
# scripts/whatsapp_call_settings.py (boto3, UpdateLinkedWhatsAppBusinessAccountPhoneNumber) whenever
# the settings change. It prints Meta's own error on failure, which the console hides.
#
# No call hours are set, so calls are accepted at any time (an emergency service has no opening
# hours). Meta rejects call hours without weekly slots, even disabled ones, so they are left out.
# Turning calling off again is `whatsapp_calling_enabled = false` + apply; destroy leaves the setting
# as it is.
#
# Meta only enables calling on numbers with a messaging limit of at least 2,000 business-initiated
# conversations per 24 hours; below that the apply fails with "Calling APIs cannot be enabled for
# this phone number". The ResQ number was below it on 2026-10-16, so calling stays off until then.

locals {
  whatsapp_call_settings = {
    callEnabled        = var.whatsapp_calling_enabled
    callIconVisibility = "DEFAULT"
  }
}

resource "terraform_data" "whatsapp_call_settings" {
  count = var.eum_social_phone_number_id != "" ? 1 : 0

  triggers_replace = [var.eum_social_phone_number_id, jsonencode(local.whatsapp_call_settings)]

  provisioner "local-exec" {
    command     = "${path.module}/scripts/whatsapp_call_settings.py"
    interpreter = [var.local_python]
    environment = {
      PHONE_NUMBER_ID = var.eum_social_phone_number_id
      AWS_REGION      = var.aws_region
      CALL_SETTINGS   = jsonencode(local.whatsapp_call_settings)
    }
  }
}
