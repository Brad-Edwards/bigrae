"""VPN certificate operations through the incumbent provisioner outbox/input."""

from __future__ import annotations

import os
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shared.db import RecordedAuthorityFields
from shared.operation_authority import credential_reference_from_record
from shared.operation_envelope import build_operation_envelope

if TYPE_CHECKING:
    from typing import Any
    from uuid import UUID

    from engine.models import ProvisionerLaunchIntent, VpnAccessProfile


_VPN_CLEANUP_UNAVAILABLE = "VPN cleanup unavailable"


def validate_vpn_command(command: list[str]) -> dict[str, Any]:
    """Parse only the supported native PKI command and canonical identifiers."""
    if (
        len(command) != 6
        or command[0] != "vpn-profile"
        or command[1] not in {"issue", "revoke", "refresh-crl", "purge"}
        or command[2] != "--request-id"
        or command[4] != "--profile-id"
    ):
        raise ValueError("VPN operation unavailable")
    return {
        "version": 1,
        "resource": "vpn-profile",
        "operation": command[1],
        "request_id": str(UUID(command[3])),
        "profile_id": str(UUID(command[5])),
    }


def _vpn_operation_workload(payload: dict[str, Any], operation_id: UUID | str, intent_id: UUID) -> dict[str, object]:
    """Bind a PKI operation to its deterministic installed native workload."""
    from engine.ecs._workload import provisioner_workload
    from engine.launch_intents import command_from_payload

    return provisioner_workload(command_from_payload({**payload, "operation_id": str(operation_id)}), str(intent_id))


def queue_vpn_operation(profile: VpnAccessProfile, operation: str) -> None:
    """Commit a reference-only immutable input with the existing launch intent."""
    from engine.models import OperationInput, ProvisionerLaunchIntent, VpnAccessProfile, VpnCrlState

    from ._launch_authority import authority_envelope_fields

    with transaction.atomic():
        if operation == "purge":
            assert_vpn_revocation_published(profile)
        crl, _created = VpnCrlState.objects.get_or_create(pk=1)
        crl = VpnCrlState.objects.select_for_update().get(pk=1)
        crl.requested_revision += 1
        crl.save(update_fields=["requested_revision"])
        operation_id, intent_id = uuid4(), uuid4()
        fields = {field.name: getattr(profile, field.name) for field in RecordedAuthorityFields._meta.fields}
        if operation != "issue":
            fields["authority_kind"] = "cleanup"
        fields["cleanup_origin_operation_uuid"] = profile.issuance_operation_id if operation != "issue" else None
        payload = {
            "version": 1,
            "resource": "vpn-profile",
            "operation": operation,
            "request_id": str(profile.generation),
            "profile_id": str(profile.pk),
        }
        workload = _vpn_operation_workload(payload, operation_id, intent_id)
        task_ref = f"{workload['namespace']}/{workload['job_name']}"
        intent = ProvisionerLaunchIntent.objects.create(
            operation_id=operation_id,
            intent_id=intent_id,
            idempotency_key=sha256(f"vpn-profile:{profile.pk}:{operation_id}".encode()).hexdigest(),
            payload=payload,
            task_ref=task_ref,
            next_attempt_at=timezone.now(),
            executing_workload=workload,
            **fields,
        )
        revocations = [
            {"serial": row.certificate_serial, "revoked_at": row.revoked_at.isoformat()}
            for row in VpnAccessProfile.objects.filter(revoked_at__isnull=False, expires_at__gt=timezone.now())
            .exclude(certificate_serial="")
            .order_by("certificate_serial")
            if row.revoked_at is not None
        ]
        if len(revocations) > 4096:
            raise ValueError("VPN CRL capacity unavailable")
        input_payload = {
            "profile_id": str(profile.pk),
            "generation": str(profile.generation),
            "expires_at": profile.expires_at.isoformat(),
            "crl_issued_at": timezone.now().isoformat(),
            "crl_revision": crl.requested_revision,
            "revocations": revocations,
            "issuer_fingerprint": crl.issuer_fingerprint,
            "certificate_serial": profile.certificate_serial,
            "secret_ref": profile.secret_ref,
        }
        if operation == "issue":
            environment = settings.ENVIRONMENT
            input_payload.update(
                storage_project_id=str(getattr(settings, "GCP_DYNAMIC_SECRET_PROJECT_ID", "") or ""),
                storage_environment=str(getattr(environment, "value", environment)),
                issuer_secret_ref=os.environ.get("RANGE_OPENVPN_ISSUER_SECRET_ID", ""),
            )
        else:
            if profile.issuance_operation_id is None:
                raise ValueError("VPN issuance unavailable")
            original = OperationInput.objects.get(operation_id=profile.issuance_operation_id).envelope["payload"]
            input_payload.update(
                {
                    key: original.get(key, "")
                    for key in ("storage_project_id", "storage_environment", "issuer_secret_ref")
                }
            )
        envelope = build_operation_envelope(
            operation_id=operation_id,
            request_id=profile.generation,
            resource="vpn-profile",
            operation=operation,
            payload=input_payload,
            **authority_envelope_fields(intent),
        )
        OperationInput.objects.create(
            operation_id=operation_id,
            request_id=profile.generation,
            resource="vpn-profile",
            operation=operation,
            contract_version=envelope["contract_version"],
            envelope=envelope,
        )
        profile.provisioner_operation_id = operation_id
        profile.provisioner_operation = operation
        if operation == "issue":
            profile.issuance_operation_id = operation_id
        profile.save(
            update_fields=["provisioner_operation_id", "provisioner_operation", "issuance_operation_id", "updated_at"]
        )
        _audit_queued_operation(profile, operation, crl.requested_revision)


