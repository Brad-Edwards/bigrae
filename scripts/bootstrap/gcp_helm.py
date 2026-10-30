"""Helm release management for GCP deployments (#2571, ADR-007, ADR-070).

The Helm chart ``platform/charts/shifter`` is the only GCP Kubernetes renderer. The
release ``shifter`` lives in the ``shifter-system`` namespace. This module builds the
generated values, adopts objects that an earlier Kustomize deploy applied, upgrades
the release atomically, and removes the closed set of Kustomize-only objects.

Adoption uses Helm's documented ownership metadata (Helm 3.2 and later): the
``app.kubernetes.io/managed-by: Helm`` label and the ``meta.helm.sh/release-name`` and
``meta.helm.sh/release-namespace`` annotations. An object that another release owns
stops the deploy; nothing is deleted to make room for the release.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from bootstrap_core import get_repo_root, info, success
from gcp_inputs import DeploymentInputs, private_workdir, write_private_file
from gcp_process import ProcessRunner

RELEASE_NAME = "shifter"
RELEASE_NAMESPACE = "shifter-system"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
RELEASE_NAME_ANNOTATION = "meta.helm.sh/release-name"
RELEASE_NAMESPACE_ANNOTATION = "meta.helm.sh/release-namespace"
HOOK_ANNOTATION = "helm.sh/hook"
#: Objects that only the removed Kustomize overlay rendered. The chart's provider-API
#: egress policies allow the same Google API ranges, and the empty runtime Secret has
#: no consumer.
RETIRED_KUSTOMIZE_OBJECTS: tuple[tuple[str, str, str], ...] = (
    ("networkpolicy.networking.k8s.io", "shifter-platform", "allow-platform-google-apis-egress"),
    ("networkpolicy.networking.k8s.io", "shifter-jobs", "allow-jobs-google-apis-egress"),
    ("secret", "shifter-platform", "platform-runtime-secrets"),
)


class HelmReleaseError(RuntimeError):
    """The Helm release cannot be rendered, adopted, or upgraded."""


def chart_path() -> Path:
    """Return the canonical Shifter chart directory."""
    return get_repo_root() / "platform" / "charts" / "shifter"


def profile_values_path(profile: str) -> Path:
    """Return the chart's backend-profile values file (``values-gcp-<profile>.yaml``)."""
    path = chart_path() / f"values-gcp-{profile}.yaml"
    if not path.is_file():
        raise HelmReleaseError(f"the chart has no values file for profile {profile!r}")
    return path


def build_values(
    inputs: DeploymentInputs,
    outputs: Mapping[str, Mapping[str, object]],
    *,
    runtime_env: Mapping[str, str],
    identities: Mapping[str, str],
    policy_binding: tuple[str, str] | None = None,
) -> dict[str, object]:
    """Build the non-secret generated Helm values for one deployment.

    ``policy_binding`` is the published OpenFGA store and model; without one,
    the values bind the pinned settings IDs, or leave the policy client unbound
    until publication (#2679).
    """
    from installation.gcp_model_broker import validate_model_broker_readback
    from installation.render import (
        render_gcp_range_telemetry_env,
        render_mission_control_lease_env,
        render_model_access_catalog,
        render_model_access_env,
    )

    from gcp_control_plane import GcpRenderArtifacts, render_gcp_helm_values
    from gcp_runtime import gdc_config

    root_config = inputs.root_config
    settings = inputs.settings
    validate_model_broker_readback(outputs.get("model_broker", {}).get("value"), root_config)  # type: ignore[arg-type]
    artifacts = GcpRenderArtifacts(
        model_access_catalog_json=render_model_access_catalog(root_config),  # type: ignore[arg-type]
        model_access_env=render_model_access_env(root_config),  # type: ignore[arg-type]
        mission_control_lease_env=render_mission_control_lease_env(root_config),  # type: ignore[arg-type]
        range_telemetry_env=render_gcp_range_telemetry_env(root_config),  # type: ignore[arg-type]
        model_broker_runtime=root_config.settings.get("model_broker_runtime"),  # type: ignore[attr-defined]
        openfga_settings=_openfga_settings(settings.openfga, policy_binding),  # type: ignore[attr-defined]
        capacity_profile_id=settings.shared_service_capacity_profile,  # type: ignore[attr-defined]
    )
    return render_gcp_helm_values(
        gdc_config(inputs),
        dict(outputs),  # type: ignore[arg-type]
        runtime_env=runtime_env,
        image_identities={
            "platform": identities["portal"],
            "guacd": identities["guacd"],
            "guacamoleClient": identities["guacamole-client"],
        },
        render_artifacts=artifacts,
    )


