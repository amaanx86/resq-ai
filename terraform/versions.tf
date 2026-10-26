terraform {
  required_version = ">= 1.10.0"

  # terraform init -backend-config=backend.hcl
  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.68 is the version this module was validated against. It ships the Bedrock AgentCore
      # resources used in agentcore_*.tf (agent runtime, gateway, gateway target with Lambda
      # tool schemas).
      version = "~> 6.68"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = var.aws_account_id == null ? null : [var.aws_account_id]

  default_tags {
    tags = {
      Project     = "ResQ-AI"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Repository  = "aws-hackathon"
    }
  }
}
