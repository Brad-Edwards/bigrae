terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  environment  = "prod"
  cluster_name = "shifter-${local.environment}-eks"
  secret_names = toset(["database", "django", "redis", "cognito", "guacamole-db", "guacamole-json-auth"])
  # Runtime processes connect with RDS IAM from start, so they read no master
  # or Guacamole database credential (#2313).
  runtime_secret_names = setsubtract(local.secret_names, ["database", "guacamole-db"])
  cognito_issuer       = regex("^https://cognito-idp\\.([a-z0-9-]+)\\.amazonaws\\.com/([A-Za-z0-9_-]+)$", var.runtime_env["OIDC_ISSUER_URL"])
  cognito_user_pool    = "arn:aws:cognito-idp:${local.cognito_issuer[0]}:${data.aws_caller_identity.current.account_id}:userpool/${local.cognito_issuer[1]}"
  # One RDS IAM login per runtime workload; each joins the portal_runtime duty.
  runtime_db_logins = {
    portal              = "portal_web"
    workers             = "portal_workers"
    ctfScheduler        = "ctf_scheduler"
    provisionerLauncher = "provisioner_launcher"
  }
  # STS web identity audience for the Guacamole gateway control endpoint.
  gateway_control_audience = "https://${var.domain_name}/guacamole-control"

  # The EKS control plane composes over the existing portal data plane
  # (ADR-044-R6): portal resources are named "${environment}-portal-*".
  portal_name_prefix       = "${local.environment}-portal"
  permissions_boundary_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/shifter-${local.environment}-ci-role-boundary"
}

module "eks" {
  source = "../../../modules/portal/eks"

  model_broker = var.model_broker

