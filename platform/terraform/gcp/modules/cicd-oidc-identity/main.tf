# Purpose-scoped GCP identities. GitHub Actions reaches them through optional
# Workload Identity Federation; operators reach the lifecycle identities through
# service-account impersonation by named principals only (ADR-004-R23, ADR-070).

locals {
  # An empty purpose_contexts map disables GitHub federation: no pool, no
  # provider, and no federated binding exist.
  federation_enabled = length(var.purpose_contexts) > 0
  build_enabled      = contains(keys(var.purpose_contexts), "build")
  # A tenant install never builds images (#2679): the builder identity exists
  # only for a federated build purpose or a foundation image-build network.
  builder_enabled      = local.build_enabled || var.image_builder_enabled
  validate_enabled     = contains(keys(var.purpose_contexts), "validate")
  promote_enabled      = contains(keys(var.purpose_contexts), "promote")
  release_scan_enabled = contains(keys(var.purpose_contexts), "release_scan") || contains(keys(var.impersonation_principals), "release_scan")
  deploy_enabled       = contains(keys(var.purpose_contexts), "deploy") || contains(keys(var.impersonation_principals), "deploy")
  destroy_enabled      = contains(keys(var.purpose_contexts), "destroy") || contains(keys(var.impersonation_principals), "destroy")

  service_account_ids = {
    build        = "${replace(var.name_prefix, "-", "")}-packer"
    validate     = "${replace(var.name_prefix, "-", "")}-validate"
    promote      = "${replace(var.name_prefix, "-", "")}-promote"
    release_scan = "${replace(var.name_prefix, "-", "")}-scan"
    deploy       = "${replace(var.name_prefix, "-", "")}-deploy"
    destroy      = "${replace(var.name_prefix, "-", "")}-destroy"
  }
  service_account_emails = {
    for purpose, account_id in local.service_account_ids : purpose => "${account_id}@${var.project_id}.iam.gserviceaccount.com"
  }
  service_account_names = {
    for purpose, email in local.service_account_emails : purpose => "projects/${var.project_id}/serviceAccounts/${email}"
  }
  subject_prefix = var.github_subject_format == "immutable" ? "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repository_id}" : "repo:${var.github_org}/${var.github_repo}"
  purpose_subjects = {
    for purpose in ["build", "validate", "promote", "release_scan", "deploy", "destroy"] :
    purpose => distinct([for context in lookup(var.purpose_contexts, purpose, []) : "${local.subject_prefix}:environment:${context.environment}"])
  }
  federated_subjects = toset(flatten(values(local.purpose_subjects)))
  purpose_subject_principals = {
    for purpose, subjects in local.purpose_subjects : purpose => {
      for sub in subjects :
      sub => "principal://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${var.name_prefix}-github/subject/${sub}"
    }
  }
  terraform_state_bucket_name = var.terraform_state_bucket_name
  platform_storage_bucket_names = toset([
    lower("${var.project_id}-${replace(var.environment, "_", "-")}-assets"),
    lower("${var.project_id}-${replace(var.environment, "_", "-")}-audit-logs"),
    lower("${var.project_id}-${var.name_prefix}-gdc-vm-images"),
    # daisy export scratch bucket created by packer-build-infra
    # (google_storage_bucket.daisy_export_scratch, name fixed by daisy as
    # <project>-daisy-bkt-<region>); the platform-core deploy/destroy create and
    # tear it down, so it must be in the conditioned platform-storage allow-list.
    lower("${var.project_id}-daisy-bkt-${var.region}"),
  ])
  platform_storage_condition = join(" || ", concat(
    [for bucket in local.platform_storage_bucket_names : "resource.name == 'projects/_/buckets/${bucket}'"],
    [for bucket in local.platform_storage_bucket_names : "resource.name.startsWith('projects/_/buckets/${bucket}/objects/')"],
  ))
  # Shared participant OpenVPN pool (#2480). Compute supports IAM Conditions on
  # instances, disks and instance templates, so those are limited to the pool's
  # <prefix>-vpn- names: deploy can run the pool but cannot create, re-image or
  # inject metadata into any other VM (runners, range hosts) or template.
  vpn_pool_name_prefix = "${var.name_prefix}-vpn-"
  vpn_pool_condition = join(" || ", [
    "(resource.type != 'compute.googleapis.com/Instance' && resource.type != 'compute.googleapis.com/Disk' && resource.type != 'compute.googleapis.com/InstanceTemplate')",
    "resource.name.extract('/instances/{name}').startsWith('${local.vpn_pool_name_prefix}')",
    "resource.name.extract('/disks/{name}').startsWith('${local.vpn_pool_name_prefix}')",
    "resource.name.extract('/instanceTemplates/{name}').startsWith('${local.vpn_pool_name_prefix}')",
  ])
  lifecycle_iam_bucket_names = setunion(
    toset([local.terraform_state_bucket_name]),
    var.platform_external_bucket_names,
  )
  # One token-creator grant per named principal and lifecycle purpose.
  impersonation_grants = merge(concat([{}], [
    for purpose, members in var.impersonation_principals : {
      for member in members : "${purpose}/${member}" => { purpose = purpose, member = member }
    }
  ])...)
  deployment_config_enabled = var.deployment_config_bucket_name != ""
  # Usage logs of the config bucket go to the retained evidence bucket under one
  # prefix, so the analytics writer can create objects under that prefix only.
  deployment_config_log_prefix = "deployment-config-access"
}

