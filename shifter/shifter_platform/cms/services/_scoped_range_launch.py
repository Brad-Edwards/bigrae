"""Principal admission composes the incumbent RAES reservation and dispatch seams."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import IntegrityError, transaction

from cms.exceptions import CMSError, WorkspaceLaunchQuotaExceeded
from cms.models import RaesPackageSource, RangeInstance, Request
from shared.credentials import resolve_local_credential
from shared.enums import RangeSource, ResourceStatus
from shared.operation_authority import credential_reference_fields
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

    from shared.authorization import TargetRef
    from shared.credentials import CredentialContext
    from shared.enums import RangeSource
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.model_access import OwnedReference
    from shared.model_access.reservation import ModelLaunchScope
    from shared.range_instantiation_policy import BackendAdmission
    from shared.schemas.range import RangeContext
    from workspaces.services import ResolvedResourceScope


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
    credential: CredentialContext,
    scope: ResourceScope,
    source: RaesPackageSource,
    *,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
) -> CredentialContext:
    """Repeat original authority and source eligibility before irreversible effects."""
    from cms.scenarios.registry import check_scenario_access

    if (authority_action is None) != (authority_target is None):
        raise CMSError("Range authority unavailable")
    if authority_action is None:
        current = require_content_scope(credential, scope, "launch_range")
        entry = check_scenario_access(source.scenario_id, current, scope=scope)
    else:
        from shared.authorization import AuthorizationRequest
        from shared.authorization.policy import require_authorization
        from shared.credentials import resolve_current_credential
        from workspaces.services import resolve_resource_scope

        current = resolve_current_credential(credential)
        resolve_resource_scope(scope)
        require_authorization(
            AuthorizationRequest(current.principal, authority_action, authority_target, scope, current.ceiling)
        )
        # The CTF owner authorized this immutable source when configuring the
        # event. A range operator may be delegated only ``event.manage_ranges``
        # and must not need an unrelated account content grant at dispatch.
        # Revalidate the source's current custody and publication facts here;
        # the owning CTF service supplies the event target and exact scope.
        from ._content_authority import content_scope

        source_scope = content_scope(source)
        shared = source_scope.kind == "installation" and source.is_operator_published
        if not shared and source_scope != scope:
            raise CMSError("Range source unavailable")
        from cms.models import ScenarioMetadata
        from cms.scenarios.registry import _raes_launchable, _raes_source_to_dict

        metadata_row = ScenarioMetadata.objects.filter(scenario_id=source.scenario_id).first()
        metadata = (
            None if metadata_row is None else {"enabled": metadata_row.enabled, "staff_only": metadata_row.staff_only}
        )
        entry = _raes_source_to_dict(source, metadata=metadata, launchable=_raes_launchable(source))
    if not entry.get("launchable") or not entry.get("enabled", True):
        raise CMSError("Range source unavailable")
    if not RaesPackageSource.objects.filter(pk=source.pk, **source_launch_binding(source)).exists():
        raise CMSError("Range source changed")
    if scope.kind == "installation":
        if current.principal.kind != "service":
            raise CMSError("Range source unavailable")
        resolve_range_scope_binding(scope)
    elif authority_target is None:
        resolve_range_scope_binding(scope, member=current.principal)
    else:
        resolve_range_scope_binding(scope)
    return current


def _validate_scoped_launch_inputs(
    scope: ResourceScope | None,
    scenario: str,
    range_source: RangeSource | None,
    workspace_uuid: UUID | str | None,
    owner_principal: PrincipalRef | None,
    authority_action: str | None,
    authority_target: TargetRef | None,
) -> tuple[ResourceScope, bool, RangeSource]:
    """Validate the closed launch shape before reading mutable state."""
    if scope is None or scope.kind != "account":
        raise CMSError("Range launch scope unavailable")
    if workspace_uuid is not None and str(workspace_uuid) != str(scope.workspace_uuid):
        raise CMSError("Range launch scope unavailable")
    if (
        range_source not in {None, RangeSource.MISSION_CONTROL, RangeSource.CTF}
        or not isinstance(scenario, str)
        or not scenario
    ):
        raise CMSError("Range launch inputs unavailable")
    is_event_launch = range_source is RangeSource.CTF
    if is_event_launch != bool(owner_principal and authority_action and authority_target):
        raise CMSError("Range launch inputs unavailable")
    return scope, is_event_launch, range_source or RangeSource.MISSION_CONTROL


def _validate_selected_model_sources(
    current: CredentialContext,
    workspace_id: int | None,
    model_sources: dict | None,
) -> None:
    """Validate optional tenant-funded model sources against the exact workspace."""
    if not model_sources:
        return
    if workspace_id is None:
        raise CMSError("Model sources unavailable")
    from django.conf import settings

    from ._model_source_selection import resolve_launch_sources

    resolve_launch_sources(
        current,
        workspace_id,
        model_sources,
        catalog=getattr(settings, "MODEL_ACCESS_CATALOG", None),
    )


def _initial_launch_egress(
    current: CredentialContext,
    resolved: ResolvedResourceScope,
    scenario: str,
    source: RaesPackageSource,
    model_admission_subject: OwnedReference | None,
) -> str:
    """Resolve initial egress and admit the scenario's model requirements."""
    from ._model_admission import assert_launch_model_access

    egress = "none"
    if resolved.workspace_id is not None:
        from ._range_workspace import resolve_effective_egress_mode

        egress = resolve_effective_egress_mode(resolved.workspace_id)
    assert_launch_model_access(
        user=current,
        scenario_id=scenario,
        egress_mode=egress,
        subject=model_admission_subject,
        package_digest=source.package_digest,
    )
    return egress


