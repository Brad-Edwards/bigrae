"""Read-only queries: return dicts/shared-schema projections, not model instances."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError

from shared.remote_access import PrincipalCredentialRequired

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from shared.schemas.app import LinkedRangeContext


def get_authoritative_range_status(
    *,
    request_id: UUID | str | None = None,
    range_id: int | None = None,
) -> str | None:
    """Return the authoritative ``engine.Range.status`` for the given correlation key.

    Prefers ``request_id`` (new pattern) over ``range_id`` (legacy).  Returns
    ``None`` when no matching Range row exists.

    This is the canonical seam for cross-layer status reconciliation: CMS and
    other consumers must use this function instead of importing engine.models
    directly (ADR-001-R2).

    Args:
        request_id: UUID that links an engine Request to a CMS Request.
        range_id: Engine Range PK (legacy, used when request_id is absent).

    Returns:
        Status string (a ``shared.enums.ResourceStatus`` value) or ``None``.
    """
    from engine.models import Range

    if request_id is not None:
        eng = Range.objects.filter(request__request_id=request_id).first()
        if eng is not None:
            return eng.status

    if range_id is not None:
        eng = Range.objects.filter(pk=range_id).first()
        if eng is not None:
            return eng.status

    return None


def get_user_ready_range_instances(
    user_id: int,
    *,
    request_id: UUID | str,
    workspace_id: int,
) -> list[dict[str, Any]]:
    """Get provisioned instances for one correlated ready range.

    The CMS-selected request and workspace binding are both required so a
    caller cannot authorize one ready range and project another ready range
    selected only by user identity.
    """
    from engine.models import Range

    range_obj = Range.objects.filter(
        user_id=user_id,
        request__request_id=request_id,
        workspace_id=workspace_id,
        status=Range.Status.READY,
    ).first()
    if not range_obj or not range_obj.provisioned_instances:
        return []
    return list(range_obj.provisioned_instances)


def get_principal_ready_range_instances(
    owner_principal_uuid: UUID,
    *,
    request_id: UUID | str,
    cms_range_uuid: UUID,
    workspace_id: int | None,
) -> list[dict[str, Any]]:
    """Project one exact principal-owned ready range by both correlation keys."""
    from engine.models import Range

    range_obj = Range.objects.filter(
        owner_principal_uuid=owner_principal_uuid,
        request__request_id=request_id,
        cms_range_uuid=cms_range_uuid,
        workspace_id=workspace_id,
        status=Range.Status.READY,
    ).first()
    if not range_obj or not range_obj.provisioned_instances:
        return []
    return list(range_obj.provisioned_instances)


def get_ranges_for_ngfw(user_id: int, ngfw_request_id: UUID | str) -> list[LinkedRangeContext]:
    """Get active ranges linked to an NGFW, identified by its request_id.

    The NGFW is identified by ``ngfw_request_id`` — the provisioning Request UUID
    that the CMS and Engine share as their cross-layer correlation key. The
    Engine NGFW ``Instance`` is resolved from that request_id; ranges are linked
    to it via ``Range.ngfw_instance``. (Callers in other layers hold the CMS
    NGFW's request_id, not the Engine Instance's integer pk, so taking the
    correlation key keeps the Engine's internal ids out of the upper layers.)

    Returns a list of ``LinkedRangeContext`` projections (the shared schema the
    NGFW templates render) for each linked range. An unknown/unprovisioned NGFW
    yields an empty list.
    """
    from engine.models import Instance, Range
    from shared.schemas.app import LinkedRangeContext

    ngfw_instance = Instance.objects.filter(
        request__request_id=ngfw_request_id,
        role=Instance.Role.NGFW,
    ).first()
    if ngfw_instance is None:
        return []

    ranges = Range.objects.filter(
        ngfw_instance=ngfw_instance,
        user_id=user_id,
        destroyed_at__isnull=True,
    ).order_by("-created_at")

    return [LinkedRangeContext(range_id=r.pk, status=r.status, created_at=r.created_at) for r in ranges]


_INSTANCE_UNAVAILABLE = "Instance unavailable"


def _owned_legacy_instance_request_ref(user: User, instance_uuid: str) -> str | None:
    """Resolve incumbent range or device request custody without disclosing principal-owned resources."""
    from engine.models import Range

    user_id = getattr(user, "id", None)
    request_ref = None
    if user_id is not None and instance_uuid:
        range_obj = Range.resolve_active_for_instance(user, instance_uuid)
        if range_obj is not None and range_obj.request is not None:
            if range_obj.owner_principal_uuid is not None:
                raise PrincipalCredentialRequired(_INSTANCE_UNAVAILABLE)
            request_ref = str(range_obj.request.request_id)
        else:
            request_ref = _legacy_instance_row_request_ref(user_id, instance_uuid)
    return request_ref


def _legacy_instance_row_request_ref(user_id: int, instance_uuid: str) -> str | None:
    """Resolve only incumbent device request ownership, including detached rows."""
    from engine.models import Instance

    request_ref = None
    try:
        instance = (
            Instance.objects.select_related("request").filter(uuid=instance_uuid, request__user_id=user_id).first()
        )
    except (DjangoValidationError, ValueError):
        instance = None
    # ``request`` is nullable on the model, so an instance whose request
    # row was detached resolves to no ref rather than raising. A malformed
    # uuid (caught above) also resolves to no ref.
    if instance is not None and instance.request is not None:
        if instance.request.owner_principal_uuid is not None:
            raise PrincipalCredentialRequired(_INSTANCE_UNAVAILABLE)
        request_ref = str(instance.request.request_id)
    return request_ref