resource "google_iam_workload_identity_pool" "github" {
  count                     = local.federation_enabled ? 1 : 0
  project                   = var.project_id
  workload_identity_pool_id = "${var.name_prefix}-github"
  display_name              = "GitHub Actions (${var.environment})"
  description               = "Purpose-scoped federation for ${var.github_org}/${var.github_repo}."
}

# Preserve the provider address during migration. Bootstrap validates the
# resolved plan against the common inventory and scans that exact saved plan.
resource "google_iam_workload_identity_pool_provider" "github" {
  count                              = local.federation_enabled ? 1 : 0
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == '${var.github_org}/${var.github_repo}' && assertion.repository_id == '${var.github_repository_id}' && assertion.repository_owner_id == '${var.github_owner_id}' && assertion.event_name == 'workflow_dispatch' && (${join(" || ", flatten([
    for purpose, contexts in var.purpose_contexts : [
      for context in contexts : "(assertion.sub == '${local.subject_prefix}:environment:${context.environment}' && assertion.ref == '${context.ref}' && assertion.workflow_ref == '${context.workflow_ref}'${context.reusable_workflow_ref == "" ? "" : " && assertion.job_workflow_ref == '${context.reusable_workflow_ref}'"})"
    ]
  ]))})"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Federation became optional (ADR-070). These moves keep the existing pool and
# provider of a federated deployment in place.
moved {
  from = google_iam_workload_identity_pool.github
  to   = google_iam_workload_identity_pool.github[0]
}

moved {
  from = google_iam_workload_identity_pool_provider.github
  to   = google_iam_workload_identity_pool_provider.github[0]
}

resource "google_service_account" "packer_build" {
  count        = local.builder_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${replace(var.name_prefix, "-", "")}-packer"
  display_name = "Shifter ${var.environment} GCE image builder"
}

moved {
  from = google_service_account.packer_build
  to   = google_service_account.packer_build[0]
}

resource "google_service_account" "validate" {
  count        = local.validate_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${replace(var.name_prefix, "-", "")}-validate"
  display_name = "Shifter ${var.environment} GCE image validator"
}

resource "google_service_account" "promote" {
  count        = local.promote_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${replace(var.name_prefix, "-", "")}-promote"
  display_name = "Shifter prod GCE image promoter"
}

resource "google_service_account" "deploy" {
  count        = local.deploy_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${replace(var.name_prefix, "-", "")}-deploy"
  display_name = "Shifter ${var.environment} platform deployer"
}

resource "google_service_account" "release_scan" {
  count        = local.release_scan_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${replace(var.name_prefix, "-", "")}-scan"
  display_name = "Shifter ${var.environment} exact-release image scanner"
}

resource "google_service_account" "destroy" {
  count        = local.destroy_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "${replace(var.name_prefix, "-", "")}-destroy"
  display_name = "Shifter ${var.environment} platform destroyer"
}

resource "google_service_account_iam_member" "packer_build_wif" {
  depends_on         = [google_service_account.packer_build, google_iam_workload_identity_pool_provider.github]
  for_each           = local.purpose_subject_principals.build
  service_account_id = local.service_account_names.build
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_service_account_iam_member" "validate_wif" {
  depends_on         = [google_service_account.validate, google_iam_workload_identity_pool_provider.github]
  for_each           = local.purpose_subject_principals.validate
  service_account_id = local.service_account_names.validate
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_service_account_iam_member" "promote_wif" {
  depends_on         = [google_service_account.promote, google_iam_workload_identity_pool_provider.github]
  for_each           = local.purpose_subject_principals.promote
  service_account_id = local.service_account_names.promote
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_service_account_iam_member" "deploy_wif" {
  depends_on         = [google_service_account.deploy, google_iam_workload_identity_pool_provider.github]
  for_each           = local.purpose_subject_principals.deploy
  service_account_id = local.service_account_names.deploy
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_service_account_iam_member" "release_scan_wif" {
  depends_on         = [google_service_account.release_scan, google_iam_workload_identity_pool_provider.github]
  for_each           = local.purpose_subject_principals.release_scan
  service_account_id = local.service_account_names.release_scan
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_service_account_iam_member" "destroy_wif" {
  depends_on         = [google_service_account.destroy, google_iam_workload_identity_pool_provider.github]
  for_each           = local.purpose_subject_principals.destroy
  service_account_id = local.service_account_names.destroy
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

# Operators run the GitHub-less commands as a lifecycle purpose identity through
# impersonation. Only the named principals receive the token-creator role, and
# only on the identity of the purpose they are named for. No key exists.
resource "google_service_account_iam_member" "impersonation" {
  depends_on         = [google_service_account.release_scan, google_service_account.deploy, google_service_account.destroy]
  for_each           = local.impersonation_grants
  service_account_id = local.service_account_names[each.value.purpose]
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value.member
}

resource "google_service_account_iam_member" "packer_build_act_as_self" {
  count              = local.build_enabled ? 1 : 0
  depends_on         = [google_service_account.packer_build]
  service_account_id = local.service_account_names.build
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.service_account_emails.build}"
}

resource "google_service_account_iam_member" "packer_build_token_creator_self" {
  count              = local.build_enabled ? 1 : 0
  depends_on         = [google_service_account.packer_build]
  service_account_id = local.service_account_names.build
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${local.service_account_emails.build}"
}

resource "google_project_iam_member" "packer_build_roles" {
  depends_on = [google_service_account.packer_build]
  for_each   = local.build_enabled ? toset(var.build_roles) : toset([])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:${local.service_account_emails.build}"
}

# Build inputs are granted by their owning bucket, not through project-wide
# Storage Admin. The exported-image bucket writer lives in packer-build-infra.
resource "google_storage_bucket_iam_member" "packer_build_reader" {
  depends_on = [google_service_account.packer_build]
  for_each   = local.build_enabled ? var.build_read_bucket_names : toset([])
  bucket     = each.value
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:${local.service_account_emails.build}"
}

resource "google_project_iam_custom_role" "validate" {
  count       = local.validate_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_validate"
  title       = "Shifter GCE image validator"
  description = "Disposable no-SA validation VM lifecycle and exact-candidate evidence labels."
  permissions = var.validate_permissions
}

resource "google_project_iam_member" "validate_roles" {
  depends_on = [google_service_account.validate, google_project_iam_custom_role.validate]
  for_each   = local.validate_enabled ? toset(concat(var.validate_roles, ["projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_validate"])) : toset([])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:${local.service_account_emails.validate}"
}

resource "google_project_iam_custom_role" "promote" {
  count       = local.promote_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_promote"
  title       = "Shifter GCE image promoter"
  description = "Verified prod image copy, channel commit, and previous-head deprecation."
  permissions = var.promote_permissions
}

resource "google_project_iam_member" "promote_role" {
  depends_on = [google_service_account.promote, google_project_iam_custom_role.promote]
  count      = local.promote_enabled ? 1 : 0
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_promote"
  member     = "serviceAccount:${local.service_account_emails.promote}"
}

resource "google_project_iam_member" "promotion_source_image_reader" {
  count   = var.promotion_reader_service_account_email == "" ? 0 : 1
  project = var.project_id
  role    = "roles/compute.imageUser"
  member  = "serviceAccount:${var.promotion_reader_service_account_email}"
}

resource "google_project_iam_member" "deploy_roles" {
  depends_on = [google_service_account.deploy]
  for_each   = local.deploy_enabled ? toset(var.deploy_roles) : toset([])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:${local.service_account_emails.deploy}"
}

resource "google_project_iam_member" "destroy_roles" {
  depends_on = [google_service_account.destroy]
  for_each   = local.destroy_enabled ? toset(var.destroy_roles) : toset([])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:${local.service_account_emails.destroy}"
}

# identityPlatform:initializeAuth checks firebaseauth.configs.create and then
# adds Firebase to the project, which needs firebase.projects.update. No
# Identity Platform role holds that permission, so a fresh project fails with
# INSUFFICIENT_PERMISSION without this role (#2594).
resource "google_project_iam_custom_role" "deploy_identity_platform" {
  count       = local.deploy_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_deploy_identity_platform"
  title       = "Shifter platform deploy Identity Platform initialization"
  description = "Add Firebase to the project when gcp-deploy initializes Identity Platform."
  permissions = var.deploy_identity_platform_permissions
}

resource "google_project_iam_member" "deploy_identity_platform" {
  depends_on = [google_service_account.deploy, google_project_iam_custom_role.deploy_identity_platform]
  count      = local.deploy_enabled ? 1 : 0
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_deploy_identity_platform"
  member     = "serviceAccount:${local.service_account_emails.deploy}"
}

# Bucket lifecycle and object access are limited to the three deterministic
# platform-core buckets. In particular, neither lifecycle identity receives a
# project-wide Storage Admin grant that could alter retained release evidence.
resource "google_project_iam_custom_role" "deploy_storage" {
  count       = local.deploy_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_deploy_storage"
  title       = "Shifter platform deploy storage"
  description = "Terraform lifecycle for deterministic platform-owned buckets only."
  permissions = var.deploy_storage_permissions
}

resource "google_project_iam_member" "deploy_storage" {
  depends_on = [google_service_account.deploy, google_project_iam_custom_role.deploy_storage]
  count      = local.deploy_enabled ? 1 : 0
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_deploy_storage"
  member     = "serviceAccount:${local.service_account_emails.deploy}"

  condition {
    title       = "platform-buckets-only"
    description = "Deploy may manage assets, audit-log, and GDC image buckets, never release evidence."
    expression  = local.platform_storage_condition
  }
}

resource "google_project_iam_custom_role" "destroy_storage" {
  count       = local.destroy_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_destroy_storage"
  title       = "Shifter platform destroy storage"
  description = "Terraform teardown for deterministic platform-owned buckets only."
  permissions = var.destroy_storage_permissions
}

resource "google_project_iam_member" "destroy_storage" {
  depends_on = [google_service_account.destroy, google_project_iam_custom_role.destroy_storage]
  count      = local.destroy_enabled ? 1 : 0
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_destroy_storage"
  member     = "serviceAccount:${local.service_account_emails.destroy}"

  condition {
    title       = "platform-buckets-only"
    description = "Destroy may tear down assets, audit-log, and GDC image buckets, never release evidence."
    expression  = local.platform_storage_condition
  }
}

resource "google_project_iam_custom_role" "deploy_vpn_pool" {
  count       = local.deploy_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_deploy_vpn_pool"
  title       = "Shifter platform deploy OpenVPN pool"
  description = "Instance template, regional instance group and autoscaler lifecycle for the shared OpenVPN pool only."
  permissions = var.deploy_vpn_pool_permissions
}

resource "google_project_iam_member" "deploy_vpn_pool" {
  depends_on = [google_service_account.deploy, google_project_iam_custom_role.deploy_vpn_pool]
  count      = local.deploy_enabled ? 1 : 0
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_deploy_vpn_pool"
  member     = "serviceAccount:${local.service_account_emails.deploy}"

  condition {
    title       = "openvpn-pool-vms-only"
    description = "VM, disk and template permissions apply only to the shared OpenVPN pool's names."
    expression  = local.vpn_pool_condition
  }
}

resource "google_project_iam_custom_role" "destroy_vpn_pool" {
  count       = local.destroy_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_destroy_vpn_pool"
  title       = "Shifter platform destroy OpenVPN pool"
  description = "Teardown of the shared OpenVPN pool's instance templates, regional instance group and autoscaler."
  permissions = var.destroy_vpn_pool_permissions
}

resource "google_project_iam_member" "destroy_vpn_pool" {
  depends_on = [google_service_account.destroy, google_project_iam_custom_role.destroy_vpn_pool]
  count      = local.destroy_enabled ? 1 : 0
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/${replace(var.name_prefix, "-", "_")}_destroy_vpn_pool"
  member     = "serviceAccount:${local.service_account_emails.destroy}"

  condition {
    title       = "openvpn-pool-vms-only"
    description = "Template permissions apply only to the shared OpenVPN pool's names."
    expression  = local.vpn_pool_condition
  }
}

# Terraform maintains per-workload object grants on the pre-existing state and
# explicitly declared content buckets. Bucket Owner is attached to each exact
# bucket resource, never inherited from the project and never applied to the
# release-evidence bucket.
resource "google_storage_bucket_iam_member" "deploy_bucket_iam_admin" {
  depends_on = [google_service_account.deploy]
  for_each   = local.deploy_enabled ? local.lifecycle_iam_bucket_names : toset([])
  bucket     = each.value
  role       = "roles/storage.legacyBucketOwner"
  member     = "serviceAccount:${local.service_account_emails.deploy}"
}

resource "google_storage_bucket_iam_member" "destroy_bucket_iam_admin" {
  depends_on = [google_service_account.destroy]
  for_each   = local.destroy_enabled ? local.lifecycle_iam_bucket_names : toset([])
  bucket     = each.value
  role       = "roles/storage.legacyBucketOwner"
  member     = "serviceAccount:${local.service_account_emails.destroy}"
}

# Raw release evidence never enters public Actions artifacts. This foundational
# bucket outlives platform-core and is writable only by the exact purpose that
# produced each evidence class. ObjectCreator prevents overwrite or deletion;
# retention and versioning make the evidence independently auditable.
resource "google_storage_bucket" "release_evidence" {
  # checkov:skip=CKV_GCP_62:This foundational bucket must exist before and outlive platform-core, including its audit-log sink. Pointing it at that sink creates a bootstrap/destroy dependency; self-logging recurses. See the time-bounded ADR-004-R11 exception (#2084).
  name                        = var.release_evidence_bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels = {
    environment = replace(var.environment, "_", "-")
    managed_by  = "terraform"
    project     = "shifter"
  }

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = "7776000"
    is_locked        = true
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 365
    }
  }
}

