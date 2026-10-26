# Application Load Balancer. Internet-facing so CloudFront can reach it, but only CloudFront can:
# the security group admits the CloudFront origin-facing prefix list and the listener forwards
# only requests carrying CloudFront's secret X-Origin-Verify header. Viewers get TLS at
# CloudFront; CloudFront -> ALB is HTTP because the ALB has no certificate (no custom domain).
resource "aws_lb" "resq_alb" {
  name               = "resq-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg.id]
  subnets            = [aws_subnet.public_1.id, aws_subnet.public_2.id]

  enable_deletion_protection = false
  drop_invalid_header_fields = true
  # Long-lived WebSocket bridge connections (/ws/...); clients also send pings
  idle_timeout = 300

  tags = {
    Name = "resq-alb"
  }
}

# Target Group for ECS Fargate FastAPI Service
resource "aws_lb_target_group" "resq_api_tg" {
  name        = "resq-api-tg"
  port        = var.app_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.resq_vpc.id
  target_type = "ip" # Required for Fargate awsvpc network mode

  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/healthz"
    port                = "traffic-port"
    protocol            = "HTTP"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  tags = {
    Name = "resq-api-tg"
  }
}

# Shared secret CloudFront sends to the ALB (origin custom header in cloudfront_cognito.tf)
resource "random_password" "origin_verify" {
  length  = 32
  special = false
}

# HTTP listener: rejects anything that did not come through CloudFront
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.resq_alb.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "from_cloudfront" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 1

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.resq_api_tg.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify.result]
    }
  }
}
