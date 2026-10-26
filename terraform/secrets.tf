# ====================================================
# AWS SECRETS MANAGER (INFRA-2)
# ====================================================
# One JSON secret holds the WhatsApp credentials. ECS injects individual keys through the
# container `secrets` block (valueFrom "<secret arn>:<KEY>::"), so no secret appears in plain
# text in a task definition. Only the execution role can read it.
#
# With End User Messaging Social (the default provider) WhatsApp is sent with IAM, so only
# WHATSAPP_VERIFY_TOKEN is always injected. WHATSAPP_APP_SECRET and WHATSAPP_ACCESS_TOKEN are
# injected when set (or always, when the secret value is managed outside Terraform).

resource "random_password" "whatsapp_verify_token" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "whatsapp" {
  name        = "resq/whatsapp"
  description = "ResQ-AI WhatsApp credentials (verify token, Meta app secret, Meta access token for rollback)"
  # Delete immediately on destroy so a re-apply can recreate resq/whatsapp at once
  recovery_window_in_days = 0

  tags = {
    Name = "resq-whatsapp-secrets"
  }
}

locals {
  whatsapp_verify_token = var.whatsapp_verify_token != "" ? var.whatsapp_verify_token : random_password.whatsapp_verify_token.result

  # Which optional keys the containers receive. nonsensitive() only reveals whether a value is set.
  inject_whatsapp_app_secret   = !var.manage_whatsapp_secret_values || nonsensitive(var.whatsapp_app_secret != "")
  inject_whatsapp_access_token = !var.manage_whatsapp_secret_values || nonsensitive(var.whatsapp_access_token != "")

  whatsapp_secret_verify_token = { name = "WHATSAPP_VERIFY_TOKEN", valueFrom = "${aws_secretsmanager_secret.whatsapp.arn}:WHATSAPP_VERIFY_TOKEN::" }
  whatsapp_secret_app_secret   = { name = "WHATSAPP_APP_SECRET", valueFrom = "${aws_secretsmanager_secret.whatsapp.arn}:WHATSAPP_APP_SECRET::" }
  whatsapp_secret_access_token = { name = "WHATSAPP_ACCESS_TOKEN", valueFrom = "${aws_secretsmanager_secret.whatsapp.arn}:WHATSAPP_ACCESS_TOKEN::" }

  # API: Meta webhook verification + signature check, and outbound sends if rolled back to Meta.
  api_container_secrets = concat(
    local.api_config_secrets,
    [local.whatsapp_secret_verify_token],
    local.inject_whatsapp_app_secret ? [local.whatsapp_secret_app_secret] : [],
    local.inject_whatsapp_access_token ? [local.whatsapp_secret_access_token] : [],
  )

  # Worker: only sends/downloads; the access token is needed only for the Meta rollback.
  worker_container_secrets = concat(
    local.worker_config_secrets,
    local.inject_whatsapp_access_token ? [local.whatsapp_secret_access_token] : [],
  )
}

resource "aws_secretsmanager_secret_version" "whatsapp" {
  count     = var.manage_whatsapp_secret_values ? 1 : 0
  secret_id = aws_secretsmanager_secret.whatsapp.id

  secret_string = jsonencode({
    WHATSAPP_VERIFY_TOKEN = local.whatsapp_verify_token
    WHATSAPP_APP_SECRET   = var.whatsapp_app_secret
    WHATSAPP_ACCESS_TOKEN = var.whatsapp_access_token
  })
}

# ----------------------------------------------------
# App config: every non-WhatsApp setting the API and worker read from the environment.
# One JSON secret per service; each key becomes an env var through the `secrets` block.
# ----------------------------------------------------
locals {
  api_app_config = merge(local.common_app_config, {
    SEED_DEMO_DATA        = var.seed_demo_data ? "True" : "False"
    AUTH_ENABLED          = var.auth_enabled ? "True" : "False"
    COGNITO_USER_POOL_ID  = aws_cognito_user_pool.portal_pool.id
    COGNITO_APP_CLIENT_ID = aws_cognito_user_pool_client.portal_client.id
    COGNITO_REGION        = var.aws_region
  })

  worker_app_config = merge(local.common_app_config, {
    SEED_DEMO_DATA                   = "False"
    AGENT_BACKEND                    = "agentcore"
    AGENTCORE_RUNTIME_ARN            = aws_bedrockagentcore_agent_runtime.triage.agent_runtime_arn
    TRANSCRIBE_TIMEOUT_SECONDS       = tostring(var.transcribe_timeout_seconds)
    TRANSCRIBE_POLL_INTERVAL_SECONDS = tostring(var.transcribe_poll_interval_seconds)
    PYTHONUNBUFFERED                 = "1"
  })

  api_config_secrets    = [for k in sort(keys(local.api_app_config)) : { name = k, valueFrom = "${aws_secretsmanager_secret.api_config.arn}:${k}::" }]
  worker_config_secrets = [for k in sort(keys(local.worker_app_config)) : { name = k, valueFrom = "${aws_secretsmanager_secret.worker_config.arn}:${k}::" }]
}

resource "aws_secretsmanager_secret" "api_config" {
  name                    = "resq/api-config"
  description             = "ResQ-AI API service runtime config"
  recovery_window_in_days = 0

  tags = {
    Name = "resq-api-config"
  }
}

resource "aws_secretsmanager_secret_version" "api_config" {
  secret_id     = aws_secretsmanager_secret.api_config.id
  secret_string = jsonencode(local.api_app_config)

  # A config change replaces the version: write the new one (it takes AWSCURRENT) before
  # the old one is removed.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_secretsmanager_secret" "worker_config" {
  name                    = "resq/worker-config"
  description             = "ResQ-AI worker service runtime config"
  recovery_window_in_days = 0

  tags = {
    Name = "resq-worker-config"
  }
}

resource "aws_secretsmanager_secret_version" "worker_config" {
  secret_id     = aws_secretsmanager_secret.worker_config.id
  secret_string = jsonencode(local.worker_app_config)

  # A config change replaces the version: write the new one (it takes AWSCURRENT) before
  # the old one is removed.
  lifecycle {
    create_before_destroy = true
  }
}
