# ====================================================
# NETWORK + API
# ====================================================
output "vpc_id" {
  description = "The ID of the ResQ VPC"
  value       = aws_vpc.resq_vpc.id
}

output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer (CloudFront origin; direct requests get 403)"
  value       = aws_lb.resq_alb.dns_name
}

output "api_endpoint" {
  description = "Base URL of the FastAPI backend through CloudFront (/api/*, /ws/*, /healthz). The ALB itself only accepts CloudFront traffic."
  value       = local.portal_base_url
}

output "meta_webhook_url" {
  description = "Meta Cloud API webhook URL (only for rolling back to whatsapp_provider = meta)"
  value       = "${local.portal_base_url}/api/v1/webhook"
}

# ====================================================
# ECS
# ====================================================
output "ecs_cluster_name" {
  description = "Name of the ECS Cluster"
  value       = aws_ecs_cluster.resq_cluster.name
}

output "ecs_api_service_name" {
  description = "Name of the API ECS service"
  value       = aws_ecs_service.resq_service.name
}

output "ecs_worker_service_name" {
  description = "Name of the worker ECS service"
  value       = aws_ecs_service.worker.name
}

output "api_log_group" {
  description = "CloudWatch log group of the API service"
  value       = aws_cloudwatch_log_group.ecs_logs.name
}

output "worker_log_group" {
  description = "CloudWatch log group of the worker service"
  value       = aws_cloudwatch_log_group.worker_logs.name
}

# ====================================================
# CONTAINER IMAGES
# ====================================================
output "ecr_backend_repository_url" {
  description = "ECR repository URL for the backend image (API and worker)"
  value       = aws_ecr_repository.resq_backend.repository_url
}

output "ecr_agentcore_repository_url" {
  description = "ECR repository URL for the ARM64 AgentCore Runtime image"
  value       = aws_ecr_repository.agentcore_runtime.repository_url
}

output "backend_image" {
  description = "Backend image URI the task definitions use"
  value       = local.backend_image
}

output "agentcore_image" {
  description = "AgentCore Runtime image URI"
  value       = local.agentcore_image
}

# ====================================================
# DATA
# ====================================================
output "dynamodb_table_name" {
  description = "Name of the DynamoDB ResQTable"
  value       = aws_dynamodb_table.resq_table.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB ResQTable"
  value       = aws_dynamodb_table.resq_table.arn
}

output "audio_vault_bucket" {
  description = "S3 bucket name for encrypted audio voice notes"
  value       = aws_s3_bucket.audio_vault.id
}

output "portal_bucket" {
  description = "S3 bucket name for static web portal assets"
  value       = aws_s3_bucket.portal_bucket.id
}

# ====================================================
# PORTAL (CLOUDFRONT + COGNITO)
# ====================================================
output "cloudfront_domain_name" {
  description = "CloudFront distribution domain name for the Web Portal"
  value       = aws_cloudfront_distribution.portal_distribution.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for create-invalidation after a portal deploy)"
  value       = aws_cloudfront_distribution.portal_distribution.id
}

output "portal_url" {
  description = "Public portal URL (PORTAL_BASE_URL)"
  value       = local.portal_base_url
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID"
  value       = aws_cognito_user_pool.portal_pool.id
}

output "cognito_client_id" {
  description = "Cognito User Pool Client ID"
  value       = aws_cognito_user_pool_client.portal_client.id
}

output "cognito_domain" {
  description = "Cognito Hosted UI base URL"
  value       = local.cognito_hosted_ui_url
}

output "cognito_domain_prefix" {
  description = "Cognito Hosted UI domain prefix"
  value       = aws_cognito_user_pool_domain.portal_domain.domain
}

output "cognito_redirect_uri" {
  description = "Hosted UI callback / sign-out URL of the deployed portal"
  value       = "${local.portal_base_url}/"
}

output "vite_build_env" {
  description = "Build-time env for `npm run build` of the portal (write into frontend/.env.production)"
  value       = local.vite_build_env
}

