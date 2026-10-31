variable "project_id" {
  description = "GCP project that hosts this environment's WIF provider and purpose identities."
  type        = string
}

variable "environment" {
  description = "Deployment ID, independent of installation profile and purpose."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$", var.environment))
    error_message = "environment must be a DNS-label-safe deployment ID."
  }
}

variable "name_prefix" {
  description = "Resource name prefix (for example shifter-gcp-dev)."
  type        = string
}

variable "region" {
  description = "Region for the private release-evidence bucket."
  type        = string
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
  description = "GitHub repository allowed to federate into the purpose identities."
  type        = string
  # Default is read only by the static CKV_GCP_125 trust-grammar check, which is
  # mandatory and un-skippable (check_tf_gcp_wif_trust). Real deployments set this
  # in their per-environment tfvars. The immutable github_repository_id and
  # github_owner_id are the binding anchor and have no default, so a fork or
  # rename cannot federate even though this name has a default (ADR-069).
  default = "bigrae"
}

variable "build_roles" {
  description = "Predefined roles exercised by the Packer build/export identity."
  type        = list(string)
  default = [
    "roles/compute.instanceAdmin.v1",
    "roles/compute.storageAdmin",
    "roles/iap.tunnelResourceAccessor",
    "roles/cloudbuild.builds.editor",
  ]
}

variable "build_read_bucket_names" {
  description = "Existing platform image input buckets the Packer build identity may read."
  type        = set(string)
  default     = []
}

variable "validate_roles" {
  description = "Predefined roles exercised by the no-SA validation VM path."
  type        = list(string)
  default = [
    "roles/iap.tunnelResourceAccessor",
  ]
}

variable "validate_permissions" {
  description = "Custom-role permissions for exact-candidate validation."
  type        = list(string)
  default = [
    "compute.disks.create",
    "compute.disks.delete",
    "compute.disks.get",
    "compute.disks.use",
    "compute.disks.useReadOnly",
    "compute.images.get",
    "compute.images.getFromFamily",
    "compute.images.setLabels",
    "compute.images.useReadOnly",
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.attachDisk",
    "compute.instances.get",
    "compute.instances.list",
    "compute.instances.reset",
    # Both candidate and scanner creation supply instance-local SSH metadata
    # and lifecycle labels; Compute checks these permissions at insert time.
    "compute.instances.setLabels",
    "compute.instances.setMetadata",
    "compute.instances.setTags",
    "compute.machineTypes.get",
    "compute.networks.get",
    "compute.networks.use",
    # gcloud SSH/SCP inspect project settings for the credentialless scanner.
    "compute.projects.get",
    "compute.subnetworks.get",
    "compute.subnetworks.use",
    "compute.zoneOperations.get",
    "compute.zones.get",
    "resourcemanager.projects.get",
    "serviceusage.services.use",
  ]
}

variable "deploy_roles" {
  description = "Platform-core roles granted only to the deploy identity."
  type        = list(string)
  default = [
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/gkehub.editor",
    "roles/gkehub.gatewayEditor",
    "roles/gkehub.viewer",
    "roles/container.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/servicenetworking.networksAdmin",
    "roles/dns.admin",
    "roles/cloudsql.admin",
    "roles/redis.admin",
    "roles/pubsub.admin",
    "roles/secretmanager.admin",
    "roles/cloudkms.admin",
    "roles/artifactregistry.admin",
    "roles/identityplatform.admin",
    "roles/monitoring.editor",
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    # Read the range guest images: gcp-deploy checks that each image in
    # range-images.json exists before the rollout (#2608).
    "roles/compute.imageUser",
    # Manage the dynamic-secret custom role DEFINITIONS created by the platform-core
    # deploy (portal/iam: shifterDynamicSecretCreator / shifterDynamicSecretLifecycle).
    # projectIamAdmin covers IAM bindings but not iam.roles.* on custom-role
    # definitions. Tracked for a least-privilege redesign (bootstrap-created roles,
    # deploy binds only) in #2227.
    "roles/iam.roleAdmin",
  ]
}

variable "destroy_roles" {
  description = "Platform-core teardown roles granted only to the destroy identity."
  type        = list(string)
  default = [
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/gkehub.editor",
    "roles/gkehub.gatewayEditor",
    "roles/gkehub.viewer",
    "roles/container.admin",
    "roles/servicenetworking.networksAdmin",
    "roles/dns.admin",
    "roles/cloudsql.admin",
    "roles/redis.admin",
    "roles/pubsub.admin",
    "roles/secretmanager.admin",
    "roles/cloudkms.admin",
    "roles/artifactregistry.admin",
    "roles/identityplatform.admin",
    "roles/monitoring.editor",
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    # Teardown must DELETE the dynamic-secret custom role definitions (portal/iam:
    # shifterDynamicSecretCreator / shifterDynamicSecretLifecycle) and the packer
    # image-export custom role. projectIamAdmin manages IAM bindings, not iam.roles.*
    # on custom-role definitions, so destroy fails with IAM_PERMISSION_DENIED reading
    # projects/<p>/roles/<custom>. Mirrors the deploy_roles grant above (#2258, #2228).
    "roles/iam.roleAdmin",
  ]
}

