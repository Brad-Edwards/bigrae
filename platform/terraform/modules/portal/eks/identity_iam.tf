# ------------------------------------------------------------------------------
# Workload grants for AWS native identity (#2313)
# ------------------------------------------------------------------------------
# Each grant names one exact resource and one purpose:
#   * the provisioning Job and the weekly credential refresh read the three
#     OpenFGA datastore secrets; OpenFGA servers get no cloud identity;
#   * the gateway mints STS web identity tokens for one audience only;
#   * the portal reads one Cognito user's state in one user pool.
# ------------------------------------------------------------------------------

locals {
  workload_openfga_secret = {
    for identity_name, identity in var.workload_identities :
    identity_name => values(local.openfga_secret_arns)
    if identity.openfga_datastore_secrets_read
  }
  workload_web_identity = {
    for identity_name, identity in var.workload_identities :
    identity_name => identity.web_identity_audience
    if identity.web_identity_audience != ""
  }
  workload_cognito_admin_get_user = toset([
    for identity_name, identity in var.workload_identities :
    identity_name
    if identity.cognito_admin_get_user
  ])
}

resource "aws_iam_role_policy" "workload_openfga_secret" {
  for_each = local.workload_openfga_secret

  name = "openfga-datastore-secret"
  role = aws_iam_role.workload[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
        Resource = each.value
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = aws_kms_key.secrets.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "workload_web_identity" {
  for_each = local.workload_web_identity

  name = "sts-web-identity"
  role = aws_iam_role.workload[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sts:GetWebIdentityToken"
      Resource = "*"
      Condition = {
        StringEquals = {
          "sts:IdentityTokenAudience" = each.value
          "sts:SigningAlgorithm"      = "ES384"
        }
        NumericLessThanEquals = { "sts:DurationSeconds" = 900 }
      }
    }]
  })
}

resource "aws_iam_role_policy" "workload_cognito_admin_get_user" {
  for_each = local.workload_cognito_admin_get_user

  name = "cognito-session-recheck"
  role = aws_iam_role.workload[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "cognito-idp:AdminGetUser"
      Resource = var.cognito_user_pool_arn
    }]
  })

  lifecycle {
    precondition {
      condition     = var.cognito_user_pool_arn != ""
      error_message = "cognito_admin_get_user requires cognito_user_pool_arn."
    }
  }
}

# The account enables outbound web identity federation in global/iam. Reading it
# here fails the plan when the account has not enabled it.
data "aws_iam_outbound_web_identity_federation" "account" {}
