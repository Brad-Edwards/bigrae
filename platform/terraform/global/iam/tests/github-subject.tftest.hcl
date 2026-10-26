mock_provider "aws" {
  override_during = plan
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  environment          = "dev"
  github_org           = "example"
  github_repo          = "product"
  github_repository_id = "123"
  github_owner_id      = "456"
}

run "immutable_subject_default" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] == "repo:example@456/product@123:*"
    error_message = "Default trust must bind the owner and repository IDs."
  }
  assert {
    condition = toset(jsondecode(aws_iam_role.github_actions_image.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset([
      "repo:example@456/product@123:ref:refs/heads/dev",
      "repo:example@456/product@123:ref:refs/heads/main",
    ])
    error_message = "Image trust must retain only the exact protected branch subjects."
  }
}

run "explicit_legacy_subject" {
  command = plan
  variables {
    github_subject_format = "default"
  }
  assert {
    condition     = jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] == "repo:example/product:*"
    error_message = "Explicit legacy trust must select only the legacy subject."
  }
}

run "renamed_repository_subject" {
  command = plan
  variables {
    github_repo = "renamed-product"
  }
  assert {
    condition     = jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] == "repo:example@456/renamed-product@123:*"
    error_message = "A renamed repository must retain its immutable IDs."
  }
}

run "invalid_repository_id" {
  command = plan
  variables {
    github_repository_id = "123:*"
  }
  expect_failures = [var.github_repository_id]
}
