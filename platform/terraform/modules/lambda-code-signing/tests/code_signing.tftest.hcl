mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "123456789012"
  }
}

# AWS sets a bucket's id to its name.
override_resource {
  target = aws_s3_bucket.artifacts
  values = {
    id  = "test-eks-lambda-artifacts-123456789012"
    arn = "arn:aws:s3:::test-eks-lambda-artifacts-123456789012"
  }
}

override_resource {
  target = aws_kms_key.artifacts
  values = {
    arn = "arn:aws:kms:us-east-2:123456789012:key/mock-lambda-artifacts"
  }
}

override_resource {
  target = aws_signer_signing_profile.lambda
  values = {
    name        = "test_eks_lambda_mock"
    version_arn = "arn:aws:signer:us-east-2:123456789012:/signing-profiles/test_eks_lambda_mock/abcdefghij"
  }
}

variables {
  name_prefix = "test-eks"
}

run "only_signed_code_deploys_from_a_hardened_bucket" {
  command = apply

  assert {
    condition = (
      aws_lambda_code_signing_config.this.policies[0].untrusted_artifact_on_deployment == "Enforce" &&
      aws_lambda_code_signing_config.this.allowed_publishers[0].signing_profile_version_arns == toset([aws_signer_signing_profile.lambda.version_arn]) &&
      aws_signer_signing_profile.lambda.platform_id == "AWSLambda-SHA384-ECDSA"
    )
    error_message = "Lambda must refuse code that this root's signing profile did not sign."
  }

  assert {
    condition = (
      aws_s3_bucket.artifacts.bucket == "test-eks-lambda-artifacts-123456789012" &&
      aws_s3_bucket_versioning.artifacts.versioning_configuration[0].status == "Enabled" &&
      one(aws_s3_bucket_server_side_encryption_configuration.artifacts.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == aws_kms_key.artifacts.arn &&
      aws_kms_key.artifacts.enable_key_rotation &&
      aws_s3_bucket_public_access_block.artifacts.block_public_acls &&
      aws_s3_bucket_public_access_block.artifacts.block_public_policy &&
      aws_s3_bucket_public_access_block.artifacts.ignore_public_acls &&
      aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets
    )
    error_message = "The artifact bucket is versioned, encrypted with a rotating CMK and private."
  }

  assert {
    condition     = jsondecode(aws_s3_bucket_policy.artifacts.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" && jsondecode(aws_s3_bucket_policy.artifacts.policy).Statement[0].Effect == "Deny"
    error_message = "The artifact bucket refuses requests without TLS."
  }

  assert {
    condition     = output.signing.bucket == aws_s3_bucket.artifacts.bucket && output.signing.code_signing_config_arn == aws_lambda_code_signing_config.this.arn
    error_message = "Consumers receive the versioned bucket and the enforcing configuration."
  }
}
