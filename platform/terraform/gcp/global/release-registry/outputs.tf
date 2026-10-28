output "release_registry" {
  description = "Release registry prefix; set as GCP_RELEASE_REGISTRY and pass to gcp-images --source."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${local.repository_id}"
}
