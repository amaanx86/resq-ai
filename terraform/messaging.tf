# ====================================================
# INBOUND WHATSAPP: END USER MESSAGING SOCIAL -> SNS -> SQS -> WORKER (INFRA-6, INFRA-7)
# ====================================================

# SNS topic for inbound WhatsApp events. Set it as the WABA's event destination in the
# End User Messaging Social console (manual step).
resource "aws_sns_topic" "whatsapp_inbound" {
  name = "resq-whatsapp-inbound-events"

  tags = {
    Name    = "resq-whatsapp-inbound-events"
    Purpose = "Inbound message notifications from AWS End User Messaging Social"
  }
}

# Lets AWS End User Messaging Social publish events to the topic (the service publishes with
# its service principal; same-account IAM principals need no statement here).
resource "aws_sns_topic_policy" "whatsapp_inbound" {
  arn = aws_sns_topic.whatsapp_inbound.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEndUserMessagingSocialPublish"
        Effect    = "Allow"
        Principal = { Service = "social-messaging.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.whatsapp_inbound.arn
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })
}

# SQS dead letter queue for messages the worker failed to process max_receive_count times
resource "aws_sqs_queue" "webhook_dlq" {
  name                      = "resq-webhook-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "resq-webhook-dlq"
  }
}

# Inbound queue consumed by the worker. Fed by the SNS topic above (End User Messaging Social)
# and directly by the API (Meta webhook rollback route and the portal simulator).
resource "aws_sqs_queue" "webhook_queue" {
  name                       = "resq-webhook-queue"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = var.webhook_queue_visibility_timeout
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.webhook_dlq.arn
    maxReceiveCount     = var.webhook_queue_max_receive_count
  })

  tags = {
    Name = "resq-webhook-queue"
  }
}

# SNS to SQS subscription (SNS envelope kept; the worker's parser accepts raw or wrapped events)
resource "aws_sns_topic_subscription" "whatsapp_to_sqs" {
  topic_arn = aws_sns_topic.whatsapp_inbound.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.webhook_queue.arn

  depends_on = [aws_sqs_queue_policy.webhook_queue_policy]
}

# Allow SNS to send messages to SQS
resource "aws_sqs_queue_policy" "webhook_queue_policy" {
  queue_url = aws_sqs_queue.webhook_queue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "sns.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.webhook_queue.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.whatsapp_inbound.arn
          }
        }
      }
    ]
  })
}

# ====================================================
# PORTAL EVENTS FAN-OUT (BRIDGE-1)
# ====================================================
# The worker and the API publish case/chat events here. Each API task creates its own
# resq-api-events-<host>-<random> queue at startup, subscribes it, and deletes it on shutdown
# (app/services/events_service.py). Queues of tasks that were killed without a clean shutdown
# stay behind and can be deleted by hand.
resource "aws_sns_topic" "portal_events" {
  name = "resq-portal-events"

  tags = {
    Name    = "resq-portal-events"
    Purpose = "Worker to API fan-out for the portal WebSocket bridge"
  }
}

# ====================================================
# OPERATIONS ALERTS
# ====================================================
# SNS topic for operations alerts (the DLQ alarm below). Addresses in var.ops_alert_emails are
# subscribed here; other endpoints (SMS, chat) can be added by hand.
resource "aws_sns_topic" "emergency_alerts" {
  name = "resq-emergency-alerts"

  tags = {
    Name    = "resq-emergency-alerts"
    Purpose = "Operations alerts: inbound WhatsApp messages that failed processing"
  }
}

resource "aws_sns_topic_subscription" "ops_alert_email" {
  for_each = toset(var.ops_alert_emails)

  topic_arn = aws_sns_topic.emergency_alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# A message in the DLQ is an inbound WhatsApp message (possibly an emergency) that was never processed.
resource "aws_cloudwatch_metric_alarm" "webhook_dlq_not_empty" {
  alarm_name          = "resq-webhook-dlq-not-empty"
  alarm_description   = "Inbound WhatsApp messages failed processing and landed in resq-webhook-dlq"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.webhook_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.emergency_alerts.arn]
}
