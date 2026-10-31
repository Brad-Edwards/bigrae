"""Release guest images for the supported GCP deployment (#2309, #2679, #2668).

A fresh GCP deployment does not bake its guest images. ``packer-gcp.yml`` publishes each
validated guest to ``<release registry>/gce-<role>`` with a ``run-<number>.<attempt>``
tag. The release workflow takes the newest publication of each role, verifies it, and
records its exact digest in the release manifest (``base_images``).

Before any change, this module verifies every guest of a release: the digest-verified OCI
manifest, the artifact/layer/role contract, and a Sigstore bundle that satisfies the
deployment trust anchor at the build revision of the artifact, from a protected build
branch. Then it imports each disk as a *native GCE image* in the range project, reusing
an image that it created before for the same canonical reference.

A mirror (``release_source``) changes only where the bytes come from. The canonical
reference in the release stays the identity that names and owns the GCE image, so a
mirror never makes an unchanged guest look new. The caller records the resulting image
references in the deployment input bundle (``range-images.json``).

Subprocess calls go through ``bootstrap_core.run_cmd`` so tests mock that boundary
(ADR-019). Registry metadata reads go through the bounded ``gcp_oci.OciClient``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from bootstrap_core import (
    header,
    info,
    run_cmd,
    subheader,
    success,
)

if TYPE_CHECKING:
    from gcp_oci import ImageReference
    from gcp_provenance import DsseVerifier

__all__ = [
    "BASE_IMAGE_ROLES",
    "GUEST_BUILD_REFS",
    "BaseImageError",
    "ImportedImage",
    "ResolvedArtifact",
    "import_base_image",
    "import_selection",
    "newest_publications",
    "package_for_role",
    "range_images_from_imports",
    "registry_credentials",
    "render_range_image_env",
    "resolve_selection",
    "transport_identity",
]

# The published guest roles that the supported GCE deployment needs.
BASE_IMAGE_ROLES = ("kali", "ubuntu", "dc")
# packer-gcp.yml publishes only from these branches (its ref guard and Environments).
GUEST_BUILD_REFS = ("refs/heads/dev", "refs/heads/main")

# OCI contract published by packer-gcp.yml.
GCE_IMAGE_ARTIFACT_TYPE = "application/vnd.shifter.gce-image.v1"
GCE_IMAGE_MEDIA_TYPE = "application/vnd.shifter.gce-image.tar.gz"

# Each base role maps to the GCE range-cell image env var the runtime consumes.
# ubuntu is the default unkeyed Linux/host profile (GCP_RANGE_LINUX_IMAGE).
_ROLE_TO_RANGE_ENV = {
    "kali": "GCP_RANGE_KALI_IMAGE",
    "ubuntu": "GCP_RANGE_LINUX_IMAGE",
    "dc": "GCP_RANGE_DC_IMAGE",
}

# Raw disk export/import does not retain image-level guest OS features. Every
# runtime range guest uses Shielded VM Secure Boot, so all imported bases must
# be marked UEFI compatible. Windows-family roles additionally need WINDOWS.
_WINDOWS_ROLES = frozenset({"dc"})

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::\d+)?(?:/[a-z0-9][a-z0-9._-]*)+$", re.ASCII)
# github.run_number increases with each run of packer-gcp.yml; a re-run raises the attempt.
_PUBLICATION_TAG_RE = re.compile(r"^run-(\d+)\.(\d+)$")
_RANGE_ENV_TO_ROLE = {
    "GCP_RANGE_LINUX_IMAGE": "linux",
    "GCP_RANGE_KALI_IMAGE": "kali",
    "GCP_RANGE_DC_IMAGE": "dc",
}


class BaseImageError(RuntimeError):
    """A guest image artifact is missing, incompatible, unverified, or failed to import."""


class ManifestReader(Protocol):
    """The part of ``gcp_oci.OciClient`` that reads a digest-verified manifest."""

    def manifest(self, image: ImageReference) -> dict[str, object]:
        """Return the parsed manifest of ``image``."""
        ...


@dataclass(frozen=True)
class ResolvedArtifact:
    """A verified, digest-pinned guest artifact ready to import."""

    role: str
    canonical: str  # exact <repository>@sha256 identity named by the release
    transport: str  # exact <repository>@sha256 identity that the bytes come from
    disk_digest: str  # sha256:<64 hex> of the validated GCE disk layer
    revision: str  # the build revision that the verified provenance binds

    @property
    def digest(self) -> str:
        """The release digest of the OCI manifest."""
        return self.canonical.rsplit("@", 1)[1]

    @property
    def pinned_reference(self) -> str:
        """Immutable ``oci://<canonical identity>`` reference that owns the GCE image."""
        return f"oci://{self.canonical}"

    @property
    def transport_repository(self) -> str:
        """Repository path that the disk layer is fetched from."""
        return self.transport.rsplit("@", 1)[0]

    @property
    def short_digest(self) -> str:
        """First 12 hex of the digest, for deterministic image naming/reuse."""
        return self.digest.split(":", 1)[1][:12]


