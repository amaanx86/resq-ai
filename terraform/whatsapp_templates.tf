# =============================================================================
# WhatsApp message templates (End User Messaging Social)
# =============================================================================
# Templates are the only messages WhatsApp delivers outside the 24-hour customer service window
# (an emergency contact or a doctor who never messaged the business number). Neither the AWS
# provider nor CloudFormation has a resource for them, so each template is a terraform_data whose
# local-exec runs scripts/whatsapp_template.py with the AWS CLI: create if missing, update in place
# if its content changed, nothing if it matches (an APPROVED template is never sent back to review
# just by being adopted). It runs again only when a definition changes. Nothing is deleted on
# destroy: Meta blocks a deleted template's name for 30 days.
#
# Needs `python` (var.local_python) and the AWS CLI on the machine running terraform, with the same
# credentials (AWS_PROFILE). Meta reviews new or changed templates; check with
#   aws socialmessaging list-whatsapp-message-templates --id <eum_social_waba_id>
# Until a template is APPROVED its sends fail, and the backend falls back (see each template).
# Rule from Meta: a body must not start or end with a variable.

locals {
  whatsapp_template_footer = { type = "FOOTER", text = "ResQ-AI Emergency Response" }
  whatsapp_example_case_id = "CASE-1A2B3C4D5E"

  # Template names, also passed to the backend (ecs.tf).
  whatsapp_offer_template_name          = "resq_doctor_case_offer"
  whatsapp_call_template_name           = "resq_incoming_patient_call"
  whatsapp_call_portal_template_name    = "resq_incoming_patient_call_portal"
  whatsapp_doctor_waiting_template_name = "resq_doctor_patient_waiting"
  whatsapp_doctor_confirm_template_name = "resq_doctor_confirm_number"

  whatsapp_templates = {
    # Patient's shared location to the emergency contact (LOC-1,
    # whatsapp_service.send_emergency_location_alert). {{1}} patient name, {{2}} map link.
    location = {
      name     = var.whatsapp_location_template_name
      language = var.whatsapp_location_template_language
      category = "UTILITY"
      components = [
        {
          type    = "BODY"
          text    = "ResQ-AI emergency alert: {{1}} has reported a medical emergency and shared their location: {{2}} A doctor is handling the case and will arrange an ambulance."
          example = { body_text = [["Ahmed Khan", "https://maps.google.com/?q=24.860700,67.001100"]] }
        },
        local.whatsapp_template_footer,
      ]
    }

    # Case offered to a doctor (OFFER-2). {{1}} ESI level, {{2}} specialty, {{3}} short summary,
    # {{4}} time to answer. Quick-reply payloads (OFFER:ACCEPT|DECLINE:<case>:<offer>) are set per
    # send. Fallback until APPROVED: interactive reply buttons (24 h window only) + the portal popup.
    doctor_offer = {
      name     = local.whatsapp_offer_template_name
      language = "en"
      category = "UTILITY"
      components = [
        { type = "HEADER", format = "TEXT", text = "New emergency case" },
        {
          type    = "BODY"
          text    = "A ResQ-AI patient needs a doctor. ESI level {{1}}, specialty {{2}}. Summary: {{3}} Please accept within {{4}}, otherwise the case is offered to the next doctor."
          example = { body_text = [["2", "Cardiology", "Severe chest pain spreading to the left arm for 20 minutes.", "60 seconds"]] }
        },
        local.whatsapp_template_footer,
        {
          type = "BUTTONS"
          buttons = [
            { type = "QUICK_REPLY", text = "Accept" },
            { type = "QUICK_REPLY", text = "Decline" },
          ]
        },
      ]
    }

    # The assigned doctor's patient is calling the ResQ number (CALL-3). {{1}} patient, {{2}} ESI.
    # URL suffixes: the case ID and a one-time direct-call token. The URLs hold the portal domain,
    # so a new portal domain updates the template (and Meta reviews it again).
    incoming_call = {
      name     = local.whatsapp_call_template_name
      language = "en"
      category = "UTILITY"
      components = [
        {
          type    = "BODY"
          text    = "Patient call: {{1}} (ESI {{2}}) is calling you on ResQ-AI now. Answer in the portal, or open their chat and call them from your own WhatsApp."
          example = { body_text = [["Ahmed Khan", "2"]] }
        },
        local.whatsapp_template_footer,
        {
          type = "BUTTONS"
          buttons = [
            {
              type    = "URL"
              text    = "Answer on portal"
              url     = "${local.portal_base_url}/?case={{1}}"
              example = ["${local.portal_base_url}/?case=${local.whatsapp_example_case_id}"]
            },
            {
              type    = "URL"
              text    = "Open chat & call"
              url     = "${local.portal_base_url}/api/v1/direct-call/{{1}}"
              example = ["${local.portal_base_url}/api/v1/direct-call/q5Yt2c9mXvB8wLk3RjN0aQ7sHdF4gUeP1iZoTyWx6bC"]
            },
          ]
        },
      ]
    }

    # Same, for doctors who did not allow direct WhatsApp calls: portal button only (CALL-3).
    incoming_call_portal = {
      name     = local.whatsapp_call_portal_template_name
      language = "en"
      category = "UTILITY"
      components = [
        {
          type    = "BODY"
          text    = "Patient call: {{1}} (ESI {{2}}) is calling you on ResQ-AI now. Answer in the portal within 30 seconds."
          example = { body_text = [["Ahmed Khan", "2"]] }
        },
        local.whatsapp_template_footer,
        {
          type = "BUTTONS"
          buttons = [
            {
              type    = "URL"
              text    = "Answer on portal"
              url     = "${local.portal_base_url}/?case={{1}}"
              example = ["${local.portal_base_url}/?case=${local.whatsapp_example_case_id}"]
            },
          ]
        },
      ]
    }

    # A patient wrote to their doctor, whose 24-hour WhatsApp window is closed (DOC-6,
    # whatsapp_service.send_doctor_patient_waiting). {{1}} patient name. The quick-reply payload
    # (DOC:SHOW:<case>) is set per send; the tap opens the window and brings the messages.
    # Until APPROVED the patient's messages wait in the portal and the case chat says so.
    doctor_patient_waiting = {
      name     = local.whatsapp_doctor_waiting_template_name
      language = "en"
      category = "UTILITY"
      components = [
        {
          type    = "BODY"
          text    = "Your ResQ-AI patient {{1}} sent you a new message. Tap Show messages to read it and reply."
          example = { body_text = [["Ahmed Khan"]] }
        },
        local.whatsapp_template_footer,
        {
          type    = "BUTTONS"
          buttons = [{ type = "QUICK_REPLY", text = "Show messages" }]
        },
      ]
    }

    # A newly registered doctor (or a changed number) confirms their WhatsApp number (DOC-8,
    # whatsapp_service.send_doctor_number_confirmation). {{1}} doctor name. Quick-reply payloads
    # (DOCV:YES|NO:<doctor>) are set per send. Until APPROVED: interactive reply buttons (24 h window
    # only), and an administrator can ask again from the roster (POST /doctors/{id}/confirm-number).
    doctor_confirm_number = {
      name     = local.whatsapp_doctor_confirm_template_name
      language = "en"
      category = "UTILITY"
      components = [
        { type = "HEADER", format = "TEXT", text = "Confirm your number" },
        {
          type    = "BODY"
          text    = "This number was added to ResQ-AI as the WhatsApp number of {{1}}. Emergency case offers and patients' messages will come here. Is this your number?"
          example = { body_text = [["Dr. Sarah Khan"]] }
        },
        local.whatsapp_template_footer,
        {
          type = "BUTTONS"
          buttons = [
            { type = "QUICK_REPLY", text = "Yes, it's me" },
            { type = "QUICK_REPLY", text = "No, not me" },
          ]
        },
      ]
    }
  }
}

resource "terraform_data" "whatsapp_template" {
  # Values JSON-encoded: the templates have different shapes, and for_each needs one value type.
  for_each = { for key, template in local.whatsapp_templates : key => jsonencode(template) if var.eum_social_waba_id != "" }

  # A changed definition (or WABA) replaces this resource, which runs the create-or-update again.
  triggers_replace = [var.eum_social_waba_id, each.value]
  input            = { name = jsondecode(each.value).name, language = jsondecode(each.value).language }

  provisioner "local-exec" {
    command     = "${path.module}/scripts/whatsapp_template.py"
    interpreter = [var.local_python]
    environment = {
      WABA_ID             = var.eum_social_waba_id
      AWS_REGION          = var.aws_region
      TEMPLATE_DEFINITION = each.value
    }
  }
}
