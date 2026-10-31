"""GCP control-plane Terraform, Helm, identity, and runtime bootstrap operations."""

import importlib.util
import ipaddress
import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from bootstrap_core import (
    _GDC_APISERVER_BACKEND_PORT,
    _GDC_SCENARIO_POD_KALI_IMAGE,
    _GKE_WORKLOAD_IDENTITY_ANNOTATION,
    _UNKNOWN_ERROR,
    GDCBootstrapConfig,
    _sample_guest_access_defaults,
    code_block,
    confirm,
    error,
    header,
    info,
    run_cmd,
    success,
    warn,
)
from gdc_cluster import (
    ensure_gdc_apis,
    ensure_gdc_instances,
    ensure_gdc_network,
    ensure_gdc_service_account,
    run_gdc_workstation_script,
    stage_gdc_bootstrap_assets,
    sync_gdc_access_secret,
    sync_gdc_instance_ssh_metadata,
    sync_gdc_vm_image_secret,
    upload_gdc_assets,
    wait_for_gdc_ssh,
)

_GUACAMOLE_RUNTIME_RESOURCE_NAME = "guacamole-runtime"
_GCP_MIGRATION_TMP_DIR = "/var/run/shifter-migrate"
_K8S_COMPONENT_LABEL = "app.kubernetes.io/component"
_K8S_PART_OF_LABEL = "app.kubernetes.io/part-of"


def _load_python_script_module(script_path: Path, module_name: str) -> ModuleType:
    """Load a local Python script as a module without changing repo packaging."""
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Python module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get_output_value(outputs: dict[str, dict[str, object]], key: str) -> object:
    """Return the Terraform output value for a key or raise a clear error."""
    try:
        return outputs[key]["value"]
    except KeyError as exc:
        raise KeyError(f"Missing Terraform output: {key}") from exc


def _get_string_mapping_output(outputs: dict[str, dict[str, object]], key: str) -> dict[str, str]:
    """Return a Terraform output value as a string mapping."""
    value = _get_output_value(outputs, key)
    if not isinstance(value, dict):
        raise TypeError(f"Terraform output {key} must be a mapping")
    return {str(item_key): str(item_value) for item_key, item_value in value.items()}