def validate_vpn_operation(
    payload: dict[str, Any], operation_id: UUID | str | None, *, receipt: bool = False
) -> VpnAccessProfile:
    """Verify the exact recorded PKI generation, authority and immutable input."""
    from engine.models import VpnAccessProfile

    profile = VpnAccessProfile.objects.select_related("range__request").get(pk=UUID(payload["profile_id"]))
    if str(profile.generation) != payload["request_id"] or (
        operation_id is not None and profile.provisioner_operation_id != UUID(str(operation_id))
    ):
        raise ValueError("VPN operation generation changed")
    intent = None
    if operation_id is not None:
        intent = _recorded_vpn_intent(profile, payload, operation_id)
    _assert_vpn_profile_state(profile, payload["operation"], receipt=receipt)
    if payload["operation"] != "issue":
        _validate_cleanup_origin(profile, intent, payload["operation"])
    return profile


def _validate_cleanup_origin(profile: VpnAccessProfile, intent: ProvisionerLaunchIntent | None, operation: str) -> None:
    """Require the recorded issuance and native interruption for PKI cleanup."""
    from engine.models import OperationInput, ProvisionerLaunchIntent

    from ._launch_authority import assert_input_authority_binding

    if profile.issuance_operation_id is None or intent is None:
        raise ValueError(_VPN_CLEANUP_UNAVAILABLE)
    origin = ProvisionerLaunchIntent.objects.get(operation_id=profile.issuance_operation_id)
    original_input = OperationInput.objects.get(operation_id=origin.operation_id)
    assert_input_authority_binding(original_input, origin)
    if (
        origin.authority_kind,
        origin.payload.get("resource"),
        origin.payload.get("operation"),
        origin.payload.get("profile_id"),
        origin.payload.get("request_id"),
        intent.cleanup_origin_operation_uuid,
    ) != ("principal", "vpn-profile", "issue", str(profile.pk), str(profile.generation), origin.operation_id):
        raise ValueError("VPN cleanup origin changed")
    if any(
        getattr(profile, field.name) != getattr(origin, field.name) for field in RecordedAuthorityFields._meta.fields
    ):
        raise ValueError("VPN cleanup authority changed")
    if operation in {"revoke", "purge"}:
        from engine.models import InterruptState

        if profile.revoked_at is None or origin.interrupt_state != InterruptState.DESTROY_ENQUEUED:
            raise ValueError("VPN issuer termination unconfirmed")
    if operation == "purge":
        assert_vpn_revocation_published(profile)


def assert_vpn_revocation_published(profile: VpnAccessProfile) -> None:
    """Private material can disappear only after its accepted public revocation."""
    from cryptography import x509

    from engine.models import OperationResultInbox, VpnCrlState

    if profile.revoked_at is None or profile.revocation_operation_id is None:
        raise ValueError("VPN revocation unconfirmed")
    if not OperationResultInbox.objects.filter(
        operation_id=profile.revocation_operation_id,
        request_id=profile.generation,
        resource="vpn-profile",
        operation="revoke",
        disposition="APPLIED",
        envelope__payload__profile_id=str(profile.pk),
    ).exists():
        raise ValueError("VPN revocation unconfirmed")
    if profile.expires_at <= timezone.now() or not profile.certificate_serial:
        return
    state = VpnCrlState.objects.filter(pk=1).first()
    if state is None or state.issuer_fingerprint != profile.issuer_fingerprint or not state.pem:
        raise ValueError("VPN revocation unpublished")
    crl = x509.load_pem_x509_crl(state.pem.encode())
    if int(profile.certificate_serial) not in {entry.serial_number for entry in crl}:
        raise ValueError("VPN revocation unpublished")


