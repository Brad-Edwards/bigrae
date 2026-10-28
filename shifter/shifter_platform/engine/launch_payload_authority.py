"""Principal policy admission before the native provisioner generation fence."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction

from engine.models import Instance, ProvisionerLaunchIntent, Range

_OPERATION_AUTHORITY_UNAVAILABLE = "Operation authority unavailable"


def authorize_provisioner_payload(
    payload: dict[str, object],
    *,
    target: Range | Instance | None = None,
    expected_operation_id: UUID | str | None = None,
) -> None:
    """Fail closed unless current domain state authorizes the queued operation."""
    from engine.launch_intents import assert_provisioner_input_matches, validate_provisioner_generation

    if payload.get("resource") == "vpn-profile":
        from engine.services._vpn_profile_operations import authorize_vpn_operation

        authorize_vpn_operation(payload, expected_operation_id)
        return
    if expected_operation_id is not None:
        row = _resolve_runtime(payload, target)
        _authorize_recorded_principal(row, payload, expected_operation_id)
    with transaction.atomic():
        validate_provisioner_generation(payload, target=target, expected_operation_id=expected_operation_id)
        if expected_operation_id is not None:
            assert_provisioner_input_matches(payload, UUID(str(expected_operation_id)))


def _resolve_runtime(payload: dict[str, object], target: Range | Instance | None) -> Range | Instance | None:
    """Resolve only the native resource named by the existing command before policy admission."""
    row = target
    if row is None and payload.get("resource") in {"range", "raes-range"} and "request_id" in payload:
        row = (
            Range.objects.select_related("request").filter(request__request_id=UUID(str(payload["request_id"]))).first()
        )
    if row is None and payload.get("resource") == "ngfw" and "request_id" in payload:
        row = (
            Instance.objects.select_related("request")
            .filter(request__request_id=UUID(str(payload["request_id"])), role="ngfw")
            .first()
        )
    return row


def _principal_owned(row: Range | Instance | None) -> bool:
    """Distinguish principal custody from the incumbent human-only compatibility path."""
    principal_owned = (isinstance(row, Range) and row.owner_principal_uuid is not None) or (
        isinstance(row, Instance) and row.request is not None and row.request.owner_principal_uuid is not None
    )
    return principal_owned


def _authorize_recorded_principal(
    row: Range | Instance | None, payload: dict[str, object], expected_operation_id: UUID | str
) -> None:
    """Reauthorize the exact recorded initiating proof before taking the native generation fence."""
    from engine.services._launch_authority import authorize_launch_authority

    if row is None or not _principal_owned(row):
        return
    intent = ProvisionerLaunchIntent.objects.filter(operation_id=expected_operation_id).first()
    if intent is None or intent.payload != {key: value for key, value in payload.items() if key != "operation_id"}:
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    # Evaluate remote policy before acquiring the generation fence.
    authorize_launch_authority(intent, row)