resource "google_storage_bucket_iam_member" "packer_build_evidence_writer" {
  depends_on = [google_service_account.packer_build]
  count      = local.build_enabled ? 1 : 0
  bucket     = google_storage_bucket.release_evidence.name
  role       = "roles/storage.objectCreator"
  member     = "serviceAccount:${local.service_account_emails.build}"

  condition {
    title       = "packer-build-evidence-only"
    description = "Build identity may create immutable source-binding records only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/packer-builds/')"
  }
}

resource "google_storage_bucket_iam_member" "validate_build_evidence_reader" {
  depends_on = [google_service_account.validate]
  count      = local.validate_enabled ? 1 : 0
  bucket     = google_storage_bucket.release_evidence.name
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:${local.service_account_emails.validate}"

  condition {
    title       = "packer-build-evidence-read-only"
    description = "Validation identity may read immutable source-binding records only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/packer-builds/')"
  }
}

resource "google_storage_bucket_iam_member" "validate_evidence_writer" {
  depends_on = [google_service_account.validate]
  count      = local.validate_enabled ? 1 : 0
  bucket     = google_storage_bucket.release_evidence.name
  role       = "roles/storage.objectCreator"
  member     = "serviceAccount:${local.service_account_emails.validate}"

  condition {
    title       = "packer-validation-evidence-only"
    description = "Validation identity may create immutable validation records only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/packer-validation/')"
  }
}

