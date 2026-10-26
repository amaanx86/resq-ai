# ECS Cluster with Container Insights
resource "aws_ecs_cluster" "resq_cluster" {
  name = "resq-ai-cluster"

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = {
    Name = "resq-ai-cluster"
  }
}

# Runtime config for both services, stored in Secrets Manager (secrets.tf) and injected
# through the container `secrets` block
locals {
  common_app_config = {
    ENVIRONMENT                = var.environment
    AWS_REGION                 = var.aws_region
    MOCK_AWS                   = var.mock_aws ? "True" : "False"
    DEMO_MODE                  = var.demo_mode ? "True" : "False"
    BEDROCK_MODEL_ID           = var.bedrock_model_id
    BEDROCK_FALLBACK_MODEL_ID  = var.bedrock_fallback_model_id
    AGENTCORE_MEMORY_ID        = local.agentcore_memory_id
    DYNAMODB_TABLE_NAME        = aws_dynamodb_table.resq_table.name
    S3_AUDIO_BUCKET            = aws_s3_bucket.audio_vault.id
    WHATSAPP_PROVIDER          = var.whatsapp_provider
    WHATSAPP_API_VERSION       = var.whatsapp_api_version
    EUM_SOCIAL_PHONE_NUMBER_ID = var.eum_social_phone_number_id
    WHATSAPP_PHONE_NUMBER_ID   = var.whatsapp_phone_number_id
    # Template that sends the patient's shared location to the emergency contact
    WHATSAPP_LOCATION_TEMPLATE_NAME     = var.whatsapp_location_template_name
    WHATSAPP_LOCATION_TEMPLATE_LANGUAGE = var.whatsapp_location_template_language
    # Templates for doctors (whatsapp_templates.tf): case offer, incoming patient call (+ portal-only)
    WHATSAPP_OFFER_TEMPLATE_NAME       = local.whatsapp_offer_template_name
    WHATSAPP_CALL_TEMPLATE_NAME        = local.whatsapp_call_template_name
    WHATSAPP_CALL_PORTAL_TEMPLATE_NAME = local.whatsapp_call_portal_template_name
    WEBHOOK_QUEUE_NAME                 = aws_sqs_queue.webhook_queue.name
    WEBHOOK_QUEUE_URL                  = aws_sqs_queue.webhook_queue.id
    WEBHOOK_DLQ_NAME                   = aws_sqs_queue.webhook_dlq.name
    PORTAL_EVENTS_TOPIC_NAME           = aws_sns_topic.portal_events.name
    PORTAL_EVENTS_TOPIC_ARN            = aws_sns_topic.portal_events.arn
    CONNECT_INSTANCE_ID                = local.connect_instance_id
    CONNECT_FLOW_ID                    = local.connect_flow_id
    CONNECT_SOURCE_PHONE_NUMBER        = local.connect_source_phone_number
    PORTAL_BASE_URL                    = local.portal_base_url
    # Doctor case offers (OFFER-1..5): both services start offer rounds (worker: triage and timeouts;
    # API: a portal decline, a doctor coming on duty, a case closed in the portal)
    DOCTOR_OFFER_TIMEOUT_ESI1    = tostring(var.doctor_offer_timeout_seconds.esi1)
    DOCTOR_OFFER_TIMEOUT_ESI2    = tostring(var.doctor_offer_timeout_seconds.esi2)
    DOCTOR_OFFER_TIMEOUT_ESI3    = tostring(var.doctor_offer_timeout_seconds.esi3)
    DOCTOR_OFFER_FANOUT_URGENT   = tostring(var.doctor_offer_fanout_urgent)
    WAITING_CASE_RECHECK_SECONDS = tostring(var.waiting_case_recheck_seconds)
    # No-doctor alerts (ADMIN-1): the worker places the calls (it has the Connect permission)
    ADMIN_ALERT_PHONE_NUMBERS  = join(",", var.admin_alert_phone_numbers)
    ADMIN_ALERT_REPEAT_SECONDS = tostring(var.admin_alert_repeat_seconds)
    ADMIN_ALERT_MAX_CALLS      = tostring(var.admin_alert_max_calls)
    # Doctors on WhatsApp (DOC-*)
    DOCTOR_WHATSAPP_RELAY_ENABLED         = var.doctor_whatsapp_relay_enabled ? "True" : "False"
    DOCTOR_AGENT_MODEL_ID                 = var.doctor_agent_model_id != "" ? var.doctor_agent_model_id : var.bedrock_model_id
    DOCTOR_CASE_CLOSED_GRACE_HOURS        = tostring(var.doctor_case_closed_grace_hours)
    DOCTOR_IDLE_REMINDER_SECONDS          = tostring(var.doctor_idle_reminder_seconds)
    WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME = local.whatsapp_doctor_waiting_template_name
    WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME = local.whatsapp_doctor_confirm_template_name
  }

  # The backend refuses to start without these when MOCK_AWS is false; fail at plan time instead.
  # The Meta access token can only be checked when Terraform writes the secret value.
  whatsapp_provider_ready = (
    var.whatsapp_provider == "eum_social"
    ? var.eum_social_phone_number_id != ""
    : var.whatsapp_phone_number_id != "" && (!var.manage_whatsapp_secret_values || nonsensitive(var.whatsapp_access_token != ""))
  )
  whatsapp_provider_error = "whatsapp_provider = \"${var.whatsapp_provider}\" needs ${var.whatsapp_provider == "eum_social" ? "eum_social_phone_number_id (phone-number-id-... from the End User Messaging Social console)" : "whatsapp_phone_number_id and whatsapp_access_token (TF_VAR_whatsapp_access_token)"}. The backend refuses to start without it when MOCK_AWS is false."
}

