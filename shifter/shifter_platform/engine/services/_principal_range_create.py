"""Native RAES creation validates explicit customer scope and independent ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import resolve_current_credential, resolve_local_credential
from shared.operation_envelope import canonical_payload_digest

if TYPE_CHECKING:
    from typing import Any
    from uuid import UUID

    from engine.models import Range
    from engine.services._raes_range import RaesRangeRef
    from shared.credentials import CredentialContext

    from ._raes_range import RangeBindings


_RANGE_CREATION_AUTHORITY_UNAVAILABLE = "Range creation authority unavailable"


@dataclass(frozen=True)
class PreparedRaesRangeCreation:
    """In-process parent admission, never accepted as a client/worker credential."""

    bindings: RangeBindings
    workspace_id: int | None
    user_id: int | None
    plan_digest: str
    fields: tuple[tuple[str, object], ...]


def prepare_raes_range_creation(
    bindings: RangeBindings, workspace_id: int | None, user_id: int | None, compiled_plan: dict[str, Any]
) -> PreparedRaesRangeCreation:
    """Freeze parent admission and the compiled plan before native creation."""
    fields = prepare_principal_range_fields(bindings, workspace_id, user_id)
    return PreparedRaesRangeCreation(
        bindings, workspace_id, user_id, canonical_payload_digest(compiled_plan), tuple(fields.items())
    )


def prepared_creation_fields(
    prepared: PreparedRaesRangeCreation | None,
    bindings: RangeBindings,
    workspace_id: int | None,
    user_id: int | None,
    compiled_plan: dict[str, Any],
) -> dict[str, Any]:
    """Validate prepared custody and plan identity before replaying creation fields."""
    if (
        not isinstance(prepared, PreparedRaesRangeCreation)
        or prepared.workspace_id != workspace_id
        or prepared.user_id != user_id
        or prepared.plan_digest != canonical_payload_digest(compiled_plan)
        or prepared.bindings.credential != bindings.credential
        or prepared.bindings.resource_scope != bindings.resource_scope
        or prepared.bindings.cms_range_uuid != bindings.cms_range_uuid
        or prepared.bindings.owner_principal != bindings.owner_principal
        or prepared.bindings.authority_action != bindings.authority_action
        or prepared.bindings.authority_target != bindings.authority_target
    ):
        raise ValueError("Range creation admission changed")
    return dict(prepared.fields)


def admitted_creation_authority(prepared: PreparedRaesRangeCreation, runtime: Range) -> dict[str, object]:
    """Fence the prepared parent before atomically queuing the new generation."""
    from ._launch_authority import admitted_range_authority_fields
    from ._range_authority import project_range_authority

    bindings = prepared.bindings
    if bindings.credential is None or runtime.request is None or not isinstance(runtime.range_config, dict):
        raise ValueError(_RANGE_CREATION_AUTHORITY_UNAVAILABLE)
    fields = prepared_creation_fields(prepared, bindings, runtime.workspace_id, runtime.user_id, runtime.range_config)
    assert_principal_range_creation(bindings, fields)
    if any(getattr(runtime, name) != value for name, value in fields.items()):
        raise ValueError("Range creation custody changed")
    if bindings.resource_scope is not None and bindings.resource_scope.kind == "installation":
        return _installation_creation_authority(bindings.credential, runtime)
    projection = project_range_authority(runtime.request.request_id)
    if projection.scope != bindings.resource_scope:
        raise ValueError("Range creation projections changed")
    parent = (
        bindings.authority_target
        if bindings.authority_target is not None and bindings.authority_target.type == "event"
        else scope_parent(projection.scope)
    )
    if parent != projection.parent:
        raise ValueError("Range creation parent changed")
    action = bindings.authority_action or f"{parent.type}.launch_range"
    target = bindings.authority_target or parent
    return admitted_range_authority_fields(bindings.credential, projection, runtime, action=action, target=target)


def _installation_creation_authority(credential: CredentialContext, runtime: Range) -> dict[str, object]:
    """Bind an admitted installation-owned warm preparation to its exact runtime."""
    from shared.operation_authority import credential_reference_fields

    from ._launch_authority import range_runtime_binding_digest

    if runtime.cms_range_uuid is None or runtime.owner_principal_uuid != credential.principal.uuid:
        raise ValueError(_RANGE_CREATION_AUTHORITY_UNAVAILABLE)
    return {
        **credential_reference_fields(credential),
        "authority_kind": "warm_pool",
        "authority_action": "installation.launch_range",
        "authority_target_type": "installation",
        "authority_target_uuid": None,
        "authority_parent_type": "installation",
        "authority_parent_uuid": None,
        "authority_scope_kind": "installation",
        "authority_account_uuid": None,
        "authority_organization_uuid": None,
        "authority_workspace_uuid": None,
        "authority_owner_uuid": credential.principal.uuid,
        "authority_resource_uuid": runtime.cms_range_uuid,
        "authority_binding_digest": range_runtime_binding_digest(runtime),
    }


def prepare_principal_range_fields(
    bindings: RangeBindings, workspace_id: int | None, user_id: int | None
) -> dict[str, object]:
    """Evaluate parent policy before the create transaction; never borrow a user."""
    from engine.ecs._config import _get_engine_task_config
    from workspaces.services import resolve_range_scope_binding

    if _get_engine_task_config() is None:
        raise ValueError("Range provisioner unavailable")
    scope = bindings.resource_scope
    if bindings.credential is None:
        raise ValueError("Range creation credential unavailable")
    current = resolve_current_credential(bindings.credential)
    if scope is None or not isinstance(bindings.cms_range_uuid, UUID):
        raise ValueError(_RANGE_CREATION_AUTHORITY_UNAVAILABLE)
    if not bindings.cms_range_uuid.int:
        raise ValueError(_RANGE_CREATION_AUTHORITY_UNAVAILABLE)
    owner = bindings.owner_principal or current.principal
    if scope.kind == "installation":
        if current.principal.kind != "service" or user_id is not None or workspace_id is not None:
            raise ValueError(_RANGE_CREATION_AUTHORITY_UNAVAILABLE)
        resolved = resolve_range_scope_binding(scope)
    else:
        _assert_historical_user(owner, user_id)
        resolved = (
            resolve_range_scope_binding(scope)
            if bindings.authority_target is not None
            else resolve_range_scope_binding(scope, member=current.principal)
        )
    if resolved.workspace_id != workspace_id:
        raise ValueError("Range scope conflicts")
    parent = scope_parent(scope)
    action = bindings.authority_action or f"{parent.type}.launch_range"
    target = bindings.authority_target or parent
    require_authorization(AuthorizationRequest(current.principal, action, target, scope, current.ceiling))
    if resolve_current_credential(current) != current:
        raise ValueError("Range authority changed")
    return {
        "owner_principal_uuid": owner.uuid,
        "cms_range_uuid": bindings.cms_range_uuid,
        "scope_kind": resolved.kind,
        "account_id": resolved.account_id,
        "organization_id": resolved.organization_id,
        "event_uuid": (
            bindings.authority_target.uuid
            if bindings.authority_target is not None and bindings.authority_target.type == "event"
            else None
        ),
    }


def _assert_historical_user(owner, user_id: int | None) -> None:
    """Human attribution cannot substitute for the admitted principal owner."""
    from management.services import human_principal_user_id

    expected_user = human_principal_user_id(owner) if owner.kind == "human" else None
    if expected_user != user_id:
        raise ValueError("Range historical attribution conflicts")


def assert_principal_range_creation(bindings: RangeBindings, fields: dict[str, object]) -> None:
    """Only fresh scalar ancestry/membership/credential checks run under SQL locks."""
    from workspaces.services import resolve_range_scope_binding

    if bindings.credential is None or bindings.resource_scope is None:
        raise ValueError(_RANGE_CREATION_AUTHORITY_UNAVAILABLE)
    current = resolve_local_credential(bindings.credential)
    owner = bindings.owner_principal or current.principal
    resolved = (
        resolve_range_scope_binding(bindings.resource_scope)
        if bindings.resource_scope.kind == "installation" or bindings.authority_target is not None
        else resolve_range_scope_binding(bindings.resource_scope, member=current.principal, for_update=True)
    )
    if (
        current != bindings.credential
        or (bindings.resource_scope.kind == "installation" and current.principal.kind != "service")
        or owner.uuid != fields["owner_principal_uuid"]
        or resolved.kind != fields["scope_kind"]
        or resolved.account_id != fields["account_id"]
        or resolved.organization_id != fields["organization_id"]
    ):
        raise ValueError("Range scope changed")


def verify_principal_range_replay(
    existing: Range, fields: dict[str, object], bindings: RangeBindings, compiled_plan: dict[str, Any]
) -> None:
    """Refuse replay that changes immutable range custody or compiled input."""
    if existing.owner_principal_uuid is None:
        if fields:
            raise ValueError("Range ownership conflict")
        return
    if bindings.credential is None:
        raise ValueError("Range creation credential unavailable")
    if not fields or any(getattr(existing, name) != value for name, value in fields.items()):
        raise ValueError("Range ownership conflict")
    if existing.range_config != compiled_plan:
        raise ValueError("Range plan conflict")
    from engine.models import ProvisionerLaunchIntent
    from shared.operation_authority import credential_reference_fields

    original = ProvisionerLaunchIntent.objects.filter(
        authority_resource_uuid=existing.cms_range_uuid, payload__operation="provision"
    ).first()
    if original is not None and any(
        getattr(original, key) != value for key, value in credential_reference_fields(bindings.credential).items()
    ):
        raise ValueError("Range initiating authority conflict")


def dispatch_principal_raes_range(
    runtime: Range, credential: CredentialContext, prepared: PreparedRaesRangeCreation | None
) -> RaesRangeRef:
    """Admission precedes locks; native queue and strict audit commit together."""
    from django.db import transaction

    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import ProvisionerLaunchIntent
    from shared.audit import AuditEntityType, AuditEvent, audit_log, principal_actor_fields

    from ._launch_authority import prepare_launch_authority
    from ._raes_range import RaesRangeRef

    if runtime.request is None:
        raise ValueError("Range request unavailable")
    request_id = runtime.request.request_id
    command = ["raes-range", "provision", "--request-id", str(request_id)]
    payload = {"version": 1, "resource": "raes-range", "operation": "provision", "request_id": str(request_id)}
    if prepared is not None:
        if prepared.bindings.credential != credential:
            raise PermissionError("Range creation authority changed")
        authority = admitted_creation_authority(prepared, runtime)
    else:
        authority = prepare_launch_authority(payload, credential)
    with transaction.atomic():
        identifier = _enqueue_admitted_provisioner_launch(command, payload, authority)
        intent = ProvisionerLaunchIntent.objects.get(intent_id=identifier)
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=runtime.pk,
                entity_ref=str(runtime.cms_range_uuid),
                action="provision",
                **principal_actor_fields(credential.principal),
                request_id=str(request_id),
                new_state={"status": runtime.status, "launch_intent": identifier},
            ),
            strict=True,
        )
        from ._common import _persist_task_arn

        _persist_task_arn(runtime, "provision", intent.task_ref or f"local-pending:{identifier}")
    return RaesRangeRef(str(request_id), str(runtime.uuid), runtime.status, True)