def _unique_nonempty_strings(values: list[str | None]) -> list[str]:
    """Return non-empty strings in first-seen order."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = (raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _host_as_single_address_cidr(value: object) -> str | None:
    """Convert a Terraform host/IP output into a /32 or /128 CIDR."""
    if value is None:
        return None
    host = str(value).strip()
    if not host:
        return None
    if "/" in host:
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"Expected IP address Terraform output, got {host!r}") from exc
    prefix = 32 if address.version == 4 else 128
    return f"{address}/{prefix}"


def render_gcp_platform_runtime_env(config: GDCBootstrapConfig) -> str:
    """Render the static, project-aware runtime env contract for the GKE control plane."""
    lines = [
        # CLOUD_PROVIDER is not rendered here: _render_gcp_runtime_env merges this
        # bootstrap-owned contract with scripts/gcp/render_runtime_env.py's output,
        # which is the renderer-owned source of the backend identity (PLAT-2005).
        # A second hardcoded literal here would be redundant and could silently
        # drift from that renderer-owned value.
        f"ENVIRONMENT={config.environment}",
        f"CLOUD_REGION={config.region}",
        f"GCP_REGION={config.region}",
        # AWS_REGION == CLOUD_REGION in Django settings (config/_cloud.py), so the
        # provisioner-launcher always emits a non-empty AWS_REGION even on GCP. It
        # must therefore be present in the runtime ConfigMap or the
        # restrict-provisioner-jobs admission policy denies the range Job (#1742).
        f"AWS_REGION={config.region}",
        f"GCP_PROJECT_ID={config.project_id}",
        f"GOOGLE_CLOUD_PROJECT={config.project_id}",
        # GCE range cells are the default GCP range backend; the GDC VM Runtime
        # block below is retained and re-selected with GCP_RANGE_BACKEND=gdc.
        "GCP_RANGE_BACKEND=gce",
        "ENGINE_TASK_NAMESPACE=shifter-jobs",
        "ENGINE_TASK_SERVICE_ACCOUNT_NAME=provisioner",
        "ENGINE_TASK_IMAGE_PULL_POLICY=Always",
        "GDC_VM_STORAGE_CLASS=local-shared",
        "# Palo Alto VM-Series on GDC VM Runtime. These are required before creating",
        "# a GCP/GDC NGFW; values are intentionally explicit because this is not a",
        "# generic firewall path.",
        "GDC_VMSERIES_IMAGE_URL=",
        "GDC_VMSERIES_BOOTSTRAP_BUCKET=",
        "GDC_VMSERIES_STORAGE_CLASS=local-shared",
        "GDC_VMSERIES_NAMESPACE_PREFIX=ngfw",
        "GDC_VMSERIES_MGMT_NETWORK_NAME=pod-network",
        "GDC_VMSERIES_MGMT_IP_CIDR=",
        "GDC_VMSERIES_DATA_NETWORK_NAME=",
        "GDC_VMSERIES_DATA_IP_CIDR=",
        "GDC_VMSERIES_ROUTE_NEXT_HOP_IP=",
        "GDC_VMSERIES_VCPUS=4",
        "GDC_VMSERIES_MEMORY=8Gi",
        "GDC_VMSERIES_DISK_SIZE_GIB=81",
        "GDC_VMSERIES_BOOTSTRAP_DISK_SIZE_GIB=1",
        "# Guest access defaults for VM Runtime assets.",
        *_sample_guest_access_defaults(),
        "# VM Runtime boot images, exported by the packer-gcp pipeline to the GDC",
        "# VM image bucket. DISK_SIZE_GIB must be >= the exported qcow2 virtual",
        "# size (the GCE builder disk size): ubuntu 20, kali 40, windows/dc 64.",
        f"GDC_KALI_IMAGE_URL={config.gdc_vm_image_url('kali')}",
        "GDC_KALI_VCPUS=2",
        "GDC_KALI_MEMORY=4Gi",
        "GDC_KALI_DISK_SIZE_GIB=40",
        f"GDC_UBUNTU_IMAGE_URL={config.gdc_vm_image_url('ubuntu')}",
        "GDC_UBUNTU_VCPUS=1",
        "GDC_UBUNTU_MEMORY=2Gi",
        "GDC_UBUNTU_DISK_SIZE_GIB=20",
        f"GDC_WINDOWS_IMAGE_URL={config.gdc_vm_image_url('windows')}",
        "GDC_WINDOWS_VCPUS=2",
        "GDC_WINDOWS_MEMORY=8Gi",
        "GDC_WINDOWS_DISK_SIZE_GIB=64",
        f"GDC_DC_IMAGE_URL={config.gdc_vm_image_url('dc')}",
        "GDC_DC_VCPUS=2",
        "GDC_DC_MEMORY=8Gi",
        "GDC_DC_DISK_SIZE_GIB=64",
        "# Optional overrides for lower-fidelity in-range scenario Pods.",
        "GDC_SCENARIO_POD_IMAGE_PULL_POLICY=IfNotPresent",
        f"GDC_SCENARIO_POD_KALI_IMAGE={_GDC_SCENARIO_POD_KALI_IMAGE}",
        "GDC_SCENARIO_POD_UBUNTU_IMAGE=docker.io/library/ubuntu:24.04",
        # settings.operator_bootstrap fills these lists through the renderer
        # projection, which overrides this contract (gcp_runtime.compose_runtime_env).
        "PLATFORM_BOOTSTRAP_STAFF_EMAILS=",
        "PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS=",
    ]
    return "".join(f"{line}\n" for line in lines)


def parse_env_contract(rendered: str) -> dict[str, str]:
    """Parse KEY=VALUE env contract text into a mapping, ignoring blank lines and comments."""
    values: dict[str, str] = {}
    for raw_line in rendered.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"Invalid env contract line: {raw_line!r}")
        values[key] = value
    return values


def validate_image_tag(image_tag: str) -> str:
    """Return a non-moving image tag suitable for deployment manifests."""
    tag = image_tag.strip()
    if not tag:
        raise ValueError("image tag must be non-empty")
    if tag == "latest":
        raise ValueError("image tag must be immutable; refusing to use latest")
    return tag


def _resolve_operator_email_domain(outputs: dict[str, dict[str, object]] | None) -> tuple[str, str]:
    """Resolve the required operator email domain and its source (Terraform output, else env)."""
    if outputs is not None:
        tf_value = outputs.get("identity_allowed_email_domain", {}).get("value")
        if isinstance(tf_value, str) and tf_value.strip():
            return tf_value.strip().lower(), "Terraform output identity_allowed_email_domain"
    env_value = os.environ.get("SHIFTER_GCP_OPERATOR_EMAIL_DOMAIN", "").strip().lower()
    if env_value:
        return env_value, "SHIFTER_GCP_OPERATOR_EMAIL_DOMAIN"
    return "", ""


def _validate_gcp_bootstrap_operator_email(
    email: str,
    outputs: dict[str, dict[str, object]] | None = None,
) -> None:
    """Validate the bootstrap operator email against the Identity Platform allow-list.

    The shape check (must contain a single `@`, non-empty local + domain parts)
    is always enforced. The domain restriction is derived, in order:

    1. The ``identity_allowed_email_domain`` Terraform output (when ``outputs``
       is supplied) — this is the same value the Identity Platform
       ``beforeCreate`` hook uses, so the bootstrap operator the bootstrap
       script writes into ``PLATFORM_BOOTSTRAP_*`` will actually be able to
       sign in to the deployed portal.
    2. The ``SHIFTER_GCP_OPERATOR_EMAIL_DOMAIN`` environment variable as a
       fallback for callers that have not yet run Terraform (e.g., unit tests
       or dry-run flows). Unset means "accept any well-formed email" — only
       legitimate when no Identity Platform deployment is in scope.
    """
    if email.count("@") != 1:
        raise ValueError("GCP operator email must contain exactly one '@' character")
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise ValueError("GCP operator email must have a non-empty local part and domain")

    required_domain, source = _resolve_operator_email_domain(outputs)

    if required_domain and not email.lower().endswith(f"@{required_domain}"):
        raise ValueError(
            f"GCP operator email must use the {required_domain} domain "
            f"(constraint from {source}). Bootstrap-time validation matches the "
            "Identity Platform allow-list, so an operator whose domain fails "
            "here cannot subsequently sign in to the deployed portal."
        )


def _gcp_identity_access_token() -> str:
    """Return a gcloud access token for Identity Platform admin calls."""
    result = subprocess.run(  # nosec B603 B607
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else _UNKNOWN_ERROR
        raise RuntimeError(f"Failed to acquire a GCP access token for Identity Platform: {stderr}")
    return result.stdout.strip()


def _gcp_identity_admin_request(
    *,
    config: GDCBootstrapConfig,
    outputs: dict[str, dict[str, object]],
    path: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Call one validated Identity Platform admin endpoint."""
    del outputs
    access_token = _gcp_identity_access_token()
    url = f"https://identitytoolkit.googleapis.com/v1{path}"
    parsed_url = urllib_parse.urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "identitytoolkit.googleapis.com" or parsed_url.query:
        raise RuntimeError(f"Refusing to call unexpected Identity Platform endpoint: {url}")
    request = urllib_request.Request(  # noqa: S310  # URL is validated immediately above
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": config.project_id,
        },
        method="POST",
    )

    try:
        with urllib_request.urlopen(request, timeout=30) as response:  # nosec B310  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:  # pragma: no cover - exercised via unit tests with monkeypatch
        body = exc.read().decode("utf-8") if exc.fp is not None else ""
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        message = parsed.get("error", {}).get("message", exc.reason)
        raise RuntimeError(str(message)) from exc


def ensure_gcp_identity_platform_operator(
    config: GDCBootstrapConfig,
    outputs: dict[str, dict[str, object]],
    dry_run: bool = False,
    *,
    credentials: tuple[str, str],
) -> str | None:
    """Create the first GCP operator account if it does not already exist.

    The deploy command passes ``credentials`` resolved from ``settings.operator_bootstrap``
    and Secret Manager, or from the CI password variable (#2571).
    """
    email, password = credentials
    _validate_gcp_bootstrap_operator_email(email, outputs=outputs)

    if dry_run:
        info("[DRY-RUN] Would create or verify the Identity Platform operator account")
        return email

    try:
        _gcp_identity_admin_request(
            config=config,
            outputs=outputs,
            path=f"/projects/{config.project_id}/accounts",
            payload={
                "email": email,
                "password": password,
                "displayName": "Shifter Operator",
                "emailVerified": True,
            },
        )
        success("Created the Identity Platform operator account")
        return email
    except RuntimeError as exc:
        if "EMAIL_EXISTS" in str(exc):
            info("The Identity Platform operator account already exists")
            return email
        raise


