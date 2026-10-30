# ------------------------------------------------------------------------------
# One signed Lambda deployment package (#2313)
# ------------------------------------------------------------------------------
# The unsigned package becomes a new object version when its content changes.
# A new version starts a new Signer job, and the function deploys the signed
# object that the job writes. A function that uses these outputs keeps a valid
# code source without the local file, so a teardown does not need the package.
# ------------------------------------------------------------------------------

resource "aws_s3_object" "unsigned" {
  bucket      = var.signing.bucket
  key         = "unsigned/${var.name}.zip"
  source      = var.package_path
  source_hash = var.package_path == null ? null : filemd5(var.package_path)
  kms_key_id  = var.signing.kms_key_arn

  lifecycle {
    # Preconditions do not run for a destroy, so teardown may omit the file.
    precondition {
      condition     = var.package_path != null
      error_message = "package_path is required to create or update a signed package."
    }
  }
}

resource "aws_signer_signing_job" "this" {
  profile_name = var.signing.signing_profile_name

  source {
    s3 {
      bucket  = aws_s3_object.unsigned.bucket
      key     = aws_s3_object.unsigned.key
      version = aws_s3_object.unsigned.version_id
    }
  }

  destination {
    s3 {
      bucket = var.signing.bucket
      prefix = "signed/${var.name}/"
    }
  }

  ignore_signing_job_failure = false
}
