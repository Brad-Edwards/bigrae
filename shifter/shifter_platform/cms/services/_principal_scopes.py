"""CMS-owned presentation seam for principal range placement scopes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from shared.identity_scope import PrincipalRef, ResourceScope
from workspaces.services import AccountScopeError, hierarchy_target_scope, individual_account_scope

if TYPE_CHECKING:
    from shared.authorization import AuthorizationProvider
    from shared.credentials import CredentialContext
    from workspaces.services import LaunchWorkspacePage


def mission_control_range_scope(principal: PrincipalRef, workspace_uuid: UUID | None = None) -> ResourceScope:
    """Resolve an individual or selected workspace scope for Mission Control."""
    if workspace_uuid is None:
        return individual_account_scope(principal)
    return hierarchy_target_scope("workspace", workspace_uuid)


def mission_control_resource_scope(
    principal: PrincipalRef,
    *,
    account_uuid: UUID | None = None,
    organization_uuid: UUID | None = None,
    workspace_uuid: UUID | None = None,
) -> ResourceScope:
    """Resolve explicit resource placement through the owning CMS boundary."""
    if account_uuid is not None:
        from workspaces.services import resolve_resource_scope

        scope = ResourceScope("account", account_uuid, organization_uuid, workspace_uuid)
        resolve_resource_scope(scope)
        return scope
    if organization_uuid is not None:
        raise AccountScopeError("Resource account unavailable")
    return mission_control_range_scope(principal, workspace_uuid)


def mission_control_launch_workspaces(
    credential: CredentialContext,
    provider: AuthorizationProvider,
    *,
    offset: int = 0,
    limit: int = 100,
) -> LaunchWorkspacePage:
    """Return exact policy-admitted launch workspaces for Mission Control."""
    from workspaces.services import list_launch_workspace_options

    return list_launch_workspace_options(credential, provider, offset=offset, limit=limit)


__all__ = ["AccountScopeError", "mission_control_launch_workspaces", "mission_control_range_scope"]
