# ====================================================
# GENERAL
# ====================================================
variable "aws_region" {
  description = "AWS deployment region"
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "Account Terraform may run against (provider allowed_account_ids). null disables the guardrail."
  type        = string
  default     = null
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "production"
}

variable "vpc_cidr" {
  description = "CIDR block for the ResQ VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Two availability zones in aws_region (public, private subnets and NAT gateways are created in the first two)"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "availability_zones needs at least two zones."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the ECS, Lambda and AgentCore log groups"
  type        = number
  default     = 30
}

variable "ops_alert_emails" {
  description = "Email addresses subscribed to resq-emergency-alerts (receives the webhook DLQ alarm). Each address must confirm the subscription email from SNS."
  type        = list(string)
  default     = []
}

# ====================================================
# AMAZON BEDROCK MODELS
# ====================================================
variable "bedrock_model_id" {
  description = "Amazon Bedrock model (or inference profile) ID for the Strands triage agent. Confirm in the Bedrock console that the model is enabled in aws_region; if it is only offered through a cross-region inference profile, use the profile ID (e.g. \"us.<model id>\"); IAM in locals.tf covers both forms."
  type        = string
  default     = "anthropic.claude-sonnet-5-5"
}

variable "bedrock_fallback_model_id" {
  description = "Amazon Bedrock fallback model (or inference profile) ID, used when the primary model fails"
  type        = string
  default     = "amazon.nova-pro-v1:0"
}

# ====================================================
# CONTAINER IMAGES (ECR)
# ====================================================
variable "app_container_image" {
  description = "Full backend image URI override. Empty uses the resq-backend ECR repository with backend_image_tag."
  type        = string
  default     = ""
}

variable "backend_image_tag" {
  description = "Tag of the backend image in the resq-backend ECR repository (API and worker share it)"
  type        = string
  default     = "latest"
}

variable "agentcore_container_image" {
  description = "Full AgentCore Runtime image URI override (ARM64). Empty uses the resq-agentcore-runtime ECR repository with agentcore_image_tag."
  type        = string
  default     = ""
}

variable "agentcore_image_tag" {
  description = "Tag of the ARM64 agent image in the resq-agentcore-runtime ECR repository"
  type        = string
  default     = "latest"
}

variable "ecr_keep_image_count" {
  description = "Number of most recent images each ECR repository keeps (older ones expire)"
  type        = number
  default     = 20
}

# ====================================================
# ECS: SHARED
# ====================================================
variable "app_port" {
  description = "Container port for FastAPI"
  type        = number
  default     = 8000
}

variable "ecs_cpu_architecture" {
  description = "CPU architecture of the backend image for Fargate (X86_64 or ARM64)"
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.ecs_cpu_architecture)
    error_message = "ecs_cpu_architecture must be \"X86_64\" or \"ARM64\"."
  }
}

variable "mock_aws" {
  description = "MOCK_AWS for both services. Must stay false in AWS, otherwise the backend runs on in-memory stores and simulated calls."
  type        = bool
  default     = false
}

variable "demo_mode" {
  description = "DEMO_MODE: pad analytics KPIs with demo numbers"
  type        = bool
  default     = false
}

variable "seed_demo_data" {
  description = "SEED_DEMO_DATA for the API service (demo doctors/patient/cases on startup; they count in analytics). Off by default so production holds real data only; hackathon demo environments set it to true. The worker never seeds."
  type        = bool
  default     = false
}

# ====================================================
# ECS: API SERVICE
# ====================================================
variable "enable_container_insights" {
  description = "ECS cluster Container Insights (extra CloudWatch metrics cost)"
  type        = bool
  default     = true
}

variable "ecs_task_cpu" {
  description = "Fargate CPU units for the API task (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "ecs_task_memory" {
  description = "Fargate memory (MB) for the API task"
  type        = number
  default     = 2048
}

variable "api_desired_count" {
  description = "Initial number of API tasks"
  type        = number
  default     = 2
}

