variable "project_id" {
  description = "GCP project that hosts the Workload Identity pool and packer build service account."
  type        = string
}

variable "environment" {
  description = "Environment name (e.g. gcp-dev)."
  type        = string
}

variable "region" {
  description = "Default provider region."
  type        = string
  default     = "us-central1"
}

variable "github_org" {
  description = "GitHub organization that owns the repository allowed to federate."
  type        = string
  # Default is read only by the static CKV_GCP_125 trust-grammar check, which is
  # mandatory and un-skippable (check_tf_gcp_wif_trust). Real deployments set this
  # in their per-environment tfvars. The immutable github_repository_id and
  # github_owner_id are the binding anchor and have no default, so a fork or
  # rename cannot federate even though this name has a default (ADR-069).
  default = "Brad-Edwards"
}

variable "github_repo" {
  description = "GitHub repository allowed to federate into the build service account."
  type        = string
  # Default is read only by the static CKV_GCP_125 trust-grammar check, which is
  # mandatory and un-skippable (check_tf_gcp_wif_trust). Real deployments set this
  # in their per-environment tfvars. The immutable github_repository_id and
  # github_owner_id are the binding anchor and have no default, so a fork or
  # rename cannot federate even though this name has a default (ADR-069).
  default = "bigrae"
}

variable "build_read_bucket_names" {
  description = "Existing GCS input buckets readable by the Packer build identity."
  type        = set(string)
  default     = []
}

variable "terraform_state_bucket_name" {
  description = "Explicit deployment-owned GCS backend bucket receiving scoped deploy/destroy access."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.terraform_state_bucket_name))
    error_message = "terraform_state_bucket_name must name the separately owned platform state bucket."
  }
}

variable "platform_external_bucket_names" {
  description = "Existing RAES/CTF content buckets whose workload IAM is managed by platform-core."
  type        = set(string)
  default     = []
}

variable "github_subject_format" {
  description = "Reviewed GitHub default subject format: legacy names or immutable owner/repository IDs."
  type        = string
  default     = "immutable"
  validation {
    condition     = contains(["default", "immutable"], var.github_subject_format)
    error_message = "github_subject_format must be default or immutable."
  }
}

variable "impersonation_principals" {
  description = "Principals that may impersonate each lifecycle purpose identity without GitHub (release_scan, deploy, destroy)."
  type        = map(list(string))
  default     = {}
}

variable "deployment_config_bucket_name" {
  description = "Bucket for the GitHub-less deployment input bundle. An empty value creates no bucket."
  type        = string
  default     = ""
}

variable "enable_gcs_usage_log_delivery" {
  description = <<-EOT
    Grant the Google-managed group cloud-storage-analytics@google.com objectCreator
    on the access-log prefix of the release-evidence bucket for usage logs of the
    deployment config bucket. A Domain Restricted Sharing org policy
    (iam.allowedPolicyMemberDomains) forbids the google.com group and fails the
    binding with Error 412. Cloud Audit Logs record config bucket access either way.
  EOT
  type        = bool
  default     = false
}
