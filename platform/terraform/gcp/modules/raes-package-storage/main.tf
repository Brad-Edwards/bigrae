# Optional post-setup content storage. Executable adapter authority is managed
# separately; possession of a pack object never grants permission to run code.
resource "google_storage_bucket" "packages" {
  project                     = var.project_id
  name                        = var.bucket_name
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  # Tenant pack removal deletes live archive objects (ADR-034-R13). Versioning
  # keeps a noncurrent generation as accidental-delete protection; expire it so
  # removed pack bytes do not stay in the bucket indefinitely.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = var.noncurrent_version_retention_days
      with_state                 = "ARCHIVED"
    }
  }

  logging {
    log_bucket        = var.access_log_bucket_name
    log_object_prefix = "raes-packages-access/"
  }
}

resource "google_storage_bucket_iam_member" "portal_reader" {
  bucket = google_storage_bucket.packages.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${var.portal_service_account_email}"
}
