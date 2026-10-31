mock_provider "aws" {}

override_resource {
  target = aws_signer_signing_job.this
  values = {
    signed_object = [{ s3 = [{ bucket = "test-lambda-artifacts", key = "signed/rotation/mock-job.zip" }] }]
  }
}

variables {
  signing = {
    bucket                  = "test-lambda-artifacts"
    kms_key_arn             = "arn:aws:kms:us-east-2:123456789012:key/mock-lambda-artifacts"
    signing_profile_name    = "shifter_test_lambda_mock"
    code_signing_config_arn = "arn:aws:lambda:us-east-2:123456789012:code-signing-config:csc-0123456789abcdef0"
  }
  name         = "rotation"
  package_path = "tests/fixtures/package.zip"
}

run "signs_the_exact_uploaded_version" {
  command = apply

  assert {
    condition = (
      aws_s3_object.unsigned.key == "unsigned/rotation.zip" &&
      aws_s3_object.unsigned.kms_key_id == var.signing.kms_key_arn &&
      aws_s3_object.unsigned.source_hash == filemd5("tests/fixtures/package.zip")
    )
    error_message = "The unsigned package is uploaded once per content, encrypted with the artifact key."
  }

  assert {
    condition = (
      aws_signer_signing_job.this.profile_name == "shifter_test_lambda_mock" &&
      one(one(aws_signer_signing_job.this.source).s3).version == aws_s3_object.unsigned.version_id &&
      one(one(aws_signer_signing_job.this.destination).s3).prefix == "signed/rotation/" &&
      aws_signer_signing_job.this.ignore_signing_job_failure == false
    )
    error_message = "Signer signs the exact uploaded version with this root's profile and fails closed."
  }

  assert {
    condition     = output.s3_key == "signed/rotation/mock-job.zip" && output.code_signing_config_arn == var.signing.code_signing_config_arn
    error_message = "Functions deploy the signed object under the root's code signing configuration."
  }
}

run "package_required_to_create_or_update" {
  command = plan
  variables {
    package_path = null
  }
  # Teardown may omit the package, but no plan may upload or sign without it.
  expect_failures = [aws_s3_object.unsigned]
}
