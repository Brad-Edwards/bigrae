mock_provider "aws" {
  override_during = plan
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_resource "aws_iam_outbound_web_identity_federation" {
    defaults = { issuer_identifier = "https://11111111-2222-3333-4444-555555555555.tokens.sts.global.api.aws" }
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
    condition     = jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == ["repo:example@456/product@123:environment:aws-dev", "repo:example@456/product@123:ref:refs/heads/dev", "repo:example@456/product@123:ref:refs/heads/main"]
    error_message = "Default trust must bind the owner and repository IDs and only the deploy Environment."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement) == 1 && !can(jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringLike)
    error_message = "Without named operators the deploy role must trust only exact GitHub Environment subjects."
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
    condition     = jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == ["repo:example/product:environment:aws-dev", "repo:example/product:ref:refs/heads/dev", "repo:example/product:ref:refs/heads/main"]
    error_message = "Explicit legacy trust must select only the legacy subject."
  }
}

run "renamed_repository_subject" {
  command = plan
  variables {
    github_repo = "renamed-product"
  }
  assert {
    condition     = jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == ["repo:example@456/renamed-product@123:environment:aws-dev", "repo:example@456/renamed-product@123:ref:refs/heads/dev", "repo:example@456/renamed-product@123:ref:refs/heads/main"]
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

run "named_operators_require_mfa_or_exact_role" {
  command = plan
  variables {
    operator_principal_arns = [
      "arn:aws:iam::123456789012:user/synthetic-operator",
      "arn:aws:iam::123456789012:role/synthetic-workforce-admin",
    ]
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement :
      statement.Sid != "NamedOperatorUsersWithMfa" || (
        statement.Principal.AWS == ["arn:aws:iam::123456789012:user/synthetic-operator"]
        && statement.Condition.Bool["aws:MultiFactorAuthPresent"] == "true"
        && statement.Condition.StringEquals["sts:SourceIdentity"] == "$${aws:username}"
      )
    ])
    error_message = "An IAM user operator must present MFA and its own user name as source identity."
  }
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role.github_actions.assume_role_policy).Statement :
      statement if statement.Sid == "NamedOperatorRoles" && statement.Principal.AWS == ["arn:aws:iam::123456789012:role/synthetic-workforce-admin"]
    ]) == 1
    error_message = "A role operator must be trusted by its exact ARN only."
  }
}

run "wildcard_operator_is_refused" {
  command = plan
  variables {
    operator_principal_arns = ["arn:aws:iam::123456789012:role/*"]
  }
  expect_failures = [var.operator_principal_arns]
}

run "tunnel_is_limited_to_the_tagged_access_node_and_port_forwarding" {
  command = plan
  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_role_policy.operator_access_tunnel.policy).Statement : statement.Resource if contains(flatten([statement.Action]), "ssm:StartSession")
      ])) == toset([
      "arn:aws:ec2:us-east-2:123456789012:instance/*",
      "arn:aws:ssm:us-east-2::document/AWS-StartPortForwardingSessionToRemoteHost",
    ])
    error_message = "The deploy role may start only the port-forwarding document on instances."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.operator_access_tunnel.policy).Statement :
      statement.Sid != "StartTunnelOnAccessNode" || statement.Condition.StringEquals["ssm:resourceTag/shifter:operator-access"] == "dev"
    ])
    error_message = "Instance sessions must require this environment's operator access tag."
  }
}

run "wildcard_ref_is_refused" {
  command = plan
  variables {
    github_deploy_refs = ["refs/heads/*"]
  }
  expect_failures = [var.github_deploy_refs]
}
