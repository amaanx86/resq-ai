# Shared lookups and derived values used across the flat module.

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = var.aws_region

  # Container images: the ECR repositories created in ecr.tf unless an explicit URI is given.
  backend_image   = var.app_container_image != "" ? var.app_container_image : "${aws_ecr_repository.resq_backend.repository_url}:${var.backend_image_tag}"
  agentcore_image = var.agentcore_container_image != "" ? var.agentcore_container_image : "${aws_ecr_repository.agentcore_runtime.repository_url}:${var.agentcore_image_tag}"

  # Public URLs. The portal (and the patient /call/... video page) is served by CloudFront,
  # which also proxies /api/* and /ws/* to the ALB.
  portal_base_url = var.portal_base_url != "" ? var.portal_base_url : "https://${aws_cloudfront_distribution.portal_distribution.domain_name}"

  # Bedrock models the agent may call (primary + fallback). Cross-region inference profile IDs
  # ("us.anthropic...") need both the profile ARN and the underlying foundation model ARNs.
  bedrock_model_ids      = distinct([var.bedrock_model_id, var.bedrock_fallback_model_id])
  bedrock_base_model_ids = distinct([for m in local.bedrock_model_ids : replace(m, "/^(us|eu|apac|global|us-gov|jp|au|ca)\\./", "")])
  bedrock_model_arns = concat(
    [for m in local.bedrock_base_model_ids : "arn:${local.partition}:bedrock:*::foundation-model/${m}"],
    [for m in local.bedrock_model_ids : "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:inference-profile/${m}"],
  )

  # End User Messaging Social phone number resources (SendWhatsAppMessage, Get/PostWhatsAppMessageMedia).
  social_messaging_phone_arn = "arn:${local.partition}:social-messaging:${local.region}:${local.account_id}:phone-number-id/*"

  # Per-API-task fan-out queues created at runtime by app/services/events_service.py.
  api_events_queue_arn_pattern = "arn:${local.partition}:sqs:${local.region}:${local.account_id}:resq-api-events-*"

  gitlab_host = trimprefix(var.gitlab_url, "https://")

  cognito_domain_prefix = var.cognito_domain_prefix != "" ? var.cognito_domain_prefix : "resq-portal-${random_id.bucket_suffix.hex}"
  cognito_hosted_ui_url = "https://${aws_cognito_user_pool_domain.portal_domain.domain}.auth.${local.region}.amazoncognito.com"
  cognito_callback_urls = distinct(concat(["${local.portal_base_url}/"], var.cognito_extra_callback_urls))
}
