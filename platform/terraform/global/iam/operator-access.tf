# ------------------------------------------------------------------------------
# Deploy role callers: exact GitHub Environments and named operators (#2313)
# ------------------------------------------------------------------------------
# GitHub jobs that assume the deploy role bind the protected deploy Environment
# (ADR-003-R5). The trust names exact Environment subjects and the exact
# protected branch refs that run reviewed drift and promotion workflows. Any
# other branch, pull request, tag or Environment subject is refused.
#
# Named operators reach the same lifecycle role, like GCP impersonation
# principals (ADR-070). An IAM user needs MFA and a source identity equal to its
# user name. An IAM role, for example a federated workforce role, is trusted by
# exact ARN; its identity provider enforces MFA. Operators bring their own
# credentials; this root creates no human user.
# ------------------------------------------------------------------------------

variable "github_deploy_environments" {
  description = "GitHub Environments whose jobs may assume the deploy role. Empty selects aws-<environment>."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for name in var.github_deploy_environments : can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", name))])
    error_message = "github_deploy_environments must contain exact Environment names without wildcards."
  }
}

variable "github_deploy_refs" {
  description = "Exact protected branch refs whose non-Environment jobs may assume the deploy role."
  type        = list(string)
  default     = ["refs/heads/dev", "refs/heads/main"]

  validation {
    condition     = alltrue([for ref in var.github_deploy_refs : can(regex("^refs/heads/[A-Za-z0-9._/-]{1,200}$", ref)) && !can(regex("[*?]", ref))])
    error_message = "github_deploy_refs must contain exact refs/heads/ branch refs without wildcards."
  }
}

variable "operator_principal_arns" {
  description = "Exact IAM user or role ARNs that may assume the deploy role. IAM users must sign in with MFA."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.operator_principal_arns :
      can(regex("^arn:aws:iam::[0-9]{12}:(user|role)/([A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]+$", arn))
    ])
    error_message = "operator_principal_arns must contain exact IAM user or role ARNs without wildcards."
  }
}

locals {
  deploy_environments = length(var.github_deploy_environments) > 0 ? var.github_deploy_environments : ["aws-${var.environment}"]
  operator_users      = [for arn in var.operator_principal_arns : arn if can(regex(":user/", arn))]
  operator_roles      = [for arn in var.operator_principal_arns : arn if can(regex(":role/", arn))]

  github_deploy_trust = {
    Sid       = "GitHubDeployEnvironments"
    Effect    = "Allow"
    Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
    Action    = "sts:AssumeRoleWithWebIdentity"
    Condition = {
      StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = concat(
          [for name in local.deploy_environments : "${local.github_subject_prefix}:environment:${name}"],
          [for ref in var.github_deploy_refs : "${local.github_subject_prefix}:ref:${ref}"],
        )
      }
    }
  }

  operator_user_trust = length(local.operator_users) == 0 ? [] : [{
    Sid       = "NamedOperatorUsersWithMfa"
    Effect    = "Allow"
    Principal = { AWS = local.operator_users }
    Action    = ["sts:AssumeRole", "sts:SetSourceIdentity"]
    Condition = {
      Bool         = { "aws:MultiFactorAuthPresent" = "true" }
      StringEquals = { "sts:SourceIdentity" = "$${aws:username}" }
    }
  }]

  operator_role_trust = length(local.operator_roles) == 0 ? [] : [{
    Sid       = "NamedOperatorRoles"
    Effect    = "Allow"
    Principal = { AWS = local.operator_roles }
    Action    = ["sts:AssumeRole", "sts:SetSourceIdentity"]
  }]

  deploy_trust_statements = concat([local.github_deploy_trust], local.operator_user_trust, local.operator_role_trust)
}

# Session Manager port forwarding to the private EKS API endpoint, following
# the AWS Session Manager sample policies. The deploy role may start sessions
# only on the instance that the EKS environment tags as this environment's
# operator access node, with the AWS port-forwarding document. It may open, end
# or resume only its own sessions.
resource "aws_iam_role_policy" "operator_access_tunnel" {
  name = "operator-access-tunnel"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartTunnelOnAccessNode"
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "ssm:resourceTag/shifter:operator-access" = var.environment }
        }
      },
      {
        Sid      = "PortForwardDocumentOnly"
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = "arn:aws:ssm:${var.aws_region}::document/AWS-StartPortForwardingSessionToRemoteHost"
      },
      {
        Sid      = "OwnSessionsOnly"
        Effect   = "Allow"
        Action   = ["ssm:TerminateSession", "ssm:ResumeSession", "ssmmessages:OpenDataChannel"]
        Resource = "arn:aws:ssm:*:*:session/$${aws:userid}-*"
      },
    ]
  })
}

# Account-wide IAM outbound web identity federation (#2313). AWS principals use
# STS GetWebIdentityToken to obtain signed JWTs. The portal trusts only this
# account's issuer for service principal proof.
resource "aws_iam_outbound_web_identity_federation" "account" {}

output "service_token_issuer" {
  description = "Account issuer URL for STS outbound web identity tokens (AWS_SERVICE_TOKEN_ISSUER)."
  value       = aws_iam_outbound_web_identity_federation.account.issuer_identifier
}
