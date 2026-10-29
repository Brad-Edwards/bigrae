"""Read-only range custody and bounded failure projections by request identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import User

    from engine.models import Range


def _vpn_custody_never_left(owner: User | None) -> bool:
    """Return whether ``owner`` could never have downloaded an OpenVPN profile.

    Profile delivery requires the authenticated owner. An inactive account with
    an unusable password -- a platform-managed spare or warm-pool identity --
    can never authenticate, so a binding it holds was never delivered.
    """
    return owner is not None and not owner.is_active and not owner.has_usable_password()


def range_owner_reassignment_available_by_request(request_id: UUID) -> bool:
    """Return whether ownership can move without leaving a client credential live."""
    from engine.models import Range

    range_obj = Range.objects.select_related("user").filter(request__request_id=request_id).first()
    return range_obj is not None and (
        principal_range_owner_reassignment_available(range_obj) or _vpn_custody_never_left(range_obj.user)
    )


def principal_range_owner_reassignment_available(range_obj: Range) -> bool:
    """Return whether principal-native VPN custody is still wholly server-side."""
    binding = range_obj.vpn_access_binding
    if binding is None:
        return True
    try:
        from engine.models import VpnAccessProfile
        from shared.remote_access import parse_openvpn_binding

        parsed = parse_openvpn_binding(binding)
        return (
            parsed.owner_principal_uuid == range_obj.owner_principal_uuid
            and not VpnAccessProfile.objects.filter(range=range_obj, revoked_at__isnull=True).exists()
        )
    except Exception:
        return False


def get_pinned_range_egress_mode_by_request(request_id: UUID) -> str | None:
    """Project the immutable launch posture for a trusted CTF spare claim."""
    from engine.models import Range

    return Range.objects.filter(request__request_id=request_id).values_list("egress_mode", flat=True).first()


def get_range_failure_reason_by_request(request_id: UUID) -> str:
    """The failure recorded for ``request_id``: reason code plus the bounded diagnostic.

    The range keeps only the closed reason code; the provisioner's authored,
    value-free diagnostic (MAX_DIAGNOSTIC_CHARS) rides the latest terminal-failed
    operation result. Empty when nothing failed.
    """
    from engine.models import OperationResultInbox, Range
    from shared.operation_results import ResultStep

    reason = Range.objects.filter(request__request_id=request_id).values_list("error_message", flat=True).first()
    envelope = (
        OperationResultInbox.objects.filter(request_id=request_id, result_step=ResultStep.RAES_TERMINAL_FAILED.value)
        .order_by("-created_at")
        .values_list("envelope", flat=True)
        .first()
    )
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    diagnostic = payload.get("diagnostic") if isinstance(payload, dict) else None
    parts = [part for part in (reason, diagnostic) if isinstance(part, str) and part]
    return ": ".join(parts)
