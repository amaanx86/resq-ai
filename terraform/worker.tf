# ====================================================
# ECS WORKER SERVICE (INFRA-6)
# ====================================================
# Same image as the API, command `python -m app.worker`. Long-polls resq-webhook-queue and runs
# the pipeline: onboarding/handover routing, S3 + Transcribe, AgentCore triage, case persistence,
# WhatsApp replies. No load balancer; scales on the queue's ApproximateNumberOfMessagesVisible.

resource "aws_cloudwatch_log_group" "worker_logs" {
  name              = "/ecs/resq-worker"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "resq-ecs-worker-logs"
  }
}

resource "aws_ecs_task_definition" "worker" {
  depends_on = [aws_secretsmanager_secret_version.worker_config]

  family                   = "resq-worker-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.worker_task_cpu)
  memory                   = tostring(var.worker_task_memory)
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.worker_task_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.ecs_cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "resq-worker"
      image     = local.backend_image
      essential = true
      command   = ["python", "-m", "app.worker"]
      # Time for the worker to finish (or release) the in-flight message after SIGTERM
      stopTimeout = 60

      secrets = local.worker_container_secrets

      dockerLabels = { "resq.config-hash" = sha1(jsonencode(local.worker_app_config)) }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
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

resource "aws_ecs_service" "worker" {
  name            = "resq-worker-service"
  cluster         = aws_ecs_cluster.resq_cluster.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_min_capacity
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.private_1.id, aws_subnet.private_2.id]
    security_groups  = [aws_security_group.worker_sg.id]
    assign_public_ip = false
  }

  deployment_controller {
    type = "ECS"
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = {
    Name = "resq-worker-service"
  }
}

# ---------------- Autoscaling on queue depth ----------------
resource "aws_appautoscaling_target" "worker" {
  max_capacity       = var.worker_max_capacity
  min_capacity       = var.worker_min_capacity
  resource_id        = "service/${aws_ecs_cluster.resq_cluster.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Scale out in steps relative to worker_scale_out_queue_depth
resource "aws_appautoscaling_policy" "worker_scale_out" {
  name               = "resq-worker-queue-scale-out"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 20
      scaling_adjustment          = 1
    }

    step_adjustment {
      metric_interval_lower_bound = 20
      metric_interval_upper_bound = 100
      scaling_adjustment          = 2
    }

    step_adjustment {
      metric_interval_lower_bound = 100
      scaling_adjustment          = 4
    }
  }
}

resource "aws_appautoscaling_policy" "worker_scale_in" {
  name               = "resq-worker-queue-scale-in"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_queue_high" {
  alarm_name          = "resq-webhook-queue-backlog-high"
  alarm_description   = "Scale out the worker service: resq-webhook-queue backlog is growing"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.webhook_queue.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = var.worker_scale_out_queue_depth
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_scale_out.arn]
}

# Scale in only when nothing is waiting AND nothing is in flight, so a worker is not stopped while
# it is still triaging a message (the pipeline can take longer than the 60 s stopTimeout).
resource "aws_cloudwatch_metric_alarm" "worker_queue_empty" {
  alarm_name          = "resq-webhook-queue-backlog-empty"
  alarm_description   = "Scale in the worker service: resq-webhook-queue has had no visible or in-flight messages for 5 minutes"
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_scale_in.arn]

  metric_query {
    id          = "backlog"
    expression  = "visible + in_flight"
    label       = "Visible + in-flight messages"
    return_data = true
  }

  metric_query {
    id = "visible"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.webhook_queue.name }
      stat        = "Maximum"
      period      = 60
    }
  }

  metric_query {
    id = "in_flight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = aws_sqs_queue.webhook_queue.name }
      stat        = "Maximum"
      period      = 60
    }
  }
}