variable "api_min_capacity" {
  description = "Minimum number of API tasks (CPU autoscaling)"
  type        = number
  default     = 2
}

variable "api_max_capacity" {
  description = "Maximum number of API tasks (CPU autoscaling)"
  type        = number
  default     = 10
}

variable "auth_enabled" {
  description = "AUTH_ENABLED for the API: require Cognito ID tokens on portal routes"
  type        = bool
  default     = true
}

# ====================================================
# ECS: WORKER SERVICE
# ====================================================
variable "worker_task_cpu" {
  description = "Fargate CPU units for the worker task"
  type        = number
  default     = 1024
}

variable "worker_task_memory" {
  description = "Fargate memory (MB) for the worker task"
  type        = number
  default     = 2048
}

variable "worker_min_capacity" {
  description = "Minimum number of worker tasks (keep at least 1 so emergencies are never waiting for a cold start)"
  type        = number
  default     = 1
}

variable "worker_max_capacity" {
  description = "Maximum number of worker tasks"
  type        = number
  default     = 10
}

variable "worker_scale_out_queue_depth" {
  description = "Add workers when the webhook queue has at least this many visible messages"
  type        = number
  default     = 10
}

variable "transcribe_timeout_seconds" {
  description = "TRANSCRIBE_TIMEOUT_SECONDS: how long the worker waits for an Amazon Transcribe job"
  type        = number
  default     = 60
}

variable "transcribe_poll_interval_seconds" {
  description = "TRANSCRIBE_POLL_INTERVAL_SECONDS: polling interval for Amazon Transcribe jobs"
  type        = number
  default     = 2
}

# ====================================================
# INBOUND QUEUE
# ====================================================
variable "webhook_queue_visibility_timeout" {
  description = "Visibility timeout (s) of resq-webhook-queue. Must exceed the worst-case pipeline time (Transcribe 60 s + AgentCore invoke up to 150 s + tools)."
  type        = number
  default     = 300
}

variable "webhook_queue_max_receive_count" {
  description = "Deliveries before a message moves to resq-webhook-dlq"
  type        = number
  default     = 5
}

# ====================================================
# WHATSAPP
# ====================================================
variable "whatsapp_provider" {
  description = "WHATSAPP_PROVIDER for both services: eum_social (AWS End User Messaging Social) or meta (Meta Cloud API, rollback)"
  type        = string
  default     = "eum_social"

  validation {
    condition     = contains(["eum_social", "meta"], var.whatsapp_provider)
    error_message = "whatsapp_provider must be \"eum_social\" or \"meta\"."
  }
}

variable "whatsapp_api_version" {
  description = "WHATSAPP_API_VERSION (Meta Graph / metaApiVersion for End User Messaging Social)"
  type        = string
  default     = "v20.0"
}

variable "eum_social_phone_number_id" {
  description = "EUM_SOCIAL_PHONE_NUMBER_ID: the AWS phone number ID (phone-number-id-...) from the End User Messaging Social console"
  type        = string
  default     = ""

  validation {
    condition     = var.eum_social_phone_number_id == "" || (startswith(var.eum_social_phone_number_id, "phone-number-id-") && !strcontains(var.eum_social_phone_number_id, "REPLACE_ME"))
    error_message = "eum_social_phone_number_id must be the real \"phone-number-id-...\" value from the End User Messaging Social console (or empty when whatsapp_provider = \"meta\")."
  }
}

variable "eum_social_waba_id" {
  description = "End User Messaging Social WABA ID (waba-...) that owns the WhatsApp templates in whatsapp_templates.tf. Empty: templates are not managed."
  type        = string
  default     = ""

  validation {
    condition     = var.eum_social_waba_id == "" || startswith(var.eum_social_waba_id, "waba-")
    error_message = "eum_social_waba_id must be the \"waba-...\" ID from the End User Messaging Social console (or empty)."
  }
}