def gcp_identity_platform_subject(config: GDCBootstrapConfig, outputs: dict[str, dict[str, object]], email: str) -> str:
    """Return the Identity Platform account ID (the token subject) of the operator email (#2679)."""
    response = _gcp_identity_admin_request(
        config=config,
        outputs=outputs,
        path=f"/projects/{config.project_id}/accounts:lookup",
        payload={"email": [email.strip().lower()]},
    )
    users = response.get("users")
    if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
        raise RuntimeError("The Identity Platform operator account is missing; enable settings.operator_bootstrap")
    subject = users[0].get("localId")
    if not isinstance(subject, str) or not subject.strip():
        raise RuntimeError("The Identity Platform operator account has no account ID")
    return subject


def _gcp_private_service_cidrs(outputs: dict[str, dict[str, object]]) -> list[str]:
    """Return private-service CIDRs for Helm network policy values."""
    control_plane_database = _get_output_value(outputs, "control_plane_database")
    control_plane_cache = _get_output_value(outputs, "control_plane_cache")
    guacamole_database = _get_output_value(outputs, "guacamole_database")
    return _unique_nonempty_strings(
        [
            _host_as_single_address_cidr(control_plane_database.get("private_ip")),
            _host_as_single_address_cidr(control_plane_cache.get("host")),
            _host_as_single_address_cidr(guacamole_database.get("host")),
        ]
    )


