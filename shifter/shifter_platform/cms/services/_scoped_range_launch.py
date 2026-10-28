"""Principal admission composes the incumbent RAES reservation and dispatch seams."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import IntegrityError, transaction

from cms.exceptions import CMSError, WorkspaceLaunchQuotaExceeded
from cms.models import RaesPackageSource, RangeInstance, Request
from shared.credentials import resolve_local_credential
from shared.enums import RangeSource, ResourceStatus
from shared.range_instantiation_policy import InstantiationPurpose, assert_range_backend_egress_supported
from workspaces.services import (
    WorkspaceQuotaAuditContext,
    WorkspaceQuotaRejected,
    record_workspace_quota_rejection,
    release_workspace_concurrent_range,
    reserve_workspace_concurrent_range,
    resolve_range_scope_binding,
)

from ._content_authority import require_content_scope
from ._range_backend_admission import assert_backend_admitted
from ._range_lease import build_resolved_mission_control_range_lease
from ._range_workspace import resolve_effective_egress_mode_locked

if TYPE_CHECKING:
    from uuid import UUID

    from shared.credentials import CredentialContext
    from shared.enums import RangeSource
    from shared.identity_scope import ResourceScope
    from shared.range_instantiation_policy import BackendAdmission
    from shared.schemas.range import RangeContext


_SOURCE_FIELDS = (
    "scenario_id",
    "source_kind",
    "package_ref",
    "package_name",
    "package_digest",
    "lock_digest",
    "package_version",
    "scope_kind",
    "account_uuid",
    "organization_uuid",
    "workspace_uuid",
    "owner_principal_uuid",
    "scope_generation",
    "is_operator_published",
    "conformance_status",
)


def source_launch_binding(source: RaesPackageSource) -> dict[str, object]:
    """Immutable registered content/custody facts inspected before any staging."""
    return {name: getattr(source, name) for name in _SOURCE_FIELDS}


def revalidate_scoped_launch(
    credential: CredentialContext, scope: ResourceScope, source: RaesPackageSource
) -> CredentialContext:
    """Repeat original authority and source eligibility before irreversible effects."""
    from cms.scenarios.registry import check_scenario_access

    current = require_content_scope(credential, scope, "launch_range")
    entry = check_scenario_access(source.scenario_id, current, scope=scope)
    if not entry.get("launchable") or not entry.get("enabled", True):
        raise CMSError("Range source unavailable")
    if not RaesPackageSource.objects.filter(pk=source.pk, **source_launch_binding(source)).exists():
        raise CMSError("Range source changed")
    resolve_range_scope_binding(scope, member=current.principal)
    return current


def create_scoped_range(
    credential: CredentialContext,
    scenario: str,
    *,
    scope: ResourceScope | None,
    range_source: RangeSource | None,
    workspace_uuid: UUID | str | None,
) -> RangeContext:
    """Explicit customer scope; no contact user or fabricated compatibility workspace."""
    from management.services import human_principal_user_id
    from shared.schemas.range import RangeContext

    from ._raes_range_create import _assert_raes_adapter_supports

    try:
        if scope is None or scope.kind != "account" or workspace_uuid is not None:
            raise CMSError("Range launch scope unavailable")
        if range_source not in {None, RangeSource.MISSION_CONTROL} or not isinstance(scenario, str) or not scenario:
            raise CMSError("Range launch inputs unavailable")
        current = require_content_scope(credential, scope, "launch_range")
        # Catalog admission filters private and explicitly published sources before staging.
        from cms.scenarios.registry import check_scenario_access

        check_scenario_access(scenario, current, scope=scope)
        source = RaesPackageSource.objects.get(scenario_id=scenario)
        current = revalidate_scoped_launch(current, scope, source)
        backend = assert_backend_admitted(InstantiationPurpose.LIVE_FIRE, RangeSource.MISSION_CONTROL)
        _assert_raes_adapter_supports(backend)
        user_id = human_principal_user_id(current.principal) if current.principal.kind == "human" else None
        request_id = uuid4()
        instance, egress = _reserve(current, scope, source, request_id, user_id, backend)
        try:
            from ._model_admission import assert_launch_model_access
            from ._raes_dispatch import RaesDispatchOptions, dispatch_object_raes_package, dispatch_repo_raes_package

            assert_launch_model_access(
                user=current, scenario_id=scenario, egress_mode=egress, package_digest=source.package_digest
            )
            revalidate_scoped_launch(current, scope, source)
            dispatch = dispatch_object_raes_package if source.source_kind == "object" else dispatch_repo_raes_package
            dispatch(
                request_id,
                current,
                source,
                RaesDispatchOptions(backend, instance.workspace_id, egress, principal_scope=scope),
            )
        except Exception:
            with transaction.atomic():
                instance.status = "failed"
                instance.save(update_fields=["status"])
                if instance.workspace_id is not None:
                    release_workspace_concurrent_range(instance.workspace_id, request_id)
            raise
        return RangeContext(
            request_id=request_id,
            range_id=None,
            scenario_id=scenario,
            user_id=user_id,
            owner_principal_uuid=current.principal.uuid,
            status=ResourceStatus.PROVISIONING,
            instances=[],
            agent_name="",
        )
    except WorkspaceLaunchQuotaExceeded:
        raise
    except Exception:
        raise CMSError("Range launch unavailable") from None


def _reserve(
    current: CredentialContext,
    scope: ResourceScope,
    source: RaesPackageSource,
    request_id: UUID,
    user_id: int | None,
    backend: BackendAdmission | None,
) -> tuple[RangeInstance, str]:
    """Atomically reserve the launch intent, owner slot and tenant lease."""
    from shared.audit import principal_actor_fields

    from ._range_launch_common import _ACTIVE_RANGE_MESSAGE, _LAUNCH_QUOTA_MESSAGE

    quota_audit = WorkspaceQuotaAuditContext(**principal_actor_fields(current.principal))
    resolved = resolve_range_scope_binding(scope, member=current.principal)
    try:
        with transaction.atomic():
            if resolve_local_credential(current) != current:
                raise CMSError("Range authority changed")
            locked = resolve_range_scope_binding(scope, member=current.principal, for_update=True)
            if (
                locked != resolved
                or not RaesPackageSource.objects.filter(pk=source.pk, **source_launch_binding(source)).exists()
            ):
                raise CMSError("Range launch changed")
            if resolved.workspace_id is not None:
                reserve_workspace_concurrent_range(resolved.workspace_id, request_id, quota_audit)
                egress = resolve_effective_egress_mode_locked(resolved.workspace_id)
            else:
                egress = "status-quo"
            assert_range_backend_egress_supported(backend.backend if backend else None, egress)
            lease = build_resolved_mission_control_range_lease(current.principal, for_update=True)
            fields = {
                "scope_kind": resolved.kind,
                "account_id": resolved.account_id,
                "organization_id": resolved.organization_id,
                "workspace_id": resolved.workspace_id,
                "owner_principal_uuid": current.principal.uuid,
            }
            request = Request.objects.create(request_id=request_id, request_type="range", user_id=user_id, **fields)
            instance = RangeInstance.objects.create(
                request=request,
                scenario_id=source.scenario_id,
                user_id=user_id,
                range_source="mission_control",
                status="provisioning",
                range_spec=None,
                model_package_digest=source.package_digest,
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
            return instance, egress
    except WorkspaceQuotaRejected as rejected:
        record_workspace_quota_rejection(
            rejected.workspace_id, rejected.verdict, quota_audit, correlation_key=str(request_id)
        )
        raise WorkspaceLaunchQuotaExceeded(_LAUNCH_QUOTA_MESSAGE) from None
    except IntegrityError as exc:
        if getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None) in {
            "uq_range_active_principal_source",
            "uq_rangeinstance_active_per_user_source",
        }:
            raise CMSError(_ACTIVE_RANGE_MESSAGE) from None
        raise
