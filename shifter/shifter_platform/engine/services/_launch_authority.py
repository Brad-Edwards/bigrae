"""Live initiating authority for the existing provisioner outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

from shared.authorization import AuthorizationRequest, TargetRef
from shared.authorization.catalog import TargetType
from shared.authorization.policy import require_authorization
from shared.credentials import CredentialContext, resolve_credential_scope, resolve_current_credential
from shared.operation_authority import (
    authority_resource_scope,
    credential_reference_fields,
    credential_reference_from_record,
)
from shared.operation_envelope import AUTHORITY_CONTRACT_VERSION, canonical_payload_digest, validate_operation_envelope

from ._launch_authority_binding import (
    assert_admitted_launch_binding,
    assert_launch_authority_binding,
    range_runtime_binding_digest,
)

__all__ = ["assert_admitted_launch_binding", "assert_launch_authority_binding", "range_runtime_binding_digest"]

if TYPE_CHECKING:
    from uuid import UUID

    from engine.models import Instance, OperationInput, ProvisionerLaunchIntent, Range
    from engine.services._range_authority import RangeAuthorityProjection
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope


_OPERATION_AUTHORITY_UNAVAILABLE = "Operation authority unavailable"

_INSTALLATION_LAUNCH_ACTION = "installation.launch_range"


def admit_launch_authority(
    payload: dict[str, object],
    credential: CredentialContext,
    *,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
) -> dict[str, object]:
    """Check the existing parent for creation; the exact leaf for lifecycle."""
    from engine.models import Range

    from ._range_authority import project_range_authority

    if (
        payload.get("resource") not in {"range", "raes-range"}
        or "request_id" not in payload
        or (authority_action is None) != (authority_target is None)
    ):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    request_uuid = UUID(str(payload["request_id"]))
    projection = project_range_authority(request_uuid)
    leaf = TargetRef("range", projection.public_uuid)
    if resolve_credential_scope(leaf) != projection.scope:
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    current = resolve_current_credential(credential)
    action, target = _launch_action_target(payload, projection, request_uuid, authority_action, authority_target)
    parent = projection.parent
    require_authorization(
        AuthorizationRequest(
            current.principal,
            action,
            target,
            projection.scope,
            current.ceiling,
            parent=parent if target.type == "range" else None,
        )
    )
    runtime = Range.objects.select_related("request").get(pk=projection.native_id)
    try:
        if resolve_current_credential(current) != current:
            raise ValueError("Operation authority changed")
        if project_range_authority(request_uuid) != projection or resolve_credential_scope(leaf) != projection.scope:
            raise ValueError("Operation authority changed")
    except Exception:  # noqa: BLE001  # fail-closed authority check; every failure is one bounded denial
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE) from None
    return admitted_range_authority_fields(current, projection, runtime, action=action, target=target)


def _launch_action_target(
    payload: dict[str, object],
    projection: RangeAuthorityProjection,
    request_uuid: UUID,
    authority_action: str | None,
    authority_target: TargetRef | None,
) -> tuple[str, TargetRef]:
    """Select creation-parent or lifecycle-leaf authority without broadening CTF overrides."""
    parent = projection.parent
    target = TargetRef("range", projection.public_uuid)
    action = "range.manage"
    if payload.get("operation") in {"provision", "activate"}:
        if parent.type == "event":
            from shared.range_workspace_aggregate import require_range_event_association

            if parent.uuid is None:
                raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
            require_range_event_association(request_uuid, parent.uuid)
        target = parent
        action = "event.manage_ranges" if parent.type == "event" else f"{parent.type}.launch_range"
    if authority_action is not None:
        if authority_action != "event.manage_ranges" or parent.type != "event" or authority_target != parent:
            raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
        action = authority_action
        target = authority_target
    return action, target


def admitted_range_authority_fields(
    current: CredentialContext, projection: RangeAuthorityProjection, runtime: Range, *, action: str, target: TargetRef
) -> dict[str, object]:
    """Bind an already evaluated owning-service decision to immutable runtime facts."""
    parent = projection.parent
    return {
        **credential_reference_fields(current),
        "authority_kind": "principal",
        "authority_action": action,
        "authority_target_type": target.type,
        "authority_target_uuid": target.uuid,
        "authority_parent_type": parent.type,
        "authority_parent_uuid": parent.uuid,
        "authority_scope_kind": projection.scope.kind,
        "authority_account_uuid": projection.scope.account_uuid,
        "authority_organization_uuid": projection.scope.organization_uuid,
        "authority_workspace_uuid": projection.scope.workspace_uuid,
        "authority_owner_uuid": projection.owner_principal_uuid,
        "authority_resource_uuid": projection.public_uuid,
        "authority_binding_digest": range_runtime_binding_digest(runtime),
    }


def authorize_launch_authority(intent: ProvisionerLaunchIntent, runtime: Range | Instance) -> None:
    """Recheck the original initiating proof; an owner cannot substitute for it."""
    from engine.models import Instance

    if isinstance(runtime, Instance):
        from ._device_authority import authorize_device_authority

        authorize_device_authority(intent, runtime)
        return
    from ._range_authority import project_range_authority

    try:
        assert_launch_authority_binding(intent, runtime)
        if intent.authority_kind == "cleanup":
            return
        current = resolve_current_credential(credential_reference_from_record(intent))
        if intent.authority_kind == "warm_pool":
            _authorize_warm_pool_authority(intent, runtime, current)
            return
        if runtime.request is None:
            raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
        projection = project_range_authority(runtime.request.request_id)
        scope, parent = _bound_range_scope(intent, projection)
        target = TargetRef(cast(TargetType, intent.authority_target_type), intent.authority_target_uuid)
        require_authorization(
            AuthorizationRequest(
                current.principal,
                intent.authority_action,
                target,
                scope,
                current.ceiling,
                parent=parent if target.type == "range" else None,
            )
        )
    except Exception:  # noqa: BLE001  # fail-closed authority check; every failure is one bounded denial
        raise PermissionError(_OPERATION_AUTHORITY_UNAVAILABLE) from None


def _bound_range_scope(
    intent: ProvisionerLaunchIntent, projection: RangeAuthorityProjection
) -> tuple[ResourceScope, TargetRef]:
    """Require the retained resource, owner and parent to match current SQL facts."""
    scope = authority_resource_scope(
        {
            name: getattr(intent, name)
            for name in (
                "authority_scope_kind",
                "authority_account_uuid",
                "authority_organization_uuid",
                "authority_workspace_uuid",
            )
        }
    )
    parent = TargetRef(cast(TargetType, intent.authority_parent_type), intent.authority_parent_uuid)
    leaf = TargetRef("range", projection.public_uuid)
    if (
        projection.scope != scope
        or projection.parent != parent
        or projection.public_uuid != intent.authority_resource_uuid
        or projection.owner_principal_uuid != intent.authority_owner_uuid
        or resolve_credential_scope(leaf) != scope
    ):
        raise ValueError("Range authority changed")
    return scope, parent


def _authorize_warm_pool_authority(intent: ProvisionerLaunchIntent, runtime: Range, current: CredentialContext) -> None:
    """Recheck the exact service and installation decision for warm preparation."""
    from shared.identity_scope import ResourceScope

    target = TargetRef("installation")
    scope = ResourceScope("installation")
    if (
        current.principal.kind != "service"
        or intent.authority_action != _INSTALLATION_LAUNCH_ACTION
        or intent.authority_target_type != "installation"
        or intent.authority_target_uuid is not None
        or intent.authority_parent_type != "installation"
        or intent.authority_parent_uuid is not None
        or intent.authority_scope_kind != "installation"
        or any(
            value is not None
            for value in (
                intent.authority_account_uuid,
                intent.authority_organization_uuid,
                intent.authority_workspace_uuid,
            )
        )
    ):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    _require_warm_pool_custody(intent, runtime, current)
    require_authorization(
        AuthorizationRequest(current.principal, _INSTALLATION_LAUNCH_ACTION, target, scope, current.ceiling)
    )
    if resolve_current_credential(current) != current:
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)


def _require_warm_pool_custody(intent: ProvisionerLaunchIntent, runtime: Range, current: CredentialContext) -> None:
    """Retain installation scope and the exact native service-owned warm generation."""
    if (
        runtime.scope_kind != "installation"
        or any(value is not None for value in (runtime.account_id, runtime.organization_id, runtime.workspace_id))
        or runtime.owner_principal_uuid != current.principal.uuid
        or runtime.owner_principal_uuid != intent.authority_owner_uuid
        or runtime.cms_range_uuid != intent.authority_resource_uuid
    ):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)


def prepare_launch_authority(
    payload: dict[str, object], credential: CredentialContext | None, cleanup_for: UUID | None = None
) -> dict[str, object]:
    """Principal-owned resources cannot enter the incumbent outbox anonymously."""

    if payload.get("resource") == "ngfw":
        return _prepare_device_launch_authority(payload, credential, cleanup_for)
    if cleanup_for is not None:
        from ._launch_cleanup_authority import prepare_cleanup_authority

        if credential is not None:
            raise ValueError("Cleanup authority cannot replace a caller proof")
        return prepare_cleanup_authority(payload, cleanup_for)
    return _principal_launch_fields(payload, credential)


def assert_launch_authority_replay(intent: ProvisionerLaunchIntent, fields: dict[str, object]) -> None:
    """Equal native replay retains the initiating proof and its original ceiling."""
    if bool(fields) != bool(intent.authority_kind) or any(
        getattr(intent, key) != value for key, value in fields.items()
    ):
        raise ValueError("Replay authority conflicts with the immutable operation")


def launch_authority_digest(intent: ProvisionerLaunchIntent) -> str:
    """Bind input/replay to the existing record without shipping credential proof."""
    fields = credential_reference_fields(credential_reference_from_record(intent))
    fields["executing_workload"] = intent.executing_workload
    names = (
        "cleanup_origin_operation_uuid",
        "authority_kind",
        "authority_action",
        "authority_target_type",
        "authority_target_uuid",
        "authority_parent_type",
        "authority_parent_uuid",
        "authority_scope_kind",
        "authority_account_uuid",
        "authority_organization_uuid",
        "authority_workspace_uuid",
        "authority_owner_uuid",
        "authority_resource_uuid",
        "authority_binding_digest",
    )
    fields.update({name: getattr(intent, name) for name in names})
    return canonical_payload_digest(
        {key: str(value) if isinstance(value, UUID) else value for key, value in fields.items()}
    )


def authority_envelope_fields(intent: ProvisionerLaunchIntent) -> dict[str, str]:
    """Bind the operation envelope to its recorded original authority digest."""
    if not intent.authority_kind:
        return {}
    return {"contract_version": AUTHORITY_CONTRACT_VERSION, "authority_digest": launch_authority_digest(intent)}


def assert_input_authority_binding(stored: OperationInput, intent: ProvisionerLaunchIntent) -> None:
    """A replay cannot shed its authority reference or silently change it."""
    expected = authority_envelope_fields(intent)
    if not expected:
        return
    envelope = validate_operation_envelope(stored.envelope)
    if (
        any(envelope.get(key) != value for key, value in expected.items())
        or stored.contract_version != AUTHORITY_CONTRACT_VERSION
        or stored.operation_id != intent.operation_id
        or envelope["operation_id"] != str(intent.operation_id)
        or envelope["request_id"] != intent.payload.get("request_id")
        or envelope["resource"] != intent.payload.get("resource")
        or envelope["operation"] != intent.payload.get("operation")
    ):
        raise ValueError("Replay authority conflicts with the immutable operation input")


def _prepare_device_launch_authority(
    payload: dict[str, object], credential: CredentialContext | None, cleanup_for: UUID | None
) -> dict[str, object]:
    """Keep device admission and bounded native cleanup on their existing owner boundary."""
    from ._device_authority import prepare_device_authority, prepare_device_cleanup

    if cleanup_for is not None:
        if credential is not None:
            raise ValueError("Cleanup cannot replace a caller proof")
        return prepare_device_cleanup(payload, cleanup_for)
    return prepare_device_authority(payload, credential)


def _principal_launch_fields(payload: dict[str, object], credential: CredentialContext | None) -> dict[str, object]:
    """Require explicit principal proof for any principal-owned native range."""
    from engine.models import Range

    runtime = None
    if payload.get("resource") in {"range", "raes-range"} and "request_id" in payload:
        runtime = Range.objects.filter(request__request_id=UUID(str(payload["request_id"]))).first()
    if credential is not None:
        if runtime is not None and runtime.scope_kind == "installation" and payload.get("operation") == "provision":
            from shared.identity_scope import ResourceScope

            from ._principal_range_create import _assert_creation_origin, _installation_creation_authority

            current = resolve_current_credential(credential)
            if current.principal.kind != "service":
                raise PermissionError(_OPERATION_AUTHORITY_UNAVAILABLE)
            _assert_creation_origin(runtime, current)
            require_authorization(
                AuthorizationRequest(
                    current.principal,
                    _INSTALLATION_LAUNCH_ACTION,
                    TargetRef("installation"),
                    ResourceScope("installation"),
                    current.ceiling,
                )
            )
            return _installation_creation_authority(current, runtime)
        return admit_launch_authority(payload, credential)
    if runtime is not None and runtime.owner_principal_uuid is not None:
        raise PermissionError(_OPERATION_AUTHORITY_UNAVAILABLE)
    return {}
