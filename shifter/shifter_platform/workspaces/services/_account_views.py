"""Persistence-neutral account projections and display-safe workspace options."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from workspaces.models import Account, Organization, Workspace

_DENIED = "Scope unavailable"


class AccountScopeError(ValueError):
    """An account scope is absent, malformed, or has invalid ancestry."""


@dataclass(frozen=True, slots=True)
class AccountView:
    """Public projection of a durable customer account."""

    id: int
    uuid: UUID
    kind: str
    individual_principal_uuid: UUID | None


@dataclass(frozen=True, slots=True)
class OrganizationView:
    """Public projection of an account-owned organization."""

    id: int
    uuid: UUID
    account_id: int
    is_default: bool


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    """Public projection of an organization-owned workspace."""

    id: int
    uuid: UUID
    organization_id: int
    is_default: bool


@dataclass(frozen=True, slots=True)
class WorkspaceOptionView:
    """Display-safe workspace projection for cross-domain selectors."""

    id: int
    uuid: UUID
    name: str
    organization_id: int
    organization_uuid: UUID
    organization_name: str


@dataclass(frozen=True, slots=True)
class ResolvedResourceScope:
    """Resolved scalar ancestry for a validated resource scope."""

    kind: str
    account_id: int | None
    organization_id: int | None
    workspace_id: int | None


def _account_view(account: Account) -> AccountView:
    """Project an account model without exposing persistence internals."""
    return AccountView(account.id, account.uuid, account.kind, account.individual_principal_uuid)


def _organization_view(organization: Organization) -> OrganizationView:
    """Project an organization only when its account ancestry is present."""
    account_id = organization.account_id
    if account_id is None:
        raise AccountScopeError(_DENIED)
    return OrganizationView(organization.id, organization.uuid, account_id, organization.is_default)


def _workspace_view(workspace: Workspace) -> WorkspaceView:
    """Project a workspace without loading unrelated ancestry."""
    return WorkspaceView(workspace.id, workspace.uuid, workspace.organization_id, workspace.is_default)


def workspace_option(workspace_uuid: UUID) -> WorkspaceOptionView:
    """Resolve one active workspace without exposing the tenancy model."""
    workspace = (
        Workspace.objects.select_related("organization")
        .filter(uuid=workspace_uuid, archived_at__isnull=True, organization__account__isnull=False)
        .first()
    )
    if workspace is None:
        raise AccountScopeError(_DENIED)
    return WorkspaceOptionView(
        workspace.pk,
        workspace.uuid,
        workspace.name,
        workspace.organization_id,
        workspace.organization.uuid,
        workspace.organization.name,
    )
