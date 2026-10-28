"""Owning target locks shared by launch admission and failure settlement."""

from uuid import UUID

from engine.models import Instance, Range, VpnAccessProfile


def lock_provisioner_target(payload: dict[str, object]) -> Range | Instance | VpnAccessProfile | None:
    """Lock the named target before its intent, inside the caller's transaction.

    A missing or malformed target cannot authorize an effect. Returning None
    still lets the worker settle a stale delivery lease without inventing state.
    Generation, custody and input checks remain with the owning validators.
    """
    resource = payload.get("resource")
    try:
        if resource == "vpn-profile":
            return (
                VpnAccessProfile.objects.select_for_update(of=("self",))
                .filter(pk=UUID(str(payload.get("profile_id"))))
                .first()
            )
        if resource not in {"range", "raes-range", "ngfw"}:
            return None
        if "request_id" in payload:
            request_id = UUID(str(payload["request_id"]))
            if resource == "ngfw":
                return (
                    Instance.objects.select_for_update(of=("self",))
                    .filter(request__request_id=request_id, role=Instance.Role.NGFW)
                    .first()
                )
            return Range.objects.select_for_update(of=("self",)).filter(request__request_id=request_id).first()
        return Range.objects.select_for_update(of=("self",)).filter(pk=int(str(payload.get("range_id")))).first()
    except (TypeError, ValueError):
        return None