def _openfga_settings(policy: object, binding: tuple[str, str] | None) -> dict[str, object]:
    """Return the OpenFGA settings with the published store and model IDs, when there are any."""
    settings = policy.model_dump()  # type: ignore[attr-defined]
    if binding is not None:
        settings["store_id"], settings["model_id"] = binding
    return settings


def write_values(workdir: Path, values: Mapping[str, object]) -> Path:
    """Write the generated values to a private file."""
    directory = private_workdir(workdir / "helm")
    return write_private_file(
        directory / "shifter.values.generated.json", json.dumps(values, indent=2, sort_keys=True) + "\n"
    )


def helm_values_args(values_files: Sequence[Path]) -> list[str]:
    """Return the ``--values`` arguments in precedence order."""
    args: list[str] = []
    for path in values_files:
        args += ["--values", str(path)]
    return args


def render_chart(
    runner: ProcessRunner, env: Mapping[str, str | None], values_files: Sequence[Path]
) -> list[dict[str, object]]:
    """Render the release locally and return its documents."""
    result = runner.run(
        [
            "helm",
            "template",
            RELEASE_NAME,
            str(chart_path()),
            "--namespace",
            RELEASE_NAMESPACE,
            *helm_values_args(values_files),
        ],
        env=env,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def resource_type(document: Mapping[str, object]) -> str:
    """Return the fully qualified kubectl resource type of a document."""
    kind = str(document.get("kind", "")).lower()
    api_version = str(document.get("apiVersion", ""))
    group = api_version.split("/", 1)[0] if "/" in api_version else ""
    return f"{kind}.{group}" if group else kind


def owned_objects(documents: Sequence[Mapping[str, object]]) -> list[tuple[str, str, str]]:
    """Return ``(type, namespace, name)`` for release-owned (non-hook) documents."""
    objects: list[tuple[str, str, str]] = []
    for document in documents:
        metadata = document.get("metadata")
        if not isinstance(metadata, Mapping) or not metadata.get("name"):
            continue
        annotations = metadata.get("annotations") or {}
        if isinstance(annotations, Mapping) and HOOK_ANNOTATION in annotations:
            continue
        objects.append((resource_type(document), str(metadata.get("namespace", "")), str(metadata["name"])))
    return objects


def _scope_args(namespace: str) -> list[str]:
    """Return the kubectl namespace arguments; a cluster-scoped object has none."""
    return ["-n", namespace] if namespace else []


def _live_items(
    runner: ProcessRunner, env: Mapping[str, str | None], kind: str, namespace: str
) -> dict[str, Mapping[str, object]]:
    """Return the metadata of the live objects of one type, keyed by name."""
    result = runner.run(["kubectl", "get", kind, *_scope_args(namespace), "-o", "json"], env=env)
    try:
        inventory = json.loads(result.stdout)
        if not isinstance(inventory, dict) or not isinstance(inventory.get("items"), list):
            raise ValueError
        items = inventory["items"]
        metadata = [_inventory_metadata(item, namespace) for item in items]
        names = [item["name"] for item in metadata]
        if len(set(names)) != len(names):
            raise ValueError
    except (ValueError, TypeError):
        raise HelmReleaseError("Kubernetes ownership inventory is invalid") from None
    return {str(item["name"]): item for item in metadata}


def _inventory_metadata(item: object, namespace: str) -> Mapping[str, object]:
    """Validate one live ownership row before the caller can adopt any object."""
    if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
        raise ValueError
    metadata = item["metadata"]
    name = metadata.get("name")
    if not isinstance(name, str) or not name or metadata.get("namespace", namespace) != namespace:
        raise ValueError
    return metadata


def _owned_by_release(metadata: Mapping[str, object], target: str) -> bool:
    """Return True when the Shifter release already owns a live object.

    An object that another Helm release owns stops the deploy.
    """
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}
    if not isinstance(annotations, Mapping):
        annotations = {}
    if not isinstance(labels, Mapping):
        labels = {}
    owner = annotations.get(RELEASE_NAME_ANNOTATION)
    owner_namespace = annotations.get(RELEASE_NAMESPACE_ANNOTATION)
    if owner not in (None, RELEASE_NAME) or owner_namespace not in (None, RELEASE_NAMESPACE):
        raise HelmReleaseError(f"{target} belongs to another Helm release; refusing to adopt it")
    return owner == RELEASE_NAME and owner_namespace == RELEASE_NAMESPACE and labels.get(MANAGED_BY_LABEL) == "Helm"


