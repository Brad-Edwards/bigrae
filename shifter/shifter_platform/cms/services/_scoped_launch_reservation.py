"""Atomic native reservation, exact replay and quota settlement."""

from __future__ import annotations

from uuid import UUID

from django.db import IntegrityError, transaction

from cms.exceptions import CMSError, WorkspaceLaunchQuotaExceeded
from cms.models import RaesPackageSource, RangeInstance, Request
from shared.credentials import CredentialContext, resolve_local_credential
from shared.enums import RangeSource, ResourceStatus
from shared.identity_scope import PrincipalRef
from shared.operation_authority import credential_reference_fields
from shared.range_instantiation_policy import assert_range_backend_egress_supported
from workspaces.services import (
    ResolvedResourceScope,
    WorkspaceQuotaAuditContext,
    WorkspaceQuotaRejected,
    record_workspace_quota_rejection,
    reserve_workspace_concurrent_range,
    resolve_range_scope_binding,
)

from ._range_lease import build_resolved_mission_control_range_lease
from ._range_workspace import resolve_effective_egress_mode_locked
from ._scoped_launch_source import source_launch_binding
from ._scoped_launch_types import PreparedScopedLaunch


def reserve_scoped_launch(launch: PreparedScopedLaunch) -> tuple[RangeInstance, str]:
    """Atomically reserve the launch intent, owner slot and tenant lease."""
    from shared.audit import principal_actor_fields

    from ._range_launch_common import _ACTIVE_RANGE_MESSAGE, _LAUNCH_QUOTA_MESSAGE

    quota_audit = WorkspaceQuotaAuditContext(**principal_actor_fields(launch.current.principal))
    resolved = _resolve_scope(launch)
    if launch.effective_source is RangeSource.MISSION_CONTROL and (
        RangeInstance.objects.filter(owner_principal_uuid=launch.owner.uuid, range_source=launch.effective_source.value)
        .exclude(status=ResourceStatus.DESTROYING.value)
        .exists()
    ):
        raise CMSError(_ACTIVE_RANGE_MESSAGE)
    try:
        with transaction.atomic():
            _fence_reservation(launch, resolved)
            egress = _reserve_quota(launch, resolved, quota_audit)
            existing = _find_reservation(launch, resolved)
            return (existing or _create_reservation(launch, resolved)), egress
    except WorkspaceQuotaRejected as rejected:
        record_workspace_quota_rejection(
            rejected.workspace_id, rejected.verdict, quota_audit, correlation_key=str(launch.request_id)
        )
        raise WorkspaceLaunchQuotaExceeded(_LAUNCH_QUOTA_MESSAGE) from None
    except IntegrityError as exc:
        return _recover_reservation(launch, resolved, exc)


def _resolve_scope(launch: PreparedScopedLaunch, *, for_update: bool = False) -> ResolvedResourceScope:
    """Event authority uses the aggregate scope; ordinary launches also require membership."""
    if launch.options.event.authority_target is not None:
        return resolve_range_scope_binding(launch.scope)
    return resolve_range_scope_binding(launch.scope, member=launch.current.principal, for_update=for_update)


def _reserve_quota(
    launch: PreparedScopedLaunch, resolved: ResolvedResourceScope, audit: WorkspaceQuotaAuditContext
) -> str:
    """Reserve the workspace draw and pin egress while holding the ancestry lock."""
    egress = "none"
    if resolved.workspace_id is not None:
        reserve_workspace_concurrent_range(resolved.workspace_id, launch.request_id, audit)
        egress = resolve_effective_egress_mode_locked(resolved.workspace_id)
    assert_range_backend_egress_supported(launch.backend.backend if launch.backend else None, egress)
    return egress


def _find_reservation(launch: PreparedScopedLaunch, resolved: ResolvedResourceScope) -> RangeInstance | None:
    """A request without a retained event intent cannot reuse another reservation."""
    if not launch.intent_digest:
        return None
    return _existing_reservation(launch.request_id, launch.intent_digest, launch.current, resolved, launch.owner)


