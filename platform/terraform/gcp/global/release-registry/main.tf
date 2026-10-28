# Release registry root (ADR-070).
#
# The release workflow publishes the control-plane images, the deployer image,
# and their Sigstore attestations here, and packer-gcp.yml publishes the base
# guest image artifacts. A deployment reads this repository without GitHub. The
# release publisher identity is the `release` purpose of the cicd-oidc root.
# This root grants repository-scoped access only: writers push, readers pull.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.1"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "release_registry" {
  source = "../../modules/portal/artifact-registry"

  project_id                 = var.project_id
  project_number             = var.project_number
  artifact_registry_location = var.region
  name_prefix                = var.name_prefix
  environment                = "release"
  artifact_repositories      = toset(["release"])
  common_labels = {
    managed_by = "terraform"
    project    = "shifter"
    purpose    = "release"
  }
}

locals {
  repository_id = module.release_registry.artifact_registry_repositories["release"]
}

resource "google_artifact_registry_repository_iam_member" "writer" {
  for_each   = var.writer_members
  project    = var.project_id
  location   = var.region
  repository = local.repository_id
  role       = "roles/artifactregistry.writer"
  member     = each.value
}

resource "google_artifact_registry_repository_iam_member" "reader" {
  for_each   = var.reader_members
  project    = var.project_id
  location   = var.region
  repository = local.repository_id
  role       = "roles/artifactregistry.reader"
  member     = each.value
}