variable "deploy_identity_platform_permissions" {
  description = "Permissions that identityPlatform:initializeAuth needs beyond roles/identityplatform.admin on a project without Firebase."
  type        = list(string)
  default = [
    "firebase.projects.get",
    "firebase.projects.update",
  ]
}

variable "deploy_storage_permissions" {
  description = "Storage permissions for Terraform-managed platform buckets, bound only to their deterministic resource names."
  type        = list(string)
  default = [
    "storage.buckets.create",
    "storage.buckets.delete",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
    "storage.buckets.update",
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
    "storage.objects.update",
  ]
}

variable "destroy_storage_permissions" {
  description = "Storage permissions needed to tear down Terraform-managed platform buckets, bound only to their deterministic resource names."
  type        = list(string)
  default = [
    "storage.buckets.delete",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
  ]
}

variable "deploy_vpn_pool_permissions" {
  description = "Compute permissions for the shared OpenVPN pool (#2480); VM, disk and template permissions are conditioned to the pool's names."
  type        = list(string)
  default = [
    "compute.autoscalers.create",
    "compute.autoscalers.delete",
    "compute.autoscalers.get",
    "compute.autoscalers.update",
    # regionInstanceGroupManagers.insert/patch check these on the caller; the
    # Compute service agent then creates the VMs from the template.
    "compute.disks.create",
    "compute.instances.create",
    "compute.instances.setLabels",
    "compute.instances.setMetadata",
    "compute.instances.setServiceAccount",
    "compute.instances.setTags",
    "compute.instanceGroupManagers.create",
    "compute.instanceGroupManagers.delete",
    "compute.instanceGroupManagers.get",
    "compute.instanceGroupManagers.update",
    "compute.instanceGroupManagers.use",
    "compute.instanceGroups.delete",
    "compute.instanceTemplates.create",
    "compute.instanceTemplates.delete",
    "compute.instanceTemplates.get",
    "compute.instanceTemplates.list",
    "compute.instanceTemplates.useReadOnly",
  ]
}

variable "destroy_vpn_pool_permissions" {
  description = "Compute permissions to tear down the shared OpenVPN pool (#2480), conditioned to the pool's names."
  type        = list(string)
  default = [
    "compute.autoscalers.delete",
    "compute.autoscalers.get",
    "compute.instanceGroupManagers.delete",
    "compute.instanceGroupManagers.get",
    "compute.instanceGroups.delete",
    "compute.instanceTemplates.delete",
    "compute.instanceTemplates.get",
  ]
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
  description = "Existing content buckets whose IAM memberships are managed by platform-core; each receives exact bucket-level lifecycle IAM administration."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for name in var.platform_external_bucket_names :
      length(trimspace(name)) > 0 && lower(name) != lower(var.release_evidence_bucket_name)
    ])
    error_message = "platform_external_bucket_names must contain non-empty names and must not include this identity root's release-evidence bucket."
  }
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
  description = "Principals that may impersonate each lifecycle purpose identity (roles/iam.serviceAccountTokenCreator). A listed purpose identity exists without GitHub federation (ADR-070)."
  type        = map(list(string))
  default     = {}

  validation {
    condition = alltrue([
      for purpose, members in var.impersonation_principals :
      contains(["release_scan", "deploy", "destroy"], purpose) && length(members) > 0 && alltrue([
        for member in members :
        can(regex("^(user|group|serviceAccount):[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+$", member))
      ])
    ])
    error_message = "impersonation_principals must map release_scan, deploy, or destroy to non-empty lists of user:, group:, or serviceAccount: members."
  }
}

variable "deployment_config_bucket_name" {
  description = "Bucket for the GitHub-less deployment input bundle (ADR-070). An empty value creates no bucket."
  type        = string
  default     = ""

  validation {
    condition = var.deployment_config_bucket_name == "" || (
      can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.deployment_config_bucket_name)) &&
      lower(var.deployment_config_bucket_name) != lower(var.release_evidence_bucket_name) &&
      lower(var.deployment_config_bucket_name) != lower(var.terraform_state_bucket_name)
    )
    error_message = "deployment_config_bucket_name must be empty or a valid bucket name that differs from the release-evidence and state buckets."
  }
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

variable "image_builder_enabled" {
  description = "Create the image-builder identity for a foundation image-build network. A federated build purpose also creates it (#2679)."
  type        = bool
  default     = false
}
