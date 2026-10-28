# Credential-free plan: gateway metadata identity is isolated from cloud workers.
mock_provider "google" {}

variables {
  workload_identity_pool    = "synthetic-project.svc.id.goog"
  project_id                = "synthetic-project"
  dynamic_secret_project_id = "synthetic-project"
  environment               = "test"
  name_prefix               = "shifter-test"
  runtime_secret_ids = {
    app            = "projects/synthetic-project/secrets/synthetic-app"
    db-migration   = "projects/synthetic-project/secrets/synthetic-migration"
    db-provisioner = "projects/synthetic-project/secrets/synthetic-provisioner"
  }
  assets_bucket_name          = "synthetic-assets"
  terraform_state_bucket_name = "synthetic-state"
}

run "guacamole_identity_isolated_from_application_workers" {
  command = plan

  assert {
    condition     = contains(keys(google_service_account.workload), "guacamole-client")
    error_message = "The gateway must have its own Google service account."
  }
  assert {
    condition     = try(google_service_account_iam_member.workload_identity["guacamole-client"].member == "serviceAccount:synthetic-project.svc.id.goog[shifter-platform/guacamole-client]", false)
    error_message = "Only the dedicated gateway Kubernetes identity may use its Google identity."
  }
  assert {
    condition     = try(length(local.workload_project_roles["guacamole-client"]) == 0, false)
    error_message = "Gateway renewal needs no project-level cloud role."
  }
  assert {
    condition     = alltrue([for binding in values(local.workload_secret_bindings) : binding.workload != "guacamole-client"])
    error_message = "The gateway must not inherit platform runtime-secret access."
  }
}
