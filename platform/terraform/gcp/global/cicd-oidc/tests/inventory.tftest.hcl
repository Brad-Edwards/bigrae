mock_provider "google" {}

run "renamed_canonical_reusable_workflow" {
  command = plan
  variables {
    github_org  = "example-org"
    github_repo = "example-repo"
    purpose_contexts = {
      for purpose, workflow in { deploy = "deploy.yml", destroy = "gcp-dev-destroy.yml" } : purpose => [{
        environment           = "customer-${purpose}"
        ref                   = "refs/heads/main"
        workflow_ref          = "example-org/example-repo/.github/workflows/${workflow}@refs/heads/main"
        reusable_workflow_ref = purpose == "deploy" ? "example-org/example-repo/.github/workflows/_gcp-dev.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" : ""
      }]
    }
  }
}

run "foreign_reusable_workflow_rejected" {
  command = plan
  variables {
    purpose_contexts = {
      for purpose, workflow in { deploy = "deploy.yml", destroy = "gcp-dev-destroy.yml" } : purpose => [{
        environment           = "customer-${purpose}"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/${workflow}@refs/heads/main"
        reusable_workflow_ref = purpose == "deploy" ? "example/foreign/.github/workflows/_gcp-dev.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" : ""
      }]
    }
  }
  expect_failures = [var.purpose_contexts]
}

variables {
  project_number               = "789"
  project_id                   = "example-project"
  environment                  = "unseen-customer"
  name_prefix                  = "cust-unseen"
  region                       = "us-central1"
  github_org                   = "example"
  github_repo                  = "product"
  github_repository_id         = "123"
  github_owner_id              = "456"
  release_evidence_bucket_name = "example-customer-evidence"
  terraform_state_bucket_name  = "example-platform-state"
  purpose_contexts = {
    deploy = [{
      environment           = "customer-deploy"
      ref                   = "refs/heads/main"
      workflow_ref          = "example/product/.github/workflows/deploy.yml@refs/heads/main"
      reusable_workflow_ref = ""
    }]
    destroy = [{
      environment           = "customer-destroy"
      ref                   = "refs/heads/main"
      workflow_ref          = "example/product/.github/workflows/gcp-dev-destroy.yml@refs/heads/main"
      reusable_workflow_ref = ""
    }]
  }
}

run "unseen_deployment" {
  command = plan

  assert {
    condition     = output.packer_validate_service_account_email == null && output.image_export_bucket_name == ""
    error_message = "Deploy onboarding must not enable image purposes."
  }
}

run "overlapping_purpose_subjects" {
  command = plan
  variables {
    purpose_contexts = {
      deploy = [{
        environment           = "shared"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/deploy.yml@refs/heads/main"
        reusable_workflow_ref = ""
      }]
      destroy = [{
        environment           = "shared"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/gcp-dev-destroy.yml@refs/heads/main"
        reusable_workflow_ref = ""
      }]
    }
  }
  expect_failures = [var.purpose_contexts]
}

run "all_purposes_first_plan" {
  command = plan
  variables {
    purpose_contexts = {
      for purpose, workflow in {
        build        = "packer-gcp.yml", validate = "packer-gcp-validate.yml",
        release_scan = "packer-gcp-release-scan.yml", deploy = "deploy.yml", destroy = "destroy.yml"
        } : purpose => [{
          environment           = "customer-${replace(purpose, "_", "-")}"
          ref                   = "refs/heads/main"
          workflow_ref          = "example/product/.github/workflows/${workflow}@refs/heads/main"
          reusable_workflow_ref = ""
      }]
    }
  }
}

run "case_insensitive_environment_ownership" {
  command = plan
  variables {
    purpose_contexts = {
      deploy = [{
        environment           = "Shared"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/deploy.yml@refs/heads/main"
        reusable_workflow_ref = ""
      }]
      destroy = [{
        environment           = "shared"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/destroy.yml@refs/heads/main"
        reusable_workflow_ref = ""
      }]
    }
  }
  expect_failures = [var.purpose_contexts]
}

# A destroy workflow rejects every dispatch ref except protected dev/main before
# auth, so a destroy tuple on the tenant deploy branch can never be satisfied.
run "destroy_on_protected_dev_with_tenant_deploy_branch" {
  command = plan
  variables {
    purpose_contexts = {
      deploy = [{
        environment           = "customer"
        ref                   = "refs/heads/customer"
        workflow_ref          = "example/product/.github/workflows/deploy.yml@refs/heads/customer"
        reusable_workflow_ref = ""
      }]
      destroy = [{
        environment           = "customer-destroy"
        ref                   = "refs/heads/dev"
        workflow_ref          = "example/product/.github/workflows/gcp-dev-destroy.yml@refs/heads/dev"
        reusable_workflow_ref = ""
      }]
    }
  }
}

run "destroy_on_tenant_branch" {
  command = plan
  variables {
    purpose_contexts = {
      deploy = [{
        environment           = "customer"
        ref                   = "refs/heads/customer"
        workflow_ref          = "example/product/.github/workflows/deploy.yml@refs/heads/customer"
        reusable_workflow_ref = ""
      }]
      destroy = [{
        environment           = "customer-destroy"
        ref                   = "refs/heads/customer"
        workflow_ref          = "example/product/.github/workflows/gcp-dev-destroy.yml@refs/heads/customer"
        reusable_workflow_ref = ""
      }]
    }
  }
  expect_failures = [var.purpose_contexts]
}

run "retired_promote_purpose_rejected" {
  command = plan
  variables {
    purpose_contexts = {
      promote = [{
        environment           = "gcp-promote-prod"
        ref                   = "refs/heads/main"
        workflow_ref          = "example/product/.github/workflows/packer-gcp-promote.yml@refs/heads/main"
        reusable_workflow_ref = ""
      }]
    }
  }
  expect_failures = [var.purpose_contexts]
}

# A developer build project (#2668): only the build and validate purposes, both
# bound to packer-gcp.yml (validation runs in the workflow that it calls), with
# the image network and the export storage and no platform resources.
run "developer_build_project" {
  command = plan
  variables {
    enable_image_build_network  = true
    enable_image_export_storage = true
    purpose_contexts = {
      for purpose, environment in { build = "gcp-image-build", validate = "gcp-image-validate" } : purpose => [
        for ref in ["refs/heads/dev", "refs/heads/main"] : {
          environment           = environment
          ref                   = ref
          workflow_ref          = "example/product/.github/workflows/packer-gcp.yml@${ref}"
          reusable_workflow_ref = ""
        }
      ]
    }
  }

  assert {
    condition     = output.image_export_bucket_name == "example-project-cust-unseen-image-export"
    error_message = "A build project must expose its export staging bucket."
  }
  assert {
    condition     = output.deploy_service_account_email == null && output.destroy_service_account_email == null && output.release_scan_service_account_email == null
    error_message = "A build project must not create lifecycle identities."
  }
}