@dataclass(frozen=True)
class ImportedImage:
    """A native GCE image made available for one base role."""

    role: str
    image_name: str
    image_ref: str  # projects/<project>/global/images/<name> (a GCP_RANGE_*_IMAGE value)
    digest: str
    reused: bool


# --- Pure parsing / validation -----------------------------------------------


def _registry_prefix(prefix: str) -> str:
    """Return ``prefix`` when it is a registry prefix such as ``<host>/<path>``."""
    if not _SOURCE_RE.fullmatch(prefix):
        raise BaseImageError("a registry prefix must look like <host>/<path>")
    return prefix


def package_for_role(role: str, registry: str) -> str:
    """Return the OCI repository of one guest role under a release registry prefix."""
    if role not in BASE_IMAGE_ROLES:
        raise BaseImageError(f"unsupported guest image role {role!r}")
    return f"{_registry_prefix(registry)}/gce-{role}"


def transport_identity(role: str, canonical: str, release_source: str = "") -> str:
    """Return where to read one release guest: the release identity, or ``<source>/gce-<role>@<digest>``."""
    repository, _, digest = canonical.rpartition("@")
    if not _DIGEST_RE.fullmatch(digest) or not repository.endswith(f"/gce-{role}"):
        raise BaseImageError(f"release guest image {role} must be <registry>/gce-{role}@sha256:<digest>")
    package_for_role(role, repository.removesuffix(f"/gce-{role}"))
    if not release_source:
        return canonical
    return f"{package_for_role(role, release_source)}@{digest}"


def _newest_publication_tag(tags: list[str]) -> str | None:
    """Return the ``run-<number>.<attempt>`` tag of the newest publication, or None."""
    published = []
    for tag in tags:
        match = _PUBLICATION_TAG_RE.fullmatch(tag.strip())
        if match:
            published.append((int(match.group(1)), int(match.group(2)), tag.strip()))
    return max(published)[2] if published else None


def _disk_layer_digest(manifest: dict[str, object]) -> str:
    """Return the one validated, digest-pinned GCE disk layer."""
    layers = manifest.get("layers") or []
    if not isinstance(layers, list) or not all(isinstance(layer, dict) for layer in layers):
        raise BaseImageError("guest image manifest layers must be an array of descriptors")
    disk_layers = [layer for layer in layers if layer.get("mediaType") == GCE_IMAGE_MEDIA_TYPE]
    if len(disk_layers) != 1:
        raise BaseImageError(f"expected exactly one {GCE_IMAGE_MEDIA_TYPE} layer, found {len(disk_layers)}")
    layer_digest = str(disk_layers[0].get("digest", "")).strip()
    if not _DIGEST_RE.fullmatch(layer_digest):
        raise BaseImageError("guest image disk layer is missing a full sha256 digest")
    return layer_digest


def _validate_annotations(manifest: dict[str, object], *, role: str) -> str:
    """Require the requested role and a full build revision, and return the revision."""
    annotations = manifest.get("annotations") or {}
    if not isinstance(annotations, dict):
        raise BaseImageError("guest image manifest annotations must be an object")
    declared_role = str(annotations.get("com.shifter.image.role", "")).strip()
    if declared_role != role:
        raise BaseImageError(f"artifact role annotation {declared_role!r} does not match requested role {role!r}")
    revision = str(annotations.get("org.opencontainers.image.revision", "")).strip()
    if not _REVISION_RE.fullmatch(revision):
        raise BaseImageError(f"guest image {role} has no full build revision annotation")
    return revision


