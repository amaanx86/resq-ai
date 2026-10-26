# VPC Resource
resource "aws_vpc" "resq_vpc" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "resq-ai-vpc"
  }
}

# Internet Gateway for Public Ingress/Egress
resource "aws_internet_gateway" "resq_igw" {
  vpc_id = aws_vpc.resq_vpc.id

  tags = {
    Name = "resq-ai-igw"
  }
}

# Public Subnets (AZ-1 and AZ-2)
resource "aws_subnet" "public_1" {
  vpc_id                  = aws_vpc.resq_vpc.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1) # 10.0.1.0/24
  availability_zone       = var.availability_zones[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "resq-public-subnet-1"
    Tier = "Public"
  }
}

resource "aws_subnet" "public_2" {
  vpc_id                  = aws_vpc.resq_vpc.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 2) # 10.0.2.0/24
  availability_zone       = var.availability_zones[1]
  map_public_ip_on_launch = true

  tags = {
    Name = "resq-public-subnet-2"
    Tier = "Public"
  }
}

# Private Application Subnets (AZ-1 and AZ-2) for ECS Fargate
resource "aws_subnet" "private_1" {
  vpc_id            = aws_vpc.resq_vpc.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10) # 10.0.10.0/24
  availability_zone = var.availability_zones[0]

  tags = {
    Name = "resq-private-subnet-1"
    Tier = "Private"
  }
}

resource "aws_subnet" "private_2" {
  vpc_id            = aws_vpc.resq_vpc.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 20) # 10.0.20.0/24
  availability_zone = var.availability_zones[1]

  tags = {
    Name = "resq-private-subnet-2"
    Tier = "Private"
  }
}

# Elastic IPs for NAT Gateways
resource "aws_eip" "nat_1" {
  domain     = "vpc"
  depends_on = [aws_internet_gateway.resq_igw]

  tags = {
    Name = "resq-nat-eip-1"
  }
}

resource "aws_eip" "nat_2" {
  count = var.single_nat_gateway ? 0 : 1

  domain     = "vpc"
  depends_on = [aws_internet_gateway.resq_igw]

  tags = {
    Name = "resq-nat-eip-2"
  }
}

# One NAT per AZ, or a single shared NAT in AZ 1 when single_nat_gateway is set
resource "aws_nat_gateway" "nat_gw_1" {
  allocation_id = aws_eip.nat_1.id
  subnet_id     = aws_subnet.public_1.id

  tags = {
    Name = "resq-nat-gw-1"
  }
}

resource "aws_nat_gateway" "nat_gw_2" {
  count = var.single_nat_gateway ? 0 : 1

  allocation_id = aws_eip.nat_2[0].id
  subnet_id     = aws_subnet.public_2.id

  tags = {
    Name = "resq-nat-gw-2"
  }
}

# Public Route Table
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.resq_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.resq_igw.id
  }

  tags = {
    Name = "resq-public-rt"
  }
}

resource "aws_route_table_association" "public_1" {
  subnet_id      = aws_subnet.public_1.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_2" {
  subnet_id      = aws_subnet.public_2.id
  route_table_id = aws_route_table.public.id
}

# Private Route Tables (routed via respective NAT Gateway per AZ)
resource "aws_route_table" "private_1" {
  vpc_id = aws_vpc.resq_vpc.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_gw_1.id
  }

  tags = {
    Name = "resq-private-rt-1"
  }
}

resource "aws_route_table" "private_2" {
  vpc_id = aws_vpc.resq_vpc.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.nat_gw_1.id : aws_nat_gateway.nat_gw_2[0].id
  }

  tags = {
    Name = "resq-private-rt-2"
  }
}

resource "aws_route_table_association" "private_1" {
  subnet_id      = aws_subnet.private_1.id
  route_table_id = aws_route_table.private_1.id
}

resource "aws_route_table_association" "private_2" {
  subnet_id      = aws_subnet.private_2.id
  route_table_id = aws_route_table.private_2.id
}
