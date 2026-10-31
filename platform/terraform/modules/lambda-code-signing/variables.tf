variable "name_prefix" {
  description = "Prefix for the artifact bucket, key alias and signing profile, for example shifter-dev-eks or dev-portal."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase letters, digits or hyphens."
  }
}

variable "noncurrent_version_expiration_days" {
  description = "Days to keep replaced artifact versions before deletion."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
