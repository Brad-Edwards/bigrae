"""Closed tenant model-source intent and actor-selected routing references."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import Field, field_validator, model_validator

if TYPE_CHECKING:
    from shared.credentials import CredentialContext

from shared.model_access.core_models import (
    ClosedModel,
    Currency,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    Region,
    _require_unique,
)
from shared.model_access.provider_runtime import AuthenticationKind, ProviderKind, ProviderTarget


class ModelSourceConfiguration(ClosedModel):
    """A provider connection with explicit region, price and real quota identity."""

    name: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")]
    provider: ProviderKind
    authentication: AuthenticationKind
    model: Annotated[str, Field(pattern=r"^[a-zA-Z0-9@.:/-]{1,256}$")]
    region: Region
    project: Annotated[str, Field(max_length=30)] = ""
    principal: Annotated[str, Field(max_length=256)] = ""
    count_region: Annotated[str, Field(max_length=64)] = ""
    quota_identity: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:/._@-]+$")]
    context_window_tokens: Annotated[int, Field(strict=True, ge=1, le=2_000_000)]
    tokens_per_minute: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
    input_price_per_million: NonNegativeInt
    output_price_per_million: NonNegativeInt
    currency: Currency = Currency.USD
    price_valid_until: datetime
    # OpenRouter execution pins the selected upstream; no hidden fallback pool.
    upstream_provider: Annotated[str, Field(max_length=100, pattern=r"^[a-zA-Z0-9 /._-]*$")] = ""

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        if self.price_valid_until.tzinfo is None or self.region == "global":
            raise ValueError("explicit price validity and data region required")
        if bool(self.upstream_provider) != (self.provider == "openrouter-v1"):
            raise ValueError("OpenRouter requires an explicit upstream provider")
        self.target(UUID(int=1), 1)
        return self

    @property
    def capacity_identity(self) -> str:
        """Cloud labels cannot split the same account/region application ceiling."""
        if self.provider == "vertex-v1":
            return f"project:{self.project}/region:{self.region}"
        if self.provider == "bedrock-v1":
            return f"account:{self.principal.split(':')[4]}/region:{self.region}"
        return self.quota_identity

    def target(self, source_id: UUID, revision: int) -> ProviderTarget:
        return ProviderTarget(
            shard_id=f"source-{source_id.hex}-v{revision}",
            provider=self.provider,
            authentication=self.authentication,
            region=self.region,
            model=self.model,
            credential_reference=f"source:{source_id}:{revision}",
            principal=self.principal,
            project=self.project,
            count_region=self.count_region,
            context_window_tokens=self.context_window_tokens,
        )


class SourceChoice(ClosedModel):
    """Revision-pinned, weighted source selection; it conveys no authority."""

    source_id: UUID
    revision: PositiveInt
    weight: Annotated[int, Field(strict=True, ge=1, le=64)] = 1


class AliasSourceSelection(ClosedModel):
    """One or several sources for a logical alias, separate from pack content."""

    logical_alias: Identifier
    sources: Annotated[tuple[SourceChoice, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({item.source_id for item in self.sources}) != len(self.sources):
            raise ValueError("duplicate source selection")
        return self


class ModelSourceSelection(ClosedModel):
    """Owner-authored launch/event selection with optimistic revision control."""

    aliases: Annotated[tuple[AliasSourceSelection, ...], Field(max_length=128)] = ()

    @model_validator(mode="after")
    def unique_aliases(self) -> Self:
        _require_unique(tuple(item.logical_alias for item in self.aliases), "aliases")
        return self


class ModelSourceSponsorship(ClosedModel):
    """Server-projected source authority for event-funded participant ranges."""

    credential_reference: ModelSourceCredentialReference
    organization_uuid: UUID
    workspace_uuid: UUID
    selection: ModelSourceSelection


class ModelSourceCredentialReference(ClosedModel):
    """Non-secret, exact initiating credential carried by delayed model work."""

    principal_uuid: UUID
    principal_kind: Literal["human", "service"]
    credential_kind: Literal["session", "personal", "service", "temporary"]
    credential_uuid: UUID
    ceiling_actions: Annotated[tuple[str, ...], Field(max_length=256)]
    target_type: str = ""
    target_uuid: UUID | None = None
    scopes: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    event_uuid: UUID | None = None

    @field_validator("ceiling_actions", "scopes")
    @classmethod
    def canonical_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("credential values must be unique and sorted")
        return values

    @classmethod
    def from_credential(cls, credential: CredentialContext) -> Self:
        """Capture limits and identity, never bearer proof."""
        from shared.operation_authority import credential_reference_fields

        fields = credential_reference_fields(credential)
        return cls(
            principal_uuid=cast(UUID, fields["initiating_principal_uuid"]),
            principal_kind=cast(Literal["human", "service"], fields["initiating_principal_kind"]),
            credential_kind=cast(Literal["session", "personal", "service", "temporary"], fields["credential_kind"]),
            credential_uuid=cast(UUID, fields["credential_uuid"]),
            ceiling_actions=tuple(str(fields["credential_ceiling_actions"]).splitlines()),
            target_type=str(fields["credential_target_type"]),
            target_uuid=cast(UUID | None, fields["credential_target_uuid"]),
            scopes=tuple(str(fields["credential_scopes"]).splitlines()),
            event_uuid=cast(UUID | None, fields["credential_event_uuid"]),
        )

    def credential(self) -> CredentialContext:
        """Rebuild the trusted reference for live owner revalidation."""
        from shared.operation_authority import credential_reference_from_fields

        return credential_reference_from_fields(
            {
                "initiating_principal_uuid": self.principal_uuid,
                "initiating_principal_kind": self.principal_kind,
                "credential_kind": self.credential_kind,
                "credential_uuid": self.credential_uuid,
                "credential_ceiling_actions": "\n".join(self.ceiling_actions),
                "credential_target_type": self.target_type,
                "credential_target_uuid": self.target_uuid,
                "credential_scopes": "\n".join(self.scopes),
                "credential_event_uuid": self.event_uuid,
            }
        )


ModelSourceSponsorship.model_rebuild()


@dataclass(frozen=True)
class ModelSourceUseScope:
    """CMS-resolved tenant scope and exact credential passed internally.

    This is not a wire payload. Engine enforces organization.manage_model_sources
    and checks source ownership and revision; CMS resolves SQL ancestry first.
    """

    credential: CredentialContext
    organization_uuid: UUID
