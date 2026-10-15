"""Principal NGFW effects retain native interpretation, operation inputs and audit."""

from django.db import transaction

from shared.audit import AuditEntityType, AuditEvent, audit_log, principal_actor_fields

from ._device_authority import admitted_device_authority, assert_device_creation, prepare_ngfw_creation


def create_principal_ngfw(request_spec, credential, scope, prepared):
    from engine.interpreter import interpret
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Instance, Request

    prepared = prepared or prepare_ngfw_creation(request_spec, credential, scope)
    if (
        len(request_spec.items) != 1
        or request_spec.items[0].role != "ngfw"
        or request_spec.items[0].ngfw_app is None
        or not request_spec.items[0].ngfw_app.is_hydrated
    ):
        raise ValueError("Device specification unavailable")
    with transaction.atomic():
        assert_device_creation(prepared, request_spec, credential, scope)
        existing = Request.objects.filter(request_id=request_spec.request_id).first()
        if existing is not None:
            raise ValueError("Device creation already recorded")
        request = interpret(request_spec, principal=credential.principal, resource_scope=scope)
        runtime = Instance.objects.select_related("request").get(request=request, role="ngfw")
        runtime.status = "provisioning"
        runtime.save(update_fields=["status", "updated_at"])
        runtime.apps.update(status="provisioning")
        authority = admitted_device_authority(runtime, credential)
        payload = {"version": 1, "resource": "ngfw", "operation": "provision", "request_id": str(request.request_id)}
        identifier = _enqueue_admitted_provisioner_launch(
            ["ngfw", "provision", "--request-id", str(request.request_id)], payload, authority
        )
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.NGFW,
                entity_id=runtime.pk,
                entity_ref=str(authority["authority_resource_uuid"]),
                action="provision",
                **principal_actor_fields(credential.principal),
                request_id=str(request.request_id),
                new_state={"status": "provisioning", "launch_intent": identifier},
            ),
            strict=True,
        )
        return request.request_id


def prepare_ngfw_lifecycle(request_id, credential, operation):
    from engine.models import Instance, Range

    from ._device_authority import prepare_device_authority

    if operation not in {"deprovision", "start", "stop"}:
        raise ValueError("Device operation unavailable")
    runtime = Instance.objects.select_related("request").get(request__request_id=request_id, role="ngfw")
    if (
        operation == "deprovision"
        and Range.objects.filter(ngfw_instance=runtime).exclude(status__in=["failed", "destroyed"]).exists()
    ):
        raise ValueError("Device still has attached ranges")
    payload = {"version": 1, "resource": "ngfw", "operation": operation, "request_id": str(request_id)}
    fields = prepare_device_authority(payload, credential)
    if not fields:
        raise PermissionError("Device authority unavailable")
    return (runtime.pk, runtime.status, tuple(fields.items()), credential, payload)


def commit_ngfw_lifecycle(prepared):
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Instance, Range

    from ._launch_authority import assert_admitted_launch_binding

    native_id, status, authority, credential, payload = prepared
    fields = dict(authority)
    operation = payload["operation"]
    with transaction.atomic():
        runtime = Instance.objects.select_for_update(of=("self",)).select_related("request").get(pk=native_id)
        assert_admitted_launch_binding(fields, runtime)
        if runtime.status != status:
            raise ValueError("Device state changed")
        allowed = {
            "deprovision": {"ready", "paused", "failed", "destroying"},
            "start": {"paused", "failed"},
            "stop": {"ready"},
        }
        if status not in allowed[operation]:
            raise ValueError("Device state unavailable")
        if operation == "deprovision":
            if Range.objects.filter(ngfw_instance=runtime).exclude(status__in=["failed", "destroyed"]).exists():
                raise ValueError("Device still has attached ranges")
            runtime.status = "destroying"
            runtime.save(update_fields=["status", "updated_at"])
            runtime.apps.update(status="destroying")
        identifier = _enqueue_admitted_provisioner_launch(
            ["ngfw", operation, "--request-id", str(runtime.request.request_id)], payload, fields
        )
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.NGFW,
                entity_id=runtime.pk,
                entity_ref=str(fields["authority_resource_uuid"]),
                action={"start": "resume", "stop": "pause", "deprovision": "deprovision"}[operation],
                **principal_actor_fields(credential.principal),
                request_id=str(runtime.request.request_id),
                new_state={"launch_intent": identifier},
            ),
            strict=True,
        )
    return True


def connect_principal_device_terminal(credential, identifier):
    from engine.models import Instance
    from engine.ssh import SSHConnection

    from ._device_authority import admit_native_device_access
    from ._terminal import _get_ssh_key, _resolve_ngfw_management_ip, _resolve_ngfw_ssh_key_secret_ref

    fields = admit_native_device_access(credential, identifier)
    runtime = Instance.objects.get(uuid=fields["authority_resource_uuid"], status="ready")
    host = _resolve_ngfw_management_ip(runtime.state or {})
    reference = _resolve_ngfw_ssh_key_secret_ref(runtime.state or {})
    if not host or not reference:
        raise ValueError("Device connection unavailable")
    key = _get_ssh_key(reference)
    if fields != admit_native_device_access(credential, identifier):
        raise PermissionError("Device connection changed")
    return SSHConnection(host=host, username="admin", private_key=key, port=22, session_id=None)
