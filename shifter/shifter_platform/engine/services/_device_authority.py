"""Owning NGFW request authority composed into the existing operation outbox."""

from dataclasses import dataclass
from uuid import UUID

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope
from shared.operation_authority import credential_reference_fields, credential_reference_from_record
from shared.operation_envelope import canonical_payload_digest


def require_device_scope(credential, scope, operation="manage_devices"):
    from workspaces.services import resolve_range_scope_binding

    current = resolve_current_credential(credential)
    resolved = resolve_range_scope_binding(scope)
    parent = scope_parent(scope)
    require_authorization(
        AuthorizationRequest(current.principal, f"{parent.type}.{operation}", parent, scope, current.ceiling)
    )
    if resolve_current_credential(current) != current:
        raise ValueError("Device authority changed")
    return current, resolved


@dataclass(frozen=True)
class PreparedDeviceCreation:
    credential: CredentialContext
    scope: ResourceScope
    request_id: UUID
    spec_digest: str


def prepare_ngfw_creation(request_spec, credential, resource_scope):
    from engine.ecs._config import _get_engine_task_config
    from management.services import human_principal_user_id
    from workspaces.services import resolve_range_scope_binding

    if _get_engine_task_config() is None or resource_scope.kind != "account":
        raise ValueError("Device provisioner unavailable")
    current, _ = require_device_scope(credential, resource_scope)
    resolve_range_scope_binding(resource_scope, member=current.principal)
    user_id = human_principal_user_id(current.principal) if current.principal.kind == "human" else None
    if request_spec.user_id != user_id or request_spec.owner_principal_uuid != current.principal.uuid:
        raise ValueError("Device ownership conflicts")
    return PreparedDeviceCreation(
        current, resource_scope, request_spec.request_id, canonical_payload_digest(request_spec.model_dump(mode="json"))
    )


def assert_device_creation(prepared, request_spec, credential, scope):
    from workspaces.services import resolve_range_scope_binding

    if (
        not isinstance(prepared, PreparedDeviceCreation)
        or prepared.credential != credential
        or prepared.scope != scope
        or prepared.request_id != request_spec.request_id
        or prepared.spec_digest != canonical_payload_digest(request_spec.model_dump(mode="json"))
        or resolve_local_credential(credential) != credential
    ):
        raise ValueError("Device creation authority changed")
    resolve_range_scope_binding(scope, member=credential.principal, for_update=True)


def device_scope(runtime):
    from workspaces.services import resource_scope_from_ids

    request = runtime.request
    if request is None or request.request_type != "ngfw" or request.owner_principal_uuid is None:
        raise ValueError("Device custody unavailable")
    return resource_scope_from_ids(
        kind=request.scope_kind,
        account_id=request.account_id,
        organization_id=request.organization_id,
        workspace_id=request.workspace_id,
    )


def device_binding_digest(runtime):
    scope = device_scope(runtime)
    return canonical_payload_digest(
        {
            "request_id": str(runtime.request.request_id),
            "instance_uuid": str(runtime.uuid),
            "owner_uuid": str(runtime.request.owner_principal_uuid),
            "scope": str(scope),
            "spec": runtime.spec,
            "apps": [
                {"uuid": str(row.uuid), "spec": row.spec}
                for row in runtime.apps.filter(app_type="ngfw").order_by("uuid")
            ],
        }
    )


def admitted_device_authority(runtime, credential):
    """Bind a previously evaluated parent decision to native SQL custody."""
    scope = device_scope(runtime)
    parent = scope_parent(scope)
    apps = tuple(runtime.apps.filter(app_type="ngfw")[:2])
    if len(apps) != 1:
        raise ValueError("Device identity unavailable")
    return {
        **credential_reference_fields(credential),
        "authority_kind": "principal",
        "authority_action": f"{parent.type}.manage_devices",
        "authority_target_type": parent.type,
        "authority_target_uuid": parent.uuid,
        "authority_parent_type": parent.type,
        "authority_parent_uuid": parent.uuid,
        "authority_scope_kind": scope.kind,
        "authority_account_uuid": scope.account_uuid,
        "authority_organization_uuid": scope.organization_uuid,
        "authority_workspace_uuid": scope.workspace_uuid,
        "authority_owner_uuid": runtime.request.owner_principal_uuid,
        "authority_resource_uuid": apps[0].uuid,
        "authority_binding_digest": device_binding_digest(runtime),
    }


