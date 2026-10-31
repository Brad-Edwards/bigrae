"""Shared test doubles and fixtures for the GitHub-less GCP deploy commands (#2571)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from gcp_process import GcpCommandError, ProcessResult, merged_env

PROJECT = "acme-shifter"
REGION = "us-central1"
ENVIRONMENT = "acme"
REVISION = "0123456789abcdef0123456789abcdef01234567"
RELEASE_REGISTRY = "us-central1-docker.pkg.dev/acme-release/bigrae-release"
IMAGE_NAMES = ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")


NATIVE_WORKLOAD_ACCOUNTS = (
    ("portal", "shiftergcpdev-portal"),
    ("workers", "shiftergcpdev-workers"),
    ("ctf-scheduler", "shiftergcpdev-ctf-scheduler"),
    ("provisioner-launcher", "shiftergcpdev-prov-launcher"),
    ("provisioner", "shiftergcpdev-provisioner"),
    ("openfga-runtime", "shiftergcpdev-openfga-runtime"),
    ("openfga-admin", "shiftergcpdev-openfga-admin"),
    ("openfga-migrator", "shiftergcpdev-openfga-migrator"),
)


def native_identity_value(project_id: str) -> dict[str, object]:
    """Return the native SQL and OpenFGA readback that the current release requires (#2679)."""
    return {
        "enabled": True,
        "openfga_enabled": True,
        "instance_connection_name": f"{project_id}:us-central1:shifter-gcp-dev-pg",
        "database_users": {workload: f"{account}@{project_id}.iam" for workload, account in NATIVE_WORKLOAD_ACCOUNTS},
    }


def native_workload_accounts(project_id: str) -> dict[str, str]:
    """Return the attached service account of each native SQL workload."""
    return {
        workload: f"{account}@{project_id}.iam.gserviceaccount.com" for workload, account in NATIVE_WORKLOAD_ACCOUNTS
    }


def sample_gcp_outputs(project_id: str = "example-gcp-project") -> dict[str, dict[str, object]]:
    """Return representative Terraform outputs for the GCP control-plane path."""
    return {
        "gke_cluster_name": {"value": "shifter-gcp-dev-platform"},
        "gke_cluster_location": {"value": "us-central1"},
        "artifact_registry_image_roots": {
            "value": {
                "portal": f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-portal/portal",
                "guacd": f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-guacd/guacd",
                "guacamole-client": (
                    f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-guacamole-client/guacamole-client"
                ),
                "pulumi-provisioner": (
                    f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-pulumi-provisioner/pulumi-provisioner"
                ),
                "openvpn": f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-openvpn/openvpn",
            }
        },
        "assets_bucket_name": {"value": f"{project_id}-gcp-dev-assets"},
        "terraform_state_bucket_name": {"value": f"{project_id}-terraform-state"},
        "platform_events_topic_id": {"value": f"projects/{project_id}/topics/shifter-gcp-dev-events"},
        "platform_event_subscriptions": {
            "value": {
                "cms": f"projects/{project_id}/subscriptions/shifter-gcp-dev-cms",
                "engine": f"projects/{project_id}/subscriptions/shifter-gcp-dev-engine",
                "mc": f"projects/{project_id}/subscriptions/shifter-gcp-dev-mc",
                "experiments": f"projects/{project_id}/subscriptions/shifter-gcp-dev-experiments",
            }
        },
        "runtime_secret_ids": {
            "value": {
                "app": f"projects/{project_id}/secrets/shifter-gcp-dev-app",
                "db": f"projects/{project_id}/secrets/shifter-gcp-dev-db",
                "db-provisioner": f"projects/{project_id}/secrets/shifter-gcp-dev-db-provisioner",
                "db-migration": f"projects/{project_id}/secrets/shifter-gcp-dev-db-migration",
                "guacamole-db": f"projects/{project_id}/secrets/shifter-gcp-dev-guacamole-db",
                "guacamole-json-auth": f"projects/{project_id}/secrets/shifter-gcp-dev-guacamole-json-auth",
                # ADR-008-R6 (#963): the GCP runtime renderer fails closed
                # without the Memorystore Secret Manager bundle ID.
                "redis": f"projects/{project_id}/secrets/shifter-gcp-dev-redis",
            }
        },
        "dynamic_secret_project_id": {"value": f"{project_id}-range-secrets"},
        "provisioner_static_secret_refs": {
            "value": {
                "GDC_VM_IMAGE_GCS_SECRET_ID": (f"projects/{project_id}/secrets/shifter-gcp-dev-gdc-vm-image-gcs"),
            }
        },
        "identity_platform_api_key": {"value": "identity-platform-api-key"},
        "identity_platform_project_id": {"value": project_id},
        "identity_allowed_email_domain": {"value": "example.com"},
        "identity_allowed_emails": {"value": []},
        "control_plane_database": {
            "value": {
                "private_ip": "10.40.0.10",
                "port": 5432,
                "database_name": "shifter",
                "user_name": "portal_runtime",
                "provisioner_user_name": "provisioner_runtime",
            }
        },
        # ADR-008-R6 (#963): Memorystore runs with TLS on the GCP runtime.
        "control_plane_cache": {"value": {"host": "10.40.0.20", "port": 6378, "tls_enabled": True}},
        "guacamole_database": {
            "value": {
                "host": "10.40.0.10",
                "port": 5432,
                "database_name": "guacamole",
                "user_name": "guacamole",
            }
        },
        "public_ingress_ip_name": {"value": "shifter-gcp-dev-platform-ip"},
        "public_ingress_ip_address": {"value": "34.123.45.67"},
        "public_hostname": {"value": "portal.example.test"},
        "managed_tls_enabled": {"value": True},
        "cloud_armor_security_policy_name": {"value": "shifter-gcp-dev-edge"},
        "range_network_id": {"value": f"projects/{project_id}/global/networks/shifter-gcp-dev-range"},
        "range_network_name": {"value": "shifter-gcp-dev-range"},
        "range_network_cidr": {"value": "10.50.0.0/16"},
        "range_network_region": {"value": "us-central1"},
        "portal_network_cidrs": {"value": ["10.46.0.0/20"]},
        "access_network_cidrs": {"value": ["10.47.0.0/20"]},
        "gke_services_cidr": {"value": "10.48.0.0/20"},
        "gke_master_ipv4_cidr": {"value": "172.16.0.0/28"},
        "range_telemetry_terraform_resources": {
            "value": {"subnetworks": ["shifter-gcp-dev-range-a"], "nat_routers": ["shifter-gcp-dev-nat"]}
        },
        "workload_service_accounts": {
            "value": {
                "portal": f"shiftergcpdev-portal@{project_id}.iam.gserviceaccount.com",
                "workers": f"shiftergcpdev-workers@{project_id}.iam.gserviceaccount.com",
                "ctf-scheduler": f"shiftergcpdev-ctf-scheduler@{project_id}.iam.gserviceaccount.com",
                "migrator": f"shiftergcpdev-migrator@{project_id}.iam.gserviceaccount.com",
                # account_id is bounded to <=30 chars, so provisioner-launcher's SA
                # localpart is shortened to prov-launcher (#1719).
                "provisioner-launcher": (f"shiftergcpdev-prov-launcher@{project_id}.iam.gserviceaccount.com"),
                "provisioner": f"shiftergcpdev-provisioner@{project_id}.iam.gserviceaccount.com",
                "openfga-runtime": f"shiftergcpdev-openfga-runtime@{project_id}.iam.gserviceaccount.com",
                "openfga-admin": f"shiftergcpdev-openfga-admin@{project_id}.iam.gserviceaccount.com",
                "openfga-migrator": f"shiftergcpdev-openfga-migrator@{project_id}.iam.gserviceaccount.com",
            }
        },
        # The current release requires native SQL and OpenFGA (#2321, #2679).
        "native_identity": {"value": native_identity_value(project_id)},
    }


Responder = ProcessResult | Callable[[list[str], dict[str, str], str | None], ProcessResult]


@dataclass
class Call:
    """One recorded child process invocation."""

    argv: list[str]
    env: dict[str, str]
    input_text: str | None


@dataclass
class FakeRunner:
    """A ProcessRunner double that records calls and answers by argv prefix."""

    responses: list[tuple[Sequence[str], Responder]] = field(default_factory=list)
    base: dict[str, str] = field(default_factory=lambda: {"PATH": "/usr/bin", "HOME": "/home/test"})
    calls: list[Call] = field(default_factory=list)

    @property
    def base_env(self) -> dict[str, str]:
        return dict(self.base)

    def on(self, prefix: Sequence[str], response: Responder) -> FakeRunner:
        """Answer every call whose argv starts with ``prefix`` (first match wins)."""
        self.responses.append((list(prefix), response))
        return self

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str | None] | None = None,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
        sensitive: bool = False,
    ) -> ProcessResult:
        del capture, timeout, sensitive
        command = [str(part) for part in argv]
        child_env = merged_env(self.base, env)
        self.calls.append(Call(command, child_env, input_text))
        result = ProcessResult(0)
        for prefix, response in self.responses:
            if command[: len(prefix)] == list(prefix):
                result = response(command, child_env, input_text) if callable(response) else response
                break
        if check and result.returncode != 0:
            raise GcpCommandError(f"{command[0]} failed with exit status {result.returncode}")
        return result

    def argvs(self) -> list[list[str]]:
        return [call.argv for call in self.calls]

    def find(self, *prefix: str) -> list[Call]:
        return [call for call in self.calls if call.argv[: len(prefix)] == list(prefix)]


