# ------------------------------------------------------------------------------
# OpenFGA datastore logins and their rotation (#2313)
# ------------------------------------------------------------------------------
# The released OpenFGA server cannot create RDS IAM tokens, so each OpenFGA duty
# has a password login (ADR-066 AWS conformance boundary). Each credential has
# its own secret, encrypted with this module's secrets key. Only the
# provisioning Job, the weekly credential refresh and the rotation function can
# read them. The OpenFGA servers read their login from a named Kubernetes Secret.
#
# The in-repo rotation function follows the documented Secrets Manager steps.
# Runtime and admin alternate between <login> and <login>_clone, so the previous
# credential stays valid until the next rotation; the weekly refresh copies the
# current one into Kubernetes and restarts the servers. The migrator rotates its own password; only deploy
# Jobs use it. The deploy tooling builds the hash-pinned package. The deploy
# turns on the schedules after the provisioning Job has created the logins.
# ------------------------------------------------------------------------------

locals {
  openfga_duties = {
    runtime  = { login = "openfga_runtime", strategy = "alternating" }
    admin    = { login = "openfga_admin", strategy = "alternating" }
    migrator = { login = "openfga_migrator", strategy = "single" }
  }
  openfga_strategies = toset([for duty in values(local.openfga_duties) : duty.strategy])
  openfga_secret_arns = {
    for key, secret in aws_secretsmanager_secret.openfga_datastore : key => secret.arn
  }
}

resource "random_password" "openfga" {
  for_each = local.openfga_duties

  # Letters and digits only, so the value never needs URI or SQL escaping.
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "openfga_datastore" {
  for_each = local.openfga_duties

  name                    = "shifter/${var.environment}/eks/openfga-${each.key}-datastore"
  description             = "OpenFGA ${each.key} datastore login for ${var.cluster_name}"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = 30

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-openfga-${each.key}-datastore"
  })
}

resource "aws_secretsmanager_secret_version" "openfga_datastore" {
  for_each = local.openfga_duties

  secret_id = aws_secretsmanager_secret.openfga_datastore[each.key].id
  secret_string = jsonencode(merge(
    {
      engine   = "postgres"
      host     = data.aws_db_instance.portal.address
      port     = data.aws_db_instance.portal.port
      dbname   = "openfga"
      username = each.value.login
      password = random_password.openfga[each.key].result
    },
    each.value.strategy == "alternating" ? { masterarn = aws_secretsmanager_secret.platform["database"].arn } : {},
  ))

  lifecycle {
    # Rotation owns the value after the first version.
    ignore_changes = [secret_string]
  }
}

resource "aws_security_group" "openfga_rotation" {
  name        = "${var.cluster_name}-openfga-rotation"
  description = "OpenFGA datastore rotation function"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "PostgreSQL to the portal database network"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.portal.cidr_block]
  }

  egress {
    description = "HTTPS to the Secrets Manager API through NAT"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-openfga-rotation"
  })
}

resource "aws_iam_role" "openfga_rotation" {
  name                 = "${var.cluster_name}-openfga-rotation"
  permissions_boundary = var.permissions_boundary_arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "openfga_rotation" {
  name = "openfga-rotation"
  role = aws_iam_role.openfga_rotation.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RotateOnlyOpenFgaDatastoreSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecretVersionStage",
        ]
        Resource = values(local.openfga_secret_arns)
      },
      {
        Sid      = "ReadMasterCredentialForAlternatingUsers"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.platform["database"].arn
      },
      {
        # GetRandomPassword has no resource; AWS requires "*".
        Sid      = "GeneratePasswords"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetRandomPassword"]
        Resource = "*"
      },
      {
        Sid      = "UseSecretsKeyThroughSecretsManager"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.secrets.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com" }
        }
      },
      {
        # Lambda VPC networking; these EC2 actions do not support resource scoping.
        Sid    = "VpcNetworkInterfaces"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses",
        ]
        Resource = "*"
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for group in aws_cloudwatch_log_group.openfga_rotation : "${group.arn}:*"]
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "openfga_rotation" {
  for_each = local.openfga_strategies

  # The cluster key allows log groups below /aws/eks/<cluster>/.
  name              = "/aws/eks/${var.cluster_name}/openfga-rotation-${each.key}"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.cluster.arn

  tags = var.tags
}

resource "aws_lambda_function" "openfga_rotation" {
  for_each = local.openfga_strategies

  function_name    = "${var.cluster_name}-openfga-rotation-${each.key}"
  filename         = var.openfga_rotation_package
  source_code_hash = var.openfga_rotation_package == null ? null : filebase64sha256(var.openfga_rotation_package)
  handler          = "openfga_rotation.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256
  role             = aws_iam_role.openfga_rotation.arn

  vpc_config {
    subnet_ids         = [for subnet in aws_subnet.private : subnet.id]
    security_group_ids = [aws_security_group.openfga_rotation.id]
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.openfga_rotation[each.key].name
  }

  lifecycle {
    # Teardown destroys without the package; every create or update needs it.
    precondition {
      condition     = var.openfga_rotation_package != null
      error_message = "openfga_rotation_package is required to create or update the rotation functions."
    }
  }

  environment {
    # Configuration, not secret content, binds each secret to its SQL login.
    variables = merge(
      {
        ROTATION_STRATEGY = each.key
        ROTATION_LOGINS = jsonencode({
          for key, duty in local.openfga_duties :
          aws_secretsmanager_secret.openfga_datastore[key].arn => duty.login if duty.strategy == each.key
        })
      },
      each.key == "alternating" ? { MASTER_SECRET_ARN = aws_secretsmanager_secret.platform["database"].arn } : {},
    )
  }

  tags = var.tags
}

resource "aws_lambda_permission" "openfga_rotation" {
  for_each = local.openfga_duties

  statement_id  = "AllowSecretsManagerRotation-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.openfga_rotation[each.value.strategy].function_name
  principal     = "secretsmanager.amazonaws.com"
  source_arn    = aws_secretsmanager_secret.openfga_datastore[each.key].arn
}

resource "aws_secretsmanager_secret_rotation" "openfga_datastore" {
  # Creating a schedule runs testSecret at once, so the deploy enables the
  # schedules only after the provisioning Job has created the logins.
  for_each = var.openfga_rotation_schedule ? local.openfga_duties : {}

  secret_id           = aws_secretsmanager_secret.openfga_datastore[each.key].id
  rotation_lambda_arn = aws_lambda_function.openfga_rotation[each.value.strategy].arn
  # The first rotation waits for the schedule; the deploy already set the login.
  rotate_immediately = false

  rotation_rules {
    automatically_after_days = 30
  }

  depends_on = [aws_lambda_permission.openfga_rotation, aws_secretsmanager_secret_version.openfga_datastore]
}