resource "google_storage_bucket_iam_member" "release_scan_evidence_writer" {
  depends_on = [google_service_account.release_scan]
  count      = local.release_scan_enabled ? 1 : 0
  bucket     = google_storage_bucket.release_evidence.name
  role       = "roles/storage.objectCreator"
  member     = "serviceAccount:${local.service_account_emails.release_scan}"

  condition {
    title       = "release-scan-evidence-only"
    description = "Release scanner may create exact-digest scan records only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/release-scans/')"
  }
}

resource "google_storage_bucket_iam_member" "deploy_evidence_writer" {
  depends_on = [google_service_account.deploy]
  count      = local.deploy_enabled ? 1 : 0
  bucket     = google_storage_bucket.release_evidence.name
  role       = "roles/storage.objectCreator"
  member     = "serviceAccount:${local.service_account_emails.deploy}"

  condition {
    title       = "deployment-evidence-only"
    description = "Deploy identity may create running-image records only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/deployments/')"
  }
}

# `gcloud storage cp` issues a pre-flight object GET plus a bucket-level object
# LIST to choose its upload strategy; create-only (objectCreator) alone makes the
# evidence cp fail (403 on get, then on the bucket list). A bucket-level list
# cannot be scoped by an object-name condition, so each evidence writer gets an
# unconditioned objectViewer (get+list) on the release-evidence bucket. This only
# grants read over provenance metadata; objectCreator still blocks
# overwrite/deletion, so evidence immutability is preserved.
resource "google_storage_bucket_iam_member" "packer_build_evidence_reader" {
  count  = local.build_enabled ? 1 : 0
  bucket = google_storage_bucket.release_evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.service_account_emails.build}"
}

