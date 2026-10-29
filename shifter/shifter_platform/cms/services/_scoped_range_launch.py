"""Native range launch composes source admission, reservation and dispatch seams."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from django.db import transaction

from cms.exceptions import CMSError, WorkspaceLaunchQuotaExceeded
from cms.models import RaesPackageSource, RangeInstance
from shared.enums import RangeSource, ResourceStatus
from shared.operation_authority import credential_reference_fields
from shared.range_instantiation_policy import InstantiationPurpose
from workspaces.services import release_workspace_concurrent_range, resolve_range_scope_binding

from ._range_backend_admission import assert_backend_admitted
from ._scoped_launch_reservation import reserve_scoped_launch
from ._scoped_launch_source import (
    revalidate_scoped_launch as revalidate_scoped_launch,
)
from ._scoped_launch_source import (
    source_launch_binding as source_launch_binding,
)
from ._scoped_launch_source import (
    source_launch_metadata as source_launch_metadata,
)
from ._scoped_launch_types import PreparedScopedLaunch, ScopedRangeLaunchOptions

if TYPE_CHECKING:
    from shared.authorization import TargetRef
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.model_access import OwnedReference
    from shared.schemas.range import RangeContext
    from workspaces.services import ResolvedResourceScope

_LAUNCH_INPUTS_UNAVAILABLE = "Range launch inputs unavailable"


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
    scope = _validate_launch_placement(scope, workspace_uuid)
    if (
        range_source not in {None, RangeSource.MISSION_CONTROL, RangeSource.CTF}
        or not isinstance(scenario, str)
        or not scenario
    ):
        raise CMSError(_LAUNCH_INPUTS_UNAVAILABLE)
    is_event_launch = range_source is RangeSource.CTF
    _validate_event_binding(is_event_launch, owner_principal, authority_action, authority_target)
    return scope, is_event_launch, range_source or RangeSource.MISSION_CONTROL


def _validate_selected_model_sources(
    current: CredentialContext,
    workspace_id: int | None,
    model_sources: dict[str, Any] | None,
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


def _warm_claim(launch: PreparedScopedLaunch) -> RangeContext | None:
    """Attempt the Mission Control warm path and project its accepted result."""
    from shared.schemas.range import RangeContext

    from ._warm_pool_claim import WarmClaimRequest, attempt_warm_claim

    if launch.is_event:
        return None
    metadata = source_launch_metadata(launch.source.scenario_id)
    claimed = attempt_warm_claim(
        WarmClaimRequest(
            credential=launch.current,
            scope=launch.scope,
            scenario=launch.source.scenario_id,
            package_digest=launch.source.package_digest,
            lock_digest=launch.source.lock_digest,
            backend=launch.backend.backend if launch.backend else "",
            instantiation_purpose=InstantiationPurpose.LIVE_FIRE,
            range_source=launch.effective_source,
            egress_mode=launch.egress,
            request_id=launch.request_id,
            model_sources=launch.options.model_sources,
            source_binding=tuple(source_launch_binding(launch.source).items()),
            source_metadata=metadata,
        )
    )
    if claimed is None:
        return None
    return RangeContext(
        request_id=claimed,
        range_id=None,
        scenario_id=launch.source.scenario_id,
        user_id=launch.user_id,
        owner_principal_uuid=launch.current.principal.uuid,
        status=ResourceStatus.PROVISIONING,
        instances=[],
        agent_name="",
    )


def _prepare_launch(
    credential: CredentialContext, scenario: str, options: ScopedRangeLaunchOptions
) -> PreparedScopedLaunch:
    """Admit current native authority and freeze every reservation input before locks."""
    from dataclasses import replace

    from management.services import human_principal_user_id

    from ._raes_range_create import _assert_raes_adapter_supports
    from ._range_launch_common import _ACTIVE_RANGE_MESSAGE

    event = options.event
    scope, is_event, effective_source = _validate_scoped_launch_inputs(
        options.scope,
        scenario,
        options.range_source,
        options.workspace_uuid,
        event.owner_principal,
        event.authority_action,
        event.authority_target,
    )
    _validate_request_uuid(event.request_uuid, is_event)
    backend = assert_backend_admitted(InstantiationPurpose.LIVE_FIRE, effective_source)
    _assert_raes_adapter_supports(backend)
    source = RaesPackageSource.objects.get(scenario_id=scenario)
    current = revalidate_scoped_launch(
        credential,
        scope,
        source,
        authority_action=event.authority_action,
        authority_target=event.authority_target,
    )
    resolved = (
        resolve_range_scope_binding(scope) if is_event else resolve_range_scope_binding(scope, member=current.principal)
    )
    _validate_selected_model_sources(current, resolved.workspace_id, options.model_sources)
    owner = event.owner_principal or current.principal
    user_id = human_principal_user_id(owner) if owner.kind == "human" else None
    launch = PreparedScopedLaunch(
        credential,
        current,
        scope,
        source,
        resolved,
        owner,
        user_id,
        event.request_uuid or uuid4(),
        backend,
        effective_source,
        "none",
        options,
    )
    if event.request_uuid is not None:
        launch = replace(launch, intent_digest=_launch_intent_digest(launch))
    if effective_source is RangeSource.MISSION_CONTROL and (
        RangeInstance.objects.filter(owner_principal_uuid=owner.uuid, range_source=effective_source.value)
        .exclude(status=ResourceStatus.DESTROYING.value)
        .exists()
    ):
        raise CMSError(_ACTIVE_RANGE_MESSAGE)
    egress = _initial_launch_egress(current, resolved, scenario, source, event.model_admission_subject)
    prepared = cast(PreparedScopedLaunch, replace(launch, egress=egress))
    return prepared


def create_scoped_range(
    credential: CredentialContext, scenario: str, options: ScopedRangeLaunchOptions
) -> RangeContext:
    """Compose native admission, warm claim, atomic reservation and external dispatch."""
    from shared.schemas.range import RangeContext

    try:
        launch = _prepare_launch(credential, scenario, options)
        claimed = _warm_claim(launch)
        if claimed is not None:
            return claimed
        instance, egress = reserve_scoped_launch(launch)
        _dispatch_reserved(launch, instance, egress)
        return RangeContext(
            request_id=launch.request_id,
            range_id=None,
            scenario_id=scenario,
            user_id=launch.user_id,
            owner_principal_uuid=launch.owner.uuid,
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


def _dispatch_reserved(launch: PreparedScopedLaunch, instance: RangeInstance, egress: str) -> None:
    """Dispatch outside SQL; retain event reservations across uncertain outcomes."""
    try:
        from ._model_admission import assert_launch_model_access
        from ._raes_dispatch import RaesDispatchOptions, dispatch_object_raes_package, dispatch_repo_raes_package

        assert_launch_model_access(
            user=launch.current,
            scenario_id=launch.source.scenario_id,
            egress_mode=egress,
            subject=launch.options.event.model_admission_subject,
            package_digest=launch.source.package_digest,
        )
        revalidate_scoped_launch(
            launch.current,
            launch.scope,
            launch.source,
            authority_action=launch.options.event.authority_action,
            authority_target=launch.options.event.authority_target,
        )
        if launch.options.event.request_uuid is not None and instance.range_id is not None:
            _resume_staged_event_range(instance, launch.scope, launch.owner, launch.options.event.authority_target)
        else:
            dispatch = (
                dispatch_object_raes_package if launch.source.source_kind == "object" else dispatch_repo_raes_package
            )
            dispatch(
                launch.request_id,
                launch.current,
                launch.source,
                RaesDispatchOptions(
                    launch.backend,
                    instance.workspace_id,
                    egress,
                    principal_scope=launch.scope,
                    owner_principal=launch.owner if launch.is_event else None,
                    authority_action=launch.options.event.authority_action,
                    authority_target=launch.options.event.authority_target,
                ),
            )
    except Exception:
        # A reserved CTF request is a durable, resumable generation. A
        # transport/policy failure does not prove that no native range was
        # committed, so neither its identity nor quota may be discarded.
        if launch.options.event.request_uuid is not None:
            raise
        with transaction.atomic():
            instance.status = "failed"
            instance.save(update_fields=["status"])
            if instance.workspace_id is not None:
                release_workspace_concurrent_range(instance.workspace_id, launch.request_id)
        raise


def _resume_staged_event_range(
    instance: RangeInstance, scope: ResourceScope, owner: PrincipalRef, event_target: TargetRef | None
) -> None:
    """Reuse the accepted native plan, never compile a second runtime generation."""
    from engine.services import project_range_authority

    from ._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch

    if instance.request is None:
        raise CMSError("Range launch request unavailable")
    projection = project_range_authority(instance.request.request_id)
    if (
        projection.public_uuid != instance.uuid
        or projection.native_id != instance.range_id
        or projection.scope != scope
        or projection.owner_principal_uuid != owner.uuid
        or projection.parent != event_target
    ):
        raise CMSError("Range launch runtime conflict")
    if needs_model_preparation(instance.request.request_id):
        prepare_model_access_for_dispatch(instance.request.request_id, range_id=projection.native_uuid)


def _launch_intent_digest(launch: PreparedScopedLaunch) -> str:
    """Bind replay to original proof, source revision and normalized launch inputs."""
    import json
    from dataclasses import asdict

    from django.core.serializers.json import DjangoJSONEncoder

    from shared.operation_envelope import canonical_payload_digest

    fields = {
        "credential": credential_reference_fields(launch.original),
        "scope": asdict(launch.scope),
        "source": source_launch_binding(launch.source),
        "owner": asdict(launch.owner),
        "target": asdict(launch.options.event.authority_target) if launch.options.event.authority_target else None,
        "deadline": launch.options.event.remote_access_teardown_at,
        "model_sources": launch.options.model_sources,
        "model_subject": launch.options.event.model_admission_subject.model_dump(mode="json")
        if launch.options.event.model_admission_subject
        else None,
        "model_scope": launch.options.event.model_launch_scope.model_dump(mode="json")
        if launch.options.event.model_launch_scope
        else None,
        "backend": launch.backend.backend if launch.backend else None,
    }
    return canonical_payload_digest(json.loads(json.dumps(fields, cls=DjangoJSONEncoder)))


def _validate_launch_placement(scope: ResourceScope | None, workspace_uuid: UUID | str | None) -> ResourceScope:
    """Require explicit customer scope and exact optional workspace selection."""
    if scope is None or scope.kind != "account":
        raise CMSError("Range launch scope unavailable")
    if workspace_uuid is not None and str(workspace_uuid) != str(scope.workspace_uuid):
        raise CMSError("Range launch scope unavailable")
    return scope


def _validate_request_uuid(request_uuid: UUID | None, is_event: bool) -> None:
    """Only retained event intents may supply a nonzero native request identity."""
    if request_uuid is not None and (not isinstance(request_uuid, UUID) or not request_uuid.int or not is_event):
        raise CMSError("Range launch request unavailable")


def _validate_event_binding(
    is_event_launch: bool, owner: PrincipalRef | None, action: str | None, target: TargetRef | None
) -> None:
    """CTF requires the complete binding; ordinary launches accept none of its authority fields."""
    if is_event_launch:
        if not (owner and action and target):
            raise CMSError(_LAUNCH_INPUTS_UNAVAILABLE)
    elif any(value is not None for value in (owner, action, target)):
        raise CMSError(_LAUNCH_INPUTS_UNAVAILABLE)
