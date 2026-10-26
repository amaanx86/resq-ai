# ====================================================
# ECS IAM: one execution role, separate task roles for the API and the worker (INFRA-6)
# ====================================================

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# 1. ECS Task Execution Role (image pull, logs, secrets injection)
resource "aws_iam_role" "ecs_execution_role" {
  name               = "resq-ecs-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_standard" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "resq-ecs-execution-secrets"
  role = aws_iam_role.ecs_execution_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadServiceSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.whatsapp.arn,
          aws_secretsmanager_secret.api_config.arn,
          aws_secretsmanager_secret.worker_config.arn,
        ]
      }
    ]
  })
}

# Statements shared by both task roles
locals {
  dynamodb_statement = {
    Sid    = "ResQTable"
    Effect = "Allow"
    Action = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:ConditionCheckItem",
      "dynamodb:Query",
      "dynamodb:Scan"
    ]
    Resource = [
      aws_dynamodb_table.resq_table.arn,
      "${aws_dynamodb_table.resq_table.arn}/index/*"
    ]
  }

  # dynamo_service checks for the table with ListTables at startup
  dynamodb_list_statement = {
    Sid      = "ListTables"
    Effect   = "Allow"
    Action   = ["dynamodb:ListTables"]
    Resource = "*"
  }

  chime_statement = {
    Sid    = "ChimeSdkMeetings"
    Effect = "Allow"
    Action = [
      "chime:CreateMeeting",
      "chime:CreateAttendee",
      "chime:BatchCreateAttendee",
      "chime:GetMeeting",
      "chime:ListAttendees",
      "chime:DeleteMeeting"
    ]
    Resource = "*"
  }

  send_whatsapp_statement = {
    Sid      = "EndUserMessagingSocialSend"
    Effect   = "Allow"
    Action   = ["social-messaging:SendWhatsAppMessage"]
    Resource = [local.social_messaging_phone_arn]
  }

  publish_portal_events_statement = {
    Sid      = "PublishPortalEvents"
    Effect   = "Allow"
    Action   = ["sns:Publish"]
    Resource = [aws_sns_topic.portal_events.arn]
  }
}

# 2. API task role: portal REST, WebSocket bridge, video join, Meta webhook -> queue
resource "aws_iam_role" "ecs_task_role" {
  name               = "resq-ecs-api-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = {
    Name    = "resq-ecs-api-task-role"
    Service = "api"
  }
}

