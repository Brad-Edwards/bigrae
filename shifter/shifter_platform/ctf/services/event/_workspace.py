"""Resolve explicit account ancestry for new CTF events (ADR-066)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import workspaces.services as workspace_services
from ctf.exceptions import CTFValidationError
from ctf.services.unified_authorization import require_event_creation
from shared.authorization import TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope

_PLACEMENT_KEYS = ("account", "organization", "workspace")


@dataclass(frozen=True, slots=True)
class EventPlacement:
    """Validated target, public scope, and scalar persistence ancestry."""

    target: TargetRef
    scope: ResourceScope
    account_id: int
    organization_id: int | None
    workspace_id: int | None


def resolve_event_placement(credential: CredentialContext, event_data: dict[str, Any]) -> EventPlacement:
    """Authorize one selected parent; omission means the human's existing individual account."""
    selected = [(kind, event_data.get(kind)) for kind in _PLACEMENT_KEYS if event_data.get(kind) is not None]
    try:
        if not selected:
            scope = workspace_services.individual_account_scope(credential.principal)
            if scope.account_uuid is None:
                raise ValueError
            target = TargetRef("account", scope.account_uuid)
        elif len(selected) == 1 and isinstance(selected[0][1], UUID):
            target = TargetRef(selected[0][0], selected[0][1])
            scope = workspace_services.event_placement_scope(target.type, target.uuid)
        else:
            raise ValueError
        authorized_scope = require_event_creation(credential, target)
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
