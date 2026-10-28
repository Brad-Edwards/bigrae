"""Closed GCP deployment-input settings that replace repository-hosted variables (#2571).

A GCP deployment that runs without GitHub keeps every deploy input in the root
installation config, the Terraform values, or Secret Manager. These models hold the
inputs that were GitHub Environment variables or secrets before #2571: range-cell
placement, the participant OpenVPN opt-in, the CTF content bucket, the first operator,
the post-deploy smoke identity, and the release provenance trust anchor.

Every model is closed (``extra="forbid"``). Values are public identifiers or secret
*references*, never secret payloads. The projections in this module produce the exact
runtime environment keys and Terraform variables that the deploy command passes to the
existing renderers, so the renderers keep one input contract.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_GCE_ZONE_PATTERN = r"^[a-z]+-[a-z]+[0-9]+-[a-z]$"
_GCP_PROJECT_ID_PATTERN = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
_EMAIL_PATTERN = r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$"
_BUCKET_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$"
_NUMERIC_ID_PATTERN = r"^[1-9][0-9]*$"
_RESOURCE_REF_PATTERN = r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/[A-Za-z0-9_-]{1,255}$"
# Same bound the runtime renderer and the provisioner apply to the key-profile JSON.
_IMAGE_KEY_PROFILES_MAX_BYTES = 32_768

GceZone = Annotated[str, Field(pattern=_GCE_ZONE_PATTERN)]
OperatorEmail = Annotated[str, Field(pattern=_EMAIL_PATTERN, max_length=254)]


class _ClosedModel(BaseModel):
    """Base model that rejects undeclared deployment settings."""

    model_config = ConfigDict(extra="forbid")


class GcpRangeCellSettings(_ClosedModel):
    """GCE range-cell placement and guest-profile inputs (formerly GitHub variables)."""

    backend: Literal["gce", "gdc"] = Field(
        default="gce",
        description="Range backend selector (GCP_RANGE_BACKEND). gce is the supported default; gdc is legacy.",
    )
    zone: str = Field(
        default="",
        pattern=rf"^$|{_GCE_ZONE_PATTERN}",
        description="Primary range zone (RANGE_NETWORK_ZONE). The GCE backend requires it to launch a range.",
    )
    zones: list[GceZone] = Field(
        default_factory=list,
        description="Optional ordered multi-region zone pool (RANGE_NETWORK_ZONES). Empty keeps one zone.",
    )
    project_id: str = Field(
        default="",
        pattern=rf"^$|{_GCP_PROJECT_ID_PATTERN}",
        description="Range-cell project (GCP_RANGE_CELL_PROJECT_ID). Empty selects the deployment project.",
    )
    host_identity_pool_size: int = Field(
        default=0,
        ge=0,
        description="Range-host identity pool size (GCP_RANGE_HOST_IDENTITY_POOL_SIZE and the Terraform pool).",
    )
    private_google_access: bool | None = Field(
        default=None,
        description="Private Google Access on range subnets (GCP_RANGE_PRIVATE_GOOGLE_ACCESS). Null keeps the default.",
    )
    image_key_profiles: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Keyed guest image profiles (GCP_RANGE_IMAGE_KEY_PROFILES_JSON). The provisioner validates entries."
        ),
    )

    @field_validator("zones")
    @classmethod
    def _unique_zones(cls, zones: list[str]) -> list[str]:
        if len(set(zones)) != len(zones):
            raise ValueError("range_cells.zones must not repeat a zone")
        return zones

    @field_validator("image_key_profiles")
    @classmethod
    def _bounded_profiles(cls, profiles: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(profiles, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _IMAGE_KEY_PROFILES_MAX_BYTES:
            raise ValueError(f"range_cells.image_key_profiles exceeds {_IMAGE_KEY_PROFILES_MAX_BYTES} bytes")
        return profiles


class GcpParticipantOpenVpnSettings(_ClosedModel):
    """Participant OpenVPN opt-in (formerly RANGE_OPENVPN_ENABLED)."""

    enabled: bool = Field(default=False, description="Deploy the shared participant OpenVPN pool.")


class GcpOperatorBootstrapSettings(_ClosedModel):
    """First Identity Platform operator and Django elevation lists."""

    enabled: bool = Field(
        default=True,
        description="Create the first operator. Set false to skip operator creation deliberately.",
    )
    admin_email: str = Field(
        default="",
        pattern=rf"^$|{_EMAIL_PATTERN}",
        max_length=254,
        description="First Identity Platform operator. It must match the Identity Platform allow-list.",
    )
    admin_password_secret_ref: str | None = Field(
        default=None,
        pattern=_RESOURCE_REF_PATTERN,
        description="Versionless Secret Manager resource that holds the initial operator password.",
    )
    staff_emails: list[OperatorEmail] = Field(
        default_factory=list, description="Accounts elevated to Django staff on first sign-in."
    )
    superuser_emails: list[OperatorEmail] = Field(
        default_factory=list, description="Accounts elevated to Django superuser on first sign-in."
    )

    @model_validator(mode="after")
    def _enabled_requires_email(self) -> GcpOperatorBootstrapSettings:
        if self.enabled and not self.admin_email:
            raise ValueError("operator_bootstrap.enabled requires admin_email")
        return self


class GcpPostDeploySmokeSettings(_ClosedModel):
    """Identity for the advisory post-deploy range smoke (formerly SMOKE_TEST_USER_EMAIL)."""

    user_email: str = Field(
        default="",
        pattern=rf"^$|{_EMAIL_PATTERN}",
        max_length=254,
        description="Existing portal account that the smoke Job acts as. Empty skips the smoke.",
    )


class GcpReleaseTrustSettings(_ClosedModel):
    """Immutable repository identity that signs release provenance (ADR-069-R3, ADR-070)."""

    github_repository_id: str = Field(
        pattern=_NUMERIC_ID_PATTERN, description="Numeric ID of the repository whose workflows sign releases."
    )
    github_owner_id: str = Field(pattern=_NUMERIC_ID_PATTERN, description="Numeric ID of that repository's owner.")


#: Optional bucket-name grammar for ``settings.ctf_content_bucket``.
CTF_CONTENT_BUCKET_PATTERN = rf"^$|{_BUCKET_NAME_PATTERN}"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _merged_emails(*groups: list[str]) -> str:
    """Join email lists in first-seen order without duplicates (case-insensitive)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            normalized = value.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
    return ",".join(ordered)


