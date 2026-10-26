# ====================================================
# AMAZON BEDROCK AGENTCORE GATEWAY: TRIAGE TOOLS OVER MCP (AGENT-2)
# ====================================================
# Inbound auth AWS_IAM: the runtime signs MCP requests with SigV4 using its role
# (bedrock-agentcore:InvokeGateway in agentcore_runtime.tf). One Lambda target,
# resq-gateway-tools, implements alert_emergency_contact, match_specialist_doctor and
# generate_video_session. There is no ambulance tool: the doctor calls the ambulance. The Lambda receives the tool name as
# "<target name>___<tool name>" (target name: resq-gateway-tools).
#
# Inputs produced by the agent stream:
#   zip:     python backend/scripts/build_gateway_lambda.py -> backend/build/gateway_tools.zip
#   schemas: backend/gateway_tools/tool_schemas.json, a JSON list (or {"tools": [...]}) of
#            {name, description, inputSchema: {type, properties, required}} entries.

locals {
  gateway_lambda_zip   = can(regex("^(/|[A-Za-z]:)", var.gateway_lambda_zip_path)) ? var.gateway_lambda_zip_path : "${path.module}/${var.gateway_lambda_zip_path}"
  gateway_tool_schemas = can(regex("^(/|[A-Za-z]:)", var.gateway_tool_schema_path)) ? var.gateway_tool_schema_path : "${path.module}/${var.gateway_tool_schema_path}"

  gateway_tool_schema_raw = jsondecode(file(local.gateway_tool_schemas))
  gateway_tools           = try(local.gateway_tool_schema_raw.tools, local.gateway_tool_schema_raw)

  gateway_target_name = "resq-gateway-tools"
}

# ---------------- Tools Lambda ----------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gateway_tools_lambda" {
  name               = "resq-gateway-tools-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = {
    Name = "resq-gateway-tools-lambda-role"
  }
}

resource "aws_iam_role_policy" "gateway_tools_lambda" {
  name = "resq-gateway-tools"
  role = aws_iam_role.gateway_tools_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.gateway_tools_lambda.arn}:log-stream:*"]
      },
      local.dynamodb_statement,
      local.dynamodb_list_statement,
      {
        Sid      = "EmergencyContactCall"
        Effect   = "Allow"
        Action   = ["connect:StartOutboundVoiceContact"]
        Resource = ["${local.connect_instance_arn}/*"]
      },
      local.chime_statement
    ]
  })
}

resource "aws_cloudwatch_log_group" "gateway_tools_lambda" {
  name              = "/aws/lambda/resq-gateway-tools"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "resq-gateway-tools-logs"
  }
}

resource "aws_lambda_function" "gateway_tools" {
  function_name    = "resq-gateway-tools"
  description      = "ResQ-AI triage tools behind the AgentCore Gateway"
  role             = aws_iam_role.gateway_tools_lambda.arn
  handler          = "gateway_tools.handler.lambda_handler"
  runtime          = var.gateway_lambda_runtime
  architectures    = [var.gateway_lambda_architecture]
  filename         = local.gateway_lambda_zip
  source_code_hash = filebase64sha256(local.gateway_lambda_zip)
  timeout          = var.gateway_lambda_timeout
  memory_size      = var.gateway_lambda_memory

  environment {
    # AWS_REGION is provided by Lambda (reserved key)
    variables = {
      ENVIRONMENT                 = var.environment
      MOCK_AWS                    = "False"
      SEED_DEMO_DATA              = "False"
      DYNAMODB_TABLE_NAME         = aws_dynamodb_table.resq_table.name
      S3_AUDIO_BUCKET             = aws_s3_bucket.audio_vault.id
      CONNECT_INSTANCE_ID         = local.connect_instance_id
      CONNECT_FLOW_ID             = local.connect_flow_id
      CONNECT_SOURCE_PHONE_NUMBER = local.connect_source_phone_number
      PORTAL_BASE_URL             = local.portal_base_url
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.gateway_tools_lambda.name
  }

  tags = {
    Name = "resq-gateway-tools"
  }

  depends_on = [aws_iam_role_policy.gateway_tools_lambda]
}

# ---------------- Gateway ----------------
resource "aws_iam_role" "agentcore_gateway" {
  name               = "resq-agentcore-gateway-role"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json

  tags = {
    Name = "resq-agentcore-gateway-role"
  }
}

resource "aws_iam_role_policy" "agentcore_gateway" {
  name = "resq-agentcore-gateway-invoke-tools"
  role = aws_iam_role.agentcore_gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeToolsLambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.gateway_tools.arn, "${aws_lambda_function.gateway_tools.arn}:*"]
      }
    ]
  })
}

