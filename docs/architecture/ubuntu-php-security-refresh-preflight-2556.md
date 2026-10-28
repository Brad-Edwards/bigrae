# Ubuntu PHP security refresh preflight (#2556)

Issue-scoped guidance, 2026-10-22; not implementation or live remediation
evidence. The supplied GitHub issue is the contract; no formal requirement is
attached. This applies existing ADR-004, ADR-010/014, ADR-032/034, ADR-037 and
ADR-043 boundaries, without adding a framework or repository-wide policy.
Consult the [current architecture handbook](../technical/architecture.md) and
[ADR registry](../adr/index.yaml) for continuing authority.

## Decision

Keep the fix in the existing Ubuntu package bake and operator maintenance
boundaries. Both `shifter/packer/ubuntu.pkr.hcl` and
`shifter/packer/gcp/ubuntu.pkr.hcl` execute
`shifter/packer/scripts/ubuntu/services.sh`. That shared install boundary owns
the PHP floor; do not implement separate AWS and GCP package policies.

For Ubuntu 22.04/jammy's official PHP 8.1 packages, the floor is
`8.1.2-1ubuntu2.28`. Canonical lists that backport as fixed for both
[CVE-2026-109618](https://ubuntu.com/security/CVE-2026-109618) and
[CVE-2026-109619](https://ubuntu.com/security/CVE-2026-109619).
Use installed package state and Debian version ordering (`dpkg-query` and
`dpkg --compare-versions`), not upstream PHP version numbers, lexical ordering,
an apt candidate, image timestamp, or `php --version`. The acceptance package
is the concrete `libapache2-mod-php8.1`, not its unversioned metapackage.
Missing, unconfigured, unreadable, or below-floor packages fail the build.
Use the signed Ubuntu archive and refresh package indexes; never disable apt
authentication or add a PPA to bypass a stale mirror.

The small extensibility seam is reviewed package policy data: distribution,
release, concrete package/family, and minimum distro revision, separate from
installation and read-only verification. A future security revision updates
that one floor; a different distro/PHP stream needs its own advisory-backed
policy. Do not accept arbitrary newer streams by comparing them with the jammy
floor, or expose an override that can lower/skip it. If multiple verification
callers need the check, share a small shell check rather than copying the
comparison. No runtime env key, JSON schema, dependency or generic CVE engine
is needed. Packer uploads scripts to temporary paths: any companion file must
be explicitly staged; repo-relative sourcing will not work in the guest.

## Canonical surfaces and cross-cutting gates

| Layer and incumbents | Required design constraint |
| --- | --- |
| Guest installation: `scripts/ubuntu/{base,services}.sh` and `scripts/common/cleanup.sh` under `shifter/packer` | Retain strict shell failure propagation and the existing apt lifecycle. Check installed binary packages after installation. Cleanup removes apt caches, temporary files and logs, so retained evidence must be captured through the existing build/validation channels. Do not rely on a marker under `/tmp` or `/var/tmp`. |
| Build authorization: `.github/workflows/packer.yml`, `packer-gcp.yml`, `platform/terraform/global/iam/github-oidc.tf`, `platform/terraform/gcp/modules/cicd-oidc-identity` | Preserve protected-ref checks, independent OIDC/WIF trust, GitHub Environment restrictions, purpose-scoped build identities, validated network/profile bindings and pinned actions. A feature branch cannot bypass these to run a credentialed bake. No broader deploy role, new service-account key or guest IAM permission is needed. |
| AWS validation/publication: `shifter/packer/scripts/bake/base-image-verify.sh`, `packer.yml`, `packer-promote.yml`, `scripts/ami.sh` | Keep exact candidate identity, fresh-boot and post-reboot SSM/DNS checks, cleanup and the publication gate before `/shifter/ami/ubuntu` changes. An Ubuntu package readback can extend that verifier; DNS success alone proves no PHP property. These disposable verifier hosts have an SSM profile; this does not describe every live range guest. |
| GCP validation/publication: `packer-gcp-validate.yml`, `packer-gcp-promote.yml`, `gcp/scripts/validate/{linux,gather-evidence,verify-build-evidence,verify-promotion-evidence}.sh` under `shifter/packer` | Preserve exact numeric image-ID/source-revision binding, runner-controlled checks on both boots, independent read-only-disk SBOM collection, private evidence, authenticated verdict and exact-candidate promotion. `linux.sh` currently checks guest-agent health only. Add any Ubuntu package assertion through this existing check/evidence path; a guest-emitted PASS or label is insufficient on its own. |
| Distribution/import: `packer-gcp.yml`, `scripts/bootstrap/gcp_base_images.py` | Respect the chosen native-tenant versus established GHCR distribution path. For existing digest-import consumers, a native family rebuild alone changes nothing: the replacement disk needs its new digest and normal import/READY readback. Preserve VM-disk media types, manifest/role/revision validation, digest identity and temporary transfer-resource cleanup. Import success is not qualification. No new hosting destination. |
| AWS config transport: `platform/terraform/modules/portal/eks-provisioner-env/main.tf`, `engine/ecs/_env.py` under `shifter/shifter_platform` | The SSM pointer projects into `VICTIM_AMI_ID` through existing Terraform/runtime configuration. Refresh the consuming configuration through its owner; a parameter write does not update already running Pods, registry rows or launched guests. Do not commit live account/network identifiers or introduce a second AMI registry. |
| GCP config shapes: `scripts/gcp/render_runtime_env.py`, `shifter/installation/runtime_inventory_gcp.py`, `engine/ecs/_env.py`, `platform/k8s/gcp/base/validatingadmissionpolicy-provisioner-jobs.yaml`, `shifter/engine/provisioner/config/{_gce,_gce_profile,_gce_image_keys}.py` | Use existing `GCP_RANGE_LINUX_IMAGE`; inspect `GCP_RANGE_IMAGE_KEY_PROFILES_JSON` only where it actually references the affected artifact. Preserve closed env/admission shapes and profile validation. Prefer the exact replacement image resource for auditable remediation; a family is a mutable selector. No PHP-policy env variable belongs in the portal or Job. |
| Registry/auth/persistence: CMS `RaesImageMappingRegisterSerializer`, `CMS_WRITE_PERMISSIONS`, `engine.services._raes_image.upsert_raes_image_mapping`, commands `raes_image_registry` and `seed_raes_image_registry` | Reuse the validated service write path and existing DTO/options. Preserve sizing, management login/port, capability and portable identity fields when updating a mapping. Seeding converges base any-version rows, not every version-specific or custom mapping. Do not use direct SQL, a migration or bulk reseeding that overwrites unrelated operator choices. |
| Frozen launch inputs: `shared.raes.{image_policy,operation_input_candidates,artifact_binding}`, `shared.runtime_plugin_binding`, provisioner `raes_ec2_image.py` and `raes_gce_image.py` | Inventory concrete references, version-specific rows, administrator adapter image profiles, admitted artifacts and queued/frozen generations as applicable. Updating the registry does not rewrite them. Preserve generation/provenance fencing; prevent stale work from creating vulnerable guests through the owning lifecycle, rather than editing immutable input or replacing a digest with a name. Pack-owned artifacts remain with their owner. |
| Live access and secrets: provisioner `executors/{factory,ec2_guest_transport,guest_ssh_executor,ssm_executor}.py` | Use authenticated operator access and each host's actual management coordinates. Native EC2 guests intentionally have no SSM role; reuse private SSH with pinned host key and management secret references where applicable. GCE/IAP build validation and runtime private SSH are distinct transports. Preserve management/participant separation, private networking and existing cloud secret stores. Do not use participant credentials, disable host-key checking, expose SSH/Apache publicly or grant guest SSM/cloud permissions for convenience. |
| OS execution/MCP: `mcp/shared/aws-helpers.js`, `mcp/ops/{schemas,policy,audit,respond}.js`, `.shifter.yaml` | Existing ops schemas/policy and `ok`/`err` envelopes govern any tool use. Keep explicit target environment/account/project/region and owned instance IDs. Pass local CLI arguments as arrays and remote commands as structured payloads/fixed scripts; local argv safety does not validate remote shell text. Secrets stay in existing auth mechanisms or protected input/files, never argv, remote command history, shell tracing or uploaded logs. No new arbitrary-command tool or policy bypass. |
| Errors/observability: shell exit status, `BaseImageError`, `RaesImageMappingError`, `executors.base` errors, `shared.api.errors.api_error_response`, `shared.log_sanitize`, provisioner `log_redact` | Reuse the owning failure/envelope boundary; add no exception hierarchy. Observe terminal command exit per host, bounded timeouts, package readback, restart and health results. An accepted SSM command is not completion. Keep safe package/version diagnostics; sanitize provider output before public records. `terraform_ops._safe_failure_message` truncates most exception text without redacting it. |

Paths abbreviated as `shared` and `engine` above are under
`shifter/shifter_platform`; provisioner paths are under
`shifter/engine/provisioner`. These are downstream boundaries to preserve, not
an instruction to modify every listed subsystem.

## Live-host evidence and operational gotchas

AWS promotion copies the dev image to a different prod AMI ID and currently
waits for availability without repeating the fresh-boot verifier: retain the
source-to-copy binding and obtain destination package evidence where deployed.
GCP's native build assigns its family before separate validation; consumers
following that family must not treat its newest member as a validated release.
For any deployed GDC VM Runtime consumer, also inspect the existing
`GDC_UBUNTU_IMAGE_URL` binding in `config/_gdc.py` and its exported disk identity;
a GCE-family update does not update that disk source.

Build prevention, replacement-image publication, consumer rebinding, and
live-host patching are separate proofs. The issue cannot be completed with a
code-only PR or static tests. Repository inspection establishes no live fleet
state. The implementation needs an explicitly scoped, paginated inventory
across the deployed accounts/projects and regions/zones, reconciled with
range ownership/generation and actual guest package inventories. Do not select
hosts solely by a name glob, latest image head, Ubuntu version, or SSM online
list. Check affected derivatives and any nested/pack-owned PHP runtime when
present; a patched outer host does not patch a container or nested VM.

For live hosts, upgrade the installed PHP binary packages from the affected
source family, including mod_php/common/CLI/opcache/extensions and FPM if
present, with dependencies converged. A metapackage update alone is not proof
that all installed vulnerable binaries changed. Do not run the entire bake
`services.sh` on a live host: it installs unrelated services and changes SSH.
Keep maintenance repeatable and bounded; honor apt holds/configuration conflicts
as failures to resolve, never delete package-manager locks or suppress errors.
Unknown OS/package origins require matching vendor evidence, not this floor.

Validate Apache configuration, perform a full restart after upgrade, and check
service/module and local application health; a graceful reload can leave old
workers alive. Restart affected FPM workers if found. Package state on disk
alone cannot establish replacement of loaded code. Do not expose `phpinfo()` or
add a public diagnostic endpoint. Coordinate interruption of active exercises;
preserve scenario content and deliberate application behavior.

Private/no-egress hosts may be unable to reach apt even though management works.
Use the deployment's approved package-delivery/maintenance route; do not add a
public IP, NAT, broad firewall access or unapproved mirror. Unreachable,
held, failed, offline or concurrently created targets remain explicit unresolved
items, never an empty/success result. Account for stopped, paused and warm
capacity before it resumes; resample inventory to detect new old-image hosts.
Retiring or replacing a host must follow its owning lifecycle, not manual cloud
deletion. Do not silently fall back to a known-vulnerable old image on failure.

Retain the exact image identities/build source, installed package versions and
states, per-host terminal result, full-restart/health outcome, inventory scope
and observation time in the existing approved evidence location. Keep private
host/pack names, topology, credentials and raw operational output out of public
issues, PRs, manifests and this note. Public completion can state sanitized
coverage/counts and unresolved categories. Patching is not evidence that an
already exposed host was never compromised; suspicious evidence follows the
existing operator incident process and must not be destroyed by a rebuild.

## Verification and non-goals

Reuse `shifter/packer/tests/test_packer.py`'s controlled-executable behavior
tests and `test_packer_gcp.py`/`test_gcp_validation_tunnel.py` for template and
runner wiring. Verify below-floor, exact-floor and later Debian revisions
(including a revision that sorts differently lexically), absent/unconfigured
packages, query/install failures, and non-jammy input. Verify failure propagation
before publication; string-presence assertions and `test_scripts.sh` are lint,
not security acceptance. Live evidence from both exact replacement images and
the scoped fleet remains necessary.

Run the Packer suite and relevant shell/HCL checks for implementation changes.
Run `python3 scripts/adr_guard/adr_guard.py --all --level ci`; workflow edits
also require actionlint and matching ADR documentation, and Terraform/runtime
changes require their existing stack-native gates. Do not weaken quality
routing, validators or IAM checks to ship an urgent refresh.

Preflight validation: the full ADR check failed on existing expired exceptions
(ADR-019-R1 and ADR-004-R24) and quality-path-ownership gaps. The exception file,
quality ownership configuration and quality workflow were unchanged. Resolve
those through their owning work; this note neither renews nor bypasses them.

No PHP major/Ubuntu release migration, Apache-to-FPM redesign, application
rewrite, provisioning-time patch loop, new scheduler/fleet agent, patch database,
API/controller/DTO, generic version comparator, new cloud/service/hosting,
scenario-specific branching or private pack content belongs in this issue.
No alteration of pinned historical generations or provenance is permitted.
This preflight performs no builds, publication, deployments, host mutations,
requirement creation or PR/issue workflow transitions.
