mock_provider "google" {}

# A deployment without GitHub (ADR-070): no federation, impersonation by named
# principals only, and a hardened deployment config bucket.
variables {
  project_number                = "789"
  project_id                    = "example-project"
  environment                   = "unseen-customer"
  name_prefix                   = "cust-unseen"
  region                        = "us-central1"
  release_evidence_bucket_name  = "example-customer-evidence"
  terraform_state_bucket_name   = "example-platform-state"
  deployment_config_bucket_name = "example-customer-deployment-config"
  purpose_contexts              = {}
  github_repository_id          = ""
  github_owner_id               = ""
  impersonation_principals = {
    deploy       = ["group:operators@example.com"]
    release_scan = ["group:operators@example.com"]
    destroy      = ["user:owner@example.com"]
  }
}

run "impersonation_only_foundation" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  assert {
    condition     = length(google_iam_workload_identity_pool.github) == 0 && length(google_iam_workload_identity_pool_provider.github) == 0
    error_message = "A deployment without GitHub must create no WIF pool or provider."
  }
  assert {
    condition     = output.workload_identity_provider == null
    error_message = "The provider output must be null without GitHub federation."
  }
  assert {
    condition     = length(google_service_account_iam_member.deploy_wif) == 0 && length(google_service_account_iam_member.destroy_wif) == 0 && length(google_service_account_iam_member.release_scan_wif) == 0
    error_message = "No federated binding may exist without GitHub federation."
  }
  assert {
    condition     = google_service_account.deploy[0].account_id == "custunseen-deploy" && google_service_account.release_scan[0].account_id == "custunseen-scan" && google_service_account.destroy[0].account_id == "custunseen-destroy"
    error_message = "Impersonation must enable the lifecycle identities with the naming contract that gcp-deploy derives."
  }
  assert {
    condition     = length(google_service_account.release) == 0 && length(google_service_account.validate) == 0 && length(google_service_account.promote) == 0
    error_message = "Impersonation must not enable the release or image purposes."
  }
  assert {
    condition     = length(google_service_account_iam_member.impersonation) == 3
    error_message = "Each named principal must receive exactly one grant for each purpose it is named for."
  }
  assert {
    condition = (
      google_service_account_iam_member.impersonation["deploy/group:operators@example.com"].role == "roles/iam.serviceAccountTokenCreator" &&
      google_service_account_iam_member.impersonation["deploy/group:operators@example.com"].service_account_id == "projects/example-project/serviceAccounts/custunseen-deploy@example-project.iam.gserviceaccount.com" &&
      google_service_account_iam_member.impersonation["destroy/user:owner@example.com"].service_account_id == "projects/example-project/serviceAccounts/custunseen-destroy@example-project.iam.gserviceaccount.com"
    )
    error_message = "A principal may impersonate only the identity of the purpose it is named for."
  }
  assert {
    condition = (
      google_storage_bucket.deployment_config[0].public_access_prevention == "enforced" &&
      google_storage_bucket.deployment_config[0].uniform_bucket_level_access == true &&
      google_storage_bucket.deployment_config[0].force_destroy == false &&
      google_storage_bucket.deployment_config[0].versioning[0].enabled == true &&
      google_storage_bucket.deployment_config[0].logging[0].log_bucket == "example-customer-evidence"
    )
    error_message = "The deployment config bucket must be private, uniform, versioned, retained, and access-logged."
  }
  assert {
    condition = (
      google_storage_bucket_iam_member.deploy_config_reader[0].role == "roles/storage.objectViewer" &&
      google_storage_bucket_iam_member.deploy_config_lock[0].condition[0].expression == "resource.name.startsWith('projects/_/buckets/example-customer-deployment-config/objects/locks/')" &&
      google_storage_bucket_iam_member.destroy_config_lock[0].condition[0].expression == "resource.name.startsWith('projects/_/buckets/example-customer-deployment-config/objects/locks/')"
    )
    error_message = "The lifecycle identities may read the inputs and change only the deployment lock."
  }
  assert {
    condition = (
      google_storage_bucket_iam_member.deployment_config_publisher["group:operators@example.com"].role == "roles/storage.objectAdmin" &&
      google_storage_bucket_iam_member.deployment_config_destroy_reader["user:owner@example.com"].role == "roles/storage.objectViewer" &&
      length(google_storage_bucket_iam_member.deployment_config_publisher) == 1
    )
    error_message = "Deploy principals publish the inputs; destroy-only principals read them."
  }
  assert {
    condition     = length(google_storage_bucket_iam_member.deployment_config_access_log_writer) == 0
    error_message = "Usage-log delivery is off by default; Domain Restricted Sharing rejects the google.com group (#2585)."
  }
  assert {
    condition     = join(",", module.storage_data_access_audit[0].enabled_log_types) == "DATA_READ,DATA_WRITE"
    error_message = "Cloud Audit Logs must record every read and write of the deployment config bucket (#2585)."
  }
}

