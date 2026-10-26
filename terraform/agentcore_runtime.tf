# ====================================================
# AMAZON BEDROCK AGENTCORE RUNTIME: STRANDS TRIAGE AGENT (AGENT-2)
# ====================================================
# The ARM64 image (backend/agent_runtime/Dockerfile) serves the AgentCore HTTP contract on
# port 8080 (/invocations, /ping). The worker calls it with bedrock-agentcore
# invoke_agent_runtime; the agent reaches its tools through the AgentCore Gateway
# (agentcore_gateway.tf), signing MCP requests with SigV4 using this runtime's role.

data "aws_iam_policy_document" "agentcore_assume" {
  statement {
    sid     = "AssumeRolePolicy"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "agentcore_role" {
  name               = "resq-agentcore-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json

  tags = {
    Name    = "resq-agentcore-runtime-role"
    Purpose = "Execution role for the Bedrock AgentCore triage runtime"
  }
}

resource "aws_iam_role_policy" "agentcore_runtime" {
  name = "resq-agentcore-runtime"
  role = aws_iam_role.agentcore_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ECRImageAccess"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [aws_ecr_repository.agentcore_runtime.arn]
      },
      {
        Sid      = "ECRTokenAccess"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "RuntimeLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogStreams", "logs:CreateLogGroup"]
        Resource = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
      },
      {
        Sid      = "DescribeLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
      },
      {
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
          "${aws_cloudwatch_log_group.agentcore_logs.arn}:log-stream:*"
        ]
      },
      {
        Sid    = "XRayTraces"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      },
      {
        Sid      = "Metrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" }
        }
      },
      {
        Sid    = "GetAgentAccessToken"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
        ]
        Resource = [
          "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default",
          "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${var.agentcore_runtime_name}-*"
        ]
      },
      {
        Sid      = "BedrockModelInvocation"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = local.bedrock_model_arns
      },
      {
        Sid      = "InvokeToolsGateway"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeGateway"]
        Resource = [aws_bedrockagentcore_gateway.tools.gateway_arn]
      }
      # No DynamoDB access: the runtime image ships only app/config, app/models and app/agents;
      # every read/write happens in the Gateway Lambda tools or the worker.
      ],
      # Patient facts for the triage agent (read only; the worker and API write events)
      local.memory_read_statements
    )
  })
}

# Observability: runtime and gateway application logs are delivered here (vended logs).
resource "aws_cloudwatch_log_group" "agentcore_logs" {
  name              = "/aws/bedrock/agentcore/resq-triage"
  retention_in_days = var.log_retention_days

  tags = {
    Name    = "resq-agentcore-logs"
    Service = "Amazon Bedrock AgentCore"
  }
}

resource "aws_bedrockagentcore_agent_runtime" "triage" {
  agent_runtime_name = var.agentcore_runtime_name
  description        = "ResQ-AI Strands triage agent (ESI 1-5) with tools on the AgentCore Gateway"
  role_arn           = aws_iam_role.agentcore_role.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agentcore_image
    }
  }

  environment_variables = {
    AWS_REGION                = var.aws_region
    ENVIRONMENT               = var.environment
    BEDROCK_MODEL_ID          = var.bedrock_model_id
    BEDROCK_FALLBACK_MODEL_ID = var.bedrock_fallback_model_id
    AGENTCORE_GATEWAY_URL     = aws_bedrockagentcore_gateway.tools.gateway_url
    AGENTCORE_MEMORY_ID       = local.agentcore_memory_id
    # Inside the runtime the agent itself runs in-process (never re-invoke AgentCore)
    AGENT_BACKEND  = "local"
    MOCK_AWS       = "False"
    SEED_DEMO_DATA = "False"
  }

  network_configuration {
    network_mode = var.agentcore_network_mode

    dynamic "network_mode_config" {
      for_each = var.agentcore_network_mode == "VPC" ? [1] : []
      content {
        subnets         = [aws_subnet.private_1.id, aws_subnet.private_2.id]
        security_groups = [aws_security_group.worker_sg.id]
      }
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  tags = {
    Name = "resq-triage-runtime"
  }

  depends_on = [aws_iam_role_policy.agentcore_runtime]
}

# ---------------- Vended log delivery to /aws/bedrock/agentcore/resq-triage ----------------
resource "aws_cloudwatch_log_resource_policy" "agentcore_log_delivery" {
  policy_name = "resq-agentcore-log-delivery"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSLogDeliveryWrite"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = ["${aws_cloudwatch_log_group.agentcore_logs.arn}:log-stream:*"]
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:*" }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_log_delivery_destination" "agentcore_logs" {
  name = "resq-agentcore-logs"

  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.agentcore_logs.arn
  }
}

resource "aws_cloudwatch_log_delivery_source" "runtime_application_logs" {
  name         = "resq-triage-runtime-logs"
  log_type     = "APPLICATION_LOGS"
  resource_arn = aws_bedrockagentcore_agent_runtime.triage.agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery" "runtime_application_logs" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.runtime_application_logs.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.agentcore_logs.arn

  depends_on = [aws_cloudwatch_log_resource_policy.agentcore_log_delivery]
}