variable "local_python" {
  description = "Python command on the machine running terraform; runs scripts/whatsapp_template.py (with the AWS CLI) and scripts/whatsapp_call_settings.py (needs boto3 >= 1.43.111)"
  type        = string
  default     = "python"
}

variable "whatsapp_calling_enabled" {
  description = "WhatsApp calling on the EUM business phone number: patients can call ResQ from WhatsApp (whatsapp_calling.tf, CALL-0/CALL-1)"
  type        = bool
  default     = false
}

variable "whatsapp_location_template_name" {
  description = "WHATSAPP_LOCATION_TEMPLATE_NAME: approved WhatsApp template that sends the patient's location to the emergency contact (whatsapp_templates.tf)"
  type        = string
  default     = "resq_emergency_location"
}

variable "whatsapp_location_template_language" {
  description = "WHATSAPP_LOCATION_TEMPLATE_LANGUAGE: language code the template was approved in"
  type        = string
  default     = "en"
}

variable "whatsapp_phone_number_id" {
  description = "WHATSAPP_PHONE_NUMBER_ID: Meta Cloud API phone number ID (only used when whatsapp_provider = meta)"
  type        = string
  default     = ""
}

variable "manage_whatsapp_secret_values" {
  description = "Write the WhatsApp secret values below into Secrets Manager. Set false to manage the secret value outside Terraform (it must then contain all three JSON keys)."
  type        = bool
  default     = true
}

variable "whatsapp_verify_token" {
  description = "WHATSAPP_VERIFY_TOKEN for the Meta webhook GET verification. Empty generates a random token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "whatsapp_app_secret" {
  description = "WHATSAPP_APP_SECRET (Meta app secret, enables X-Hub-Signature-256 checks). Optional."
  type        = string
  default     = ""
  sensitive   = true
}

variable "whatsapp_access_token" {
  description = "WHATSAPP_ACCESS_TOKEN (Meta Cloud API). Only needed to roll back to whatsapp_provider = meta."
  type        = string
  default     = ""
  sensitive   = true
}

# ====================================================
# AMAZON CONNECT (EMERGENCY CONTACT CALLS)
# ====================================================
variable "create_connect_instance" {
  description = "Create an Amazon Connect instance, outbound contact flow and phone number (INFRA-4). False uses the IDs below."
  type        = bool
  default     = false
}

variable "connect_instance_alias" {
  description = "Alias of the Connect instance when create_connect_instance = true (globally unique). Empty generates one."
  type        = string
  default     = ""
}

variable "connect_phone_number_country_code" {
  description = "Country code of the Connect phone number to claim when create_connect_instance = true"
  type        = string
  default     = "US"
}

variable "connect_phone_number_type" {
  description = "Type of the Connect phone number to claim (DID or TOLL_FREE)"
  type        = string
  default     = "DID"

  validation {
    condition     = contains(["DID", "TOLL_FREE"], var.connect_phone_number_type)
    error_message = "connect_phone_number_type must be \"DID\" or \"TOLL_FREE\"."
  }
}

variable "connect_instance_id" {
  description = "CONNECT_INSTANCE_ID of an existing instance (used when create_connect_instance = false)"
  type        = string
  default     = "00000000-0000-0000-0000-000000000000"
}

variable "emergency_contact_phone_flow_id" {
  description = "CONNECT_FLOW_ID of an existing outbound contact flow (used when create_connect_instance = false)"
  type        = string
  default     = "00000000-0000-0000-0000-000000000000"
}

variable "connect_source_phone_number" {
  description = "CONNECT_SOURCE_PHONE_NUMBER (E.164) claimed in the existing instance (used when create_connect_instance = false)"
  type        = string
  default     = "+18005550199"
}

# ====================================================
# PORTAL
# ====================================================
variable "portal_base_url" {
  description = "PORTAL_BASE_URL (used in patient video links). Empty uses https://<CloudFront domain>."
  type        = string
  default     = ""
}