resource "google_storage_bucket_iam_member" "validate_evidence_reader" {
  count  = local.validate_enabled ? 1 : 0
  bucket = google_storage_bucket.release_evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.validate[0].email}"
}

resource "google_storage_bucket_iam_member" "release_scan_evidence_reader" {
  count  = local.release_scan_enabled ? 1 : 0
  bucket = google_storage_bucket.release_evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.release_scan[0].email}"
}

resource "google_storage_bucket_iam_member" "deploy_evidence_reader" {
  count  = local.deploy_enabled ? 1 : 0
  bucket = google_storage_bucket.release_evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.deploy[0].email}"
}

resource "google_storage_bucket_iam_member" "promotion_evidence_reader" {
  count  = var.promotion_reader_service_account_email == "" ? 0 : 1
  bucket = google_storage_bucket.release_evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.promotion_reader_service_account_email}"

  condition {
    title       = "packer-validation-evidence-read-only"
    description = "Promotion identity may read immutable validation records only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/packer-validation/')"
  }
}


# The foundational root owns CI access to its pre-existing backend bucket.
# These bindings outlive platform-core and replace workflow-time self-grants.
resource "google_storage_bucket_iam_member" "deploy_state_object_admin" {
  depends_on = [google_service_account.deploy]
  count      = local.deploy_enabled ? 1 : 0
  bucket     = local.terraform_state_bucket_name
  role       = "roles/storage.objectAdmin"
  member     = "serviceAccount:${local.service_account_emails.deploy}"
}

