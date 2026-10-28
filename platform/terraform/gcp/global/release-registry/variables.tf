variable "project_id" {
  description = "GCP project that hosts the release registry."
  type        = string
}

variable "project_number" {
  description = "Numeric project number; the Artifact Registry service agent uses the registry encryption key."
  type        = string
  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.project_number))
    error_message = "project_number must be a numeric project number."
  }
}

variable "region" {
  description = "Artifact Registry location of the release repository."
  type        = string
  default     = "us-central1"
}

variable "name_prefix" {
  description = "Prefix of the release repository and its encryption key; the repository is <name_prefix>-release."
  type        = string
  default     = "bigrae"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,40}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be a lowercase DNS-label prefix."
  }
}

variable "writer_members" {
  description = "Identities that publish releases and base guest images: the release publisher and the image build identities."
  type        = set(string)
  validation {
    condition = length(var.writer_members) > 0 && alltrue([
      for member in var.writer_members : can(regex("^serviceAccount:[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", member))
    ])
    error_message = "writer_members must name only service accounts; people never push releases."
  }
}

variable "reader_members" {
  description = "Identities that read releases: deployment deploy and release-scan identities and the operators who verify the deployer image."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([
      for member in var.reader_members :
      can(regex("^(user|group|serviceAccount):[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+$", member))
    ])
    error_message = "reader_members must contain only user:, group:, or serviceAccount: members."
  }
}
