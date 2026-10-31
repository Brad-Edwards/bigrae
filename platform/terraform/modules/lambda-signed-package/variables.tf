variable "signing" {
  description = "The signing output of the lambda-code-signing module for this root."
  type = object({
    bucket                  = string
    kms_key_arn             = string
    signing_profile_name    = string
    code_signing_config_arn = string
  })
}

variable "name" {
  description = "Package name; it forms the object keys unsigned/<name>.zip and signed/<name>/."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.name))
    error_message = "name must be lowercase letters, digits or hyphens."
  }
}

variable "package_path" {
  description = "Local path of the unsigned .zip package. Required to create or update the package; teardown may omit it."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.package_path == null || fileexists(var.package_path)
    error_message = "package_path must name an existing package file."
  }
}
