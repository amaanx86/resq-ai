# ====================================================
# VPC ENDPOINTS (INFRA-5)
# ====================================================
# Keeps AWS API traffic from the private subnets off the NAT gateways. Gateway endpoints
# (S3, DynamoDB) are free; each interface endpoint is billed per AZ-hour, so trim
# var.interface_endpoint_services or set enable_interface_endpoints = false to save cost.
# Calls to services without an endpoint here (End User Messaging Social, AgentCore, Chime,
# Connect, Meta Graph API) still leave through the NAT gateways.

resource "aws_vpc_endpoint" "s3" {
  count = var.enable_vpc_endpoints ? 1 : 0

  vpc_id            = aws_vpc.resq_vpc.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private_1.id, aws_route_table.private_2.id]

  tags = {
    Name = "resq-vpce-s3"
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = var.enable_vpc_endpoints ? 1 : 0

  vpc_id            = aws_vpc.resq_vpc.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private_1.id, aws_route_table.private_2.id]

  tags = {
    Name = "resq-vpce-dynamodb"
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = var.enable_vpc_endpoints && var.enable_interface_endpoints ? toset(var.interface_endpoint_services) : toset([])

  vpc_id              = aws_vpc.resq_vpc.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private_1.id, aws_subnet.private_2.id]
  security_group_ids  = [aws_security_group.vpce_sg.id]
  private_dns_enabled = true

  tags = {
    Name = "resq-vpce-${replace(each.value, ".", "-")}"
  }
}