def _adopt(runner: ProcessRunner, env: Mapping[str, str | None], target: str, namespace: str) -> None:
    """Add the Helm ownership label and annotations to one live object."""
    scoped = [target, *_scope_args(namespace)]
    runner.run(["kubectl", "label", *scoped, f"{MANAGED_BY_LABEL}=Helm", "--overwrite"], env=env)
    runner.run(
        [
            "kubectl",
            "annotate",
            *scoped,
            f"{RELEASE_NAME_ANNOTATION}={RELEASE_NAME}",
            f"{RELEASE_NAMESPACE_ANNOTATION}={RELEASE_NAMESPACE}",
            "--overwrite",
        ],
        env=env,
    )


def adopt_objects(
    runner: ProcessRunner, env: Mapping[str, str | None], documents: Sequence[Mapping[str, object]]
) -> list[str]:
    """Add Helm ownership to rendered objects that already exist without an owner."""
    adopted: list[str] = []
    grouped: dict[tuple[str, str], list[str]] = {}
    for kind, namespace, name in owned_objects(documents):
        grouped.setdefault((kind, namespace), []).append(name)
    inventories = {key: _live_items(runner, env, *key) for key in sorted(grouped)}
    pending: list[tuple[str, str]] = []
    for (kind, namespace), names in sorted(grouped.items()):
        live = inventories[kind, namespace]
        for name in names:
            metadata = live.get(name)
            if metadata is None or _owned_by_release(metadata, f"{kind}/{name}"):
                continue
            pending.append((f"{kind}/{name}", namespace))
    for target, namespace in pending:
        _adopt(runner, env, target, namespace)
        adopted.append(target)
    if adopted:
        info(f"Adopted {len(adopted)} existing objects into the Helm release")
    return adopted


def helm_upgrade(
    runner: ProcessRunner, env: Mapping[str, str | None], values_files: Sequence[Path], *, timeout: str = "15m"
) -> None:
    """Install or upgrade the release atomically and wait for it."""
    runner.run(
        [
            "helm",
            "upgrade",
            "--install",
            RELEASE_NAME,
            str(chart_path()),
            "--namespace",
            RELEASE_NAMESPACE,
            "--create-namespace",
            *helm_values_args(values_files),
            "--atomic",
            "--wait",
            "--timeout",
            timeout,
            "--history-max",
            "10",
        ],
        env=env,
        capture=False,
    )
    success("Helm release is current")


def remove_retired_objects(runner: ProcessRunner, env: Mapping[str, str | None]) -> list[str]:
    """Delete the Kustomize-only objects that no Helm release owns."""
    removed: list[str] = []
    for kind, namespace, name in RETIRED_KUSTOMIZE_OBJECTS:
        result = runner.run(
            ["kubectl", "get", f"{kind}/{name}", *_scope_args(namespace), "-o", "json"], env=env, check=False
        )
        if result.returncode != 0:
            continue
        try:
            metadata = json.loads(result.stdout).get("metadata", {})
        except (json.JSONDecodeError, AttributeError):
            continue
        if (metadata.get("annotations") or {}).get(RELEASE_NAME_ANNOTATION):
            continue
        runner.run(
            ["kubectl", "delete", f"{kind}/{name}", *_scope_args(namespace), "--ignore-not-found", "--wait=true"],
            env=env,
        )
        removed.append(f"{kind}/{name}")
    if removed:
        info("Removed retired Kustomize objects: " + ", ".join(removed))
    return removed