# ====================================================
# MESSAGING
# ====================================================
output "webhook_queue_url" {
  description = "URL of resq-webhook-queue (inbound WhatsApp messages for the worker)"
  value       = aws_sqs_queue.webhook_queue.id
}

output "webhook_dlq_url" {
  description = "URL of resq-webhook-dlq"
  value       = aws_sqs_queue.webhook_dlq.id
}

output "whatsapp_inbound_topic_arn" {
  description = "SNS topic to set as the WABA event destination in End User Messaging Social"
  value       = aws_sns_topic.whatsapp_inbound.arn
}

output "portal_events_topic_arn" {
  description = "SNS topic for the worker -> API portal event fan-out"
  value       = aws_sns_topic.portal_events.arn
}

output "emergency_alerts_topic_arn" {
  description = "SNS topic ARN for high-priority emergency alerts (also receives the DLQ alarm)"
  value       = aws_sns_topic.emergency_alerts.arn
}

output "whatsapp_secret_arn" {
  description = "Secrets Manager secret with WHATSAPP_VERIFY_TOKEN / WHATSAPP_APP_SECRET / WHATSAPP_ACCESS_TOKEN"
  value       = aws_secretsmanager_secret.whatsapp.arn
}

output "whatsapp_verify_token" {
  description = "Meta webhook verify token (only needed for the Meta rollback). Read with: terraform output -raw whatsapp_verify_token"
  value       = local.whatsapp_verify_token
  sensitive   = true
}

# ====================================================
# BEDROCK AGENTCORE
# ====================================================
output "agentcore_runtime_arn" {
  description = "AgentCore Runtime ARN (AGENTCORE_RUNTIME_ARN on the worker)"
  value       = aws_bedrockagentcore_agent_runtime.triage.agent_runtime_arn
}

output "agentcore_runtime_id" {
  description = "AgentCore Runtime ID"
  value       = aws_bedrockagentcore_agent_runtime.triage.agent_runtime_id
}

output "agentcore_gateway_id" {
  description = "AgentCore Gateway ID"
  value       = aws_bedrockagentcore_gateway.tools.gateway_id
}

output "agentcore_gateway_url" {
  description = "AgentCore Gateway MCP URL (AGENTCORE_GATEWAY_URL)"
  value       = aws_bedrockagentcore_gateway.tools.gateway_url
}

output "agentcore_gateway_target_name" {
  description = "Gateway target name; tools reach the Lambda as <target>___<tool>"
  value       = aws_bedrockagentcore_gateway_target.tools_lambda.name
}

output "gateway_tools_lambda_name" {
  description = "Lambda function behind the Gateway target"
  value       = aws_lambda_function.gateway_tools.function_name
}

output "agentcore_log_group" {
  description = "Log group receiving AgentCore runtime and gateway application logs"
  value       = aws_cloudwatch_log_group.agentcore_logs.name
}

# ====================================================
# AMAZON CONNECT
# ====================================================
output "connect_instance_id" {
  description = "CONNECT_INSTANCE_ID in use"
  value       = local.connect_instance_id
}

output "connect_contact_flow_id" {
  description = "CONNECT_FLOW_ID in use"
  value       = local.connect_flow_id
}

output "connect_source_phone_number" {
  description = "CONNECT_SOURCE_PHONE_NUMBER in use"
  value       = local.connect_source_phone_number
}

output "agentcore_memory_id" {
  description = "AgentCore Memory ID for patient history (AGENTCORE_MEMORY_ID); empty when agentcore_memory_enabled = false"
  value       = local.agentcore_memory_id
}

output "ci_deploy_role_arn" {
  description = "IAM role GitLab CI assumes through OIDC (AWS_ROLE_ARN in .gitlab-ci.yml)"
  value       = var.ci_oidc_enabled ? aws_iam_role.ci_deploy[0].arn : null
}