def prepare_device_authority(payload, credential):
    from engine.models import Instance

    runtime = (
        Instance.objects.select_related("request")
        .filter(request__request_id=payload.get("request_id"), role="ngfw")
        .first()
    )
    if runtime is None or runtime.request.owner_principal_uuid is None:
        if credential is not None:
            raise PermissionError("Device authority unavailable")
        return {}
    if credential is None:
        raise PermissionError("Device authority unavailable")
    current, _ = require_device_scope(credential, device_scope(runtime))
    return admitted_device_authority(runtime, current)


def assert_device_authority_binding(intent, runtime):
    if intent.authority_binding_digest != device_binding_digest(runtime):
        raise ValueError("Device custody changed")
    if intent.authority_kind == "cleanup":
        if intent.payload.get("resource") != "ngfw" or intent.payload.get("operation") != "deprovision":
            raise ValueError("Device cleanup operation changed")
        assert_device_cleanup_origin(intent.cleanup_origin_operation_uuid, runtime)
    elif intent.authority_kind != "principal":
        raise ValueError("Device authority unavailable")


def authorize_device_authority(intent, runtime):
    try:
        assert_device_authority_binding(intent, runtime)
        if intent.authority_kind == "cleanup":
            return
        current = resolve_current_credential(credential_reference_from_record(intent))
        require_device_scope(current, device_scope(runtime))
    except Exception:
        raise PermissionError("Device authority unavailable") from None


def assert_device_cleanup_origin(origin_id, runtime, *, require_absence=True):
    """Only a terminal, already-authorized teardown permits bounded system replay."""
    from engine.models import InterruptState, ProvisionerLaunchIntent

    origin = ProvisionerLaunchIntent.objects.get(operation_id=origin_id)
    if (
        origin.authority_kind != "principal"
        or origin.payload
        != {"version": 1, "resource": "ngfw", "operation": "deprovision", "request_id": str(runtime.request.request_id)}
        or origin.authority_binding_digest != device_binding_digest(runtime)
    ):
        raise ValueError("Device cleanup origin unavailable")
    if require_absence and origin.interrupt_state not in {
        InterruptState.TERMINAL_ABSENT,
        InterruptState.DESTROY_ENQUEUED,
    }:
        raise ValueError("Device cleanup task absence unverified")
    if runtime.provisioner_operation_id != origin_id:
        current = ProvisionerLaunchIntent.objects.filter(operation_id=runtime.provisioner_operation_id).first()
        if current is None or current.authority_kind != "cleanup" or current.cleanup_origin_operation_uuid != origin_id:
            raise ValueError("Device cleanup generation changed")
    return origin