def validate_artifact_manifest(manifest: dict[str, object], *, role: str) -> tuple[str, str]:
    """Validate an OCI manifest against the GCE guest artifact contract.

    Returns the disk layer digest and the build revision. The revision annotation is a
    claim; the Sigstore bundle verified at that revision is the trust.
    """
    if not isinstance(manifest, dict):
        raise BaseImageError("guest image manifest is not a JSON object")
    artifact_type = manifest.get("artifactType", "")
    if artifact_type != GCE_IMAGE_ARTIFACT_TYPE:
        raise BaseImageError(f"unexpected artifactType {artifact_type!r}; expected {GCE_IMAGE_ARTIFACT_TYPE}")
    return _disk_layer_digest(manifest), _validate_annotations(manifest, role=role)


def image_name_for(role: str, short_digest: str) -> str:
    """Deterministic, digest-derived GCE image name (drives idempotent reuse)."""
    return f"shifter-{role}-{short_digest}"


def render_range_image_env(imported: dict[str, ImportedImage]) -> dict[str, str]:
    """Map imported base images to their ``GCP_RANGE_*_IMAGE`` runtime values."""
    return {_ROLE_TO_RANGE_ENV[role]: image.image_ref for role, image in imported.items() if role in _ROLE_TO_RANGE_ENV}


def range_images_from_imports(range_image_env: dict[str, str]) -> dict[str, str]:
    """Convert imported ``GCP_RANGE_*_IMAGE`` values to ``range-images.json`` roles."""
    if set(range_image_env) != set(_RANGE_ENV_TO_ROLE):
        raise BaseImageError("the guest image import did not return the complete range-image set")
    return {_RANGE_ENV_TO_ROLE[key]: value for key, value in range_image_env.items()}


def _required_guest_os_features(role: str) -> frozenset[str]:
    """Return image features required by the range runtime posture."""
    features = {"UEFI_COMPATIBLE"}
    if role in _WINDOWS_ROLES:
        features.add("WINDOWS")
    return frozenset(features)


def _guest_os_feature_types(metadata: dict[str, object]) -> frozenset[str]:
    """Read normalized guest feature names from GCE image metadata."""
    raw = metadata.get("guestOsFeatures") or []
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        str(item.get("type", "")).strip()
        for item in raw
        if isinstance(item, dict) and str(item.get("type", "")).strip()
    )


# --- Registry boundary --------------------------------------------------------


