"""Engine-owned selection of private inventory and explicitly shared catalog rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import resolve_credential_scope, resolve_current_credential

if TYPE_CHECKING:
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope


def inventory_scope_filter(scope: ResourceScope) -> Q:
    """The caller has resolved this scope through its owning service boundary."""
    private = Q(
        scope_kind=scope.kind,
        account_uuid=scope.account_uuid,
        organization_uuid=scope.organization_uuid,
        workspace_uuid=scope.workspace_uuid,
        owner_principal_uuid__isnull=False,
    )
    shared = Q(
        scope_kind="installation",
        catalog_publication_scope="operator",
        owner_principal_uuid__isnull=False,
        account_uuid__isnull=True,
        organization_uuid__isnull=True,
        workspace_uuid__isnull=True,
    )
    return private | shared


def authorize_inventory_read(credential: CredentialContext, scope: ResourceScope) -> Q:
    """Resolve and admit the actual inventory scope before selecting artifacts."""
    current = resolve_current_credential(credential)
    parent = scope_parent(scope)
    if resolve_credential_scope(parent) != scope:
        raise ValueError("Inventory scope unavailable")
    require_authorization(
        AuthorizationRequest(current.principal, f"{parent.type}.read_content", parent, scope, current.ceiling)
    )
    return inventory_scope_filter(scope)