def prepare_device_cleanup(payload, origin_id):
    from django.db import transaction

    from engine.ecs import interrupt_provisioner_task
    from engine.launch_intents import _request_intent_interrupt, command_from_payload
    from engine.models import Instance, InterruptState, ProvisionerLaunchIntent
    from shared.cloud.types import TaskInterruptDisposition

    if payload.get("resource") != "ngfw" or payload.get("operation") != "deprovision":
        raise ValueError("Device cleanup operation unavailable")
    runtime = Instance.objects.select_related("request").get(request__request_id=payload["request_id"], role="ngfw")
    origin = assert_device_cleanup_origin(origin_id, runtime, require_absence=False)
    with transaction.atomic():
        locked = ProvisionerLaunchIntent.objects.select_for_update().get(pk=origin.pk)
        if locked.interrupt_state in {InterruptState.IDENTITY_MISMATCH, InterruptState.EXHAUSTED}:
            raise ValueError("Device cleanup stopped at native identity boundary")
        _request_intent_interrupt(locked)
        state = locked.interrupt_state
    if state not in {InterruptState.TERMINAL_ABSENT, InterruptState.DESTROY_ENQUEUED}:
        if not origin.task_ref or not origin.executing_workload:
            raise ValueError("Device cleanup workload unavailable")
        command = command_from_payload({**origin.payload, "operation_id": str(origin.operation_id)})
        disposition = interrupt_provisioner_task(
            origin.task_ref, command, str(origin.intent_id), expected_workload=origin.executing_workload
        )
        if disposition != TaskInterruptDisposition.TERMINAL_ABSENT:
            raise ValueError("Device cleanup awaits native task absence")
        with transaction.atomic():
            current = ProvisionerLaunchIntent.objects.select_for_update().get(pk=origin.pk)
            if current.interrupt_state in {InterruptState.IDENTITY_MISMATCH, InterruptState.EXHAUSTED}:
                raise ValueError("Device cleanup stopped at native identity boundary")
            if current.interrupt_state != InterruptState.DESTROY_ENQUEUED:
                current.interrupt_state = InterruptState.TERMINAL_ABSENT
                current.save(update_fields=["interrupt_state"])
    origin = assert_device_cleanup_origin(origin_id, runtime)
    fields = admitted_device_authority(runtime, credential_reference_from_record(origin))
    fields.update(authority_kind="cleanup", cleanup_origin_operation_uuid=origin_id)
    return fields


def device_status_authority_fields(runtime):
    if runtime.request is None or runtime.request.owner_principal_uuid is None:
        return {}
    return {
        "owner_principal_uuid": str(runtime.request.owner_principal_uuid),
        "operation_id": str(runtime.provisioner_operation_id),
        "authority_binding_digest": device_binding_digest(runtime),
    }


def verified_device_status_projection(event):
    from engine.models import Instance, RangeEventOutbox
    from shared.messages.events import EVENT_TYPE_NGFW

    try:
        stored = RangeEventOutbox.objects.get(event_id=UUID(event["event_id"]), event_type=EVENT_TYPE_NGFW)
        if event.get("event_type") != EVENT_TYPE_NGFW or canonical_payload_digest(
            stored.payload
        ) != canonical_payload_digest(event):
            return None
        runtime = Instance.objects.select_related("request").get(uuid=UUID(event["instance_id"]), role="ngfw")
        if (
            runtime.status != event["status"]
            or str(runtime.request.request_id) != event["request_id"]
            or not runtime.apps.filter(uuid=UUID(event["app_id"]), app_type="ngfw", status=event["status"]).exists()
            or any(event.get(key) != value for key, value in device_status_authority_fields(runtime).items())
        ):
            return None
        return (runtime.request.request_id, runtime.uuid, runtime.request.owner_principal_uuid, device_scope(runtime))
    except Exception:
        return None


def admit_native_device_access(credential, identifier):
    from engine.models import Instance

    try:
        runtime = Instance.objects.select_related("request").get(
            apps__uuid=UUID(str(identifier)),
            apps__app_type="ngfw",
            role="ngfw",
            status="ready",
            deleted_at__isnull=True,
        )
        binding = device_binding_digest(runtime)
        current, _ = require_device_scope(credential, device_scope(runtime), "access_devices")
        latest = Instance.objects.select_related("request").get(pk=runtime.pk, status="ready", deleted_at__isnull=True)
        if binding != device_binding_digest(latest):
            raise ValueError("Device access changed")
        fields = admitted_device_authority(latest, current)
        fields["authority_action"] = fields["authority_action"].replace("manage_devices", "access_devices")
        fields["authority_resource_uuid"] = latest.uuid
        return fields
    except Exception:
        raise PermissionError("Device access unavailable") from None