def root_config(**settings_overrides: object) -> dict[str, object]:
    """Return a complete GitHub-less GCP root config mapping."""
    settings: dict[str, object] = {
        "project_id": PROJECT,
        "dynamic_secret_project_id": "acme-range-secrets",
        "region": REGION,
        "range_cells": {"zone": "us-central1-a"},
        "operator_bootstrap": {
            "admin_email": "ops@example.com",
            "admin_password_secret_ref": f"projects/{PROJECT}/secrets/operator-password",
        },
        "post_deploy_smoke": {"user_email": "smoke@example.com"},
        "release_trust": {"github_repository_id": "1234", "github_owner_id": "5678"},
    }
    settings.update(settings_overrides)
    return {
        "version": 1,
        "backend": "gcp",
        "deployment": {"name": ENVIRONMENT, "domain": "portal.acme.example", "profile": "prod"},
        "secrets": {"django_secret_key": "prompt"},
        "settings": settings,
    }


def write_bundle(
    directory: Path,
    *,
    config: Mapping[str, object] | None = None,
    terraform_values: Mapping[str, object] | None = None,
    range_images: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> Path:
    """Write a valid local deployment input bundle into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "bigrae.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump(dict(config or root_config()), handle, sort_keys=False)
    values = dict(terraform_values or {"identity_allowed_email_domain": "example.com"})
    with open(directory / "terraform.tfvars.json", "w", encoding="utf-8") as handle:
        json.dump(values, handle)
    images = (
        dict(range_images)
        if range_images is not None
        else {
            "linux": f"projects/{PROJECT}/global/images/shifter-ubuntu-aaaa",
            "kali": f"projects/{PROJECT}/global/images/shifter-kali-bbbb",
            "dc": f"projects/{PROJECT}/global/images/shifter-dc-cccc",
        }
    )
    if images:
        with open(directory / "range-images.json", "w", encoding="utf-8") as handle:
            json.dump(images, handle)
    for name, text in (extra or {}).items():
        with open(directory / name, "w", encoding="utf-8") as handle:
            handle.write(text)
    return directory


def release_images(registry: str = RELEASE_REGISTRY) -> dict[str, str]:
    """Return exact release identities for every image."""
    return {name: f"{registry}/{name}@sha256:{str(index) * 64}" for index, name in enumerate(IMAGE_NAMES, 1)}


def write_manifest(
    path: Path,
    *,
    images: Mapping[str, str] | None = None,
    revision: str = REVISION,
    authorization_model: Mapping[str, object] | None = None,
    base_images: Mapping[str, str] | None = None,
) -> Path:
    """Write a valid release manifest file."""
    payload: dict[str, object] = {
        "schema": "bigrae.gcp-release.v1",
        "version": "1.4.0",
        "source_revision": revision,
        "chart_version": "0.2.0",
        "images": dict(images or release_images()),
    }
    if base_images:
        payload["base_images"] = dict(base_images)
    if authorization_model is not None:
        payload["authorization_model"] = dict(authorization_model)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


_IMAGE_ROOT_KEYS = {
    "portal": "portal",
    "provisioner": "pulumi-provisioner",
    "guacd": "guacd",
    "guacamole-client": "guacamole-client",
    "openvpn": "openvpn",
}


def generated_helm_values(inputs, outputs: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    """Build the generated Helm values that gcp-deploy renders for ``inputs`` and ``outputs``."""
    from gcp_control_plane import _load_python_script_module, parse_env_contract, render_gcp_platform_runtime_env
    from gcp_helm import build_values
    from gcp_runtime import gdc_config

    renderer = _load_python_script_module(
        Path(__file__).resolve().parents[2] / "gcp" / "render_runtime_env.py", "gcp_test_support_runtime_renderer"
    )
    runtime = parse_env_contract(render_gcp_platform_runtime_env(gdc_config(inputs)))
    if outputs.get("managed_tls_enabled", {}).get("value"):
        runtime.update(parse_env_contract(renderer.render_env(dict(outputs), engine_image="sha256:" + "2" * 64)))
    roots = outputs["artifact_registry_image_roots"]["value"]
    identities = {
        name: f"{roots[key]}@sha256:{str(index) * 64}"  # type: ignore[index]
        for index, (name, key) in enumerate(_IMAGE_ROOT_KEYS.items(), 1)
    }
    return build_values(inputs, outputs, runtime_env=runtime, identities=identities)