def _warm_claim(
    *,
    is_event_launch: bool,
    current: CredentialContext,
    scope: ResourceScope,
    source: RaesPackageSource,
    scenario: str,
    backend: BackendAdmission | None,
    effective_source: RangeSource,
    egress: str,
    request_id: UUID,
    model_sources: dict | None,
    user_id: int | None,
) -> RangeContext | None:
    """Attempt the Mission Control warm path and project its accepted result."""
    from shared.schemas.range import RangeContext

    from ._warm_pool_claim import WarmClaimRequest, attempt_warm_claim

    if is_event_launch:
        return None
    claimed = attempt_warm_claim(
        WarmClaimRequest(
            credential=current,
            scope=scope,
            scenario=scenario,
            package_digest=source.package_digest,
            lock_digest=source.lock_digest,
            backend=backend.backend if backend else "",
            instantiation_purpose=InstantiationPurpose.LIVE_FIRE,
            range_source=effective_source,
            egress_mode=egress,
            request_id=request_id,
            model_sources=model_sources,
        )
    )
    if claimed is None:
        return None
    return RangeContext(
        request_id=claimed,
        range_id=None,
        scenario_id=scenario,
        user_id=user_id,
        owner_principal_uuid=current.principal.uuid,
        status=ResourceStatus.PROVISIONING,
        instances=[],
        agent_name="",
    )


def create_scoped_range(
    credential: CredentialContext,
    scenario: str,
    *,
    scope: ResourceScope | None,
    range_source: RangeSource | None,
    workspace_uuid: UUID | str | None,
    model_sources: dict | None = None,
    owner_principal: PrincipalRef | None = None,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
    remote_access_teardown_at=None,
    model_admission_subject: OwnedReference | None = None,
    model_launch_scope: ModelLaunchScope | None = None,
) -> RangeContext:
    """Explicit customer scope; no contact user or fabricated compatibility workspace."""
    from management.services import human_principal_user_id
    from shared.schemas.range import RangeContext

    from ._raes_range_create import _assert_raes_adapter_supports

    try:
        scope, is_event_launch, effective_source = _validate_scoped_launch_inputs(
            scope,
            scenario,
            range_source,
            workspace_uuid,
            owner_principal,
            authority_action,
            authority_target,
        )
        # Reject a disallowed runtime before resolving or staging any content.
        # This is a deployment-wide policy decision and performs no mutation.
        backend = assert_backend_admitted(InstantiationPurpose.LIVE_FIRE, effective_source)
        _assert_raes_adapter_supports(backend)
        current = credential
        source = RaesPackageSource.objects.get(scenario_id=scenario)
        current = revalidate_scoped_launch(
            current,
            scope,
            source,
            authority_action=authority_action,
            authority_target=authority_target,
        )
        resolved = (
            resolve_range_scope_binding(scope)
            if is_event_launch
            else resolve_range_scope_binding(scope, member=current.principal)
        )
        _validate_selected_model_sources(current, resolved.workspace_id, model_sources)
        range_owner = owner_principal or current.principal
        user_id = human_principal_user_id(range_owner) if range_owner.kind == "human" else None
        request_id = uuid4()
        from ._range_launch_common import _ACTIVE_RANGE_MESSAGE

        if effective_source is RangeSource.MISSION_CONTROL and (
            RangeInstance.objects.filter(
                owner_principal_uuid=range_owner.uuid,
                range_source=effective_source.value,
            )
            .exclude(status=ResourceStatus.DESTROYING.value)
            .exists()
        ):
            raise CMSError(_ACTIVE_RANGE_MESSAGE)
        egress = _initial_launch_egress(current, resolved, scenario, source, model_admission_subject)
        claimed = _warm_claim(
            is_event_launch=is_event_launch,
            current=current,
            scope=scope,
            source=source,
            scenario=scenario,
            backend=backend,
            effective_source=effective_source,
            egress=egress,
            request_id=request_id,
            model_sources=model_sources,
            user_id=user_id,
        )
        if claimed is not None:
            return claimed
        instance, egress = _reserve(
            current,
            scope,
            source,
            request_id,
            user_id,
            backend,
            model_sources,
            owner_principal=range_owner,
            range_source=effective_source,
            remote_access_teardown_at=remote_access_teardown_at,
            model_launch_scope=model_launch_scope,
            authority_target=authority_target,
        )
        try:
            from ._model_admission import assert_launch_model_access
            from ._raes_dispatch import RaesDispatchOptions, dispatch_object_raes_package, dispatch_repo_raes_package

            assert_launch_model_access(
                user=current,
                scenario_id=scenario,
                egress_mode=egress,
                subject=model_admission_subject,
                package_digest=source.package_digest,
            )
            revalidate_scoped_launch(
                current,
                scope,
                source,
                authority_action=authority_action,
                authority_target=authority_target,
            )
            dispatch = dispatch_object_raes_package if source.source_kind == "object" else dispatch_repo_raes_package
            dispatch(
                request_id,
                current,
                source,
                RaesDispatchOptions(
                    backend,
                    instance.workspace_id,
                    egress,
                    principal_scope=scope,
                    owner_principal=range_owner if is_event_launch else None,
                    authority_action=authority_action,
                    authority_target=authority_target,
                ),
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
            owner_principal_uuid=range_owner.uuid,
            status=ResourceStatus.PROVISIONING,
            instances=[],
            agent_name="",
        )
    except WorkspaceLaunchQuotaExceeded:
        raise
    except CMSError as exc:
        from ._range_launch_common import _ACTIVE_RANGE_MESSAGE

        if str(exc) == _ACTIVE_RANGE_MESSAGE:
            raise
        # Keep the outward message non-enumerating while preserving only the
        # closed policy classification callers need to decide whether retrying
        # is safe. Arbitrary upstream details are deliberately discarded.
        from shared.range_instantiation_policy import (
            POLICY_DENIAL_CODE,
            PREREQUISITE_DENIAL_CODE,
            UNSUPPORTED_CAPABILITY_CODE,
        )

        code = exc.details.get("code") if isinstance(exc.details, dict) else None
        allowed_codes = {POLICY_DENIAL_CODE, PREREQUISITE_DENIAL_CODE, UNSUPPORTED_CAPABILITY_CODE}
        details = {"code": code} if code in allowed_codes else None
        raise CMSError("Range launch unavailable", details=details) from None
    except Exception:
        raise CMSError("Range launch unavailable") from None