# CloudWatch Log Group for the API service
resource "aws_cloudwatch_log_group" "ecs_logs" {
  name              = "/ecs/resq-api"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "resq-ecs-api-logs"
  }
}

# API task: portal REST, WebSocket bridge, video join, Meta webhook (enqueue only)
resource "aws_ecs_task_definition" "resq_task" {
  depends_on = [aws_secretsmanager_secret_version.api_config]

  family                   = "resq-api-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.ecs_task_cpu)
  memory                   = tostring(var.ecs_task_memory)
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.ecs_cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "resq-api"
      image     = local.backend_image
      essential = true
      command = [
        "uvicorn", "app.main:app",
        "--host", "0.0.0.0",
        "--port", tostring(var.app_port),
        "--proxy-headers", "--forwarded-allow-ips", "*"
      ]

      portMappings = [
        {
          containerPort = var.app_port
          hostPort      = var.app_port
          protocol      = "tcp"
        }
      ]

      secrets = local.api_container_secrets

      # Changes when resq/api-config changes, so ECS rolls new tasks that read the new values
      dockerLabels = { "resq.config-hash" = sha1(jsonencode(local.api_app_config)) }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:${var.app_port}/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])

  lifecycle {
    # Register the new revision before deregistering the old one, so a failed apply never
    # leaves the service on a deregistered task definition.
    create_before_destroy = true

    precondition {
      condition     = local.whatsapp_provider_ready
      error_message = local.whatsapp_provider_error
    }
  }
}

# API ECS Fargate Service (behind the ALB)
resource "aws_ecs_service" "resq_service" {
  name            = "resq-api-service"
  cluster         = aws_ecs_cluster.resq_cluster.id
  task_definition = aws_ecs_task_definition.resq_task.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  # Matches the container healthCheck startPeriod: startup (table check, seeding) is not a failed ALB check
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = [aws_subnet.private_1.id, aws_subnet.private_2.id]
    security_groups  = [aws_security_group.ecs_sg.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.resq_api_tg.arn
    container_name   = "resq-api"
    container_port   = var.app_port
  }

  deployment_controller {
    type = "ECS"
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http, aws_lb_listener_rule.from_cloudfront]

  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = {
    Name = "resq-api-service"
  }
}

# API Auto-Scaling Target
resource "aws_appautoscaling_target" "ecs_target" {
  max_capacity       = var.api_max_capacity
  min_capacity       = var.api_min_capacity
  resource_id        = "service/${aws_ecs_cluster.resq_cluster.name}/${aws_ecs_service.resq_service.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# API Auto-Scaling Policy (CPU > 70%)
resource "aws_appautoscaling_policy" "ecs_cpu_policy" {
  name               = "resq-ecs-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs_target.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs_target.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs_target.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
