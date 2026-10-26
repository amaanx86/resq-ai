# Single-Table DynamoDB Architecture: ResQTable
resource "aws_dynamodb_table" "resq_table" {
  name         = "ResQTable"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  # Primary Key Attributes
  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # Global Secondary Index 1: Doctor Specialty & Active Status
  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  # Global Secondary Index 2: Monthly Emergency Analytics & Cases
  attribute {
    name = "GSI2PK"
    type = "S"
  }

  attribute {
    name = "GSI2SK"
    type = "S"
  }

  global_secondary_index {
    name = "GSI_SpecialtyActive"
    key_schema {
      attribute_name = "GSI1PK"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "GSI1SK"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  global_secondary_index {
    name = "GSI_Analytics"
    key_schema {
      attribute_name = "GSI2PK"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "GSI2SK"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  # Idempotency records (msg:/tool: keys) and direct-call links carry an epoch-seconds `ttl` attribute and expire on their own
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name        = "ResQTable"
    Description = "Single-table store for users doctors triage cases and chat messages"
  }
}