resource "aws_iam_role_policy" "api_task_policy" {
  name = "resq-api-least-privilege"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      local.dynamodb_statement,
      local.dynamodb_list_statement,
      # Presigned playback URLs for voice notes (UI-1) and photos (IMAGE-1)
      {
        Sid      = "AudioVaultRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.audio_vault.arn}/*"]
      },
      # An administrator deletes a closed case with its voice notes and photos (DELETE /cases/{id})
      {
        Sid    = "AudioVaultDeleteCaseMedia"
        Effect = "Allow"
        Action = ["s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.audio_vault.arn}/voice-notes/*",
          "${aws_s3_bucket.audio_vault.arn}/images/*",
          "${aws_s3_bucket.audio_vault.arn}/outbound/*"
        ]
      },
      # Photos and voice notes a doctor sends from the portal (POST /cases/{id}/messages/media)
      {
        Sid      = "AudioVaultWriteOutboundMedia"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.audio_vault.arn}/outbound/*"]
      },
      local.chime_statement,
      # Doctor chat replies and video invites to the patient
      local.send_whatsapp_statement,
      # The doctor's photo / voice note is uploaded to WhatsApp from the vault before it is sent
      {
        Sid      = "UploadOutboundWhatsAppMedia"
        Effect   = "Allow"
        Action   = ["social-messaging:PostWhatsAppMessageMedia"]
        Resource = [local.social_messaging_phone_arn]
      },
      # Meta webhook route and the portal simulator publish to the inbound queue
      {
        Sid      = "EnqueueInboundMessages"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
        Resource = [aws_sqs_queue.webhook_queue.arn]
      },
      local.publish_portal_events_statement,
      {
        Sid      = "SubscribeTaskQueueToPortalEvents"
        Effect   = "Allow"
        Action   = ["sns:Subscribe", "sns:Unsubscribe"]
        Resource = [aws_sns_topic.portal_events.arn]
      },
      # Registering a doctor or adding an administrator (ADMIN-2) creates their portal user and sends
      # the invitation; removing an administrator takes them out of the group (cognito_service.py)
      {
        Sid    = "InviteDoctorsToPortal"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminUpdateUserAttributes",
          "cognito-idp:AdminAddUserToGroup",
          "cognito-idp:AdminRemoveUserFromGroup",
          "cognito-idp:AdminDeleteUser"
        ]
        Resource = [aws_cognito_user_pool.portal_pool.arn]
      },
      # Per-task fan-out queue lifecycle (create + policy on startup, poll, delete on shutdown)
      {
        Sid    = "PerTaskEventQueues"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
          "sqs:SetQueueAttributes",
          "sqs:TagQueue",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:DeleteMessageBatch",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = [local.api_events_queue_arn_pattern]
      }
      ],
      # Doctor chat messages -> patient memory (agentcore_memory.tf)
      local.memory_write_statements
    )
  })
}

# 3. Worker task role: queue consumer, speech, AgentCore invocation, WhatsApp replies
resource "aws_iam_role" "worker_task_role" {
  name               = "resq-ecs-worker-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = {
    Name    = "resq-ecs-worker-task-role"
    Service = "worker"
  }
}

resource "aws_iam_role_policy" "worker_task_policy" {
  name = "resq-worker-least-privilege"
  role = aws_iam_role.worker_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # SendMessage: internal tasks the worker schedules for itself (offer timeouts; re-queued
      # tasks whose delay is over the 900 s SQS limit), see app/tasks.py.
      {
        Sid    = "ConsumeInboundQueue"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:DeleteMessageBatch",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes"
        ]
        Resource = [aws_sqs_queue.webhook_queue.arn]
      },
      local.dynamodb_statement,
      local.dynamodb_list_statement,
      # Voice notes and photos (IMAGE-1): Transcribe and GetWhatsAppMessageMedia both use the
      # caller's credentials; the image agent reads the photo back for the vision model.
      # outbound/: photos and voice notes a doctor sends from their WhatsApp (app/doctor_chat.py)
      # are stored there and PostWhatsAppMessageMedia reads them back with the caller's credentials
      {
        Sid    = "AudioVaultReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.audio_vault.arn}/voice-notes/*",
          "${aws_s3_bucket.audio_vault.arn}/images/*",
          "${aws_s3_bucket.audio_vault.arn}/outbound/*"
        ]
      },
      {
        Sid      = "AudioVaultList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.audio_vault.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["voice-notes/*", "images/*"] }
        }
      },
      {
        Sid      = "Transcribe"
        Effect   = "Allow"
        Action   = ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"]
        Resource = "*"
      },
      {
        Sid    = "EndUserMessagingSocial"
        Effect = "Allow"
        Action = [
          "social-messaging:SendWhatsAppMessage",
          "social-messaging:GetWhatsAppMessageMedia",
          "social-messaging:PostWhatsAppMessageMedia"
        ]
        Resource = [local.social_messaging_phone_arn]
      },
      {
        Sid    = "InvokeTriageAgent"
        Effect = "Allow"
        Action = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = [
          aws_bedrockagentcore_agent_runtime.triage.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.triage.agent_runtime_arn}/runtime-endpoint/*"
        ]
      },
      local.publish_portal_events_statement,
      # Degraded path: if AgentCore fails the worker runs the same agent and tools in-process.
      # Also reading emergency-contact registrations from free text (app/registration.py) and
      # describing the patient's photos (app/agents/image_agent.py, same models)
      {
        Sid      = "BedrockFallbackModels"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = local.bedrock_model_arns
      },
      {
        Sid      = "ConnectFallbackCalls"
        Effect   = "Allow"
        Action   = ["connect:StartOutboundVoiceContact"]
        Resource = ["${local.connect_instance_arn}/*"]
      },
      local.chime_statement
      ],
      # Patient memory: events from the pipeline, facts for the in-process fallback agent
      local.memory_read_statements,
      local.memory_write_statements
    )
  })
}
