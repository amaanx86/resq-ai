# ====================================================
# GITLAB CI OIDC (image build + ECS redeploy + portal upload)
# ====================================================
# GitLab jobs exchange their id_token for short-lived credentials; no access keys are stored
# in CI variables. The trust policy pins the issuer, the project path and the deploy branch,
# so another project or branch on the same GitLab cannot assume the role.
#
# The role can push the two images, redeploy the two ECS services, read the portal deploy
# settings from SSM, upload the portal to its bucket and invalidate CloudFront. Infrastructure
# changes, including a new AgentCore runtime image tag, still go through terraform apply.

resource "aws_iam_openid_connect_provider" "gitlab" {
  count = var.ci_oidc_enabled ? 1 : 0

  url            = var.gitlab_url
  client_id_list = [var.ci_oidc_audience]

  tags = {
    Name = "resq-gitlab-oidc"
  }
}

data "aws_iam_policy_document" "ci_deploy_trust" {
  count = var.ci_oidc_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.gitlab[0].arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.gitlab_host}:aud"
      values   = [var.ci_oidc_audience]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.gitlab_host}:sub"
      values   = ["project_path:${var.gitlab_project_path}:ref_type:branch:ref:${var.ci_deploy_branch}"]
    }
  }
}

resource "aws_iam_role" "ci_deploy" {
  count = var.ci_oidc_enabled ? 1 : 0

  name                 = "resq-gitlab-ci-deploy"
  description          = "GitLab CI (${var.gitlab_project_path}@${var.ci_deploy_branch}): push images, redeploy ECS, upload the portal"
  assume_role_policy   = data.aws_iam_policy_document.ci_deploy_trust[0].json
  max_session_duration = 3600

  tags = {
    Name = "resq-gitlab-ci-deploy"
  }
}

data "aws_iam_policy_document" "ci_deploy" {
  count = var.ci_oidc_enabled ? 1 : 0

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      aws_ecr_repository.resq_backend.arn,
      aws_ecr_repository.agentcore_runtime.arn,
    ]
  }

  statement {
    sid     = "EcsRedeploy"
    actions = ["ecs:UpdateService", "ecs:DescribeServices"]
    resources = [
      aws_ecs_service.resq_service.id,
      aws_ecs_service.worker.id,
    ]
  }

  statement {
    sid       = "PortalDeploySettings"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.portal_deploy[0].arn]
  }

  statement {
    sid       = "PortalBucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.portal_bucket.arn]
  }

  statement {
    sid       = "PortalBucketUpload"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.portal_bucket.arn}/*"]
  }

  statement {
    sid       = "PortalInvalidate"
    actions   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
    resources = [aws_cloudfront_distribution.portal_distribution.arn]
  }
}

# What the portal job needs, kept in Terraform so CI never hard-codes IDs: the Vite build env
# (none of it is secret, it ends up in the public bundle), the bucket and the distribution.
resource "aws_ssm_parameter" "portal_deploy" {
  count = var.ci_oidc_enabled ? 1 : 0

  name        = "/resq/ci/portal-deploy"
  description = "Portal build env, bucket and CloudFront distribution for the GitLab portal job"
  type        = "String"
  value = jsonencode({
    build_env       = local.vite_build_env
    bucket          = aws_s3_bucket.portal_bucket.id
    distribution_id = aws_cloudfront_distribution.portal_distribution.id
  })

  tags = {
    Name = "resq-ci-portal-deploy"
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  count = var.ci_oidc_enabled ? 1 : 0

  name   = "resq-gitlab-ci-deploy"
  role   = aws_iam_role.ci_deploy[0].id
  policy = data.aws_iam_policy_document.ci_deploy[0].json
}
