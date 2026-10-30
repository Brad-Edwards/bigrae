# ------------------------------------------------------------------------------
# Lambda code signing for one Terraform root (#2313)
# ------------------------------------------------------------------------------
# AWS Signer signs each deployment package, and the code signing configuration
# makes Lambda refuse any deployment that this root's profile did not sign.
# Signer reads the unsigned package from a versioned S3 bucket and writes the
# signed package to the same bucket, so functions deploy from S3. Each function
# uses the lambda-signed-package module with this module's outputs.
# ------------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "artifacts" {
  description             = "Lambda deployment artifacts for ${var.name_prefix}"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-lambda-artifacts"
  })
}

resource "aws_kms_alias" "artifacts" {
  name          = "alias/${var.name_prefix}-lambda-artifacts"
  target_key_id = aws_kms_key.artifacts.key_id
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name_prefix}-lambda-artifacts-${data.aws_caller_identity.current.account_id}"
  # Packages are reproducible build output, so teardown may delete every version.
  force_destroy = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-lambda-artifacts"
  })
}

# Signer requires a versioned source bucket.
resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.artifacts.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  # A deployed function may be recreated from its current signed object, so
  # only replaced versions expire.
  rule {
    id     = "expire-replaced-artifacts"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

resource "aws_signer_signing_profile" "lambda" {
  platform_id = "AWSLambda-SHA384-ECDSA"
  # Profile names allow only letters, digits and underscores.
  name_prefix = "${replace(var.name_prefix, "-", "_")}_lambda_"

  tags = var.tags
}

resource "aws_lambda_code_signing_config" "this" {
  description = "Code signed by the ${var.name_prefix} Lambda signing profile"

  allowed_publishers {
    signing_profile_version_arns = [aws_signer_signing_profile.lambda.version_arn]
  }

  policies {
    untrusted_artifact_on_deployment = "Enforce"
  }

  tags = var.tags
}
