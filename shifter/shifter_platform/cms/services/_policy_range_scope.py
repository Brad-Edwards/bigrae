"""Prepared source/destination scope moves reuse locks, quotas and Engine CAS."""

from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet

from cms.exceptions import RangeScopeAdminError
from cms.models import RangeInstance
from engine.services import (
    RangeProjectionIntegrityError,
    RangeWorkspaceRebindOutcome,
    rebind_range_workspace_by_request,
)
from shared.audit import AuditAction, AuditEntityType, AuditEvent, RequestAudit, audit_log
from shared.authorization import AuthorizationProvider
from shared.credentials import CredentialContext
from shared.principal_port import principal_identity_for_user
from shared.range_workspace_aggregate import range_in_domain_aggregate
from workspaces.services import (
    WorkspaceAuthorizationError,
    WorkspaceQuotaAuditContext,
    WorkspaceQuotaError,
    WorkspaceQuotaRejected,
    authorize_policy_range_rebind,
    authorize_policy_range_scope_listing,
    lock_policy_range_rebind,
    move_workspace_concurrent_range_reservation,
    record_workspace_quota_rejection,
    resolve_resource_scope,
)

from ._range_workspace_admin import RangeRebindResult, _resolve_locked_range


def policy_list_range_scope_bindings(
    actor: CredentialContext, provider: AuthorizationProvider, *, workspace_uuid: UUID
) -> QuerySet[RangeInstance]:
    """Authorize the owning workspace before returning a countable projection."""
    try:
        workspace_id = authorize_policy_range_scope_listing(actor, provider, workspace_uuid)
    except (ValueError, WorkspaceAuthorizationError) as exc:
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.NOT_FOUND, "Workspace unavailable") from exc
    return (
        RangeInstance.objects.filter(workspace_id=workspace_id).select_related("request").order_by("-created_at", "-id")
    )


def policy_rebind_range_workspace(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    *,
    request_id: UUID,
    target_workspace_uuid: UUID,
    audit: RequestAudit | None = None,
) -> RangeRebindResult:
    """Authorize both scopes, then move every ancestry and reservation atomically."""
    hint = RangeInstance.objects.select_related("request__user").filter(request__request_id=request_id).first()
    if hint is None or hint.request is None or hint.workspace_id is None:
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.NOT_FOUND, "Range scope unavailable")
    try:
        proof = authorize_policy_range_rebind(
            actor, provider, hint.workspace_id, target_workspace_uuid, principal_identity_for_user(hint.request.user)
        )
    except WorkspaceAuthorizationError as exc:
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.TARGET_DENIED, "Range scope unavailable") from exc
    request = audit or RequestAudit()
    quota_audit = WorkspaceQuotaAuditContext(
        actor_type="principal",
        actor_id=None,
        actor_principal_uuid=actor.principal.uuid,
        source_ip=request.source_ip,
        user_agent=request.user_agent,
        request_id=request.request_id,
    )
    try:
        with transaction.atomic():
            source_id, destination_id = lock_policy_range_rebind(proof)
            instance, intent, locked_source = _resolve_locked_range(request_id)
            if locked_source != source_id or instance.user_id != hint.user_id or intent.user_id != hint.request.user_id:
                raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope changed")
            if range_in_domain_aggregate(request_id, instance.pk):
                raise RangeScopeAdminError(RangeScopeAdminError.Kind.NOT_REASSIGNABLE, "Range scope cannot be moved")
            expected = (instance.scope_kind, instance.account_id, instance.organization_id)
            if expected != (intent.scope_kind, intent.account_id, intent.organization_id):
                raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope projections disagree")
            if (instance.scope_kind, instance.account_id, instance.organization_id, source_id) != (
                "account",
                proof.source_account_id,
                proof.source_organization_id,
                proof.source_workspace_id,
            ):
                raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope projections disagree")
            target = resolve_resource_scope(proof.target_scope)
            ancestry = (target.kind, target.account_id, target.organization_id)
            move_workspace_concurrent_range_reservation(source_id, destination_id, request_id, quota_audit)
            outcome = rebind_range_workspace_by_request(
                request_id,
                expected_workspace_id=source_id,
                new_workspace_id=destination_id,
                expected_ancestry=expected,
                new_ancestry=ancestry,
            )
            if (
                outcome is RangeWorkspaceRebindOutcome.NOT_FOUND
                or outcome is RangeWorkspaceRebindOutcome.SOURCE_MISMATCH
            ):
                raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope projections disagree")
            if source_id == destination_id:
                return RangeRebindResult(changed=False)
            if outcome is RangeWorkspaceRebindOutcome.UNCHANGED:
                raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope projections disagree")
            for row in (instance, intent):
                row.scope_kind, row.account_id, row.organization_id = ancestry
                row.workspace_id = destination_id
                row.save(update_fields=["scope_kind", "account_id", "organization_id", "workspace_id"])
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.RANGE,
                    entity_id=instance.pk,
                    action=AuditAction.UPDATE,
                    actor_type="principal",
                    actor_principal_uuid=actor.principal.uuid,
                    previous_state={"workspace_id": source_id},
                    new_state={"workspace_id": destination_id},
                    context="range workspace scope reassignment",
                    source_ip=request.source_ip,
                    user_agent=request.user_agent[:500],
                    request_id=request.request_id[:64],
                ),
                strict=True,
            )
            return RangeRebindResult(changed=True)
    except WorkspaceQuotaRejected as exc:
        record_workspace_quota_rejection(exc.workspace_id, exc.verdict, quota_audit)
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.TARGET_DENIED, "Destination quota reached") from exc
    except (WorkspaceQuotaError, WorkspaceAuthorizationError, RangeProjectionIntegrityError) as exc:
        raise RangeScopeAdminError(RangeScopeAdminError.Kind.CONFLICT, "Range scope unavailable") from exc
