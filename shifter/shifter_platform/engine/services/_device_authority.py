"""Owning NGFW request authority composed into the existing operation outbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope
from shared.operation_authority import credential_reference_fields, credential_reference_from_record
from shared.operation_envelope import canonical_payload_digest

if TYPE_CHECKING:
    from typing import Any
    from uuid import UUID

    from engine.models import Instance, ProvisionerLaunchIntent, Request
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope
    from shared.schemas import RequestSpec


_DEVICE_AUTHORITY_UNAVAILABLE = "Device authority unavailable"


_DEVICE_CUSTODY_UNAVAILABLE = "Device custody unavailable"


_DEVICE_CALLBACK_UNAVAILABLE = "Device callback unavailable"


def require_device_scope(
    credential: CredentialContext, scope: ResourceScope, operation: str = "manage_devices"
) -> CredentialContext:
    """Authorize the device action through its persisted customer parent."""
    from workspaces.services import resolve_range_scope_binding

    current = resolve_current_credential(credential)
    resolve_range_scope_binding(scope)
    parent = scope_parent(scope)
    require_authorization(
        AuthorizationRequest(current.principal, f"{parent.type}.{operation}", parent, scope, current.ceiling)
    )
    if resolve_current_credential(current) != current:
        raise ValueError("Device authority changed")
    return current


@dataclass(frozen=True)
class PreparedDeviceCreation:
    """Frozen device input, scope and original creation admission."""

    credential: CredentialContext
    scope: ResourceScope
    request_id: UUID
    spec_digest: str


def prepare_ngfw_creation(
    request_spec: RequestSpec, credential: CredentialContext, resource_scope: ResourceScope
) -> PreparedDeviceCreation:
    """Admit device creation before taking native persistence locks."""
    from engine.ecs._config import _get_engine_task_config
    from management.services import human_principal_user_id
    from workspaces.services import resolve_range_scope_binding

    if _get_engine_task_config() is None or resource_scope.kind != "account":
        raise ValueError("Device provisioner unavailable")
    current = require_device_scope(credential, resource_scope)
    resolve_range_scope_binding(resource_scope, member=current.principal)
    user_id = human_principal_user_id(current.principal) if current.principal.kind == "human" else None
    if request_spec.user_id != user_id or request_spec.owner_principal_uuid != current.principal.uuid:
        raise ValueError("Device ownership conflicts")
    return PreparedDeviceCreation(
        current, resource_scope, request_spec.request_id, canonical_payload_digest(request_spec.model_dump(mode="json"))
    )


def assert_device_creation(
    prepared: PreparedDeviceCreation, request_spec: RequestSpec, credential: CredentialContext, scope: ResourceScope
) -> None:
    """Fence prepared device input and original scope before persistence."""
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


def _device_request(runtime: Instance) -> Request:
    """Require native device custody before reading request fields."""
    request = runtime.request
    if request is None or request.request_type != "ngfw" or request.owner_principal_uuid is None:
        raise ValueError(_DEVICE_CUSTODY_UNAVAILABLE)
    return request


def device_scope(runtime: Instance) -> ResourceScope:
    """Resolve the native request customer ancestry without a creator fallback."""
    from workspaces.services import resource_scope_from_ids

    request = _device_request(runtime)
    return resource_scope_from_ids(
        kind=request.scope_kind,
        account_id=request.account_id,
        organization_id=request.organization_id,
        workspace_id=request.workspace_id,
    )


def device_binding_digest(runtime: Instance) -> str:
    """Pin native device identity, ownership and resolved customer custody."""
    request = _device_request(runtime)
    scope = device_scope(runtime)
    return canonical_payload_digest(
        {
            "request_id": str(request.request_id),
            "instance_uuid": str(runtime.uuid),
            "owner_uuid": str(request.owner_principal_uuid),
            "scope": str(scope),
            "spec": runtime.spec,
            "apps": [
                {"uuid": str(row.uuid), "spec": row.spec}
                for row in runtime.apps.filter(app_type="ngfw").order_by("uuid")
            ],
        }
    )


def admitted_device_authority(runtime: Instance, credential: CredentialContext) -> dict[str, Any]:
    """Bind a previously evaluated parent decision to native SQL custody."""
    return {**credential_reference_fields(credential), **_device_authority_fields(runtime)}


def _device_authority_fields(runtime: Instance) -> dict[str, Any]:
    """Project native custody without evaluating or granting application policy."""
    request = _device_request(runtime)
    scope = device_scope(runtime)
    parent = scope_parent(scope)
    apps = tuple(runtime.apps.filter(app_type="ngfw")[:2])
    if len(apps) != 1:
        raise ValueError("Device identity unavailable")
    return {
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
        "authority_owner_uuid": request.owner_principal_uuid,
        "authority_resource_uuid": apps[0].uuid,
        "authority_binding_digest": device_binding_digest(runtime),
    }


def prepare_device_authority(payload: dict[str, Any], credential: CredentialContext | None) -> dict[str, Any]:
    """Resolve the original credential and admit the exact native device effect."""
    from engine.models import Instance

    runtime = (
        Instance.objects.select_related("request")
        .filter(request__request_id=str(payload.get("request_id", "")), role="ngfw")
        .first()
    )
    if runtime is None or runtime.request is None or runtime.request.owner_principal_uuid is None:
        if credential is not None:
            raise PermissionError(_DEVICE_AUTHORITY_UNAVAILABLE)
        return {}
    if credential is None:
        raise PermissionError(_DEVICE_AUTHORITY_UNAVAILABLE)
    current = require_device_scope(credential, device_scope(runtime))
    return admitted_device_authority(runtime, current)


def assert_device_authority_binding(intent: ProvisionerLaunchIntent, runtime: Instance) -> None:
    """Reject stale device custody before committing a queued effect."""
    if intent.authority_binding_digest != device_binding_digest(runtime):
        raise ValueError("Device custody changed")
    if intent.authority_kind == "cleanup":
        if intent.payload.get("resource") != "ngfw" or intent.payload.get("operation") != "deprovision":
            raise ValueError("Device cleanup operation changed")
        assert_device_cleanup_origin(intent.cleanup_origin_operation_uuid, runtime)
    elif intent.authority_kind != "principal":
        raise ValueError(_DEVICE_AUTHORITY_UNAVAILABLE)


def authorize_device_authority(intent: ProvisionerLaunchIntent, runtime: Instance) -> None:
    """Reauthorize recorded device work without widening its original ceiling."""
    try:
        assert_device_authority_binding(intent, runtime)
        if intent.authority_kind == "cleanup":
            return
        current = resolve_current_credential(credential_reference_from_record(intent))
        require_device_scope(current, device_scope(runtime))
    except Exception:
        raise PermissionError(_DEVICE_AUTHORITY_UNAVAILABLE) from None


def assert_device_cleanup_origin(
    origin_id: UUID | None, runtime: Instance, *, require_absence: bool = True
) -> ProvisionerLaunchIntent:
    """Only a terminal, already-authorized teardown permits bounded system replay."""
    from engine.models import InterruptState, ProvisionerLaunchIntent

    if origin_id is None or runtime.request is None:
        raise ValueError("Device cleanup origin unavailable")
    origin = ProvisionerLaunchIntent.objects.get(operation_id=origin_id)
    if (origin.authority_kind, origin.payload) != (
        "principal",
        {"version": 1, "resource": "ngfw", "operation": "deprovision", "request_id": str(runtime.request.request_id)},
    ) or origin.authority_binding_digest != device_binding_digest(runtime):
        raise ValueError("Device cleanup origin unavailable")
    if require_absence and origin.interrupt_state not in {
        InterruptState.TERMINAL_ABSENT,
        InterruptState.DESTROY_ENQUEUED,
    }:
        raise ValueError("Device cleanup task absence unverified")
    _assert_device_cleanup_generation(runtime, origin_id)
    return origin


def _assert_device_cleanup_generation(runtime: Instance, origin_id: UUID) -> None:
    """Only the original task or its recorded cleanup may own this device generation."""
    from engine.models import ProvisionerLaunchIntent

    if runtime.provisioner_operation_id != origin_id:
        if runtime.provisioner_operation_id is None:
            raise ValueError("Device cleanup generation unavailable")
        current = ProvisionerLaunchIntent.objects.filter(operation_id=runtime.provisioner_operation_id).first()
        if current is None or (current.authority_kind, current.cleanup_origin_operation_uuid) != ("cleanup", origin_id):
            raise ValueError("Device cleanup generation changed")


def prepare_device_cleanup(payload: dict[str, Any], origin_id: UUID) -> dict[str, Any]:
    """Bind required destruction to verified native device cleanup evidence."""
    from django.db import transaction

    from engine.ecs import interrupt_provisioner_task
    from engine.launch_intents import command_from_payload
    from engine.launch_interrupt import _request_intent_interrupt
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


def device_status_authority_fields(runtime: Instance) -> dict[str, str]:
    """Project accepted native device custody for the owning CMS callback."""
    if runtime.request is None or runtime.request.owner_principal_uuid is None:
        return {}
    return {
        "owner_principal_uuid": str(runtime.request.owner_principal_uuid),
        "operation_id": str(runtime.provisioner_operation_id),
        "authority_binding_digest": device_binding_digest(runtime),
    }


def verified_device_status_projection(event: dict[str, Any]) -> tuple[UUID, UUID, UUID, ResourceScope] | None:
    """Verify the recorded callback before disclosing native device custody."""
    from engine.models import Instance, RangeEventOutbox
    from shared.messages.events import EVENT_TYPE_NGFW

    try:
        stored = RangeEventOutbox.objects.get(event_id=UUID(event["event_id"]), event_type=EVENT_TYPE_NGFW)
        if event.get("event_type") != EVENT_TYPE_NGFW or canonical_payload_digest(
            stored.payload
        ) != canonical_payload_digest(event):
            raise ValueError(_DEVICE_CALLBACK_UNAVAILABLE)
        runtime = Instance.objects.select_related("request").get(uuid=UUID(event["instance_id"]), role="ngfw")
        if runtime.request is None or runtime.request.owner_principal_uuid is None:
            raise ValueError(_DEVICE_CALLBACK_UNAVAILABLE)
        if (
            (runtime.status, str(runtime.request.request_id)) != (event["status"], event["request_id"])
            or not runtime.apps.filter(uuid=UUID(event["app_id"]), app_type="ngfw", status=event["status"]).exists()
            or any(event.get(key) != value for key, value in device_status_authority_fields(runtime).items())
        ):
            raise ValueError(_DEVICE_CALLBACK_UNAVAILABLE)
        return (runtime.request.request_id, runtime.uuid, runtime.request.owner_principal_uuid, device_scope(runtime))
    except Exception:
        return None


def admit_native_device_access(credential: CredentialContext, identifier: UUID | str) -> dict[str, Any]:
    """Authorize the device before loading connection material or opening transport."""
    from engine.models import Instance

    try:
        runtime = Instance.objects.select_related("request").get(
            apps__uuid=UUID(str(identifier)),
            apps__app_type="ngfw",
            apps__status="ready",
            apps__deleted_at__isnull=True,
            role="ngfw",
            status="ready",
            deleted_at__isnull=True,
        )
        binding = device_binding_digest(runtime)
        current = require_device_scope(credential, device_scope(runtime), "access_devices")
        latest = Instance.objects.select_related("request").get(pk=runtime.pk, status="ready", deleted_at__isnull=True)
        if binding != device_binding_digest(latest):
            raise ValueError("Device access changed")
        return {**credential_reference_fields(current), **_device_remote_authority_fields(latest)}
    except Exception:
        raise PermissionError("Device access unavailable") from None


def _device_remote_authority_fields(runtime: Instance) -> dict[str, Any]:
    fields = _device_authority_fields(runtime)
    fields["authority_action"] = fields["authority_action"].replace("manage_devices", "access_devices")
    fields["authority_resource_uuid"] = runtime.uuid
    return fields


def lock_native_device_access_binding(identifier: UUID | str) -> dict[str, Any]:
    """Lock and project exact native remote custody, without a policy decision."""
    from django.db import transaction

    from engine.models import App, Instance, Request

    if not transaction.get_connection().in_atomic_block:
        raise ValueError("Device binding requires a transaction")
    runtime = Instance.objects.select_for_update(of=("self",)).get(
        apps__uuid=UUID(str(identifier)),
        apps__app_type="ngfw",
        apps__status="ready",
        apps__deleted_at__isnull=True,
        role="ngfw",
        status="ready",
        deleted_at__isnull=True,
    )
    if runtime.request_id is None:
        raise ValueError("Device request unavailable")
    runtime.request = Request.objects.select_for_update().get(pk=runtime.request_id)
    App.objects.select_for_update().get(
        instance=runtime, uuid=UUID(str(identifier)), app_type="ngfw", status="ready", deleted_at__isnull=True
    )
    return _device_remote_authority_fields(runtime)
