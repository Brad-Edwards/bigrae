# Storage for `gcloud compute images export` (#2309, #2668).
#
# The export runs a Daisy workflow as the Packer build identity. Daisy stages the
# export in a scratch bucket whose name it fixes as `<project>-daisy-bkt-<region>`.
# It lists the project buckets to find that bucket and creates it when it is
# absent. Pre-creating it with a bucket-scoped grant lets the export reuse it,
# so the build identity needs no project-wide bucket create or object access.
# That keeps the create-only grants on the release-evidence bucket intact.
#
# A developer build project (#2668) also needs the staging bucket that receives
# the exported disk.raw tarball before the publish job pushes it.

variable "project_id" {
  description = "Project that runs the image export."
  type        = string
}

variable "region" {
  description = "Region of the export and of the Daisy scratch bucket."
  type        = string
}

variable "name_prefix" {
  description = "Stable resource name prefix."
  type        = string
}

variable "packer_service_account_email" {
  description = "Packer build identity that runs the export."
  type        = string
}

variable "access_log_bucket_name" {
  description = "Terminal GCS bucket that receives the access logs of these buckets."
  type        = string
}

variable "staging_object_retention_days" {
  description = "Age in days after which an exported tarball is deleted. The publish job pushes it to the registry first."
  type        = number
  default     = 7
}

resource "google_storage_bucket" "staging" {
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name_prefix}-image-export"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  logging {
    log_bucket        = var.access_log_bucket_name
    log_object_prefix = "image-export-access/"
  }

  lifecycle_rule {
    condition {
      age = var.staging_object_retention_days
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 1
    }
    action {
      type = "Delete"
    }
  }
}

# The export writes the tarball and its logs here, and the publish job reads it.
resource "google_storage_bucket_iam_member" "staging_writer" {
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.packer_service_account_email}"
}

resource "google_storage_bucket_iam_member" "staging_bucket_reader" {
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${var.packer_service_account_email}"
}

resource "google_storage_bucket" "daisy_export_scratch" {
  project                     = var.project_id
  name                        = "${var.project_id}-daisy-bkt-${var.region}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  # Storage baseline: versioning (CKV_GCP_78) and access logging (CKV_GCP_62).
  # Versioning is paired with a noncurrent-version expiry below so retained old
  # versions cannot defeat the 1-day scratch cleanup.
  versioning {
    enabled = true
  }

  logging {
    log_bucket        = var.access_log_bucket_name
    log_object_prefix = "daisy-export-scratch-access/"
  }

  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 1
    }
    action {
      type = "Delete"
    }
  }
}

# The export runs as the packer SA (--cloudbuild-service-account /
# --compute-service-account); it needs full control of just this scratch bucket
# to stage and clean up the exported disk.
resource "google_storage_bucket_iam_member" "packer_daisy_scratch_admin" {
  bucket = google_storage_bucket.daisy_export_scratch.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${var.packer_service_account_email}"
}

# Daisy checks for the scratch bucket with a project-level storage.buckets.list
# (then a get), which the bucket-scoped grant above does not cover. This narrow
# custom role grants only bucket metadata list/get project-wide -- no object
# access, so evidence-bucket immutability is unaffected.
resource "google_project_iam_custom_role" "packer_image_export_bucket_discovery" {
  project     = var.project_id
  role_id     = "packerImageExportBucketDiscovery"
  title       = "Packer Image Export Bucket Discovery"
  description = "Project-level bucket list/get so gcloud compute images export (daisy) can locate its pre-created scratch bucket without project-wide storage admin."
  permissions = [
    "storage.buckets.list",
    "storage.buckets.get",
  ]
}

resource "google_project_iam_member" "packer_image_export_bucket_discovery" {
  project = var.project_id
  role    = google_project_iam_custom_role.packer_image_export_bucket_discovery.id
  member  = "serviceAccount:${var.packer_service_account_email}"
}

output "staging_bucket_name" {
  description = "Export staging bucket (GCP_GCE_BASE_IMAGE_BUCKET), or an empty string when this module creates none."
  value       = google_storage_bucket.staging.name
}

output "daisy_scratch_bucket_name" {
  description = "Daisy scratch bucket that gcloud compute images export reuses."
  value       = google_storage_bucket.daisy_export_scratch.name
}