def deployment_runtime_env(
    range_cells: GcpRangeCellSettings,
    participant_openvpn: GcpParticipantOpenVpnSettings,
    operator_bootstrap: GcpOperatorBootstrapSettings | None,
) -> dict[str, str]:
    """Project the deployment settings onto the runtime renderer's process inputs.

    The keys are the exact names ``scripts/gcp/render_runtime_env.py`` reads. The
    projection emits only configured values, so an unset optional input keeps the
    renderer and provisioner defaults.
    """
    env = {
        "GCP_RANGE_BACKEND": range_cells.backend,
        "GCP_RANGE_HOST_IDENTITY_POOL_SIZE": str(range_cells.host_identity_pool_size),
        "RANGE_OPENVPN_ENABLED": _bool_text(participant_openvpn.enabled),
    }
    if range_cells.zone:
        env["RANGE_NETWORK_ZONE"] = range_cells.zone
    if range_cells.zones:
        env["RANGE_NETWORK_ZONES"] = ",".join(range_cells.zones)
    if range_cells.project_id:
        env["GCP_RANGE_CELL_PROJECT_ID"] = range_cells.project_id
    if range_cells.private_google_access is not None:
        env["GCP_RANGE_PRIVATE_GOOGLE_ACCESS"] = _bool_text(range_cells.private_google_access)
    if range_cells.image_key_profiles:
        env["GCP_RANGE_IMAGE_KEY_PROFILES_JSON"] = json.dumps(
            range_cells.image_key_profiles, sort_keys=True, separators=(",", ":")
        )
    if operator_bootstrap is not None:
        operator = [operator_bootstrap.admin_email] if operator_bootstrap.enabled else []
        env["PLATFORM_BOOTSTRAP_STAFF_EMAILS"] = _merged_emails(operator_bootstrap.staff_emails, operator)
        env["PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS"] = _merged_emails(operator_bootstrap.superuser_emails, operator)
    return env


def deployment_terraform_values(
    range_cells: GcpRangeCellSettings,
    participant_openvpn: GcpParticipantOpenVpnSettings,
    ctf_content_bucket: str,
) -> dict[str, object]:
    """Project the deployment settings onto the platform Terraform variables they own."""
    values: dict[str, object] = {
        "openvpn_pool_enabled": participant_openvpn.enabled,
        "range_host_identity_pool_size": range_cells.host_identity_pool_size,
        "range_network_zones": list(range_cells.zones),
    }
    if ctf_content_bucket:
        values["ctf_content_bucket_name"] = ctf_content_bucket
    return values