run "usage_log_delivery_enabled" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    enable_gcs_usage_log_delivery = true
  }
  assert {
    condition     = google_storage_bucket_iam_member.deployment_config_access_log_writer[0].condition[0].expression == "resource.name.startsWith('projects/_/buckets/example-customer-evidence/objects/deployment-config-access')"
    error_message = "The usage-log writer may create objects under the access-log prefix only."
  }
}

run "federation_requires_repository_ids" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    github_org  = "example"
    github_repo = "product"
    purpose_contexts = {
      deploy = [{
        environment           = "customer-deploy"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/deploy.yml@refs/heads/main"
        reusable_workflow_ref = ""
      }]
    }
  }
  expect_failures = [var.github_repository_id, var.github_owner_id]
}

run "public_impersonation_rejected" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    impersonation_principals = {
      deploy = ["allUsers"]
    }
  }
  expect_failures = [var.impersonation_principals]
}

run "image_purpose_impersonation_rejected" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    impersonation_principals = {
      build = ["user:owner@example.com"]
    }
  }
  expect_failures = [var.impersonation_principals]
}

run "config_bucket_differs_from_evidence" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    deployment_config_bucket_name = "example-customer-evidence"
  }
  expect_failures = [var.deployment_config_bucket_name]
}

run "release_purpose_on_release_refs" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    github_org           = "example"
    github_repo          = "product"
    github_repository_id = "123"
    github_owner_id      = "456"
    purpose_contexts = {
      release = [for ref in ["refs/heads/main", "refs/heads/dev", "refs/heads/deploy-test"] : {
        environment           = "gcp-release"
        ref                   = ref
        workflow_ref          = "example/product/.github/workflows/release-gcp.yml@${ref}"
        reusable_workflow_ref = ""
      }]
    }
  }
  assert {
    condition     = google_service_account.release[0].account_id == "custunseen-release"
    error_message = "The release purpose must create the release publisher identity."
  }
  assert {
    condition     = google_service_account_iam_member.release_wif["repo:example@456/product@123:environment:gcp-release"].service_account_id == "projects/example-project/serviceAccounts/custunseen-release@example-project.iam.gserviceaccount.com"
    error_message = "The release binding must use the exact immutable release subject."
  }
  assert {
    condition     = strcontains(google_iam_workload_identity_pool_provider.github[0].attribute_condition, "assertion.workflow_ref == 'example/product/.github/workflows/release-gcp.yml@refs/heads/main'")
    error_message = "The provider must bind the release workflow on protected main."
  }
  assert {
    condition     = strcontains(google_iam_workload_identity_pool_provider.github[0].attribute_condition, "assertion.workflow_ref == 'example/product/.github/workflows/release-gcp.yml@refs/heads/deploy-test'")
    error_message = "The provider must bind the release workflow on deploy-test."
  }
}

run "release_purpose_off_release_refs_rejected" {
  command = plan
  module {
    source = "../../modules/cicd-oidc-identity"
  }
  variables {
    github_org           = "example"
    github_repo          = "product"
    github_repository_id = "123"
    github_owner_id      = "456"
    purpose_contexts = {
      release = [{
        environment           = "gcp-release"
        ref                   = "refs/heads/feature"
        workflow_ref          = "example/product/.github/workflows/release-gcp.yml@refs/heads/feature"
        reusable_workflow_ref = ""
      }]
    }
  }
  expect_failures = [var.purpose_contexts]
}
