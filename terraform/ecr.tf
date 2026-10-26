# ====================================================
# AMAZON ECR REPOSITORIES (INFRA-3, AGENT-2)
# ====================================================
# resq-backend: one image for both ECS services (API: uvicorn, worker: python -m app.worker).
# resq-agentcore-runtime: the ARM64 Strands agent image served by Bedrock AgentCore Runtime.
# Push both images before the first full `terraform apply` (see outputs.tf for the URLs).

resource "aws_ecr_repository" "resq_backend" {
  name                 = "resq-backend"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name    = "resq-backend"
    Purpose = "FastAPI API and SQS worker image"
  }
}

resource "aws_ecr_repository" "agentcore_runtime" {
  name                 = "resq-agentcore-runtime"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name    = "resq-agentcore-runtime"
    Purpose = "ARM64 Strands triage agent image for Bedrock AgentCore Runtime"
  }
}

locals {
  ecr_lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_keep_image_count
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "resq_backend" {
  repository = aws_ecr_repository.resq_backend.name
  policy     = local.ecr_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "agentcore_runtime" {
  repository = aws_ecr_repository.agentcore_runtime.name
  policy     = local.ecr_lifecycle_policy
}
