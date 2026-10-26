resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# 1. Raw Audio Vault Bucket (WhatsApp OGG Opus Voice Notes)
resource "aws_s3_bucket" "audio_vault" {
  bucket        = "resq-raw-audio-vault-${random_id.bucket_suffix.hex}"
  force_destroy = true

  tags = {
    Name       = "resq-audio-vault"
    Purpose    = "Encrypted storage for emergency voice notes"
    Compliance = "HIPAA-Grade"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audio_vault_crypto" {
  bucket = aws_s3_bucket.audio_vault.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "audio_vault_versioning" {
  bucket = aws_s3_bucket.audio_vault.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "audio_vault_block_public" {
  bucket = aws_s3_bucket.audio_vault.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "audio_vault_lifecycle" {
  bucket = aws_s3_bucket.audio_vault.id

  rule {
    id     = "archive-old-recordings"
    status = "Enabled"

    filter {}

    # Instant Retrieval: older voice notes stay playable from the portal (presigned GetObject);
    # Flexible Retrieval ("GLACIER") would need a restore first.
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
  }
}

# 2. Web Portal Static Hosting Bucket (React 19 / Tailwind SPA)
resource "aws_s3_bucket" "portal_bucket" {
  bucket        = "resq-portal-app-${random_id.bucket_suffix.hex}"
  force_destroy = true

  tags = {
    Name    = "resq-portal-app"
    Purpose = "Static web app assets for the admin and doctor portal"
  }
}

resource "aws_s3_bucket_public_access_block" "portal_bucket_block_public" {
  bucket = aws_s3_bucket.portal_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
