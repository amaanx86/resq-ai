# ====================================================
# AMAZON BEDROCK AGENTCORE MEMORY: LONG-TERM PATIENT MEMORY (MEM-1)
# ====================================================
# Events: actor = patient ("patient-<digits of phone>"), session = case ID. The worker writes
# the patient's words and each triage outcome, the API writes the doctor's chat messages
# (backend/app/services/memory_service.py). A semantic strategy extracts durable facts into
# /patients/{actorId}/facts, which the triage agent reads (runtime, and the worker's in-process
# fallback) to escalate a non-emergency when the history makes it high-risk. Never to lower ESI.
#
# The strategy is CUSTOM (SEMANTIC_OVERRIDE) only to append an instruction to the built-in
# prompts: facts are written in English, because patients speak Urdu/Hindi and the built-in
# strategy keeps the conversation's language, while the facts are shown to the doctor in the
# portal. Custom prompts run the model through memory_execution_role_arn.
#
# The namespace template must match PATIENT_FACTS_NAMESPACE in backend/app/agents/memory.py.
# Off (agentcore_memory_enabled = false): no resources, AGENTCORE_MEMORY_ID is empty, the app
# runs exactly as without memory. Encryption: AWS-managed key.

resource "aws_bedrockagentcore_memory" "patients" {
  count = var.agentcore_memory_enabled ? 1 : 0

  name                      = "resq_patient_memory"
  description               = "ResQ-AI long-term patient memory: facts from voice notes, triage outcomes and doctor notes"
  event_expiry_duration     = var.agentcore_memory_event_expiry_days
  memory_execution_role_arn = aws_iam_role.agentcore_memory[0].arn

  tags = {
    Name    = "resq-patient-memory"
    Service = "Amazon Bedrock AgentCore"
  }
}

resource "aws_bedrockagentcore_memory_strategy" "patient_facts" {
  count = var.agentcore_memory_enabled ? 1 : 0

  name                      = "patient_facts"
  memory_id                 = aws_bedrockagentcore_memory.patients[0].id
  memory_execution_role_arn = aws_iam_role.agentcore_memory[0].arn
  type                      = "CUSTOM"
  description               = "Durable medical facts about the patient: conditions, medication, allergies, past emergencies, home access"
  namespace_templates       = ["/patients/{actorId}/facts"]

  configuration {
    type = "SEMANTIC_OVERRIDE"

    extraction {
      append_to_prompt = local.memory_english_instruction
      model_id         = local.agentcore_memory_model_id
    }

    consolidation {
      append_to_prompt = local.memory_english_instruction
      model_id         = local.agentcore_memory_model_id
    }
  }

  depends_on = [aws_iam_role_policy_attachment.agentcore_memory_inference]
}

# Role the memory service assumes to run the extraction and consolidation model
resource "aws_iam_role" "agentcore_memory" {
  count = var.agentcore_memory_enabled ? 1 : 0

  name = "resq-agentcore-memory-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "agentcore_memory_inference" {
  count = var.agentcore_memory_enabled ? 1 : 0

  role       = aws_iam_role.agentcore_memory[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy"
}

locals {
  agentcore_memory_id  = var.agentcore_memory_enabled ? aws_bedrockagentcore_memory.patients[0].id : ""
  agentcore_memory_arn = var.agentcore_memory_enabled ? aws_bedrockagentcore_memory.patients[0].arn : ""

  agentcore_memory_model_id = var.agentcore_memory_model_id != "" ? var.agentcore_memory_model_id : var.bedrock_model_id
  memory_english_instruction = join(" ", [
    "The conversation is often in Urdu, Hindi, Roman Urdu or Hinglish.",
    "Always write every memory/fact in English, translating it, whatever language the conversation is in.",
    "Keep medical terms, medication names, dates and numbers exact.",
  ])

  # Triage agent (runtime, worker fallback): read the extracted facts
  memory_read_statements = var.agentcore_memory_enabled ? [
    {
      Sid      = "PatientMemoryRead"
      Effect   = "Allow"
      Action   = ["bedrock-agentcore:RetrieveMemoryRecords"]
      Resource = [local.agentcore_memory_arn]
    }
  ] : []

  # Worker (patient messages, triage outcomes) and API (doctor messages): write events
  memory_write_statements = var.agentcore_memory_enabled ? [
    {
      Sid      = "PatientMemoryWrite"
      Effect   = "Allow"
      Action   = ["bedrock-agentcore:CreateEvent"]
      Resource = [local.agentcore_memory_arn]
    }
  ] : []
}
