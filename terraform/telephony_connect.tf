# ====================================================
# AMAZON CONNECT: EMERGENCY CONTACT CALLS (INFRA-4, optional)
# ====================================================
# create_connect_instance = false (default): use an existing instance, flow and number from the
# connect_instance_id / emergency_contact_phone_flow_id / connect_source_phone_number variables.
# create_connect_instance = true: create the instance, an outbound flow that reads
# $.Attributes.SpokenMessage with Amazon Polly, and claim a phone number. Outbound calls to some
# countries also need a service-quota / country allow-list change in the Connect console.

resource "aws_connect_instance" "resq" {
  count = var.create_connect_instance ? 1 : 0

  identity_management_type  = "CONNECT_MANAGED"
  instance_alias            = var.connect_instance_alias != "" ? var.connect_instance_alias : "resq-ai-${random_id.bucket_suffix.hex}"
  inbound_calls_enabled     = true
  outbound_calls_enabled    = true
  contact_flow_logs_enabled = true

  tags = {
    Name = "resq-connect"
  }
}

resource "aws_connect_contact_flow" "emergency_contact" {
  count = var.create_connect_instance ? 1 : 0

  instance_id = aws_connect_instance.resq[0].id
  name        = "ResQ Emergency Contact Outbound"
  description = "Plays the emergency message passed by the backend in the SpokenMessage attribute, twice, then hangs up"
  type        = "CONTACT_FLOW"

  # Polly has no Urdu voice: Urdu and Hindi messages arrive in Devanagari (telephony_service.py)
  # and are read by Kajal (hi-IN + en-IN); everything else by Joanna.
  content = jsonencode({
    Version     = "2019-10-30"
    StartAction = "choose-voice"
    Actions = [
      {
        Identifier = "choose-voice"
        Type       = "Compare"
        Parameters = { ComparisonValue = "$.Attributes.Language" }
        Transitions = {
          NextAction = "voice-english"
          Conditions = [
            { NextAction = "voice-hindustani", Condition = { Operator = "Equals", Operands = ["Urdu"] } },
            { NextAction = "voice-hindustani", Condition = { Operator = "Equals", Operands = ["Hindi"] } },
          ]
          Errors = [{ NextAction = "voice-english", ErrorType = "NoMatchingCondition" }]
        }
      },
      {
        Identifier  = "voice-hindustani"
        Type        = "UpdateContactTextToSpeechVoice"
        Parameters  = { TextToSpeechVoice = "Kajal", TextToSpeechEngine = "Neural" }
        Transitions = { NextAction = "play-message" }
      },
      {
        Identifier  = "voice-english"
        Type        = "UpdateContactTextToSpeechVoice"
        Parameters  = { TextToSpeechVoice = "Joanna", TextToSpeechEngine = "Neural" }
        Transitions = { NextAction = "play-message" }
      },
      {
        Identifier = "play-message"
        Type       = "MessageParticipant"
        Parameters = { Text = "$.Attributes.SpokenMessage" }
        Transitions = {
          NextAction = "repeat-message"
          Errors     = [{ NextAction = "disconnect", ErrorType = "NoMatchingError" }]
        }
      },
      {
        Identifier = "repeat-message"
        Type       = "MessageParticipant"
        Parameters = { Text = "$.Attributes.SpokenMessage" }
        Transitions = {
          NextAction = "disconnect"
          Errors     = [{ NextAction = "disconnect", ErrorType = "NoMatchingError" }]
        }
      },
      {
        Identifier  = "disconnect"
        Type        = "DisconnectParticipant"
        Parameters  = {}
        Transitions = {}
      }
    ]
  })

  tags = {
    Name = "resq-emergency-contact-flow"
  }
}

resource "aws_connect_phone_number" "emergency_caller_id" {
  count = var.create_connect_instance ? 1 : 0

  target_arn   = aws_connect_instance.resq[0].arn
  country_code = var.connect_phone_number_country_code
  type         = var.connect_phone_number_type
  description  = "ResQ-AI emergency contact caller ID"

  tags = {
    Name = "resq-emergency-caller-id"
  }
}

locals {
  connect_instance_id         = var.create_connect_instance ? aws_connect_instance.resq[0].id : var.connect_instance_id
  connect_flow_id             = var.create_connect_instance ? aws_connect_contact_flow.emergency_contact[0].contact_flow_id : var.emergency_contact_phone_flow_id
  connect_source_phone_number = var.create_connect_instance ? aws_connect_phone_number.emergency_caller_id[0].phone_number : var.connect_source_phone_number
  connect_instance_arn        = "arn:${local.partition}:connect:${local.region}:${local.account_id}:instance/${local.connect_instance_id}"
}
