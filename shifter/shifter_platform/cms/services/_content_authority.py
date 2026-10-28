"""CMS's SQL scope and principal-neutral content authority boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cms.models import RaesPackageSource
    from cms.models.assets import Asset

from typing import Literal, cast
from uuid import UUID

from django.db import transaction

from cms.exceptions import CMSError
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import AuthorizationProviderBindingError, AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope
from workspaces.services import resolve_resource_scope

_UNAVAILABLE = "Content unavailable"


class ContentAuthorityError(CMSError):
    """A live content action could not be admitted under the original proof."""


def require_native_content_credential(credential: object) -> CredentialContext:
    """User attribution and SQL ownership cannot replace authenticated native proof."""
    if not isinstance(credential, CredentialContext):
        raise ContentAuthorityError(_UNAVAILABLE)
    return credential


def content_scope(row: Asset | RaesPackageSource | None) -> ResourceScope:
    """Reject absent legacy ownership, invalid scope and stale directory facts."""
    try:
        if row is None:
            raise ValueError(_UNAVAILABLE)
        owner = row.owner_principal_uuid
        if not isinstance(owner, UUID) or not owner.int:
            raise ValueError(_UNAVAILABLE)
        scope = ResourceScope(
            cast(Literal["installation", "account"], row.scope_kind),
            row.account_uuid,
            row.organization_uuid,
            row.workspace_uuid,
        )
        resolve_resource_scope(scope)
        return scope
    except Exception:
        raise CMSError(_UNAVAILABLE) from None


def set_catalog_publication(credential: CredentialContext, scenario_id: str, *, published: bool) -> None:
    """Only an explicitly installation-owned source enters the shared catalog."""
    from cms.models import RaesPackageSource

    if type(published) is not bool:
        raise CMSError(_UNAVAILABLE)
    source = RaesPackageSource.objects.filter(scenario_id=scenario_id).first()
    if source is None:
        raise CMSError("Content unavailable")
    scope = content_scope(source)
    if scope.kind != "installation":
        raise CMSError(_UNAVAILABLE)
    current = require_content_scope(credential, scope, "publish_content")
    with transaction.atomic():
        row = RaesPackageSource.objects.select_for_update().filter(pk=source.pk).first()
        if (
            row is None
            or content_scope(row) != scope
            or row.scope_generation != source.scope_generation
            or resolve_local_credential(current) != current
        ):
            raise CMSError(_UNAVAILABLE)
        if row.owner_principal_uuid != source.owner_principal_uuid:
            raise CMSError(_UNAVAILABLE)
        previous = row.is_operator_published
        row.is_operator_published = published
        row.save(update_fields=["is_operator_published", "updated_at"])
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.SCENARIO,
                entity_id=0,
                entity_ref=row.scenario_id,
                action=AuditAction.UPDATE,
                **principal_actor_fields(current.principal),
                previous_state={"is_operator_published": previous},
                new_state={"is_operator_published": published, "scope_generation": row.scope_generation},
            ),
            strict=True,
        )


def content_scope_fields(scope: ResourceScope) -> dict[str, object]:
    """Explicit scalar ancestry; absence never implies operator publication."""
    return {
        "scope_kind": scope.kind,
        "account_uuid": scope.account_uuid,
        "organization_uuid": scope.organization_uuid,
        "workspace_uuid": scope.workspace_uuid,
    }


def fence_content_scope(credential: CredentialContext, scope: ResourceScope) -> None:
    """Recheck admitted local proof and exact current ancestry inside its SQL write."""
    from workspaces.services import resolve_range_scope_binding

    try:
        if not transaction.get_connection().in_atomic_block:
            raise ValueError("Content commit requires a transaction")
        resolve_range_scope_binding(scope, for_update=True)
        if resolve_local_credential(credential) != credential:
            raise ValueError("Content proof changed")
    except Exception:
        raise ContentAuthorityError(_UNAVAILABLE) from None


def require_content_scope(
    credential: CredentialContext, scope: ResourceScope | None, operation: str
) -> CredentialContext:
    """Authorize the real parent before a query, mutation or storage effect."""
    try:
        if scope is None:
            raise ValueError(_UNAVAILABLE)
        current = resolve_current_credential(credential)
        resolve_resource_scope(scope)
        parent = scope_parent(scope)
        require_authorization(
            AuthorizationRequest(current.principal, f"{parent.type}.{operation}", parent, scope, current.ceiling)
        )
        if resolve_current_credential(current) != current:
            raise ValueError(_UNAVAILABLE)
        return current
    except AuthorizationProviderBindingError:
        raise ContentAuthorityError(
            "Content authority unavailable", details={"code": "authority-unavailable"}
        ) from None
    except Exception:
        raise ContentAuthorityError(_UNAVAILABLE) from None