# ====================================================
# COGNITO
# ====================================================
variable "cognito_domain_prefix" {
  description = "Cognito Hosted UI domain prefix (globally unique). Empty generates resq-portal-<suffix>."
  type        = string
  default     = ""
}

variable "cognito_extra_callback_urls" {
  description = "Extra Hosted UI callback/sign-out URLs besides https://<CloudFront domain>/ (e.g. the Vite dev server)"
  type        = list(string)
  default     = ["http://localhost:5173/"]
}

# ====================================================
# BEDROCK AGENTCORE
# ====================================================
variable "agentcore_runtime_name" {
  description = "AgentCore Runtime name (letters, digits and underscores only)"
  type        = string
  default     = "resq_triage"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.agentcore_runtime_name))
    error_message = "agentcore_runtime_name must start with a letter and contain only letters, digits and underscores (max 48 characters)."
  }
}

variable "agentcore_network_mode" {
  description = "AgentCore Runtime network mode (PUBLIC or VPC). VPC mode uses the private subnets and the ECS worker security group."
  type        = string
  default     = "PUBLIC"

  validation {
    condition     = contains(["PUBLIC", "VPC"], var.agentcore_network_mode)
    error_message = "agentcore_network_mode must be \"PUBLIC\" or \"VPC\"."
  }
}

variable "agentcore_memory_enabled" {
  description = "Create the AgentCore Memory for long-term patient history (MEM-1). false: no memory, triage works as before."
  type        = bool
  default     = true
}

variable "agentcore_memory_event_expiry_days" {
  description = "Days AgentCore Memory keeps raw events (patient messages, triage outcomes, doctor notes). Extracted facts are kept separately."
  type        = number
  default     = 90

  validation {
    condition     = var.agentcore_memory_event_expiry_days >= 7 && var.agentcore_memory_event_expiry_days <= 365
    error_message = "agentcore_memory_event_expiry_days must be between 7 and 365."
  }
}

variable "agentcore_memory_model_id" {
  description = "Amazon Bedrock model (or inference profile) ID that extracts and consolidates patient facts in English. Empty uses bedrock_model_id."
  type        = string
  default     = ""
}

variable "gateway_lambda_zip_path" {
  description = "Path of the Gateway tools Lambda zip, built by `python backend/scripts/build_gateway_lambda.py`"
  type        = string
  default     = "../backend/build/gateway_tools.zip"
}

variable "gateway_tool_schema_path" {
  description = "Path of the Gateway tool schema JSON (list of {name, description, inputSchema})"
  type        = string
  default     = "../backend/gateway_tools/tool_schemas.json"
}

variable "gateway_lambda_runtime" {
  description = "Lambda runtime of the Gateway tools function"
  type        = string
  default     = "python3.13"
}

variable "gateway_lambda_architecture" {
  description = "Lambda architecture (x86_64 or arm64). Must match the platform the zip's compiled dependencies were built for."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.gateway_lambda_architecture)
    error_message = "gateway_lambda_architecture must be \"x86_64\" or \"arm64\"."
  }
}

variable "gateway_lambda_timeout" {
  description = "Gateway tools Lambda timeout in seconds"
  type        = number
  default     = 30
}

variable "gateway_lambda_memory" {
  description = "Gateway tools Lambda memory in MB"
  type        = number
  default     = 512
}

# ====================================================
# NETWORKING
# ====================================================
variable "enable_vpc_endpoints" {
  description = "Create S3/DynamoDB gateway endpoints and the interface endpoints below (INFRA-5)"
  type        = bool
  default     = true
}

variable "enable_interface_endpoints" {
  description = "Create the interface endpoints in interface_endpoint_services (billed per AZ-hour). Gateway endpoints follow enable_vpc_endpoints only."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway in AZ 1 for both private subnets instead of one per AZ (cheaper, not AZ-resilient)"
  type        = bool
  default     = false
}

