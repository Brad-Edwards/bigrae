# gcp-deploy initializes Identity Platform (initializeAuth) before Terraform
# runs. The provider can not create a config that is already enabled, so the
# root adopts the existing singleton. A CLI `terraform import` fails on a fresh
# state, because it can not resolve the for_each keys of resources that do not
# exist yet; an import block is part of the normal plan (#2598).
import {
  to = module.platform_core.module.portal_identity_platform.google_identity_platform_config.platform
  id = var.project_id
}

# gcp-destroy removes the Artifact Registry key ring and key from state, because
# GCP can not delete them. A later install finds them in place. gcp-deploy sets
# adopt_artifact_registry_kms when the key ring exists but the state does not
# hold it, and these blocks adopt both (#2643).
import {
  for_each = var.adopt_artifact_registry_kms ? toset(["adopt"]) : toset([])
  to       = module.platform_core.module.portal_artifact_registry.google_kms_key_ring.artifact_registry
  id       = "projects/${var.project_id}/locations/${var.artifact_registry_location}/keyRings/shifter-${var.environment}-ar"
}

import {
  for_each = var.adopt_artifact_registry_kms ? toset(["adopt"]) : toset([])
  to       = module.platform_core.module.portal_artifact_registry.google_kms_crypto_key.artifact_registry
  id       = "projects/${var.project_id}/locations/${var.artifact_registry_location}/keyRings/shifter-${var.environment}-ar/cryptoKeys/shifter-${var.environment}-ar-docker"
}