def _helm_service_account_values(service_accounts: dict[str, str]) -> dict[str, object]:
    """Return workload-identity annotations for chart service accounts."""
    values = {
        "portal": {"annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["portal"]}},
        "workers": {"annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["workers"]}},
        "ctfScheduler": {"annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["ctf-scheduler"]}},
        "migrator": {
            "name": "migrator",
            "annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["migrator"]},
        },
        "provisionerLauncher": {
            "name": "provisioner-launcher",
            "annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["provisioner-launcher"]},
        },
        "provisioner": {"annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["provisioner"]}},
    }
    if service_accounts.get("guacamole-client"):
        values["guacamoleClient"] = {
            "annotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: service_accounts["guacamole-client"]}
        }
    return values


# The acquisition Job reaches public package registries and object storage on 443
# through the portal VPC's Cloud NAT; private, carrier-grade NAT and link-local
# ranges are carved out so it cannot reach the cluster, the VPC or the metadata
# server through this rule (Workload Identity's metadata access is a separate,
# port-scoped chart policy).
_ACQUISITION_EGRESS_EXCEPT = (
    "10.0.0.0/8",  # NOSONAR - RFC 1918 private range, excluded.
    "172.16.0.0/12",  # NOSONAR - RFC 1918 private range, excluded.
    "192.168.0.0/16",  # NOSONAR - RFC 1918 private range, excluded.
    "100.64.0.0/10",  # NOSONAR - carrier-grade NAT range, excluded.
    "169.254.0.0/16",  # NOSONAR - link-local range, excluded.
)


def _gcp_feature_artifact_acquisition_values(acquirer: object) -> dict[str, object]:
    """Acquisition Job identity and egress values (#2479); empty when disabled."""
    if not acquirer:
        return {"serviceAccountAnnotations": {}, "egressCidrs": [], "egressExcept": []}
    return {
        "serviceAccountAnnotations": {_GKE_WORKLOAD_IDENTITY_ANNOTATION: str(acquirer)},
        "egressCidrs": ["0.0.0.0/0"],  # NOSONAR - public 443 for the acquisition namespace only.
        "egressExcept": list(_ACQUISITION_EGRESS_EXCEPT),
    }


def _helm_image_values(image_identities: dict[str, str]) -> dict[str, str]:
    """Return exact attested image identities for chart workloads."""
    required = {"platform", "guacd", "guacamoleClient"}
    if set(image_identities) != required:
        raise ValueError(f"GCP Helm image identities must contain exactly {', '.join(sorted(required))}")
    digest_pattern = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
    for name, identity in image_identities.items():
        if not digest_pattern.fullmatch(identity):
            raise ValueError(f"GCP Helm image {name!r} must be repository@sha256:<64 lowercase hex>")
    return dict(image_identities)


def _helm_ingress_values(
    outputs: dict[str, dict[str, object]],
    *,
    public_hostname: str,
    managed_tls_enabled: bool,
) -> dict[str, object]:
    """Return public ingress chart values."""
    return {
        "enabled": True,
        "class": "gce",
        "staticIpName": _get_output_value(outputs, "public_ingress_ip_name"),
        "host": public_hostname,
        "managedTls": {
            "enabled": managed_tls_enabled,
            "certificateName": "platform-managed-cert",
            "frontendConfigName": "platform-frontend-config",
        },
    }


def _helm_backend_config_values(edge_policy_name: str) -> dict[str, object]:
    """Return backend-config values for public services."""
    backend = {"enabled": True, "securityPolicyName": edge_policy_name}
    return {
        "portal": {"backendConfig": {**backend, "name": "portal-web"}},
        "guacamoleClient": {"backendConfig": {**backend, "name": "guacamole-client"}},
    }


def _helm_network_policy_values(
    private_service_cidrs: list[str],
    kubernetes_api_cidrs: list[str],
    range_cluster_api_cidrs: list[str],
    range_cluster_api_port: int,
    range_access_cidrs: list[str],
) -> dict[str, object]:
    """Return network-policy CIDR values for the Helm release."""
    return {
        "enabled": True,
        "gclbSourceRanges": [
            "35.191.0.0/16",  # NOSONAR - Google Cloud Load Balancer health check/proxy range.
            "130.211.0.0/22",  # NOSONAR - Google Cloud Load Balancer health check/proxy range.
        ],
        "googleApiCidrs": [
            "199.36.153.4/30",  # NOSONAR - restricted.googleapis.com VIP range.
            "199.36.153.8/30",  # NOSONAR - private.googleapis.com VIP range.
        ],
        "privateServiceCidrs": private_service_cidrs,
        "kubernetesApiCidrs": kubernetes_api_cidrs,
        "rangeClusterApiCidrs": range_cluster_api_cidrs,
        "rangeClusterApiPort": range_cluster_api_port,
        # Participant/operator range access (issue #1349): the portal and guacd
        # workloads dial range guests on the range network for browser SSH and
        # Guacamole SSH/RDP. Scope egress to the range network CIDR; the chart
        # defaults the ports (22/3389). Empty list -> no policy rendered.
        "rangeAccessCidrs": range_access_cidrs,
    }


@dataclass(frozen=True)
class GcpRenderArtifacts:
    """Installation-render artifacts threaded from validated root config into Helm values.

    Grouped so ``render_gcp_helm_values`` stays within the parameter budget (S107): the
    model-access catalog/env feed the model broker, and the Mission Control lease policy
    env (#27) merges into the runtime ConfigMap.
    """

    model_access_catalog_json: str = ""
    model_access_env: str = ""
    mission_control_lease_env: str = ""
    range_telemetry_env: str = ""
    model_broker_runtime: dict | None = None
    openfga_settings: dict | None = None
    capacity_profile_id: str = "gcp-shared-v1-p10"


def _merge_capacity_values(target: dict[str, object], projection: dict[str, object]) -> dict[str, object]:
    """Merge the typed capacity projection without replacing unrelated chart values."""
    for key, projected in projection.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(projected, dict):
            _merge_capacity_values(current, projected)
        else:
            target[key] = projected
    return target


def render_gcp_helm_values(
    config: GDCBootstrapConfig,
    outputs: dict[str, dict[str, object]],
    *,
    runtime_env: Mapping[str, str],
    image_identities: dict[str, str],
    render_artifacts: "GcpRenderArtifacts | None" = None,
) -> dict[str, object]:
    """Render non-secret Helm values for the Shifter release from Terraform outputs.

    ``runtime_env`` is the composed platform runtime contract (``gcp_runtime``), and
    ``image_identities`` names the exact verified release digests (#2571).
    """
    from installation.capacity_profiles_gcp import resolve_capacity_profile
    from installation.gcp_model_broker import project_model_broker

    artifacts = render_artifacts or GcpRenderArtifacts()
    service_accounts = _get_output_value(outputs, "workload_service_accounts")
    public_hostname = str(_get_output_value(outputs, "public_hostname")).strip()
    managed_tls_enabled = bool(_get_output_value(outputs, "managed_tls_enabled"))
    if not public_hostname or not managed_tls_enabled:
        # ADR-008-R1/R3: never render a public HTTP or ingress-IP edge.
        raise ValueError(
            "GCP Helm values require public_hostname and managed_tls_enabled; refusing to render an insecure edge"
        )
    runtime_env = dict(runtime_env)
    # Mission Control lease policy (#27): derived from the validated root config and
    # merged into the runtime env so a configured policy (including extensions_enabled:
    # false) reaches the platform-runtime ConfigMap rather than defaulting. The value is
    # authoritative here, independent of this render process's environment.
    if artifacts.mission_control_lease_env:
        runtime_env.update(parse_env_contract(artifacts.mission_control_lease_env))
    # Range audit telemetry policy (#2535): the provisioner applies the same validated
    # settings.range_telemetry the Terraform bridge consumed.
    if artifacts.range_telemetry_env:
        runtime_env.update(parse_env_contract(artifacts.range_telemetry_env))
    edge_policy_name = str(_get_output_value(outputs, "cloud_armor_security_policy_name")).strip()
    # The range-provisioning Jobs reach the GDC range cluster apiserver through
    # the internal TCP load balancer on the peered range VPC. Allow egress to
    # exactly that endpoint host/port; empty until the endpoint is wired.
    range_cluster_host, _, range_cluster_port = (config.control_plane_platform_endpoint or "").partition(":")
    range_cluster_api_cidrs = _unique_nonempty_strings([_host_as_single_address_cidr(range_cluster_host)])
    # Range network CIDR the portal/guacd workloads reach for participant SSH/RDP
    # (issue #1349). Empty until the range network is provisioned, which leaves
    # the range-access egress policy unrendered.
    range_access_cidrs = _unique_nonempty_strings([str(_get_output_value(outputs, "range_network_cidr")).strip()])

    broker = project_model_broker(
        outputs.get("model_broker", {}).get("value"),
        catalog_json=artifacts.model_access_catalog_json,
        model_access_env=artifacts.model_access_env,
        runtime_settings=artifacts.model_broker_runtime,
    )
    runtime_env.update(broker.get("enrollment_env", {}))
    images = _helm_image_values(image_identities)
    acquirer = service_accounts.get("artifact-acquirer") if isinstance(service_accounts, Mapping) else None
    if acquirer:
        # Isolated acquisition Jobs run exactly the attested platform image, which
        # their admission policy pins (#2479).
        runtime_env["FEATURE_ARTIFACT_JOB_IMAGE"] = images["platform"]
    values: dict[str, object] = {
        "releaseNamespace": "shifter-system",
        "modelBroker": broker,
        "capabilities": {"featureArtifactAcquisition": bool(acquirer)},
        "featureArtifactAcquisition": _gcp_feature_artifact_acquisition_values(acquirer),
        "serviceAccounts": _helm_service_account_values(service_accounts),
        "runtimeEnv": runtime_env,
        # Reference only: the guacamole-runtime Kubernetes Secret is synced out
        # of band from Secret Manager (see sync_gcp_guacamole_runtime_secret).
        # Secret values must never enter Helm values or release history (#1180).
        "guacamoleRuntimeSecret": {"name": _GUACAMOLE_RUNTIME_RESOURCE_NAME},
        "images": images,
        "edge": {
            "ingress": {
                "enabled": True,
                "className": "gce",
                "annotations": {
                    "kubernetes.io/ingress.global-static-ip-name": str(
                        _get_output_value(outputs, "public_ingress_ip_name")
                    )
                },
                "host": public_hostname,
                "tls": {"enabled": False, "secretName": ""},
                "gcpManagedTls": {
                    "enabled": managed_tls_enabled,
                    "certificateName": "platform-managed-cert",
                    "frontendConfigName": "platform-frontend-config",
                },
            }
        },
        "services": _helm_backend_config_values(edge_policy_name),
        "network": {
            "enabled": True,
            "ingressSourceCidrs": [
                "35.191.0.0/16",  # NOSONAR - Google Cloud Load Balancer range.
                "130.211.0.0/22",  # NOSONAR - Google Cloud Load Balancer range.
            ],
            "providerApiCidrs": [
                "199.36.153.4/30",  # NOSONAR - restricted.googleapis.com VIP.
                "199.36.153.8/30",  # NOSONAR - private.googleapis.com VIP.
            ],
            "privateServiceCidrs": _gcp_private_service_cidrs(outputs),
            # Both the services CIDR (the kubernetes.default ClusterIP) and the
            # GKE master CIDR: under Dataplane V2 the API ClusterIP (10.x.0.1)
            # DNATs to the private control-plane endpoint in the master CIDR, so
            # the launcher's egress must allow both or it cannot reach the API
            # server to create the provisioner Job.
            "kubernetesApiCidrs": [
                str(_get_output_value(outputs, "gke_services_cidr")).strip(),
                str(_get_output_value(outputs, "gke_master_ipv4_cidr")).strip(),
            ],
            "rangeClusterApiCidrs": range_cluster_api_cidrs,
            "rangeClusterApiPort": int(range_cluster_port or _GDC_APISERVER_BACKEND_PORT),
            "rangeAccessCidrs": range_access_cidrs,
            "rangeAccessPorts": [22, 3389],
        },
    }
    capacity = resolve_capacity_profile(artifacts.capacity_profile_id)
    from installation.gcp_native_identity import project_native_helm

    values.update(project_native_helm(outputs, openfga_settings=artifacts.openfga_settings))
    return _merge_capacity_values(values, capacity.helm_projection())


def fetch_gcp_secret_payload(secret_id: str, project_id: str) -> str:
    """Return the latest Secret Manager payload for the given secret resource/name."""
    secret_name = secret_id.rstrip("/").split("/")[-1]
    result = subprocess.run(  # nosec B603 B607
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            secret_name,
            "--project",
            project_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else _UNKNOWN_ERROR
        raise RuntimeError(f"Failed to read Secret Manager payload for {secret_name}: {stderr}")
    return result.stdout


def get_latest_gcp_secret_payload(secret_id: str, project_id: str) -> str | None:
    """Return the latest secret payload when one exists, otherwise None."""
    try:
        return fetch_gcp_secret_payload(secret_id, project_id)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "not found" in message or "has no versions" in message:
            return None
        raise


def ensure_gcp_artifact_registry_service_identity(config: GDCBootstrapConfig, dry_run: bool = False) -> None:
    """Force-create the Artifact Registry service agent before Terraform grants it IAM."""
    service = "artifactregistry.googleapis.com"
    if dry_run:
        info(f"[DRY-RUN] Would provision the {service} service identity (P4SA)")
        return

    # Terraform also enables the service, but the service identity must exist
    # before Terraform grants that identity the CMEK encrypter role.
    run_cmd(["gcloud", "services", "enable", service, "--project", config.project_id])

    access_token = _gcp_identity_access_token()
    url = (
        "https://serviceusage.googleapis.com/v1beta1/"
        f"projects/{config.project_id}/services/{service}:generateServiceIdentity"
    )
    parsed_url = urllib_parse.urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "serviceusage.googleapis.com" or parsed_url.query:
        raise RuntimeError(f"Refusing to call unexpected Service Usage endpoint: {url}")
    request = urllib_request.Request(  # noqa: S310  # URL is validated immediately above
        url,
        data=b"",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": config.project_id,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=30) as response:  # nosec B310  # noqa: S310
            json.loads(response.read().decode("utf-8"))
        success(f"Ensured {service} service identity exists")
    except urllib_error.HTTPError as exc:  # pragma: no cover - exercised via unit tests with monkeypatch
        body = exc.read().decode("utf-8") if exc.fp is not None else ""
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        message = parsed.get("error", {}).get("message", exc.reason)
        raise RuntimeError(f"Failed to provision {service} service identity: {message}") from exc


def render_gcp_guacamole_runtime_secret_manifest(
    guacamole_db_payload: dict[str, str],
    guacamole_json_auth_value: str,
) -> dict[str, object]:
    """Render the Kubernetes Secret manifest consumed by guacamole-client."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": _GUACAMOLE_RUNTIME_RESOURCE_NAME,
            "namespace": "shifter-platform",
            "labels": {_K8S_PART_OF_LABEL: "shifter"},
        },
        "type": "Opaque",
        "stringData": {
            "POSTGRESQL_USER": guacamole_db_payload["username"],
            "POSTGRESQL_PASSWORD": guacamole_db_payload["password"],
            "JSON_SECRET_KEY": guacamole_json_auth_value,
        },
    }


def sync_gcp_guacamole_runtime_secret(
    config: GDCBootstrapConfig,
    outputs: dict[str, dict[str, object]],
    *,
    dry_run: bool = False,
) -> bool:
    """Sync guacamole-client runtime credentials to Kubernetes without writing them to disk."""
    if dry_run:
        info("[DRY-RUN] Would sync guacamole-runtime Kubernetes Secret from GCP Secret Manager")
        return False

    runtime_secret_ids = _get_string_mapping_output(outputs, "runtime_secret_ids")
    guacamole_db_payload = json.loads(fetch_gcp_secret_payload(runtime_secret_ids["guacamole-db"], config.project_id))
    guacamole_json_auth_value = fetch_gcp_secret_payload(
        runtime_secret_ids["guacamole-json-auth"],
        config.project_id,
    ).strip()
    manifest = render_gcp_guacamole_runtime_secret_manifest(guacamole_db_payload, guacamole_json_auth_value)
    result = subprocess.run(  # nosec B603 B607
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(manifest),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to sync guacamole-runtime Kubernetes Secret")
    status = result.stdout.strip().lower()
    return "created" in status or "configured" in status


def _get_kubernetes_namespace(name: str) -> dict[str, object] | None:
    """Return namespace JSON or None when the namespace does not exist."""
    result = subprocess.run(  # nosec B603 B607
        ["kubectl", "get", "namespace", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)

    stderr = result.stderr.strip().lower() if result.stderr else ""
    if f'namespaces "{name}" not found' in stderr:
        return None

    raise RuntimeError(f"Failed to inspect namespace {name}: {result.stderr}")


def _wait_for_namespace_absent(name: str, timeout_seconds: int = 300, poll_seconds: int = 2) -> None:
    """Wait until a namespace no longer exists."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        namespace = _get_kubernetes_namespace(name)
        if namespace is None:
            return
        time.sleep(poll_seconds)

    raise RuntimeError(f"Namespace {name} is still terminating after {timeout_seconds} seconds")


def _wait_for_namespace_active(name: str, timeout_seconds: int = 120, poll_seconds: int = 2) -> None:
    """Wait until a namespace exists and is Active."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        namespace = _get_kubernetes_namespace(name)
        if namespace and namespace.get("status", {}).get("phase") == "Active":
            return
        time.sleep(poll_seconds)

    raise RuntimeError(f"Namespace {name} did not become Active within {timeout_seconds} seconds")


def ensure_gcp_control_plane_namespaces(dry_run: bool = False) -> None:
    """Ensure Helm target namespaces exist outside of the release lifecycle."""
    namespace_specs = {
        "shifter-platform": {
            _K8S_PART_OF_LABEL: "shifter",
            "shifter.dev/plane": "control",
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/warn": "restricted",
        },
        "shifter-jobs": {
            _K8S_PART_OF_LABEL: "shifter",
            "shifter.dev/plane": "jobs",
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/warn": "restricted",
        },
    }

    for namespace_name, labels in namespace_specs.items():
        namespace = _get_kubernetes_namespace(namespace_name)
        if namespace and namespace.get("metadata", {}).get("deletionTimestamp"):
            warn(f"Namespace {namespace_name} is terminating; waiting for deletion before recreating it")
            if not dry_run:
                _wait_for_namespace_absent(namespace_name)

        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace_name,
                "labels": labels,
            },
        }

        if dry_run:
            info(f"Would apply namespace manifest for {namespace_name}")
            continue

        subprocess.run(  # nosec B603 B607
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(manifest),
            text=True,
            check=True,
            capture_output=True,
        )
        _wait_for_namespace_active(namespace_name)


def _gcp_migration_prerequisites(
    service_account: str,
    runtime_data: dict[str, str],
) -> dict[str, object]:
    """Build the Helm-owned service account and runtime configuration."""
    helm_labels = {
        _K8S_PART_OF_LABEL: "shifter",
        "app.kubernetes.io/managed-by": "Helm",
    }
    helm_annotations = {
        "meta.helm.sh/release-name": "shifter",
        "meta.helm.sh/release-namespace": "shifter-system",
    }
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": "migrator",
                    "namespace": "shifter-platform",
                    "labels": {**helm_labels, _K8S_COMPONENT_LABEL: "migrator"},
                    "annotations": {
                        **helm_annotations,
                        _GKE_WORKLOAD_IDENTITY_ANNOTATION: service_account,
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "platform-runtime",
                    "namespace": "shifter-platform",
                    "labels": helm_labels,
                    "annotations": helm_annotations,
                },
                "data": runtime_data,
            },
        ],
    }


def render_gcp_migration_manifests(
    outputs: dict[str, dict[str, object]], values: Mapping[str, object], *, duties_only: bool = False
) -> tuple[dict[str, object], dict[str, object]]:
    """Render Helm-owned prerequisites and the one-off schema migration Job.

    ``duties_only`` renders the ``platform-duties`` Job, which reconciles the
    SQL duties without a schema migration (#2704).
    """
    runtime_env = values.get("runtimeEnv")
    images = values.get("images")
    if not isinstance(runtime_env, Mapping) or not isinstance(images, Mapping):
        raise ValueError("GCP migration requires rendered runtimeEnv and images mappings")
    platform_image = str(images.get("platform", ""))
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", platform_image):
        raise ValueError("GCP migration platform image must be repository@sha256:<64 lowercase hex>")
    if not str(runtime_env.get("DB_MIGRATION_SECRET_ID", "")).strip():
        raise ValueError("GCP migration requires DB_MIGRATION_SECRET_ID")

    service_accounts = _get_string_mapping_output(outputs, "workload_service_accounts")
    runtime_data = {str(key): str(value) for key, value in runtime_env.items()}
    capabilities = values.get("capabilities")
    if isinstance(capabilities, Mapping) and capabilities.get("kubernetesJobLauncher"):
        runtime_data["ENGINE_TASK_NAMESPACE"] = "shifter-jobs"
        runtime_data["ENGINE_TASK_SERVICE_ACCOUNT_NAME"] = "provisioner"
    from installation.gcp_migrations import render_migration_job, render_migration_native_sql_egress

    proxy = values.get("cloudSqlProxy", {})
    instance = str(proxy.get("instanceConnectionName", "")) if proxy.get("enabled") else ""
    prerequisites = _gcp_migration_prerequisites(service_accounts["migrator"], runtime_data)
    if instance:
        prerequisites["items"].append(
            render_migration_native_sql_egress(
                proxy.get("databaseCidrs", []),
                provider_api_cidrs=values.get("network", {}).get("providerApiCidrs", []),
            )
        )
    return prerequisites, render_migration_job(
        platform_image,
        instance_connection_name=instance,
        job_name="platform-duties" if duties_only else "platform-migrate",
        duties_only=duties_only,
    )


def run_gcp_database_migrations(
    outputs: dict[str, dict[str, object]], values_path: Path, *, dry_run: bool = False, duties_only: bool = False
) -> None:
    """Run migrations under the dedicated GCP Workload Identity and DB owner.

    ``duties_only`` reconciles only the SQL duties, with no schema migration.
    """
    values = json.loads(values_path.read_text(encoding="utf-8"))
    prerequisites, job = render_gcp_migration_manifests(outputs, values, duties_only=duties_only)
    job_ref = f"job/{job['metadata']['name']}"  # type: ignore[index]
    if dry_run:
        info("[DRY-RUN] Would run the dedicated GCP database migration Job")
        return

    retirement = subprocess.run(  # nosec B603 B607
        [
            "kubectl",
            "delete",
            job_ref,
            "--namespace",
            "shifter-platform",
            "--ignore-not-found",
            "--wait=true",
            "--timeout=180s",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if retirement.returncode != 0:
        raise RuntimeError("Failed to retire the previous GCP migration Job")

    for manifest in (prerequisites, job):
        result = subprocess.run(  # nosec B603 B607
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to apply GCP migration resource: {result.stderr.strip() or _UNKNOWN_ERROR}")

    try:
        wait = subprocess.run(  # nosec B603 B607
            [
                "kubectl",
                "wait",
                "--for=condition=complete",
                job_ref,
                "--namespace",
                "shifter-platform",
                "--timeout=15m",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        logs = subprocess.run(  # nosec B603 B607
            ["kubectl", "logs", job_ref, "--namespace", "shifter-platform", "--all-containers=true"],
            capture_output=True,
            text=True,
            check=False,
        )
        if logs.stdout:
            print(logs.stdout, end="")
        if wait.returncode != 0:
            raise RuntimeError(f"GCP database migration Job failed: {wait.stderr.strip() or _UNKNOWN_ERROR}")
    finally:
        if values.get("cloudSqlProxy", {}).get("enabled"):
            subprocess.run(  # nosec B603 B607 -- bounded migration-lane cleanup
                [
                    "kubectl",
                    "delete",
                    "networkpolicy/platform-migration-native-sql-egress",
                    "--namespace",
                    "shifter-platform",
                    "--ignore-not-found",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        subprocess.run(  # nosec B603 B607
            [
                "kubectl",
                "delete",
                job_ref,
                "--namespace",
                "shifter-platform",
                "--ignore-not-found",
                "--wait=false",
            ],
            capture_output=True,
            text=True,
            check=False,
        )


def _announce_gdc_bootstrap_plan(config: GDCBootstrapConfig) -> str:
    """Print the substrate plan banner and return the confirmation prompt."""
    header(f"Bootstrapping {config.cluster_id} GDC Cluster")
    info(f"GCP Project: {config.project_id}")
    info(f"Region / Zone: {config.region} / {config.zone}")
    info(f"Network: {config.resolved_network_name} ({config.subnet_cidr})")
    info(f"Service Account: {config.service_account_email}")
    info(f"VM Runtime VIPs: control-plane={config.control_plane_vip}, ingress={config.ingress_vip}")
    return "Create or reconcile these GDC bootstrap resources?"


@dataclass(frozen=True)
class _GDCSubstrateOperations:
    """Explicit operation ports for the GDC substrate orchestration."""

    ensure_apis: Callable[..., None] = ensure_gdc_apis
    ensure_service_account: Callable[..., None] = ensure_gdc_service_account
    stage_assets: Callable[..., dict[str, Path]] = stage_gdc_bootstrap_assets
    ensure_network: Callable[..., None] = ensure_gdc_network
    ensure_instances: Callable[..., None] = ensure_gdc_instances
    sync_ssh_metadata: Callable[..., None] = sync_gdc_instance_ssh_metadata
    wait_for_ssh: Callable[..., None] = wait_for_gdc_ssh
    upload_assets: Callable[..., None] = upload_gdc_assets
    run_workstation_script: Callable[..., None] = run_gdc_workstation_script
    sync_access_secret: Callable[..., None] = sync_gdc_access_secret
    sync_vm_image_secret: Callable[..., None] = sync_gdc_vm_image_secret


def _build_gdc_substrate(
    config: GDCBootstrapConfig,
    dry_run: bool = False,
    *,
    operations: _GDCSubstrateOperations | None = None,
) -> None:
    """Create the ABM/GDC VM Runtime substrate (only required for the gdc range backend)."""
    operations = operations or _GDCSubstrateOperations()
    operations.ensure_apis(config, dry_run=dry_run)
    operations.ensure_service_account(config, dry_run=dry_run)

    with tempfile.TemporaryDirectory(prefix="shifter-gdc-bootstrap-") as staging_dir_name:
        staged_assets = operations.stage_assets(config, Path(staging_dir_name), dry_run=dry_run)
        operations.ensure_network(config, dry_run=dry_run)
        operations.ensure_instances(config, staged_assets["ssh_metadata"], dry_run=dry_run)
        operations.sync_ssh_metadata(config, staged_assets["ssh_metadata"], dry_run=dry_run)

        for host in config.all_hosts:
            operations.wait_for_ssh(config, host, dry_run=dry_run)

        operations.upload_assets(config, staged_assets["assets_dir"], dry_run=dry_run)
        operations.run_workstation_script(config, "prepare-workstation.sh", dry_run=dry_run)
        operations.run_workstation_script(config, "prepare-hosts.sh", dry_run=dry_run)
        operations.run_workstation_script(config, "create-cluster.sh", dry_run=dry_run)
        operations.run_workstation_script(config, "install-helper.sh", dry_run=dry_run)
        operations.sync_access_secret(config, dry_run=dry_run)
        operations.sync_vm_image_secret(config, staged_assets["service_account_key"], dry_run=dry_run)


def _report_gdc_bootstrap_success(config: GDCBootstrapConfig) -> None:
    """Print the success banner and the follow-up commands for the substrate."""
    success("GDC bootstrap complete")
    print("\nNext commands:")
    ssh_command = (
        f"gcloud compute ssh root@{config.workstation.name} --tunnel-through-iap "
        f"--project {config.project_id} --zone {config.zone}"
    )
    code_block(
        f"""{ssh_command}
shifter-gdc-kubectl get nodes
shifter-gdc-kubeconfig"""
    )


def _gdc_bootstrap_result(config: GDCBootstrapConfig) -> dict[str, str]:
    """Assemble the gdc-bootstrap return payload."""
    return {
        "project_id": config.project_id,
        "cluster_id": config.cluster_id,
        "region": config.region,
        "zone": config.zone,
        "network_name": config.resolved_network_name,
        "subnetwork_name": config.resolved_subnetwork_name,
        "workstation": config.workstation.name,
        "kubeconfig_path": config.kubeconfig_path,
        "gdc_access_secret_id": config.gdc_access_secret_id,
        "gdc_vm_image_gcs_secret_id": config.gdc_vm_image_gcs_secret_id,
    }


# --- GCE range-plane preconditions ------------------------------------------------
#
# gcp-deploy runs these checks before the rollout (#2571). They stop a deployment
# that has no range images or range identity, because a control plane without them
# can not launch a range.

# Required GCE range-cell variables. A live range needs each of these set.
_REQUIRED_GCE_RANGE_VARS: tuple[str, ...] = (
    "RANGE_NETWORK_ZONE",
    "GCP_RANGE_LINUX_IMAGE",
    "GCP_RANGE_DC_IMAGE",
    "GCP_RANGE_HOST_SERVICE_ACCOUNT_EMAIL",
)

# Range guest-image variables whose referenced image must already exist in the
# range-cell project. Only variables that are set are checked.
_GCE_RANGE_IMAGE_VARS: tuple[str, ...] = (
    "GCP_RANGE_LINUX_IMAGE",
    "GCP_RANGE_DC_IMAGE",
    "GCP_RANGE_KALI_IMAGE",
    "GCP_RANGE_WINDOWS_IMAGE",
)


def _range_cell_project_id(config: GDCBootstrapConfig, env: Mapping[str, str]) -> str:
    """Resolve the project the range cells provision into (GCP_RANGE_CELL_PROJECT_ID, else control plane)."""
    return (env.get("GCP_RANGE_CELL_PROJECT_ID") or "").strip() or config.project_id


def _parse_gce_image_reference(reference: str, default_project: str) -> tuple[str, str, str]:
    """Return ``(project, kind, name)`` for a GCE image reference; ``kind`` is ``family`` or ``image``.

    Accepts ``projects/<p>/global/images/family/<f>``, ``projects/<p>/global/images/<img>``,
    ``family/<f>``, and a bare name (treated as a family, matching the packer image contract).
    """
    ref = reference.strip()
    if match := re.fullmatch(r"projects/(?P<project>[^/]+)/global/images/family/(?P<name>.+)", ref):
        return match.group("project"), "family", match.group("name")
    if match := re.fullmatch(r"projects/(?P<project>[^/]+)/global/images/(?P<name>.+)", ref):
        return match.group("project"), "image", match.group("name")
    if ref.startswith("family/"):
        return default_project, "family", ref[len("family/") :]
    return default_project, "family", ref


class GceImageLookupError(RuntimeError):
    """The deploy identity could not read a range guest image for a reason other than absence."""


def _gce_image_exists(project: str, kind: str, name: str) -> bool:
    """Return True when the referenced GCE image (or image family) resolves in the project.

    Only a not-found answer means the image is absent. Any other failure, for example
    a missing ``compute.images.get`` permission, raises ``GceImageLookupError`` (#2608).
    """
    if kind == "family":
        cmd = ["gcloud", "compute", "images", "describe-from-family", name]
    else:
        cmd = ["gcloud", "compute", "images", "describe", name]
    cmd += ["--project", project, "--format=value(name)"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # nosec B603 B607
    if result.returncode == 0:
        return bool(result.stdout.strip())
    if "was not found" in result.stderr:
        return False
    reason = next((line.strip(" -") for line in reversed(result.stderr.splitlines()) if line.strip()), "")
    raise GceImageLookupError(f"cannot read {kind} '{name}' in project '{project}': {reason}")


def _missing_gce_range_images(
    env: Mapping[str, str], project: str, image_exists: Callable[[str, str, str], bool]
) -> list[str]:
    """Return a description for each configured range guest image that does not resolve in the project."""
    missing: list[str] = []
    for name in _GCE_RANGE_IMAGE_VARS:
        if not (reference := (env.get(name) or "").strip()):
            continue
        image_project, kind, image_name = _parse_gce_image_reference(reference, project)
        if not image_exists(image_project, kind, image_name):
            missing.append(f"{name}={reference} (no {kind} '{image_name}' in project '{image_project}')")
    return missing


def _report_gce_range_precondition_failures(missing_vars: list[str], missing_images: list[str]) -> None:
    """Emit one error line per missing range-cell input and per missing range guest image."""
    for name in missing_vars:
        error(
            f"Required GCE range-cell input {name} is not set. Set it in the deployment input bundle. "
            "See docs/technical/handbook/gcp-github-less-deployment.md."
        )
    for detail in missing_images:
        error(
            f"Range guest image not imported: {detail}. Run deploy.py gcp-images, then deploy again. "
            "See docs/architecture/gcp-guest-images.md."
        )


def check_gce_range_preconditions(
    config: GDCBootstrapConfig,
    *,
    allow_missing_range_images: bool = False,
    env: Mapping[str, str] | None = None,
    image_exists: Callable[[str, str, str], bool] | None = None,
) -> None:
    """Fail fast on the range prerequisites before the GCE control-plane rollout.

    Fails when a required GCE range-cell input is unset or a referenced range guest image
    does not exist in the range-cell project. The GitHub-less deploy command (#2571)
    passes the inputs from ``settings.range_cells``, ``range-images.json``, and the
    Terraform outputs. ``allow_missing_range_images`` downgrades those
    failures to warnings for a deliberate platform-first bring-up. ``env`` and
    ``image_exists`` are injectable boundaries for tests.
    """
    env = os.environ if env is None else env
    image_exists = _gce_image_exists if image_exists is None else image_exists

    header("GCE range-plane preconditions")
    project = _range_cell_project_id(config, env)

    missing_vars = [name for name in _REQUIRED_GCE_RANGE_VARS if not (env.get(name) or "").strip()]
    try:
        missing_images = _missing_gce_range_images(env, project, image_exists)
    except GceImageLookupError as exc:
        error(f"Range guest image check failed: {exc}. Grant the deploy identity read access to the images.")
        sys.exit(1)

    if not missing_vars and not missing_images:
        success(f"GCE range preconditions satisfied (range-cell project {project}).")
        return

    _report_gce_range_precondition_failures(missing_vars, missing_images)

    if allow_missing_range_images:
        warn(
            "Proceeding despite the range prerequisites above (--allow-missing-range-images): the control "
            "plane will deploy, but ranges cannot launch until the images are imported and the inputs are set."
        )
        return

    error(
        "GCE range preconditions failed. Set settings.range_cells and import the range guest images "
        "(gcp-images) before deploying, or pass --allow-missing-range-images for a deliberate "
        "platform-first bring-up."
    )
    sys.exit(1)


def gdc_bootstrap_cluster(
    config: GDCBootstrapConfig,
    dry_run: bool = False,
    *,
    confirmation_obtained: bool = False,
    confirmation: Callable[[str], bool] | None = None,
    substrate_builder: Callable[..., None] | None = None,
) -> dict[str, str]:
    """Build the legacy GDC-on-Compute-Engine VM Runtime substrate.

    The GKE control plane and the GCE range plane deploy through ``gcp-deploy``
    (#2571). This command builds only the substrate that the ``gdc`` range backend needs.
    """
    confirmation = confirmation or confirm
    substrate_builder = substrate_builder or _build_gdc_substrate
    if not config.project_id:
        error("GDC bootstrap requires a GCP project ID. Set PANW_GCP_DEV or pass --project-id.")
        sys.exit(1)
    if not config.builds_gdc_substrate:
        error(
            "gdc-bootstrap builds only the GDC VM Runtime substrate for the gdc range backend. "
            "Deploy the control plane with: deploy.py gcp-deploy --store gs://<deployment-config-bucket>"
        )
        sys.exit(1)

    confirm_prompt = _announce_gdc_bootstrap_plan(config)
    if not dry_run and not confirmation_obtained and not confirmation(confirm_prompt):
        warn("Aborted by user")
        sys.exit(0)

    substrate_builder(config, dry_run=dry_run)
    _report_gdc_bootstrap_success(config)
    return _gdc_bootstrap_result(config)
