"""Live initiating authority for the existing provisioner outbox."""

from uuid import UUID

from shared.authorization import AuthorizationRequest, TargetRef
from shared.authorization.policy import require_authorization
from shared.credentials import CredentialContext, resolve_credential_scope, resolve_current_credential
from shared.identity_scope import ResourceScope
from shared.operation_authority import credential_reference_fields, credential_reference_from_record
from shared.operation_envelope import AUTHORITY_CONTRACT_VERSION, canonical_payload_digest, validate_operation_envelope


def range_runtime_binding_digest(row) -> str:
    """Pin native resource/owner/scope/backend facts, including for cleanup."""
    from engine.models import Instance

    if isinstance(row, Instance):
        from ._device_authority import device_binding_digest

        return device_binding_digest(row)
    return canonical_payload_digest(
        {
            "native_uuid": str(row.uuid),
            "public_uuid": str(row.cms_range_uuid),
            "request_uuid": str(row.request.request_id),
            "owner_uuid": str(row.owner_principal_uuid),
            "scope_kind": row.scope_kind,
            "account_id": row.account_id,
            "organization_id": row.organization_id,
            "workspace_id": row.workspace_id,
            "event_uuid": str(row.event_uuid),
            "range_backend": row.range_backend,
            "resource_generation": str(row.resource_generation),
        }
    )


def admit_launch_authority(payload: dict[str, object], credential: CredentialContext) -> dict[str, object]:
    """Check the existing parent for creation; the exact leaf for lifecycle."""
    from engine.models import Range

    from ._range_authority import project_range_authority

    if payload.get("resource") not in {"range", "raes-range"} or "request_id" not in payload:
        raise ValueError("Operation authority unavailable")
    request_uuid = UUID(str(payload["request_id"]))
    projection = project_range_authority(request_uuid)
    leaf = TargetRef("range", projection.public_uuid)
    if resolve_credential_scope(leaf) != projection.scope:
        raise ValueError("Operation authority unavailable")
    current = resolve_current_credential(credential)
    target = leaf
    action = "range.manage"
    parent = projection.parent
    if payload.get("operation") in {"provision", "activate"}:
        target = parent
        action = "event.manage_ranges" if parent.type == "event" else f"{parent.type}.launch_range"
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
    except Exception:
        raise ValueError("Operation authority unavailable") from None
    return admitted_range_authority_fields(current, projection, runtime, action=action, target=target)


def admitted_range_authority_fields(current, projection, runtime, *, action, target):
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


def authorize_launch_authority(intent, runtime) -> None:
    """Recheck the original initiating proof; an owner cannot substitute for it."""
    from engine.models import Instance

    if isinstance(runtime, Instance):
        from ._device_authority import authorize_device_authority

        return authorize_device_authority(intent, runtime)
    from ._range_authority import project_range_authority

    try:
        assert_launch_authority_binding(intent, runtime)
        if intent.authority_kind == "cleanup":
            return
        current = resolve_current_credential(credential_reference_from_record(intent))
        projection = project_range_authority(runtime.request.request_id)
        scope = ResourceScope(
            intent.authority_scope_kind,
            intent.authority_account_uuid,
            intent.authority_organization_uuid,
            intent.authority_workspace_uuid,
        )
        parent = TargetRef(intent.authority_parent_type, intent.authority_parent_uuid)
        leaf = TargetRef("range", projection.public_uuid)
        if (
            projection.scope != scope
            or projection.parent != parent
            or projection.public_uuid != intent.authority_resource_uuid
            or projection.owner_principal_uuid != intent.authority_owner_uuid
            or resolve_credential_scope(leaf) != scope
        ):
            raise ValueError("Range authority changed")
        target = TargetRef(intent.authority_target_type, intent.authority_target_uuid)
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
    except Exception:
        raise PermissionError("Operation authority unavailable") from None


def assert_launch_authority_binding(intent, runtime) -> None:
    """Pure SQL-fact fence after policy evaluation, safe inside a row lock."""
    from engine.models import Instance

    if isinstance(runtime, Instance):
        from ._device_authority import assert_device_authority_binding

        return assert_device_authority_binding(intent, runtime)
    if intent.authority_kind not in {
        "principal",
        "cleanup",
    } or intent.authority_binding_digest != range_runtime_binding_digest(runtime):
        raise ValueError("Operation authority unavailable")
    if intent.authority_kind == "cleanup":
        from ._launch_cleanup_authority import assert_cleanup_origin

        assert_cleanup_origin(intent, runtime)


def prepare_launch_authority(
    payload: dict[str, object], credential: CredentialContext | None, cleanup_for: UUID | None = None
) -> dict[str, object]:
    """Principal-owned resources cannot enter the incumbent outbox anonymously."""
    from engine.models import Range

    if payload.get("resource") == "ngfw":
        from ._device_authority import prepare_device_authority, prepare_device_cleanup

        if cleanup_for is not None:
            if credential is not None:
                raise ValueError("Cleanup cannot replace a caller proof")
            return prepare_device_cleanup(payload, cleanup_for)
        return prepare_device_authority(payload, credential)
    if cleanup_for is not None:
        from ._launch_cleanup_authority import prepare_cleanup_authority

        if credential is not None:
            raise ValueError("Cleanup authority cannot replace a caller proof")
        return prepare_cleanup_authority(payload, cleanup_for)
    runtime = None
    if payload.get("resource") in {"range", "raes-range"} and "request_id" in payload:
        runtime = Range.objects.filter(request__request_id=UUID(str(payload["request_id"]))).first()
    if credential is not None:
        return admit_launch_authority(payload, credential)
    if runtime is not None and runtime.owner_principal_uuid is not None:
        raise PermissionError("Operation authority unavailable")
    return {}


def assert_admitted_launch_binding(fields: dict[str, object], runtime) -> None:
    """Fence scope/owner/backend drift after admission and before durable commit."""
    if fields and fields["authority_binding_digest"] != range_runtime_binding_digest(runtime):
        raise ValueError("Operation authority unavailable")
    from engine.models import Instance

    if isinstance(runtime, Instance):
        if runtime.request.owner_principal_uuid is not None and not fields:
            raise PermissionError("Device authority unavailable")
        if fields.get("authority_kind") == "cleanup":
            from ._device_authority import assert_device_cleanup_origin

            assert_device_cleanup_origin(fields["cleanup_origin_operation_uuid"], runtime)
        return
    if fields and fields.get("authority_kind") == "principal":
        leaf = TargetRef("range", runtime.cms_range_uuid)
        scope = ResourceScope(
            fields["authority_scope_kind"],
            fields["authority_account_uuid"],
            fields["authority_organization_uuid"],
            fields["authority_workspace_uuid"],
        )
        if resolve_credential_scope(leaf) != scope:
            raise ValueError("Operation authority unavailable")


def assert_launch_authority_replay(intent, fields: dict[str, object]) -> None:
    """Equal native replay retains the initiating proof and its original ceiling."""
    if bool(fields) != bool(intent.authority_kind) or any(
        getattr(intent, key) != value for key, value in fields.items()
    ):
        raise ValueError("Replay authority conflicts with the immutable operation")


def launch_authority_digest(intent) -> str:
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


def authority_envelope_fields(intent) -> dict[str, str]:
    if not intent.authority_kind:
        return {}
    return {"contract_version": AUTHORITY_CONTRACT_VERSION, "authority_digest": launch_authority_digest(intent)}


def assert_input_authority_binding(stored, intent) -> None:
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