resource "aws_bedrockagentcore_gateway" "tools" {
  name            = "resq-tools-gateway"
  description     = "MCP gateway exposing the ResQ-AI triage tools to the AgentCore runtime"
  role_arn        = aws_iam_role.agentcore_gateway.arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"

  tags = {
    Name = "resq-tools-gateway"
  }
}

resource "aws_bedrockagentcore_gateway_target" "tools_lambda" {
  name               = local.gateway_target_name
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  description        = "Lambda resq-gateway-tools: emergency contact call, specialist match, video session"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.gateway_tools.arn

        tool_schema {
          dynamic "inline_payload" {
            for_each = local.gateway_tools
            iterator = tool
            content {
              name        = tool.value.name
              description = tool.value.description

              input_schema {
                type        = try(tool.value.inputSchema.type, tool.value.input_schema.type, "object")
                description = try(tool.value.inputSchema.description, tool.value.input_schema.description, null)

                dynamic "property" {
                  for_each = try(tool.value.inputSchema.properties, tool.value.input_schema.properties, {})
                  iterator = prop
                  content {
                    name        = prop.key
                    type        = prop.value.type
                    description = try(prop.value.description, null)
                    required    = contains(try(tool.value.inputSchema.required, tool.value.input_schema.required, []), prop.key)

                    # Arrays: one level of item schema (arrays of objects get leaf properties)
                    dynamic "items" {
                      for_each = prop.value.type == "array" ? [try(prop.value.items, { type = "string" })] : []
                      content {
                        type        = items.value.type
                        description = try(items.value.description, null)

                        dynamic "property" {
                          for_each = items.value.type == "object" ? try(items.value.properties, {}) : {}
                          iterator = item_prop
                          content {
                            name            = item_prop.key
                            type            = item_prop.value.type
                            description     = try(item_prop.value.description, null)
                            required        = contains(try(items.value.required, []), item_prop.key)
                            items_json      = item_prop.value.type == "array" ? jsonencode(try(item_prop.value.items, {})) : null
                            properties_json = item_prop.value.type == "object" ? jsonencode({ properties = try(item_prop.value.properties, {}), required = try(item_prop.value.required, []) }) : null
                          }
                        }
                      }
                    }

                    # Objects: nested properties, deeper levels JSON-encoded
                    dynamic "property" {
                      for_each = prop.value.type == "object" ? try(prop.value.properties, {}) : {}
                      iterator = sub
                      content {
                        name            = sub.key
                        type            = sub.value.type
                        description     = try(sub.value.description, null)
                        required        = contains(try(prop.value.required, []), sub.key)
                        items_json      = sub.value.type == "array" ? jsonencode(try(sub.value.items, {})) : null
                        properties_json = sub.value.type == "object" ? jsonencode({ properties = try(sub.value.properties, {}), required = try(sub.value.required, []) }) : null
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.agentcore_gateway]
}

# Gateway application logs go to the same AgentCore log group as the runtime
resource "aws_cloudwatch_log_delivery_source" "gateway_application_logs" {
  name         = "resq-tools-gateway-logs"
  log_type     = "APPLICATION_LOGS"
  resource_arn = aws_bedrockagentcore_gateway.tools.gateway_arn
}

resource "aws_cloudwatch_log_delivery" "gateway_application_logs" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.gateway_application_logs.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.agentcore_logs.arn

  depends_on = [
    aws_cloudwatch_log_resource_policy.agentcore_log_delivery,
    aws_cloudwatch_log_delivery.runtime_application_logs
  ]
}
