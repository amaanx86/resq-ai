# CloudFront origin-facing IP ranges. The rule using it counts as ~55 rules against the
# security group rule quota (default 60), so keep it the ALB group's only ingress rule.
data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# Application Load Balancer Security Group: reachable only from CloudFront (which also adds the
# X-Origin-Verify header checked by the listener in alb.tf)
resource "aws_security_group" "alb_sg" {
  name        = "resq-alb-sg"
  description = "Security group for the internet-facing ALB, CloudFront origin traffic only"
  vpc_id      = aws_vpc.resq_vpc.id

  ingress {
    description     = "HTTP from CloudFront origin-facing servers"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront_origin_facing.id]
  }

  egress {
    description = "Forward requests and health checks to the API tasks"
    from_port   = var.app_port
    to_port     = var.app_port
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = {
    Name = "resq-alb-sg"
  }
}

# ECS Fargate Tasks Security Group
resource "aws_security_group" "ecs_sg" {
  name        = "resq-ecs-tasks-sg"
  description = "Security group for private ECS Fargate tasks"
  vpc_id      = aws_vpc.resq_vpc.id

  ingress {
    description     = "FastAPI port, from the ALB only"
    from_port       = var.app_port
    to_port         = var.app_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg.id]
  }

  egress {
    description = "Allow all outbound traffic for AWS API calls & NAT egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "resq-ecs-sg"
  }
}

# ECS worker tasks and the AgentCore Runtime in VPC mode: no inbound traffic, outbound only
resource "aws_security_group" "worker_sg" {
  name        = "resq-worker-tasks-sg"
  description = "Security group for the private ECS worker tasks (no inbound)"
  vpc_id      = aws_vpc.resq_vpc.id

  egress {
    description = "Allow all outbound traffic for AWS API calls & NAT egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "resq-worker-sg"
  }
}

# VPC Endpoints Security Group
resource "aws_security_group" "vpce_sg" {
  name        = "resq-vpc-endpoints-sg"
  description = "Security group for PrivateLink VPC Endpoints"
  vpc_id      = aws_vpc.resq_vpc.id

  ingress {
    description     = "Allow HTTPS from ECS tasks to AWS PrivateLink endpoints"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_sg.id, aws_security_group.worker_sg.id]
  }

  egress {
    description = "Allow outbound response traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "resq-vpce-sg"
  }
}
