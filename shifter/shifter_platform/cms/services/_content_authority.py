"""CMS's SQL scope and principal-neutral content authority boundary."""

from uuid import UUID

from django.db import transaction

from cms.exceptions import CMSError
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import AuthorizationProviderBindingError, AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential
from shared.identity_scope import ResourceScope
from workspaces.services import resolve_resource_scope

_UNAVAILABLE = "Content unavailable"


def content_scope(row) -> ResourceScope:
    """Reject absent legacy ownership, invalid scope and stale directory facts."""
    try:
        owner = row.owner_principal_uuid
        if not isinstance(owner, UUID) or not owner.int:
            raise ValueError(_UNAVAILABLE)
        scope = ResourceScope(row.scope_kind, row.account_uuid, row.organization_uuid, row.workspace_uuid)
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
    scope = content_scope(source)
    if scope.kind != "installation":
        raise CMSError(_UNAVAILABLE)
    current = require_content_scope(credential, scope, "publish_content")
    with transaction.atomic():
        row = RaesPackageSource.objects.select_for_update().filter(pk=source.pk).first()
        if row is None or content_scope(row) != scope or row.scope_generation != source.scope_generation:
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


def require_content_scope(credential: CredentialContext, scope: ResourceScope, operation: str) -> CredentialContext:
    """Authorize the real parent before a query, mutation or storage effect."""
    try:
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
        raise CMSError("Content authority unavailable", details={"code": "authority-unavailable"}) from None
    except Exception:
        raise CMSError(_UNAVAILABLE) from None
