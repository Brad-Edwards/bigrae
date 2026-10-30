output "signing" {
  description = "Inputs for lambda-signed-package and aws_lambda_function.code_signing_config_arn."
  value = {
    # Referencing the versioning resource makes uploads wait until versioning is on.
    bucket                  = aws_s3_bucket_versioning.artifacts.bucket
    kms_key_arn             = aws_kms_key.artifacts.arn
    signing_profile_name    = aws_signer_signing_profile.lambda.name
    code_signing_config_arn = aws_lambda_code_signing_config.this.arn
  }
}
