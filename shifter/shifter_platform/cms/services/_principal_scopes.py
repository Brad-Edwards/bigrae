"""CMS-owned presentation seam for principal range placement scopes."""

from __future__ import annotations

from uuid import UUID

from shared.identity_scope import PrincipalRef, ResourceScope
from workspaces.services import AccountScopeError, hierarchy_target_scope, individual_account_scope


def mission_control_range_scope(principal: PrincipalRef, workspace_uuid: UUID | None = None) -> ResourceScope:
    """Resolve an individual or selected workspace scope for Mission Control."""
    if workspace_uuid is None:
        return individual_account_scope(principal)
    return hierarchy_target_scope("workspace", workspace_uuid)


__all__ = ["AccountScopeError", "mission_control_range_scope"]
