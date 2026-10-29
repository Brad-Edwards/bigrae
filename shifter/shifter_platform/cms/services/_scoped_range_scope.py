"""CMS-owned principal scope moves fence source, destination and all projections."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cms.services._range_workspace_admin import RangeRebindResult, RangeScopeAuditContext

from uuid import UUID

from django.db import transaction

from cms.exceptions import RangeScopeAdminError
from cms.models import RangeInstance, Request
from engine.services import commit_range_scope_move, prepare_range_scope_move
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext
from shared.principal_port import resolve_principal_uuid
from shared.range_workspace_aggregate import range_in_domain_aggregate
from workspaces.services import (
    WorkspaceQuotaAuditContext,
    WorkspaceQuotaRejected,
    assert_scope_principal_member,
    hierarchy_target_scope,
    lock_resource_scope_bindings,
    move_workspace_range_reservation,
    record_workspace_quota_rejection,
    resolve_resource_scope,
)

from ._content_authority import require_content_scope
from ._unified_authorization import range_authorization_binding


def rebind_scoped_range_workspace(
    credential: CredentialContext, *, request_id: UUID, target_workspace_uuid: UUID | str, audit: RangeScopeAuditContext
) -> RangeRebindResult:
    """Authorize both parents before atomically moving runtime and quota custody."""
    from ._range_workspace_admin import RangeRebindResult

    try:
        instance = RangeInstance.objects.select_related("request").get(request__request_id=request_id)
        _, source = range_authorization_binding(instance.uuid)
        if range_in_domain_aggregate(request_id, instance.pk):
            raise ValueError("Range belongs to an immutable aggregate")
        require_content_scope(credential, source, "manage_range_scope")
        target = hierarchy_target_scope("workspace", UUID(str(target_workspace_uuid)))
        current = require_content_scope(credential, target, "manage_range_scope")
        quota_audit = WorkspaceQuotaAuditContext(
            **principal_actor_fields(current.principal),
            request_id=audit.request_id,
            source_ip=audit.source_ip,
            user_agent=audit.user_agent,
        )
        owner = resolve_principal_uuid(instance.owner_principal_uuid)
        if source != target:
            assert_scope_principal_member(target, owner)
        prepared = prepare_range_scope_move(current, request_id, target)
        with transaction.atomic():
            lock_resource_scope_bindings((source, prepared.source.scope, target))
            row = RangeInstance.objects.select_for_update().get(pk=instance.pk)
            request = Request.objects.select_for_update().get(pk=instance.request_id)
            if row.status != instance.status or range_authorization_binding(row.uuid)[1] != source:
                raise ValueError("Range scope changed")
            if range_in_domain_aggregate(request_id, row.pk):
                raise ValueError("Range aggregate changed")
            destination = resolve_resource_scope(target)
            if source != target:
                # Launch admission takes the account mutex before quota workspaces.
                assert_scope_principal_member(target, owner, for_update=True)
                if row.status != "failed":
                    move_workspace_range_reservation(
                        instance.workspace_id, destination.workspace_id, request_id, quota_audit
                    )
            changed = commit_range_scope_move(prepared)
            if not changed:
                return RangeRebindResult(False)
            for projection in (row, request):
                projection.scope_kind = destination.kind
                projection.account_id = destination.account_id
                projection.organization_id = destination.organization_id
                projection.workspace_id = destination.workspace_id
                projection.save(update_fields=["scope_kind", "account_id", "organization_id", "workspace_id"])
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.RANGE,
                    entity_id=row.pk,
                    entity_ref=str(row.uuid),
                    action=AuditAction.UPDATE,
                    **principal_actor_fields(current.principal),
                    previous_state={"workspace_uuid": str(source.workspace_uuid)},
                    new_state={"workspace_uuid": str(target.workspace_uuid)},
                    request_id=audit.request_id,
                    source_ip=audit.source_ip,
                    user_agent=audit.user_agent,
                    context="range workspace scope reassignment",
                ),
                strict=True,
            )
            return RangeRebindResult(True)
    except WorkspaceQuotaRejected as rejected:
        record_workspace_quota_rejection(
            rejected.workspace_id, rejected.verdict, quota_audit, correlation_key=str(request_id)
        )
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.TARGET_DENIED, "Range scope move unavailable") from None
    except Exception:
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope move unavailable") from None