def authorize_vpn_operation(payload: dict[str, Any], operation_id: UUID | str | None) -> None:
    """Admit ordinary issuance or its bounded recorded native cleanup."""
    from ._vpn_profiles import admit_vpn_profile_authority

    try:
        profile = validate_vpn_operation(payload, operation_id)
        if payload["operation"] == "issue":
            fields = admit_vpn_profile_authority(
                credential_reference_from_record(profile),
                profile.generation,
                participation=profile.authority_action == "event.participate",
            )
            if any(getattr(profile, name) != value for name, value in fields.items()):
                raise ValueError("VPN credential changed")
    except Exception:
        raise PermissionError("VPN operation unavailable") from None


def enqueue_interrupted_vpn_cleanup(origin: ProvisionerLaunchIntent) -> bool:
    """Called only after the native interrupt observes terminal worker absence."""

    operation = origin.payload.get("operation")
    if operation in {"revoke", "purge", "refresh-crl"}:
        return _enqueue_interrupted_maintenance(origin, operation)
    if operation != "issue":
        return False
    return _enqueue_interrupted_issuance(origin)


def _audit_queued_operation(profile: VpnAccessProfile, operation: str, revision: int) -> None:
    """Record the original principal and the queued public CRL revision in the same transaction."""
    from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields

    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.RANGE,
            entity_id=profile.range_id,
            action=AuditAction.UPDATE,
            **principal_actor_fields(credential_reference_from_record(profile).principal),
            new_state={
                "profile_uuid": str(profile.pk),
                "operation": operation,
                "crl_revision": revision,
            },
            context="range_vpn_pki_queued",
            request_id=str(profile.generation),
        ),
        strict=True,
    )


def _recorded_vpn_intent(
    profile: VpnAccessProfile, payload: dict[str, Any], operation_id: UUID | str
) -> ProvisionerLaunchIntent:
    """Fence recorded command, initiating authority and immutable input before admission."""
    from engine.models import OperationInput, ProvisionerLaunchIntent

    from ._launch_authority import assert_input_authority_binding

    intent = ProvisionerLaunchIntent.objects.get(operation_id=operation_id)
    stored = OperationInput.objects.get(operation_id=operation_id)
    if intent.payload != payload or profile.provisioner_operation != payload["operation"]:
        raise ValueError("VPN operation changed")
    assert_input_authority_binding(stored, intent)
    for field in RecordedAuthorityFields._meta.fields:
        if field.name != "authority_kind" and getattr(profile, field.name) != getattr(intent, field.name):
            raise ValueError("VPN operation authority changed")
    if stored.envelope["payload"]["profile_id"] != str(profile.pk):
        raise ValueError("VPN input changed")
    return intent


def _enqueue_interrupted_maintenance(origin: ProvisionerLaunchIntent, operation: str) -> bool:
    """Replace terminated maintenance once and preserve replay idempotency."""
    from engine.models import VpnAccessProfile

    with transaction.atomic():
        profile = (
            VpnAccessProfile.objects.select_for_update()
            .filter(pk=origin.payload.get("profile_id"), generation=origin.payload.get("request_id"))
            .first()
        )
        if profile is None:
            return False
        if profile.provisioner_operation_id == origin.operation_id:
            if profile.provisioner_operation != operation:
                return False
            replacement = (
                "revoke"
                if operation == "refresh-crl"
                and profile.revoked_at is not None
                and profile.revocation_operation_id is None
                else operation
            )
            queue_vpn_operation(profile, replacement)
        # A replay after queue commit must not mint another generation.
        return True


def _enqueue_interrupted_issuance(origin: ProvisionerLaunchIntent) -> bool:
    """Queue revocation only after the admitted issuer has reached verified native absence."""
    from engine.models import VpnAccessProfile

    with transaction.atomic():
        profile = VpnAccessProfile.objects.select_for_update().filter(issuance_operation_id=origin.operation_id).first()
        if profile is None or profile.revoked_at is None or origin.payload.get("profile_id") != str(profile.pk):
            return False
        if profile.provisioner_operation == "refresh-crl":
            from engine.launch_interrupt import _request_intent_interrupt
            from engine.models import ProvisionerLaunchIntent

            if profile.provisioner_operation_id is None:
                raise ValueError(_VPN_CLEANUP_UNAVAILABLE)
            maintenance = ProvisionerLaunchIntent.objects.select_for_update().get(
                operation_id=profile.provisioner_operation_id
            )
            _request_intent_interrupt(maintenance)
        elif profile.provisioner_operation not in {"revoke", "purge"}:
            queue_vpn_operation(profile, "revoke")
        return True


def _assert_vpn_profile_state(profile: VpnAccessProfile, operation: str, *, receipt: bool) -> None:
    """Admit live issuance or retained cleanup evidence without widening the operation kind."""
    from ._vpn_profiles import assert_profile_runtime

    if operation == "issue" and not receipt:
        assert_profile_runtime(profile, pending=True)
    elif profile.issuance_operation_id is None or (operation == "revoke" and profile.revoked_at is None):
        raise ValueError(_VPN_CLEANUP_UNAVAILABLE)
