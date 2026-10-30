output "s3_bucket" {
  description = "Bucket of the signed package, for aws_lambda_function.s3_bucket."
  value       = one(one(aws_signer_signing_job.this.signed_object).s3).bucket
}

output "s3_key" {
  description = "Key of the signed package, for aws_lambda_function.s3_key."
  value       = one(one(aws_signer_signing_job.this.signed_object).s3).key
}

output "code_signing_config_arn" {
  description = "Code signing configuration that admits only this root's signed packages."
  value       = var.signing.code_signing_config_arn
}
