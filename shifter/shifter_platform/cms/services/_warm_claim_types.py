"""Immutable warm-claim request and structural Engine generation projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from shared.credentials import CredentialContext
from shared.enums import RangeSource
from shared.identity_scope import ResourceScope
from shared.range_instantiation_policy import InstantiationPurpose

if TYPE_CHECKING:
    from shared.model_access.reservation import ModelLaunchScope


class _WarmGeneration(Protocol):
    """Narrow structural view received from the Engine claim service."""

    request_id: UUID
    uuid: UUID
    bucket_id: str


@dataclass(frozen=True)
class WarmClaimRequest:
    """Immutable launch inputs for one warm-pool claim attempt (#28).

    Groups the realization identity and launch context so the claim path derives
    the same compatibility digest the warm reconciler stamped, resolved
    independently on each side.
    """

    credential: CredentialContext
    scope: ResourceScope
    scenario: str
    package_digest: str
    lock_digest: str
    backend: str
    instantiation_purpose: InstantiationPurpose
    range_source: RangeSource
    egress_mode: str
    request_id: UUID
    #: Trusted event deadline for a CTF-owned generation. Mission Control policy is
    #: resolved only after a warm generation is claimed inside this transaction.
    enforced_deadline: datetime | None = None
    model_launch_scope: ModelLaunchScope | None = None
    model_sources: dict[str, Any] | None = None
    policy_workspace_id: int | None = None
    source_binding: tuple[tuple[str, object], ...] | None = None
    source_metadata: tuple[bool, bool] | None = None


@dataclass(frozen=True)
class _ClaimOutcome:
    """The result of a committed atomic claim, carried to the post-commit steps."""

    request_id: UUID
    bucket_id: str
    generation_uuid: UUID
