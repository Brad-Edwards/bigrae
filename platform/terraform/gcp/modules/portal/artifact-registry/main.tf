terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
    google-beta = {
      source = "hashicorp/google-beta"
    }
    time = {
      source = "hashicorp/time"
    }
  }
}

# Google creates the Artifact Registry service agent lazily. A fresh project has
# no agent yet, so the key grant below fails until this resource creates it
# (#2589).
resource "google_project_service_identity" "artifact_registry" {
  provider = google-beta
  project  = var.project_id
  service  = "artifactregistry.googleapis.com"
}

# A new service agent takes time to become visible to IAM. Without the wait, the
# key grant fails with "Service account ... does not exist" (#2589).
resource "time_sleep" "artifact_registry_agent" {
  create_duration = "60s"
  triggers = {
    agent = google_project_service_identity.artifact_registry.email
  }
}

resource "google_kms_key_ring" "artifact_registry" {
  name     = "${var.name_prefix}-ar"
  location = var.artifact_registry_location
  project  = var.project_id
}

resource "google_kms_crypto_key" "artifact_registry" {
  name            = "${var.name_prefix}-ar-docker"
  key_ring        = google_kms_key_ring.artifact_registry.id
  rotation_period = "7776000s"
  purpose         = "ENCRYPT_DECRYPT"
  labels          = var.common_labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "artifact_registry" {
  crypto_key_id = google_kms_crypto_key.artifact_registry.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${var.project_number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"

  depends_on = [time_sleep.artifact_registry_agent]
}

resource "google_artifact_registry_repository" "docker" {
  for_each = var.artifact_repositories

  project       = var.project_id
  location      = var.artifact_registry_location
  repository_id = "${var.name_prefix}-${each.key}"
  description   = "Docker images for ${each.key} in ${var.environment}"
  format        = "DOCKER"
  kms_key_name  = google_kms_crypto_key.artifact_registry.id

  depends_on = [google_kms_crypto_key_iam_member.artifact_registry]
}

# Repository-scoped pull authority for the isolated exact-release scan job.
# It can read only the four deployment repositories and cannot push, deploy,
# administer the project, or access runtime credentials.
resource "google_artifact_registry_repository_iam_member" "release_scan_reader" {
  for_each = var.release_scan_service_account_email == "" ? toset([]) : var.artifact_repositories

  project    = var.project_id
  location   = var.artifact_registry_location
  repository = google_artifact_registry_repository.docker[each.key].name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.release_scan_service_account_email}"
}
