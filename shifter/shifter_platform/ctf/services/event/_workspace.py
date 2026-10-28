"""Resolve explicit account ancestry for new CTF events (ADR-066)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import workspaces.services as workspace_services
from ctf.exceptions import CTFValidationError
from ctf.services.unified_authorization import require_event_creation
from shared.authorization import TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope

_PLACEMENT_KEYS: tuple[Literal["account", "organization", "workspace"], ...] = ("account", "organization", "workspace")


@dataclass(frozen=True, slots=True)
class EventPlacement:
    """Validated target, public scope, and scalar persistence ancestry."""

    target: TargetRef
    scope: ResourceScope
    account_id: int
    organization_id: int | None
    workspace_id: int | None


def resolve_event_placement(
    credential: CredentialContext, event_data: dict[str, Any], *, compatibility_actor: object | None = None
) -> EventPlacement:
    """Authorize one selected parent; omission means the human's existing individual account."""
    selected = [(kind, event_data.get(kind)) for kind in _PLACEMENT_KEYS if event_data.get(kind) is not None]
    try:
        if not selected:
            scope = workspace_services.individual_account_scope(credential.principal)
            if scope.account_uuid is None:
                raise ValueError
            target = TargetRef("account", scope.account_uuid)
        elif len(selected) == 1:
            kind, selected_uuid = selected[0]
            if not isinstance(selected_uuid, UUID):
                raise ValueError
            target = TargetRef(kind, selected_uuid)
            scope = workspace_services.event_placement_scope(kind, selected_uuid)
        else:
            raise ValueError
        authorized_scope = require_event_creation(
            compatibility_actor if compatibility_actor is not None else credential, target
        )
        if authorized_scope != scope:
            raise ValueError
        binding = workspace_services.resolve_resource_scope(scope)
        if binding.account_id is None:
            raise ValueError
    except (ValueError, workspace_services.AccountScopeError):
        raise CTFValidationError(
            "Event placement is not available.",
            code="CTF_EVENT_PLACEMENT_NOT_AVAILABLE",
        ) from None
    return EventPlacement(
        target,
        scope,
        binding.account_id,
        binding.organization_id,
        binding.workspace_id,
    )