def _reserve(
    current: CredentialContext,
    scope: ResourceScope,
    source: RaesPackageSource,
    request_id: UUID,
    user_id: int | None,
    backend: BackendAdmission | None,
    model_sources: dict | None,
    *,
    owner_principal: PrincipalRef,
    range_source: RangeSource,
    remote_access_teardown_at,
    model_launch_scope: ModelLaunchScope | None,
    authority_target: TargetRef | None,
) -> tuple[RangeInstance, str]:
    """Atomically reserve the launch intent, owner slot and tenant lease."""
    from shared.audit import principal_actor_fields

    from ._range_launch_common import _ACTIVE_RANGE_MESSAGE, _LAUNCH_QUOTA_MESSAGE

    quota_audit = WorkspaceQuotaAuditContext(**principal_actor_fields(current.principal))
    resolved = (
        resolve_range_scope_binding(scope)
        if authority_target is not None
        else resolve_range_scope_binding(scope, member=current.principal)
    )
    if range_source is RangeSource.MISSION_CONTROL and (
        RangeInstance.objects.filter(
            owner_principal_uuid=owner_principal.uuid,
            range_source=range_source.value,
        )
        .exclude(status=ResourceStatus.DESTROYING.value)
        .exists()
    ):
        raise CMSError(_ACTIVE_RANGE_MESSAGE)
    try:
        with transaction.atomic():
            if resolve_local_credential(current) != current:
                raise CMSError("Range authority changed")
            locked = (
                resolve_range_scope_binding(scope)
                if authority_target is not None
                else resolve_range_scope_binding(scope, member=current.principal, for_update=True)
            )
            if (
                locked != resolved
                or not RaesPackageSource.objects.filter(pk=source.pk, **source_launch_binding(source)).exists()
            ):
                raise CMSError("Range launch changed")
            if resolved.workspace_id is not None:
                reserve_workspace_concurrent_range(resolved.workspace_id, request_id, quota_audit)
                egress = resolve_effective_egress_mode_locked(resolved.workspace_id)
            else:
                egress = "none"
            assert_range_backend_egress_supported(backend.backend if backend else None, egress)
            if range_source is RangeSource.CTF:
                from ._range_lease import build_range_lease

                lease = build_range_lease(range_source, enforced_deadline=remote_access_teardown_at)
            else:
                lease = build_resolved_mission_control_range_lease(current.principal, for_update=True)
            fields = {
                "scope_kind": resolved.kind,
                "account_id": resolved.account_id,
                "organization_id": resolved.organization_id,
                "workspace_id": resolved.workspace_id,
                "owner_principal_uuid": owner_principal.uuid,
            }
            request = Request.objects.create(
                request_id=request_id,
                request_type="range",
                user_id=user_id,
                **credential_reference_fields(current),
                **fields,
            )
            instance = RangeInstance.objects.create(
                request=request,
                scenario_id=source.scenario_id,
                user_id=user_id,
                range_source=range_source.value,
                status="provisioning",
                range_spec=None,
                model_sources=model_sources or {"aliases": []},
                model_launch_scope=model_launch_scope.model_dump(mode="json") if model_launch_scope else None,
                model_package_digest=source.package_digest,
                event_uuid=authority_target.uuid if authority_target is not None else None,
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
