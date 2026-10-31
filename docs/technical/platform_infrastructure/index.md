# Platform Infrastructure

BigRAE platform workloads use the shared Helm chart on EKS or GKE.
Terraform owns the supporting cloud infrastructure and workload identity grants.

- [AWS EKS backend bundle](aws-eks-bundle.md)
- [AWS native identity](aws-native-identity.md)
- [GCP native identity](gcp-native-identity.md)
- [GCP range audit telemetry](gcp-range-audit-telemetry.md)

## AWS

| Surface | Source | Responsibility |
| --- | --- | --- |
| Account foundation | `platform/terraform/global/` | IAM, runners and developer workstations |
| Core | `platform/terraform/environments/<env>/` | Registries, budgets and shared state services |
| Range | `platform/terraform/environments/<env>/range/` | Range networking and exported provider settings |
| Portal data plane | `platform/terraform/environments/<env>/portal/` | RDS, Redis, Cognito, object storage and secrets |
| Control plane | `platform/terraform/environments/<env>/eks/` | EKS, workload identity and ingress prerequisites |
| Application workloads | `platform/charts/shifter/` | Web, workers, provisioner Jobs, Guacamole and guacd |

The retained `prod` roots are owned by @Brad-Edwards.
Their callers are the bootstrap `terraform`/`full` commands and EKS lifecycle.
Only `aws-dev` is an AWS deployment workflow choice.
The optional portal CTFd module has the same owner and is called by `module.ctfd` in the portal root.
It uses upstream CTFd setup; it has no repository-specific seed script.

Terraform state uses S3 native locking.
Engine/Pulumi state has separate storage and lock ownership.
Unused connector templates and the standalone workshop root are retired.
See [configuration and recovery](../../dev/deployment-configuration.md) before accessing earlier infrastructure revisions.

## GCP

`platform/terraform/gcp/environments/platform/` is the single platform execution root.
`platform/terraform/gcp/environments/<environment>/terraform.tfvars` supplies each environment's values.
Local overrides stay beside those value files and are passed explicitly by bootstrap.
Each environment keeps its own state bucket/prefix and Terraform metadata directory.
Foundation identity and runner roots remain independent under `gcp/global/`.

| Service | Responsibility |
| --- | --- |
| GKE | Private control plane and platform node pools; Connect Gateway access |
| Cloud SQL | Platform and Guacamole PostgreSQL databases |
| Memorystore | Redis cache and coordination |
| Pub/Sub | Domain events and subscriptions |
| Artifact Registry | Digest-bound control-plane images |
| Secret Manager | Runtime secret bundles and references |
| Cloud DNS / Cloud Armor | Public hostname, TLS and edge policy |

The shared chart owns application workloads.
Kustomize overlays supply deployment assets and validation manifests.
The common runtime component supplies repeated patches; overlays supply environment values.
The identity renderer reads Terraform outputs instead of committing live identities.

## Related Docs

- [GCP Infrastructure](gcp-infrastructure) - GKE, Helm, Cloud Armor, and GCP services
- [GDC Provisioning](gdc-provisioning) - Range guest provisioning on GDC
- [Range Instantiation Policy](range-instantiation-policy) - Which range backend may serve which kind of launch
- [AMI Management](ami-management) - Packer builds and SSM parameter management (AWS)
- [Manual Deployment](manual-deployment) - Infrastructure elements deployed without CI/CD
- [CI/CD](cicd) - Deployment pipelines
- [GitHub Runners](github-runners) - Self-hosted runner setup and maintenance
- [Networking](networking) - VPC architecture and peering
- [Guacamole](guacamole) - Browser-based RDP integration
