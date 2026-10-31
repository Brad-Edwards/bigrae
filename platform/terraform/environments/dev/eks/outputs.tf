output "bundle_outputs" {
  description = "Non-secret provider values consumed by the AWS bundle renderer."
  value = {
    cluster_name                    = module.eks.cluster_name
    cluster_endpoint                = module.eks.cluster_endpoint
    vpc_id                          = module.eks.vpc_id
    private_subnet_ids              = module.eks.private_subnet_ids
    workload_role_arns              = module.eks.workload_role_arns
    workload_identity_subjects      = module.eks.workload_identity_subjects
    secret_arns                     = module.eks.secret_arns
    ingress_certificate_arn         = module.eks.ingress_certificate_arn
    ingress_certificate_dns_records = module.eks.ingress_certificate_validation_records
    ingress_waf_acl_arn             = module.eks.ingress_waf_acl_arn
    portal_db_address               = module.eks.portal_db_address
    portal_db_port                  = module.eks.portal_db_port
    portal_vpc_cidr                 = module.eks.portal_vpc_cidr
  }
}

output "cluster_ca_certificate" {
  description = "Sensitive CA material for short-lived kubeconfig generation."
  value       = module.eks.cluster_ca_certificate
  sensitive   = true
}

output "cluster_name" {
  description = "EKS cluster name consumed by the lifecycle owner."
  value       = module.eks.cluster_name
}

output "cluster_access_role_arn" {
  description = "Protected short-lived deployment role authorized through an EKS access entry."
  value       = var.deployment_role_arn
}

output "certificate_arn" {
  description = "ACM certificate ARN consumed by ingress configuration."
  value       = module.eks.ingress_certificate_arn
}

output "waf_acl_arn" {
  description = "Regional WAF ACL ARN consumed by ingress configuration."
  value       = module.eks.ingress_waf_acl_arn
}

output "workload_role_arns" {
  description = "Exact service-account workload roles keyed by process."
  value       = module.eks.workload_role_arns
}

output "runtime_env" {
  description = "Management-plane runtime bindings merged with the assembled provisioner Job environment, consumed by the AWS renderer."
  value = merge(module.eks_provisioner_env.runtime_env, {
    PORTAL_NETWORK_CIDRS = join(",", module.eks.private_subnet_cidrs)
    ACCESS_NETWORK_CIDRS = join(",", module.eks.private_subnet_cidrs)
  })
  sensitive = true
}

output "ingress_source_cidrs" {
  description = "EKS public-subnet CIDRs from which ALB target traffic reaches pods."
  value       = var.public_subnet_cidrs
}

output "edge_client_cidrs" {
  description = "Validated public client CIDRs consumed by the ALB inbound restriction."
  value       = var.edge_client_cidrs
}

output "provider_api_cidrs" {
  description = "Validated AWS API CIDRs consumed by chart network policy."
  value       = var.provider_api_cidrs
}

output "private_service_cidrs" {
  description = "Portal VPC CIDR(s) hosting the shared RDS/Redis data plane, consumed by chart network policy (ADR-044-R6)."
  value       = [module.eks.portal_vpc_cidr]
}

output "kubernetes_api_cidrs" {
  description = "Kubernetes API reachability CIDRs for the provisioner-launcher egress policy. In-cluster clients dial the API at the kubernetes.default ClusterIP (the service CIDR); the private subnet CIDRs are retained so the control-plane ENIs stay reachable too."
  value       = concat([module.eks.service_ipv4_cidr], var.private_subnet_cidrs)
}

output "provider_api_egress_except" {
  description = "CIDRs carved out of the broad provider-API (0.0.0.0/0:443) egress so only the launcher policy grants Kubernetes API access. The service CIDR covers the kubernetes.default ClusterIP that every in-cluster client dials."
  value       = [module.eks.service_ipv4_cidr]
}

output "model_broker" {
  description = "Applied private model broker configuration."
  value       = module.eks.model_broker
}

output "runtime_plugin_node_group_asg" {
  description = "Auto Scaling group of the runtime-plugin pool, consumed by the trusted node-pool labeler (#2526)."
  value       = module.eks.runtime_plugin_node_group_asg
}

output "database_iam_users" {
  description = "RDS IAM login of each runtime workload; each is a member of the portal_runtime duty."
  value       = local.runtime_db_logins
}

output "openfga_datastore_secret_arns" {
  description = "OpenFGA datastore login secrets by duty."
  value       = module.eks.openfga_datastore_secret_arns
}

output "openfga_rotation_scheduled" {
  description = "Whether the OpenFGA datastore rotation schedules exist."
  value       = module.eks.openfga_rotation_scheduled
}

output "operator_access_instance_id" {
  description = "Session Manager target for operator access to the private EKS API."
  value       = module.eks.operator_access_instance_id
}

output "service_token_issuer" {
  description = "Account issuer URL for STS outbound web identity tokens."
  value       = module.eks.service_token_issuer
}

output "gateway_control_audience" {
  description = "STS web identity audience of the Guacamole gateway control endpoint."
  value       = local.gateway_control_audience
}

output "aws_account_id" {
  description = "AWS account that owns the service token issuer."
  value       = data.aws_caller_identity.current.account_id
}