resource "google_storage_bucket_iam_member" "deploy_state_bucket_reader" {
  depends_on = [google_service_account.deploy]
  count      = local.deploy_enabled ? 1 : 0
  bucket     = local.terraform_state_bucket_name
  role       = "roles/storage.legacyBucketReader"
  member     = "serviceAccount:${local.service_account_emails.deploy}"
}

resource "google_storage_bucket_iam_member" "destroy_state_object_admin" {
  depends_on = [google_service_account.destroy]
  count      = local.destroy_enabled ? 1 : 0
  bucket     = local.terraform_state_bucket_name
  role       = "roles/storage.objectAdmin"
  member     = "serviceAccount:${local.service_account_emails.destroy}"
}

resource "google_storage_bucket_iam_member" "destroy_state_bucket_reader" {
  depends_on = [google_service_account.destroy]
  count      = local.destroy_enabled ? 1 : 0
  bucket     = local.terraform_state_bucket_name
  role       = "roles/storage.legacyBucketReader"
  member     = "serviceAccount:${local.service_account_emails.destroy}"
}

# The principals that may deploy also run this foundation, which keeps its state
# in the same bucket. Each deploy removes the broad project bindings from the
# state bucket, so these explicit grants keep the operators' access (#2700).
resource "google_storage_bucket_iam_member" "operator_state_object_admin" {
  for_each = toset(lookup(var.impersonation_principals, "deploy", []))
  bucket   = local.terraform_state_bucket_name
  role     = "roles/storage.objectAdmin"
  member   = each.value
}

resource "google_storage_bucket_iam_member" "operator_state_bucket_reader" {
  for_each = toset(lookup(var.impersonation_principals, "deploy", []))
  bucket   = local.terraform_state_bucket_name
  role     = "roles/storage.legacyBucketReader"
  member   = each.value
}

