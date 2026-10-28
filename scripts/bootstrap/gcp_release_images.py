"""Release image import into the deployment Artifact Registry (#2571, ADR-070).

A deployment runs only the exact digests of a release manifest. The command copies each
release image, with its attestation referrers, from the release registry into the
deployment's own Artifact Registry repository. The copy keeps the manifest digest, so the
deployment identity is ``<deployment root>@<release digest>``. Workloads keep pulling
from the deployment project, and the copied referrers let the provenance gate verify the
bytes that will run. CI-built images already live in the deployment roots, so the copy is
skipped for them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from bootstrap_core import info
from gcp_inputs import private_workdir, write_private_file
from gcp_process import GcpCommandError, ProcessRunner

#: Release image name -> Terraform ``artifact_registry_image_roots`` key.
DEPLOYMENT_ROOT_KEYS = {
    "portal": "portal",
    "provisioner": "pulumi-provisioner",
    "guacd": "guacd",
    "guacamole-client": "guacamole-client",
    "openvpn": "openvpn",
}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


class ReleaseImageError(RuntimeError):
    """A release image cannot be imported with its exact digest."""


def deployment_image_roots(outputs: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    """Map each release image name to its deployment repository root."""
    try:
        roots = outputs["artifact_registry_image_roots"]["value"]
    except KeyError:
        raise ReleaseImageError("Terraform output artifact_registry_image_roots is missing") from None
    if not isinstance(roots, Mapping):
        raise ReleaseImageError("Terraform output artifact_registry_image_roots must be a map")
    missing = sorted(key for key in DEPLOYMENT_ROOT_KEYS.values() if key not in roots)
    if missing:
        raise ReleaseImageError("deployment image roots are missing: " + ", ".join(missing))
    return {name: str(roots[key]) for name, key in DEPLOYMENT_ROOT_KEYS.items()}


def write_image_roots(path: Path, outputs: Mapping[str, Mapping[str, object]]) -> Path:
    """Write the deployment repository root of each release image; CI builds into them."""
    return write_private_file(path, json.dumps(deployment_image_roots(outputs), indent=2, sort_keys=True) + "\n")


def registry_hosts(*identities: str) -> list[str]:
    """Return the distinct registry hosts of image identities or roots."""
    return sorted({identity.split("/", 1)[0] for identity in identities})


def docker_config(workdir: Path, hosts: list[str]) -> Path:
    """Write an isolated Docker client config that uses the gcloud credential helper."""
    directory = private_workdir(workdir / "docker")
    write_private_file(directory / "config.json", json.dumps({"credHelpers": dict.fromkeys(hosts, "gcloud")}))
    return directory


def release_tag(source_revision: str) -> str:
    """Return the deployment tag that records the release revision."""
    tag = f"release-{source_revision[:12]}"
    if not _TAG_RE.fullmatch(tag):
        raise ReleaseImageError("release tag is invalid")
    return tag


def import_release_images(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    release_images: Mapping[str, str],
    roots: Mapping[str, str],
    source_revision: str,
    workdir: Path,
    oras: str = "oras",
) -> dict[str, str]:
    """Copy each release image and its referrers to the deployment root by digest.

    Returns the deployment identity of every image. An image whose repository already is
    the deployment root (a CI build) keeps its identity unchanged.
    """
    hosts = registry_hosts(*release_images.values(), *roots.values())
    oras_env = {**env, "DOCKER_CONFIG": str(docker_config(workdir, hosts))}
    tag = release_tag(source_revision)
    identities: dict[str, str] = {}
    for name, identity in release_images.items():
        repository, _, digest = identity.rpartition("@")
        if not _DIGEST_RE.fullmatch(digest):
            raise ReleaseImageError(f"release image {name} has no exact digest")
        root = roots[name]
        if repository == root:
            identities[name] = identity
            continue
        info(f"Importing release image {name} into the deployment registry")
        try:
            runner.run([oras, "cp", "--recursive", identity, f"{root}:{tag}"], env=oras_env)
            resolved = runner.run([oras, "resolve", f"{root}:{tag}"], env=oras_env).stdout.strip()
        except GcpCommandError as exc:
            raise ReleaseImageError(f"cannot import release image {name}: {exc}") from None
        if resolved != digest:
            raise ReleaseImageError(f"imported image {name} resolved to a different digest")
        identities[name] = f"{root}@{digest}"
    return identities