variable "interface_endpoint_services" {
  description = "AWS services that get an interface VPC endpoint in the private subnets"
  type        = list(string)
  default     = ["bedrock-runtime", "transcribe", "ecr.api", "ecr.dkr", "logs", "secretsmanager", "sqs", "sns"]
}

# ====================================================
# CI (GitLab OIDC)
# ====================================================
variable "ci_oidc_enabled" {
  description = "Create the GitLab OIDC provider and the CI deploy role (ci_oidc.tf)"
  type        = bool
  default     = true
}

variable "gitlab_url" {
  description = "GitLab issuer URL (the OIDC provider URL, no trailing slash)"
  type        = string
  default     = "https://code.sudoconsultants.com"
}

variable "gitlab_project_path" {
  description = "GitLab project allowed to assume the CI deploy role"
  type        = string
  default     = "ai/demos/resq-ai"
}

variable "ci_deploy_branch" {
  description = "Only pipelines on this branch can assume the CI deploy role"
  type        = string
  default     = "main"
}

variable "ci_oidc_audience" {
  description = "aud claim the CI id_token carries; must match id_tokens.aud in .gitlab-ci.yml"
  type        = string
  default     = "sts.amazonaws.com"
}

variable "doctor_offer_timeout_seconds" {
  description = "DOCTOR_OFFER_TIMEOUT_ESI1/2/3: seconds a doctor case offer round waits for an answer, per ESI level (OFFER-1)"
  type = object({
    esi1 = number
    esi2 = number
    esi3 = number
  })
  default = { esi1 = 60, esi2 = 60, esi3 = 300 }
}

variable "doctor_offer_fanout_urgent" {
  description = "DOCTOR_OFFER_FANOUT_URGENT: free doctors offered an ESI 1-2 case at once, per round (ESI 3: one at a time). Rounds go on until no free doctor is left (OFFER-3)"
  type        = number
  default     = 3
}

variable "waiting_case_recheck_seconds" {
  description = "WAITING_CASE_RECHECK_SECONDS: how often a case that no free doctor took is checked again (OFFER-5; a doctor becoming free offers it at once)"
  type        = number
  default     = 60
}

variable "admin_alert_phone_numbers" {
  description = "ADMIN_ALERT_PHONE_NUMBERS: extra administrator numbers (E.164) called through Amazon Connect when an ESI 1-2 case has no doctor (ADMIN-1), besides the administrators added in the portal with a phone (ADMIN-2). Both empty: the portal alert only"
  type        = list(string)
  default     = []
}

variable "admin_alert_repeat_seconds" {
  description = "ADMIN_ALERT_REPEAT_SECONDS: seconds between administrator calls while nobody acknowledges the no-doctor alert"
  type        = number
  default     = 300
}

variable "admin_alert_max_calls" {
  description = "ADMIN_ALERT_MAX_CALLS: administrator calls per waiting period at most"
  type        = number
  default     = 3
}

variable "doctor_whatsapp_relay_enabled" {
  description = "DOCTOR_WHATSAPP_RELAY_ENABLED: doctors talk to their patient through the ResQ number and use @resq commands (DOC-*). False: doctors are told to use the portal"
  type        = bool
  default     = true
}

variable "doctor_agent_model_id" {
  description = "DOCTOR_AGENT_MODEL_ID: Bedrock model for free-form @resq commands (DOC-5). Empty: bedrock_model_id"
  type        = string
  default     = ""
}

variable "doctor_case_closed_grace_hours" {
  description = "DOCTOR_CASE_CLOSED_GRACE_HOURS: a doctor whose case closed this recently gets 'case closed' instead of being triaged as a patient"
  type        = number
  default     = 12
}

variable "doctor_idle_reminder_seconds" {
  description = "DOCTOR_IDLE_REMINDER_SECONDS: a doctor whose open case had no messages this long is asked whether they are still on it (0: off)"
  type        = number
  default     = 7200
}