# GitHub-less deployment inputs (ADR-070): bigrae.yaml, the Terraform values,
# and generated references. The bucket holds no secret values; Secret Manager
# holds them. It outlives platform-core like the state and evidence buckets.
resource "google_storage_bucket" "deployment_config" {
  count                       = local.deployment_config_enabled ? 1 : 0
  name                        = var.deployment_config_bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels = {
    environment = replace(var.environment, "_", "-")
    managed_by  = "terraform"
    project     = "shifter"
  }

  versioning {
    enabled = true
  }

  logging {
    log_bucket        = google_storage_bucket.release_evidence.name
    log_object_prefix = local.deployment_config_log_prefix
  }
}

# Cloud Storage writes usage logs as this Google group. The grant is limited to
# the access-log prefix, so the group can not create release evidence. A Domain
# Restricted Sharing org policy (iam.allowedPolicyMemberDomains) rejects the
# google.com group with Error 412, so the grant is optional (#2585).
resource "google_storage_bucket_iam_member" "deployment_config_access_log_writer" {
  count  = local.deployment_config_enabled && var.enable_gcs_usage_log_delivery ? 1 : 0
  bucket = google_storage_bucket.release_evidence.name
  role   = "roles/storage.objectCreator"
  member = "group:cloud-storage-analytics@google.com"

  condition {
    title       = "deployment-config-access-logs-only"
    description = "Cloud Storage may create usage-log objects for the deployment config bucket only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.release_evidence.name}/objects/${local.deployment_config_log_prefix}')"
  }
}

# Cloud Audit Logs record every read and write of the deployment config bucket
# also when usage-log delivery is off. No other root owns the storage audit
# configuration of the foundation project (#2585).
module "storage_data_access_audit" {
  count  = local.deployment_config_enabled ? 1 : 0
  source = "../project-audit-config"

  project_id = var.project_id
  service    = "storage.googleapis.com"
  log_types  = ["DATA_READ", "DATA_WRITE"]
}

# The lifecycle identities read the inputs and manage only the deployment lock.
resource "google_storage_bucket_iam_member" "deploy_config_reader" {
  depends_on = [google_service_account.deploy]
  count      = local.deployment_config_enabled && local.deploy_enabled ? 1 : 0
  bucket     = google_storage_bucket.deployment_config[0].name
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:${local.service_account_emails.deploy}"
}

resource "google_storage_bucket_iam_member" "deploy_config_lock" {
  depends_on = [google_service_account.deploy]
  count      = local.deployment_config_enabled && local.deploy_enabled ? 1 : 0
  bucket     = google_storage_bucket.deployment_config[0].name
  role       = "roles/storage.objectAdmin"
  member     = "serviceAccount:${local.service_account_emails.deploy}"

  condition {
    title       = "deployment-lock-only"
    description = "Deploy may create and delete the deployment lock only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.deployment_config[0].name}/objects/locks/')"
  }
}

resource "google_storage_bucket_iam_member" "destroy_config_reader" {
  depends_on = [google_service_account.destroy]
  count      = local.deployment_config_enabled && local.destroy_enabled ? 1 : 0
  bucket     = google_storage_bucket.deployment_config[0].name
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:${local.service_account_emails.destroy}"
}

resource "google_storage_bucket_iam_member" "destroy_config_lock" {
  depends_on = [google_service_account.destroy]
  count      = local.deployment_config_enabled && local.destroy_enabled ? 1 : 0
  bucket     = google_storage_bucket.deployment_config[0].name
  role       = "roles/storage.objectAdmin"
  member     = "serviceAccount:${local.service_account_emails.destroy}"

  condition {
    title       = "deployment-lock-only"
    description = "Destroy may create and delete the deployment lock only."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.deployment_config[0].name}/objects/locks/')"
  }
}

# The principals that may deploy publish the inputs. The principals that may
# only destroy read them.
resource "google_storage_bucket_iam_member" "deployment_config_publisher" {
  for_each = local.deployment_config_enabled ? toset(lookup(var.impersonation_principals, "deploy", [])) : toset([])
  bucket   = google_storage_bucket.deployment_config[0].name
  role     = "roles/storage.objectAdmin"
  member   = each.value
}

resource "google_storage_bucket_iam_member" "deployment_config_destroy_reader" {
  for_each = local.deployment_config_enabled ? toset(lookup(var.impersonation_principals, "destroy", [])) : toset([])
  bucket   = google_storage_bucket.deployment_config[0].name
  role     = "roles/storage.objectViewer"
  member   = each.value
}
