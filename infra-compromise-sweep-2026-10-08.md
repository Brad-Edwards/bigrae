# Orthanc infrastructure compromise sweep

Date: 2026-10-08 UTC  
Project: `prod-lk3ssb`  
Review window: previous 72 hours

## Result

The sweep found no evidence that an Arsenal Lab participant compromised the
Orthanc GCP project or GKE platform.

This is a qualified result. Vertex AI Data Access audit logs are not present,
range-specific subnets do not have VPC Flow Logs enabled, and the range Cloud
NAT does not log connections. Those gaps prevent a complete reconstruction of
participant model calls and outbound network traffic.

## Evidence checked

- Current GCP IAM policy and the preceding 30 days of IAM administration logs.
- All project service accounts and user-managed service-account keys.
- Audit activity by the range-host service account during the review window.
- Compute instances, metadata keys, SSH-key usernames, disks, snapshots,
  firewall rules, routes, subnetworks, addresses, forwarding rules, and assets.
- Current GKE namespaces, workloads, images, pod security settings, services,
  cluster-admin bindings, secrets metadata, pod-exec audit records, and
  controller/RBAC changes.
- Active Security Command Center findings.

## Findings

### No compromise indicators found

- No IAM policy change occurred during the lab window.
- No user-managed service-account key exists in the project.
- The range-host custom role contains only
  `aiplatform.endpoints.predict`. Its other project roles can write logs and
  metrics. It has no Compute, Secret Manager, IAM, or Kubernetes permission.
- Only the provisioner service account can act as the range-host service
  account directly.
- No audit record shows the range-host identity calling a service outside
  Vertex AI, Cloud Logging, or Cloud Monitoring.
- No range VM has a public IP address.
- No enabled firewall rule allows traffic from `0.0.0.0/0`. The public
  forwarding rules belong to the existing GKE platform load balancer.
- No unexplained high-risk Compute, DNS, logging, or Kubernetes mutation was
  found. Observed changes came from the Shifter deployer/provisioner, GKE system
  controllers, Cortex scanning, or the operator account used for lab support.
- GKE has no unexpected namespace or non-system privileged workload. No user
  other than the operator account used pod exec during the review window.
- Recent assets match Shifter provisioning, image builds, GKE rollouts,
  scheduled SQL backups, quota preferences, and Cortex scanner snapshots.

### Material exposure that is not evidence of compromise

- Security Command Center reports 621 active critical vulnerability findings:
  573 software findings and 48 OS findings across 20 resources. The affected
  resources are 14 GKE nodes and six older Arsenal range VMs. These findings
  show exploitable software exposure. They do not report malware, persistence,
  anomalous IAM, or another compromise category.
- The project grants `roles/owner` to the pre-existing
  `torque-mgmt.svc.id.goog[wli/agent-sa]` Workload Identity principal. Audit
  history places this trust path at project creation, before the lab events.
  It is a broad standing authority and should remain only if Torque still
  requires it.
- Nine current `cortex-scan-*` snapshots were created by the configured Cortex
  scanning service. Their names, sources, and audit principal are consistent
  with the scanner.

## Observability gaps

- Vertex prediction requests have no Data Access audit records. The sweep
  cannot enumerate each request or its source IP address.
- Range-specific subnets have VPC Flow Logs disabled.
- The range Cloud NAT has logging disabled.
- These gaps mean the sweep cannot prove the absence of data exfiltration from
  inside a range. The IAM review does show that the range token cannot modify
  project infrastructure.

## Cleanup completed

- Removed the temporary `atomik` SSH public-key entry from range 30. The
  required `hostadmin` entry remains.
- Verified that Secret Manager has no live secret for the destroyed range IDs
  119 through 173.
- Left all range lifecycle states and platform workloads unchanged.

