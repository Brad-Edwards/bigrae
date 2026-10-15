variable "project_id" {
  type        = string
  description = "Existing tenant project hosting private pack objects."
}

variable "bucket_name" {
  type        = string
  description = "Globally unique private pack bucket name."
}

variable "location" {
  type        = string
  description = "Storage location within the tenant's selected region."
}

variable "access_log_bucket_name" {
  type        = string
  description = "Existing terminal GCS bucket that receives private pack access logs."
}

variable "portal_service_account_email" {
  type        = string
  description = "Existing portal identity; receives object access for reads, tenant uploads and cleanup in this bucket only."
}

variable "noncurrent_version_retention_days" {
  type        = number
  default     = 7
  description = "Days a replaced or deleted pack archive generation is kept before the bucket deletes it."

  validation {
    condition     = var.noncurrent_version_retention_days >= 1 && var.noncurrent_version_retention_days <= 365
    error_message = "noncurrent_version_retention_days must be between 1 and 365."
  }
}
