"""Typed native range-launch inputs and the exact prepared reservation context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from shared.enums import RangeSource

if TYPE_CHECKING:
    from cms.models import RaesPackageSource
    from shared.authorization import TargetRef
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.model_access import OwnedReference
    from shared.model_access.reservation import ModelLaunchScope
    from shared.range_instantiation_policy import BackendAdmission
    from workspaces.services import ResolvedResourceScope


@dataclass(frozen=True, slots=True)
class NativeEventLaunchOptions:
    """Server-owned event authority and immutable replay inputs at the native facade."""

    owner_principal: PrincipalRef | None = None
    authority_action: str | None = None
    authority_target: TargetRef | None = None
    remote_access_teardown_at: datetime | None = None
    model_admission_subject: OwnedReference | None = None
    model_launch_scope: ModelLaunchScope | None = None
    request_uuid: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScopedRangeLaunchOptions:
    """Explicit customer placement and optional event-owned launch binding."""

    scope: ResourceScope | None
    range_source: RangeSource | None = None
    workspace_uuid: UUID | str | None = None
    model_sources: dict[str, Any] | None = None
    event: NativeEventLaunchOptions = NativeEventLaunchOptions()


@dataclass(frozen=True, slots=True)
class PreparedScopedLaunch:
    """Freeze current proof, source, placement and owner before reservation."""

    original: CredentialContext
    current: CredentialContext
    scope: ResourceScope
    source: RaesPackageSource
    resolved: ResolvedResourceScope
    owner: PrincipalRef
    user_id: int | None
    request_id: UUID
    backend: BackendAdmission | None
    effective_source: RangeSource
    egress: str
    options: ScopedRangeLaunchOptions
    intent_digest: str = ""

    @property
    def is_event(self) -> bool:
        """An event launch has the closed CTF source kind."""
        return self.effective_source is RangeSource.CTF
