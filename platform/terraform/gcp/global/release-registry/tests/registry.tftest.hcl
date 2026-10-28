mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id     = "example-release"
  project_number = "789"
  region         = "us-central1"
  writer_members = ["serviceAccount:exampleprefix-release@example-release.iam.gserviceaccount.com"]
  reader_members = [
    "serviceAccount:customer-deploy@customer-project.iam.gserviceaccount.com",
    "group:operators@example.com",
  ]
}

run "release_repository" {
  command = plan

  assert {
    condition     = output.release_registry == "us-central1-docker.pkg.dev/example-release/bigrae-release"
    error_message = "The release registry must be <region>-docker.pkg.dev/<project>/<prefix>-release."
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.writer["serviceAccount:exampleprefix-release@example-release.iam.gserviceaccount.com"].role == "roles/artifactregistry.writer"
    error_message = "The release publisher must receive repository-scoped write access only."
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.reader["group:operators@example.com"].role == "roles/artifactregistry.reader" && length(google_artifact_registry_repository_iam_member.reader) == 2
    error_message = "Each reader must receive repository-scoped read access only."
  }
}

run "people_never_publish" {
  command = plan
  variables {
    writer_members = ["user:owner@example.com"]
  }
  expect_failures = [var.writer_members]
}

run "public_read_rejected" {
  command = plan
  variables {
    reader_members = ["allUsers"]
  }
  expect_failures = [var.reader_members]
}