  environment               = local.environment
  aws_region                = var.aws_region
  cluster_name              = local.cluster_name
  deployment_role_arn       = var.deployment_role_arn
  permissions_boundary_arn  = local.permissions_boundary_arn
  vpc_cidr                  = var.vpc_cidr
  availability_zones        = var.availability_zones
  private_subnet_cidrs      = var.private_subnet_cidrs
  public_subnet_cidrs       = var.public_subnet_cidrs
  kubernetes_version        = var.kubernetes_version
  addon_versions            = var.addon_versions
  node_instance_types       = var.node_instance_types
  node_desired_size         = var.node_desired_size
  node_min_size             = var.node_min_size
  node_max_size             = var.node_max_size
  domain_name               = var.domain_name
  oidc_thumbprints          = var.oidc_thumbprints
  openfga_rotation_package  = var.openfga_rotation_package
  openfga_rotation_schedule = var.openfga_rotation_schedule
  cognito_user_pool_arn     = local.cognito_user_pool
  secret_names              = local.secret_names
  workload_identities = {
    cni = {
      namespace       = "kube-system"
      service_account = "aws-node"
      policy_arns     = ["arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"]
      secret_names    = []
    }
    ingress = {
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
      policy_arns     = []
      secret_names    = []
    }
    portal = {
      namespace       = "shifter-platform"
      service_account = "portal"
      policy_arns     = []
      secret_names    = local.runtime_secret_names
      # Session provider recheck: AdminGetUser on this user pool only (#2313).
      cognito_admin_get_user = true
      # Broker participant SSH/RDP connections to realized range guests (#1826).
      range_participant_secret_read = true
      rds_iam_db_user               = local.runtime_db_logins.portal
      object_read_arns = (
        var.ctf_content_bucket_arn == ""
        ? []
        : ["${var.ctf_content_bucket_arn}/${var.ctf_content_prefix}*"]
      )
    }
    workers = {
      namespace       = "shifter-platform"
      service_account = "workers"
      policy_arns     = []
      secret_names    = local.runtime_secret_names
      rds_iam_db_user = local.runtime_db_logins.workers
      # Delayed work rechecks stored session credentials with Cognito (#2313).
      cognito_admin_get_user = true
    }
    ctfScheduler = {
      namespace       = "shifter-platform"
      service_account = "ctf-scheduler"
      policy_arns     = []
      secret_names    = local.runtime_secret_names
      rds_iam_db_user = local.runtime_db_logins.ctfScheduler
      # Delayed work rechecks stored session credentials with Cognito (#2313).
      cognito_admin_get_user = true
    }
    # Dedicated provisioner Job launcher + the privileged provisioner Job (#1826).
    # The provisioner's range-provisioning permission set is attached separately
    # by module.provisioner_iam (shared with the ECS task role); these entries
    # create the exact-subject IRSA roles and grant platform secret access.
    provisionerLauncher = {
      namespace       = "shifter-platform"
      service_account = "provisioner-launcher"
      policy_arns     = []
      secret_names    = local.runtime_secret_names
      rds_iam_db_user = local.runtime_db_logins.provisionerLauncher
      # Delayed work rechecks stored session credentials with Cognito (#2313).
      cognito_admin_get_user = true
    }
    provisioner = {
      namespace       = "shifter-jobs"
      service_account = "provisioner"
      policy_arns     = []
      secret_names    = local.runtime_secret_names
    }
    # One-shot schema-migration + content-bootstrap Job (AWS EKS parity with the GCP
    # platform-migrate Job, #1826). aws_eks.py runs it once before the chart install so
    # the workloads deploy with SKIP_MIGRATIONS=1 and never race per-pod migrations on
    # startup (the provisioner-launcher's slow per-pod migration tripped its liveness
    # probe). entrypoint.sh hydrates the master (database) + app (django) + cognito
    # secrets to migrate as the master user. The master owner then reconciles the
    # per-workload RDS IAM logins and runs the content bootstrap (#2313).
    migrator = {
      namespace       = "shifter-platform"
      service_account = "migrator"
      policy_arns     = []
      secret_names    = local.secret_names
    }
    # One-shot guacamole database/role provisioner (AWS EKS parity with the GCP
    # cloud-sql module). RDS has no native terraform user/database resource and the
    # deploy runner cannot reach RDS, so aws_eks.py runs provision_guacamole_database
    # as a Job under this exact-subject identity before the chart install; it reads
    # the master (database), app (django) and guacamole-db secrets to create the
    # guacamole_admin role + guacamole database. It connects as the RDS master via
    # password, so it needs no rds_iam_db_user grant.
    # Guacamole gateway renewal: STS web identity for one audience only (#2313).
    guacamoleClient = {
      namespace             = "shifter-platform"
      service_account       = "guacamole-client"
      policy_arns           = []
      secret_names          = []
      web_identity_audience = local.gateway_control_audience
    }
    # Weekly OpenFGA credential refresh: reads the three datastore login secrets,
    # patches their named Kubernetes Secrets and restarts the servers (#2313).
    policyCredentialRefresh = {
      namespace                      = "shifter-platform"
      service_account                = "openfga-credential-refresh"
      policy_arns                    = []
      secret_names                   = []
      openfga_datastore_secrets_read = true
    }
    guacamoleProvisioner = {
      namespace       = "shifter-platform"
      service_account = "guacamole-db-provisioner"
      policy_arns     = []
      # It also creates the OpenFGA database and logins (#2313).
      openfga_datastore_secrets_read = true
      # The provision command reads only the RDS master (database) and guacamole-db
      # credentials from Secrets Manager; it runs under ENVIRONMENT=build so Django
      # settings load without the other platform secrets.
      secret_names = toset(["database", "guacamole-db"])
    }
    # EKS add-on controller identities (#1826). AWS-managed CSI driver policies;
    # controllers run in kube-system with their driver-default service accounts.
    ebs-csi = {
      namespace       = "kube-system"
      service_account = "ebs-csi-controller-sa"
      policy_arns     = ["arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"]
      secret_names    = []
    }
    efs-csi = {
      namespace       = "kube-system"
      service_account = "efs-csi-controller-sa"
      policy_arns     = ["arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"]
      secret_names    = []
    }
  }

  tags = merge(var.tags, {
    Environment = local.environment
    ManagedBy   = "terraform"
    Project     = "shifter"
    Substrate   = "eks"
  })
}

# Compose the provisioner Job environment over the existing portal + range data
# plane (ADR-044-R6) and attach the shared provisioner permission set to the
# provisioner IRSA role.
module "eks_provisioner_env" {
  source = "../../../modules/portal/eks-provisioner-env"

  environment              = local.environment
  name_prefix              = local.portal_name_prefix
  runtime_env              = var.runtime_env
  provisioner_role_name    = module.eks.workload_role_names["provisioner"]
  provisioner_role_id      = module.eks.workload_role_ids["provisioner"]
  permissions_boundary_arn = local.permissions_boundary_arn
  storage_bucket_name      = var.runtime_env["STORAGE_BUCKET_NAME"]
  db_name                  = var.db_name
  dc_domain_name           = var.dc_domain_name
  extra_env                = var.provisioner_extra_env

  tags = merge(var.tags, {
    Environment = local.environment
    ManagedBy   = "terraform"
    Project     = "shifter"
    Substrate   = "eks"
  })
}
