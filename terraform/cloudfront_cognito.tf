# ====================================================
# AMAZON COGNITO AUTHENTICATION (ADMIN & DOCTOR PORTAL, UI-2)
# ====================================================
# Users are created by an administrator (no self sign-up). Doctors are created by the API when an
# administrator registers them in the portal (backend/app/services/cognito_service.py): Doctors
# group, custom:doctor_id = their DOCTOR#<id> record ID (the API only shows them their own cases),
# and Cognito emails the invitation below. Administrators are added the same way from the portal's
# Administrators tab (ADMIN-2, backend/app/routers/admins.py: "Administrators" group, the same
# invitation email, a phone number for the no-doctor alert calls); the first one is still added by
# hand, to the "Administrators" group.
locals {
  portal_temporary_password_days = 7

  # Build-time settings of the portal (Vite inlines VITE_* into the bundle). Read by
  # `terraform output vite_build_env` and, through SSM, by the GitLab portal job (ci_oidc.tf).
  vite_build_env = {
    VITE_AUTH_ENABLED         = var.auth_enabled ? "true" : "false"
    VITE_COGNITO_DOMAIN       = local.cognito_hosted_ui_url
    VITE_COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.portal_client.id
    VITE_COGNITO_USER_POOL_ID = aws_cognito_user_pool.portal_pool.id
    VITE_COGNITO_REDIRECT_URI = "${local.portal_base_url}/"
    VITE_COGNITO_LOGOUT_URI   = "${local.portal_base_url}/"
    VITE_AWS_REGION           = var.aws_region
  }
}

resource "aws_cognito_user_pool" "portal_pool" {
  name = "resq-portal-user-pool"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true

    # Invitation with the temporary password, sent by Cognito's own mailer (COGNITO_DEFAULT:
    # no-reply@verificationemail.com, 50 emails a day). SES would allow our own sender, but the
    # account's SES is still in the sandbox (verified recipients only).
    invite_message_template {
      email_subject = "Your ResQ-AI portal invitation"
      email_message = replace(templatefile("${path.module}/templates/portal_invite_email.html", {
        portal_url              = "${local.portal_base_url}/"
        temporary_password_days = local.portal_temporary_password_days
      }), "\r", "") # same text from a Windows (CRLF) checkout
      sms_message = "ResQ-AI portal invitation. Username {username}, temporary password {####}"
    }
  }

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = local.portal_temporary_password_days
  }

  schema {
    attribute_data_type = "String"
    name                = "role"
    mutable             = true
    required            = false

    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }

  schema {
    attribute_data_type = "String"
    name                = "specialty"
    mutable             = true
    required            = false

    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }

  # Links a portal user to their doctor record (read by backend/app/auth.py)
  schema {
    attribute_data_type = "String"
    name                = "doctor_id"
    mutable             = true
    required            = false

    string_attribute_constraints {
      min_length = 0
      max_length = 256
    }
  }

  tags = {
    Name = "resq-cognito-pool"
  }
}

# Hosted UI domain: https://<prefix>.auth.<region>.amazoncognito.com
resource "aws_cognito_user_pool_domain" "portal_domain" {
  domain                = local.cognito_domain_prefix
  user_pool_id          = aws_cognito_user_pool.portal_pool.id
  managed_login_version = 1 # classic Hosted UI, no branding resource needed
}

# Public SPA client: authorization code + PKCE through the Hosted UI
resource "aws_cognito_user_pool_client" "portal_client" {
  name         = "resq-portal-web-client"
  user_pool_id = aws_cognito_user_pool.portal_pool.id

  generate_secret = false
  # The portal signs in through the Hosted UI (code + PKCE); no plain-password API flow
  explicit_auth_flows = [
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH"
  ]

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = local.cognito_callback_urls
  logout_urls                          = local.cognito_callback_urls

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 30
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }

  # The ID token carries these. custom:doctor_id and custom:role decide what a user may see, so
  # users can read them but not change them; only administrators set them (AdminUpdateUserAttributes).
  read_attributes  = ["email", "email_verified", "name", "custom:doctor_id", "custom:role", "custom:specialty"]
  write_attributes = ["email", "name"]
}

resource "aws_cognito_user_group" "admins" {
  name         = "Administrators"
  user_pool_id = aws_cognito_user_pool.portal_pool.id
  description  = "Hospital Administrators with access to analytics and doctor management"
}

resource "aws_cognito_user_group" "doctors" {
  name         = "Doctors"
  user_pool_id = aws_cognito_user_pool.portal_pool.id
  description  = "On-duty doctors with access to active cases and video consultation console"
}

# ====================================================
# AMAZON CLOUDFRONT CDN (REACT SPA + /api and /ws PROXY TO THE ALB)
# ====================================================
# The portal calls relative /api/v1 and /ws paths, so CloudFront routes them to the ALB and
# serves everything else from the S3 bucket. This also makes PORTAL_BASE_URL https, which the
# browser requires for camera/microphone on the /call/... video page.
resource "aws_cloudfront_origin_access_control" "portal_oac" {
  name                              = "resq-portal-oac"
  description                       = "Origin Access Control for ResQ Web Portal"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  # Forwards every viewer header (incl. Authorization and the WebSocket upgrade headers),
  # cookies and query strings (the WebSocket ?token=...) to the ALB.
  name = "Managed-AllViewer"
}

# SPA routing: extension-less paths (/, /call/<meeting>, ...) get index.html. Done here instead
# of distribution-wide 403/404 custom error responses, which would also turn API 404/403
# responses (e.g. "no audio for this case") into a 200 index.html.
resource "aws_cloudfront_function" "spa_rewrite" {
  name    = "resq-portal-spa-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "Serve index.html for client-side routes of the ResQ portal"
  publish = true
  # No carriage returns: a Windows checkout (CRLF) must plan the same as everyone else.
  code = replace(<<-EOT
    function handler(event) {
      var request = event.request;
      var last = request.uri.split('/').pop();
      if (last === '' || last.indexOf('.') === -1) {
        request.uri = '/index.html';
      }
      return request;
    }
  EOT
  , "\r", "")
}

resource "aws_cloudfront_distribution" "portal_distribution" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "ResQ-AI portal (S3) + API/WebSocket proxy (ALB)"

  origin {
    domain_name              = aws_s3_bucket.portal_bucket.bucket_regional_domain_name
    origin_id                = "S3-resq-portal"
    origin_access_control_id = aws_cloudfront_origin_access_control.portal_oac.id
  }

  origin {
    domain_name = aws_lb.resq_alb.dns_name
    origin_id   = "ALB-resq-api"

    # The ALB listener only forwards requests carrying this header (alb.tf)
    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "S3-resq-portal"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite.arn
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "ALB-resq-api"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    viewer_protocol_policy   = "redirect-to-https"
    compress                 = true
  }

  # API liveness check through CloudFront (the ALB itself only accepts CloudFront traffic)
  ordered_cache_behavior {
    path_pattern             = "/healthz"
    target_origin_id         = "ALB-resq-api"
    allowed_methods          = ["GET", "HEAD"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    viewer_protocol_policy   = "redirect-to-https"
  }

  ordered_cache_behavior {
    path_pattern             = "/ws/*"
    target_origin_id         = "ALB-resq-api"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    viewer_protocol_policy   = "https-only"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name = "resq-portal-cdn"
  }
}

# Allow CloudFront OAC to read from the Portal S3 bucket
resource "aws_s3_bucket_policy" "portal_bucket_policy" {
  bucket = aws_s3_bucket.portal_bucket.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOAC"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.portal_bucket.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.portal_distribution.arn
          }
        }
      }
    ]
  })
}
