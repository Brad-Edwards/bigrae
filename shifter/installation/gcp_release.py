"""GCP release manifest (``bigrae.gcp-release.v1``) for release-driven deploys (#2571, ADR-070).

A release names one source revision and the exact image identities that the release
workflow built and attested for it. The deployer image embeds the manifest. CI writes
the same document for the images that one CI run built. The deploy command reads only
this closed model, so a deploy never builds an image or resolves a mutable tag.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigIssue, InstallationConfigError

RELEASE_MANIFEST_SCHEMA = "bigrae.gcp-release.v1"
_SUBJECT = "GCP release manifest"
#: Release image names, in deploy order. ``provisioner`` maps to the deployment
#: ``pulumi-provisioner`` Artifact Registry root (a retained compatibility name).
RELEASE_IMAGE_NAMES: tuple[str, ...] = ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")
ReleaseImageName = Literal["portal", "provisioner", "guacd", "guacamole-client", "openvpn"]

_IMAGE_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$"
_CHART_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"


class GcpReleaseManifest(BaseModel):
    """Closed release manifest: one revision and five exact image identities."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)

    schema_id: Literal["bigrae.gcp-release.v1"] = Field(alias="schema")
    version: str = Field(pattern=_VERSION_PATTERN, description="Product version from version.txt, or a CI label.")
    source_revision: str = Field(pattern=_REVISION_PATTERN, description="Full commit SHA that every image built.")
    chart_version: str = Field(pattern=_CHART_VERSION_PATTERN, description="Helm chart version of the revision.")
    images: dict[ReleaseImageName, str] = Field(description="Exact repository@sha256 identity for each image.")

    @field_validator("images")
    @classmethod
    def _closed_exact_images(cls, images: dict[str, str]) -> dict[str, str]:
        missing = sorted(set(RELEASE_IMAGE_NAMES) - set(images))
        if missing:
            raise ValueError("release images are missing: " + ", ".join(missing))
        invalid = sorted(name for name, identity in images.items() if not _IMAGE_IDENTITY_RE.fullmatch(identity))
        if invalid:
            raise ValueError("release images must be exact repository@sha256 identities: " + ", ".join(invalid))
        return images

    def digest(self, name: str) -> str:
        """Return the ``sha256:<hex>`` digest of one release image."""
        return self.images[name].rsplit("@", 1)[1]  # type: ignore[index]

    def repository(self, name: str) -> str:
        """Return the repository path of one release image, without the digest."""
        return self.images[name].rsplit("@", 1)[0]  # type: ignore[index]


def _issues(exc: ValidationError) -> list[ConfigIssue]:
    """Name the rejected fields without echoing their values."""
    return [
        ConfigIssue(
            "release." + (".".join(str(part) for part in error["loc"]) or "<root>"),
            str(error.get("msg", "invalid value")),
        )
        for error in exc.errors()
    ]


def parse_release_manifest(text: str) -> GcpReleaseManifest:
    """Parse and validate a release manifest document."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise InstallationConfigError(
            [ConfigIssue("release", "release manifest is not valid JSON")], subject=_SUBJECT
        ) from None
    if not isinstance(payload, dict):
        raise InstallationConfigError(
            [ConfigIssue("release", "release manifest must be a JSON object")], subject=_SUBJECT
        )
    try:
        return GcpReleaseManifest.model_validate(payload)
    except ValidationError as exc:
        raise InstallationConfigError(_issues(exc), subject=_SUBJECT) from None


def load_release_manifest(path: Path) -> GcpReleaseManifest:
    """Read and validate a release manifest file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise InstallationConfigError(
            [ConfigIssue("release", f"cannot read release manifest {path}")], subject=_SUBJECT
        ) from None
    return parse_release_manifest(text)


def build_release_manifest(
    *, version: str, source_revision: str, chart_version: str, images: dict[str, str]
) -> GcpReleaseManifest:
    """Build and validate a release manifest from its parts."""
    try:
        return GcpReleaseManifest.model_validate(
            {
                "schema": RELEASE_MANIFEST_SCHEMA,
                "version": version,
                "source_revision": source_revision,
                "chart_version": chart_version,
                "images": images,
            }
        )
    except ValidationError as exc:
        raise InstallationConfigError(_issues(exc), subject=_SUBJECT) from None


def render_release_manifest(manifest: GcpReleaseManifest) -> str:
    """Render the canonical JSON form of a release manifest."""
    return json.dumps(manifest.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True) + "\n"