def _fence_reservation(launch: PreparedScopedLaunch, resolved: ResolvedResourceScope) -> None:
    """Lock the event intent first, then require current proof, ancestry and source."""
    if launch.intent_digest:
        from shared.range_workspace_aggregate import fence_range_event_launch

        if (
            launch.options.event.authority_target is None
            or launch.options.event.authority_target.type != "event"
            or launch.options.event.authority_target.uuid is None
        ):
            raise CMSError("Range launch intent unavailable")
        fence_range_event_launch(launch.request_id, launch.options.event.authority_target.uuid)
    if resolve_local_credential(launch.current) != launch.current:
        raise CMSError("Range authority changed")
    locked = _resolve_scope(launch, for_update=True)
    if (
        locked != resolved
        or not RaesPackageSource.objects.filter(pk=launch.source.pk, **source_launch_binding(launch.source)).exists()
    ):
        raise CMSError("Range launch changed")


def _create_reservation(launch: PreparedScopedLaunch, resolved: ResolvedResourceScope) -> RangeInstance:
    """Write one native request and its CMS intent after every local fence succeeds."""
    if launch.effective_source is RangeSource.CTF:
        from ._range_lease import build_range_lease

        lease = build_range_lease(
            launch.effective_source, enforced_deadline=launch.options.event.remote_access_teardown_at
        )
    else:
        lease = build_resolved_mission_control_range_lease(launch.current.principal, for_update=True)
    fields = {
        "scope_kind": resolved.kind,
        "account_id": resolved.account_id,
        "organization_id": resolved.organization_id,
        "workspace_id": resolved.workspace_id,
        "owner_principal_uuid": launch.owner.uuid,
    }
    request = Request.objects.create(
        request_id=launch.request_id,
        request_type="range",
        user_id=launch.user_id,
        **credential_reference_fields(launch.current),
        **fields,
    )
    instance = RangeInstance.objects.create(
        request=request,
        scenario_id=launch.source.scenario_id,
        user_id=launch.user_id,
        range_source=launch.effective_source.value,
        status="provisioning",
        range_spec=None,
        model_sources=launch.options.model_sources or {"aliases": []},
        model_launch_scope=launch.options.event.model_launch_scope.model_dump(mode="json")
        if launch.options.event.model_launch_scope
        else None,
        model_package_digest=launch.source.package_digest,
        launch_intent_digest=launch.intent_digest,
        event_uuid=launch.options.event.authority_target.uuid
        if launch.options.event.authority_target is not None
        else None,
        expires_at=lease.expires_at,
        maximum_expires_at=lease.maximum_expires_at,
        extension_days=lease.extension_days,
        lease_initial_days=lease.initial_days,
        lease_maximum_days=lease.maximum_days,
        lease_policy_source=lease.policy_source,
        lease_policy_tenant_revision=lease.tenant_revision,
        lease_policy_group_revisions=[
            {"group_id": group_id, "revision": revision} for group_id, revision in lease.group_revisions
        ],
        **fields,
    )
    return instance


def _recover_reservation(
    launch: PreparedScopedLaunch, resolved: ResolvedResourceScope, exc: IntegrityError
) -> tuple[RangeInstance, str]:
    """Reuse only the exact concurrent winner or report the native owner-slot conflict."""
    from ._range_launch_common import _ACTIVE_RANGE_MESSAGE

    if launch.intent_digest:
        existing = _existing_reservation(
            launch.request_id, launch.intent_digest, launch.current, resolved, launch.owner
        )
        if existing is not None:
            from ._range_workspace import resolve_effective_egress_mode

            return existing, (
                resolve_effective_egress_mode(resolved.workspace_id) if resolved.workspace_id is not None else "none"
            )
    if getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None) in {
        "uq_range_active_principal_source",
        "uq_rangeinstance_active_per_user_source",
    }:
        raise CMSError(_ACTIVE_RANGE_MESSAGE) from None
    raise exc


def _existing_reservation(
    request_id: UUID, digest: str, credential: CredentialContext, resolved: ResolvedResourceScope, owner: PrincipalRef
) -> RangeInstance | None:
    """Only an identical live reservation can reuse a server-reserved UUID."""
    row = RangeInstance.all_objects.select_related("request").filter(request__request_id=request_id).first()
    if row is None:
        return None
    if (
        row.deleted_at is not None
        or row.launch_intent_digest != digest
        or (row.scope_kind, row.account_id, row.organization_id, row.workspace_id, row.owner_principal_uuid)
        != (resolved.kind, resolved.account_id, resolved.organization_id, resolved.workspace_id, owner.uuid)
        or any(getattr(row.request, key) != value for key, value in credential_reference_fields(credential).items())
    ):
        raise CMSError("Range launch intent conflict")
    return row