def _oras_json(cmd: list[str], *, what: str) -> dict[str, object]:
    """Run an oras command that emits JSON and parse it, failing clearly."""
    result = run_cmd(cmd, check=False, capture=True)
    if result is None or result.returncode != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise BaseImageError(f"{what} failed: {stderr or 'oras returned a non-zero status'}")
    try:
        document = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise BaseImageError(f"{what} returned invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise BaseImageError(f"{what} returned no JSON object")
    return document


def _newest_tag(package: str) -> str:
    """List the publication tags of a guest repository and return the newest."""
    result = run_cmd(["oras", "repo", "tags", package], check=False, capture=True)
    if result is None or result.returncode != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise BaseImageError(
            f"no guest image found at {package}; publish it with packer-gcp.yml ({stderr or 'oras repo tags failed'})"
        )
    newest = _newest_publication_tag((result.stdout or "").splitlines())
    if newest is None:
        raise BaseImageError(f"{package} has no run-<number>.<attempt> publication tag")
    return newest


def newest_publications(registry: str) -> dict[str, str]:
    """Return the exact identity of the newest publication of each guest role.

    The release workflow verifies the result with :func:`resolve_selection` before it
    records the identities in the release manifest.
    """
    newest: dict[str, str] = {}
    for role in BASE_IMAGE_ROLES:
        package = package_for_role(role, registry)
        tag = _newest_tag(package)
        descriptor = _oras_json(
            ["oras", "manifest", "fetch", "--descriptor", f"{package}:{tag}"],
            what=f"resolving {package}:{tag}",
        )
        digest = str(descriptor.get("digest", "")).strip()
        if not _DIGEST_RE.fullmatch(digest):
            raise BaseImageError(f"the registry returned no sha256 digest for {package}:{tag}")
        newest[role] = f"{package}@{digest}"
    return newest


def resolve_selection(
    base_images: Mapping[str, str],
    *,
    repository_id: str,
    owner_id: str,
    oci: ManifestReader,
    verifier: DsseVerifier,
    release_source: str = "",
) -> list[ResolvedArtifact]:
    """Verify every guest of a release before any change and return the resolved artifacts.

    A guest passes only when its digest-verified manifest meets the artifact contract and
    one stored Sigstore bundle satisfies the trust anchor at its build revision, from a
    protected build branch.
    """
    from gcp_oci import ImageReference, OciError
    from gcp_provenance import ProvenanceError, ReleaseTrust, verify_image_provenance

    if not base_images:
        raise BaseImageError("the release names no guest images; use a release that names them")
    if set(base_images) != set(BASE_IMAGE_ROLES):
        raise BaseImageError("the release must name exactly the guest images: " + ", ".join(BASE_IMAGE_ROLES))
    artifacts: list[ResolvedArtifact] = []
    for role in BASE_IMAGE_ROLES:
        canonical = base_images[role]
        transport = transport_identity(role, canonical, release_source)
        try:
            manifest = oci.manifest(ImageReference.parse(transport))
            disk_digest, revision = validate_artifact_manifest(manifest, role=role)
            trust = ReleaseTrust(
                repository_id=repository_id,
                owner_id=owner_id,
                source_revision=revision,
                source_refs=GUEST_BUILD_REFS,
            )
            verify_image_provenance(transport, trust, oci=oci, verifier=verifier)  # type: ignore[arg-type]
        except (OciError, ProvenanceError) as exc:
            raise BaseImageError(f"guest image {role} failed verification: {exc}") from None
        info(f"Verified guest '{role}' (build {revision[:12]})")
        artifacts.append(
            ResolvedArtifact(
                role=role, canonical=canonical, transport=transport, disk_digest=disk_digest, revision=revision
            )
        )
    return artifacts


# --- GCE boundary (mocked in tests via bootstrap_core.run_cmd) -----------------


def _existing_image_metadata(project: str, image_name: str) -> dict[str, object] | None:
    """Return code-owned provenance, status, and boot features for an existing image."""
    result = run_cmd(
        [
            "gcloud",
            "compute",
            "images",
            "describe",
            image_name,
            "--project",
            project,
            "--format=json(description,labels,status,guestOsFeatures)",
        ],
        check=False,
        capture=True,
    )
    if result is None or result.returncode != 0:
        return None
    try:
        metadata = json.loads(getattr(result, "stdout", "") or "{}")
    except json.JSONDecodeError as exc:
        raise BaseImageError(f"GCE image {image_name} returned invalid metadata JSON") from exc
    if not isinstance(metadata, dict):
        raise BaseImageError(f"GCE image {image_name} returned invalid metadata")
    return metadata


def _verify_image_ready(project: str, image_name: str) -> None:
    """Fail unless the freshly created GCE image reports status READY."""
    result = run_cmd(
        ["gcloud", "compute", "images", "describe", image_name, "--project", project, "--format=value(status)"],
        check=False,
        capture=True,
    )
    status = (getattr(result, "stdout", "") or "").strip()
    if status != "READY":
        raise BaseImageError(f"imported GCE image {image_name} is not READY (status={status or 'unknown'})")


def _existing_image_is_reusable(artifact: ResolvedArtifact, metadata: dict[str, object], image_name: str) -> bool:
    """Return true only for an exact code-owned, READY, runtime-compatible image."""
    labels = metadata.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    exact_source = str(metadata.get("description", "")).strip() == artifact.pinned_reference
    exact_role = str(labels.get("shifter-base-role", "")).strip() == artifact.role
    exact_digest_label = str(labels.get("shifter-base-digest", "")).strip() == artifact.short_digest
    if not (exact_source and exact_role and exact_digest_label):
        raise BaseImageError(
            f"GCE image {image_name} already exists without the exact source and role metadata; "
            "refusing to reuse an image this import did not create"
        )
    return str(metadata.get("status", "")).strip() == "READY" and _required_guest_os_features(artifact.role).issubset(
        _guest_os_feature_types(metadata)
    )


def _delete_owned_image(project: str, image_name: str) -> None:
    """Delete a proven code-owned image before converging its import contract."""
    run_cmd(["gcloud", "compute", "images", "delete", image_name, "--project", project, "--quiet"])


def _pull_disk_tarball(artifact: ResolvedArtifact, destination: Path) -> Path:
    """Fetch the validated disk blob to a caller-owned path and return it."""
    tarball = destination / f"{artifact.role}-{artifact.short_digest}.tar.gz"
    run_cmd(
        [
            "oras",
            "blob",
            "fetch",
            "--output",
            str(tarball),
            f"{artifact.transport_repository}@{artifact.disk_digest}",
        ]
    )
    if not tarball.is_file():
        raise BaseImageError(f"disk blob fetch produced no tarball for {artifact.pinned_reference}")
    return tarball


def _reconcile_existing_owned_image(
    artifact: ResolvedArtifact,
    metadata: dict[str, object],
    *,
    image_name: str,
    image_ref: str,
    project: str,
    dry_run: bool,
) -> ImportedImage | None:
    """Reuse, or clear for reimport, an existing code-owned image.

    Returns the reusable image when its digest and runtime guest features
    match; otherwise deletes the incompatible owned image (unless ``dry_run``)
    and returns ``None`` so the caller reimports.
    """
    if _existing_image_is_reusable(artifact, metadata, image_name):
        _verify_image_ready(project, image_name)
        info(f"Reusing existing GCE image {image_name} for role '{artifact.role}' (digest unchanged)")
        return ImportedImage(artifact.role, image_name, image_ref, artifact.digest, reused=True)
    status = str(metadata.get("status", "")).strip() or "unknown"
    if status not in {"FAILED", "READY"}:
        raise BaseImageError(f"code-owned GCE image {image_name} is not READY (status={status}); retry later")
    missing_features = _required_guest_os_features(artifact.role) - _guest_os_feature_types(metadata)
    reason = f"missing guest features {sorted(missing_features)}" if missing_features else f"status={status}"
    if dry_run:
        info(f"[DRY-RUN] Would replace code-owned GCE image {image_name} ({reason})")
    else:
        info(f"Replacing code-owned GCE image {image_name} ({reason})")
        _delete_owned_image(project, image_name)
    return None


def import_base_image(
    artifact: ResolvedArtifact,
    *,
    project: str,
    staging_bucket: str,
    dry_run: bool = False,
) -> ImportedImage:
    """Import (or reuse) a validated artifact as a native GCE image.

    An unchanged digest reuses the existing digest-named image when its runtime
    guest features match. A changed digest or incompatible owned image is
    imported as a traceably named image and moves the ``shifter-<role>`` family
    head. The staging object is removed on success and failure.
    """
    image_name = image_name_for(artifact.role, artifact.short_digest)
    family = f"shifter-{artifact.role}"
    image_ref = f"projects/{project}/global/images/{image_name}"

    metadata = _existing_image_metadata(project, image_name)
    if metadata is not None:
        reused = _reconcile_existing_owned_image(
            artifact,
            metadata,
            image_name=image_name,
            image_ref=image_ref,
            project=project,
            dry_run=dry_run,
        )
        if reused is not None:
            return reused

    if dry_run:
        info(f"[DRY-RUN] Would import {artifact.pinned_reference} as native GCE image {image_name}")
        return ImportedImage(artifact.role, image_name, image_ref, artifact.digest, reused=False)
    if not staging_bucket:
        raise BaseImageError("base-image import requires a run-owned transfer bucket")

    subheader(f"Importing '{artifact.role}' base image as native GCE image {image_name}")
    staging_uri = f"gs://{staging_bucket}/base-images/{image_name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="shifter-gce-base-") as tmp:
        tarball = _pull_disk_tarball(artifact, Path(tmp))
        try:
            run_cmd(["gcloud", "storage", "cp", "--quiet", str(tarball), staging_uri, "--project", project])
            create_cmd = [
                "gcloud",
                "compute",
                "images",
                "create",
                image_name,
                "--source-uri",
                staging_uri,
                "--family",
                family,
                "--project",
                project,
                "--labels",
                f"shifter-base-role={artifact.role},shifter-base-digest={artifact.short_digest}",
                "--description",
                artifact.pinned_reference,
            ]
            create_cmd += [
                "--guest-os-features",
                ",".join(sorted(_required_guest_os_features(artifact.role))),
            ]
            run_cmd(create_cmd)
            _verify_image_ready(project, image_name)
        finally:
            # Always clear the staging object so no transfer drift is left behind.
            run_cmd(["gcloud", "storage", "rm", "--quiet", staging_uri, "--project", project], check=False)

    success(f"Imported '{artifact.role}' as native GCE image {image_name}")
    return ImportedImage(artifact.role, image_name, image_ref, artifact.digest, reused=False)


@contextmanager
def registry_credentials(*identities: str) -> Iterator[None]:
    """Use the gcloud credential helper for Artifact Registry hosts, for this import only.

    Any other registry is read anonymously with an empty, isolated client config, so no
    unrelated credential reaches it.
    """
    from gcp_oci import is_artifact_registry_host

    hosts = {identity.split("/", 1)[0] for identity in identities}
    helpers = {host: "gcloud" for host in sorted(hosts) if is_artifact_registry_host(host)}
    previous = os.environ.get("DOCKER_CONFIG")
    with tempfile.TemporaryDirectory(prefix="bigrae-oras-") as directory:
        Path(directory, "config.json").write_text(json.dumps({"credHelpers": helpers}), encoding="utf-8")
        os.environ["DOCKER_CONFIG"] = directory
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("DOCKER_CONFIG", None)
            else:
                os.environ["DOCKER_CONFIG"] = previous


def _run_checked(cmd: list[str], *, failure: str) -> None:
    """Run a command while retaining domain-specific failure handling."""
    result = run_cmd(cmd, check=False, capture=True)
    if result is None or result.returncode != 0:
        raise BaseImageError(failure)


def _create_transfer_bucket(project: str, region: str) -> str:
    """Create and return one private, run-owned transfer bucket."""
    bucket = f"shifter-base-import-{project}-{uuid.uuid4().hex[:12]}"
    if len(bucket) > 63:
        raise BaseImageError("project ID is too long for the run-owned transfer bucket name")
    _run_checked(
        [
            "gcloud",
            "storage",
            "buckets",
            "create",
            f"gs://{bucket}",
            "--project",
            project,
            "--location",
            region,
            "--uniform-bucket-level-access",
            "--public-access-prevention",
            "--soft-delete-duration=0",
        ],
        failure=f"failed to create run-owned base-image transfer bucket {bucket}",
    )
    return bucket


def _delete_transfer_bucket(project: str, bucket: str) -> None:
    """Delete every object/version and the exact run-owned bucket."""
    _run_checked(
        ["gcloud", "storage", "rm", "--recursive", f"gs://{bucket}/", "--project", project, "--quiet"],
        failure=f"base-image transfer cleanup failed for run-owned bucket {bucket}",
    )


def _needs_import(artifact: ResolvedArtifact, project: str) -> bool:
    """True unless an exact, reusable, code-owned image for ``artifact`` already exists."""
    metadata = _existing_image_metadata(project, image_name_for(artifact.role, artifact.short_digest))
    if metadata is None:
        return True
    return not _existing_image_is_reusable(artifact, metadata, image_name_for(artifact.role, artifact.short_digest))


def import_selection(
    artifacts: list[ResolvedArtifact],
    *,
    project: str,
    region: str = "us-central1",
    dry_run: bool = False,
) -> dict[str, ImportedImage]:
    """Import or reuse every verified guest as a native GCE image.

    Returns the image for each role; :func:`render_range_image_env` turns that into the
    ``GCP_RANGE_*_IMAGE`` values. A set where every image is reusable creates no
    transfer bucket.
    """
    header("Release guest images (registry -> native GCE)")
    if dry_run or not any(_needs_import(artifact, project) for artifact in artifacts):
        return {
            artifact.role: import_base_image(artifact, project=project, staging_bucket="", dry_run=dry_run)
            for artifact in artifacts
        }
    bucket = _create_transfer_bucket(project, region)
    try:
        with registry_credentials(*(artifact.transport for artifact in artifacts)):
            return {
                artifact.role: import_base_image(artifact, project=project, staging_bucket=bucket)
                for artifact in artifacts
            }
    finally:
        _delete_transfer_bucket(project, bucket)
