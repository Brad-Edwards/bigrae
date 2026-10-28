# CTF organizer image copies (#2090): the scheduler writes copies only under the
# ctf-content-images/ prefix of the assets bucket; nothing else in the bucket is
# writable to it.
mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
  mock_resource "google_service_account" {
    defaults = {
      email = "synthetic-runtime@synthetic-project.iam.gserviceaccount.com"
      name  = "projects/synthetic-project/serviceAccounts/synthetic-runtime@synthetic-project.iam.gserviceaccount.com"
    }
  }
}

variables {
  project_id                  = "synthetic-project"
  dynamic_secret_project_id   = "synthetic-project"
  environment                 = "test"
  name_prefix                 = "shifter-test"
  workload_identity_pool      = "synthetic-project.svc.id.goog"
  assets_bucket_name          = "synthetic-assets"
  terraform_state_bucket_name = "synthetic-state"
  runtime_secret_ids = {
    app            = "projects/synthetic-project/secrets/app"
    db             = "projects/synthetic-project/secrets/db"
    db-migration   = "projects/synthetic-project/secrets/migrate"
    db-provisioner = "projects/synthetic-project/secrets/provisioner"
    guacamole-db   = "projects/synthetic-project/secrets/guacamole"
  }
}

run "scheduler_writes_image_copies_only_under_their_prefix" {
  command = plan
  assert {
    condition     = google_storage_bucket_iam_member.ctf_content_image_copies.bucket == "synthetic-assets"
    error_message = "Image copies live in the assets bucket."
  }
  assert {
    condition     = google_storage_bucket_iam_member.ctf_content_image_copies.role == "roles/storage.objectUser"
    error_message = "The scheduler needs object create, read and delete for copies, and no IAM or bucket administration."
  }
  assert {
    condition     = google_storage_bucket_iam_member.ctf_content_image_copies.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/synthetic-assets/objects/ctf-content-images/\")"
    error_message = "The scheduler write grant must be limited to the image-copy prefix."
  }
  assert {
    condition     = local.workload_bucket_bindings["ctf-scheduler:assets"].role == "roles/storage.objectViewer"
    error_message = "The scheduler keeps read-only access to the rest of the assets bucket."
  }
}
