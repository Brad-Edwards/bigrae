# Deployment configuration supplies the immutable GitHub IDs (ADR-069). No default.
variable "github_repository_id" {
  description = "Immutable GitHub repository ID verified before bootstrap."
  type        = string
  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.github_repository_id))
    error_message = "github_repository_id must be a positive numeric GitHub ID."
  }
}
variable "github_owner_id" {
  description = "Immutable GitHub owner ID verified before bootstrap."
  type        = string
  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.github_owner_id))
    error_message = "github_owner_id must be a positive numeric GitHub ID."
  }
}

variable "github_subject_format" {
  description = "Explicit GitHub subject format; immutable IDs are the default after rename."
  type        = string
  default     = "immutable"
  validation {
    condition     = contains(["default", "immutable"], var.github_subject_format)
    error_message = "github_subject_format must be default or immutable."
  }
}
