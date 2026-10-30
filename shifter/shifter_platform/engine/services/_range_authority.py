"""Engine-owned immutable scalar projection for common range authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import Range

from dataclasses import dataclass
from uuid import UUID

from shared.authorization import AuthorizationRequest, TargetRef, resolve_authorization_event_scope
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_credential_scope, resolve_current_credential
from shared.identity_scope import ResourceScope

from ._common import EngineError

_RANGE_UNAVAILABLE = "Range unavailable"


@dataclass(frozen=True, slots=True)
class RangeAuthorityProjection:
    """Correlation and business ownership, distinct from a permission grant."""

    native_id: int
    native_uuid: UUID
    public_uuid: UUID
    owner_principal_uuid: UUID
    request_uuid: UUID
    scope: ResourceScope
    parent: TargetRef
    resource_generation: UUID | None


def native_range_id_by_request(request_uuid: UUID) -> int:
    """Return the private runtime identifier for an already-created range."""
    from engine.models import Range

    try:
        return int(Range.objects.values_list("pk", flat=True).get(request__request_id=request_uuid))
    except (Range.DoesNotExist, Range.MultipleObjectsReturned):
        raise EngineError(_RANGE_UNAVAILABLE) from None


def has_range_runtime(request_uuid: UUID, public_uuid: UUID) -> bool:
    """Establish runtime presence independently of valid principal custody."""
    from django.db.models import Q

    from engine.models import Range

    return Range.objects.filter(Q(request__request_id=request_uuid) | Q(cms_range_uuid=public_uuid)).exists()


def project_range_authority(request_uuid: UUID) -> RangeAuthorityProjection:
    """Resolve only Engine's SQL facts, preserving existing runtime/public IDs."""
    from engine.models import Range
    from workspaces.services import resource_scope_from_ids

    try:
        row = Range.objects.select_related("request").filter(request__request_id=request_uuid).first()
        if (
            row is None
            or row.request is None
            or row.cms_range_uuid is None
            or row.owner_principal_uuid is None
            or row.request.owner_principal_uuid != row.owner_principal_uuid
        ):
            raise ValueError("Missing range binding")
        scope = resource_scope_from_ids(
            kind=row.scope_kind,
            account_id=row.account_id,
            organization_id=row.organization_id,
            workspace_id=row.workspace_id,
        )
        if scope.kind != "account":
            raise ValueError("Missing customer binding")
        parent = scope_parent(scope)
        if row.event_uuid is not None:
            if resolve_authorization_event_scope(row.event_uuid) != scope:
                raise ValueError("Invalid event binding")
            parent = TargetRef("event", row.event_uuid)
        return RangeAuthorityProjection(
            row.pk,
            row.uuid,
            row.cms_range_uuid,
            row.owner_principal_uuid,
            row.request.request_id,
            scope,
            parent,
            row.resource_generation,
        )
    except Exception:  # noqa: BLE001  # fail-closed range authority; only bounded denial escapes
        raise EngineError(_RANGE_UNAVAILABLE) from None


def lock_range_remote_authority(range_uuid: UUID, member: str) -> RangeAuthorityProjection:
    """Lock ready native custody and exact guest membership before handoff."""
    from django.db import transaction

    from engine.models import Range, Request

    if not transaction.get_connection().in_atomic_block:
        raise ValueError("Range binding requires a transaction")
    runtime = Range.objects.select_for_update().get(cms_range_uuid=range_uuid, status=Range.Status.READY)
    if runtime.request_id is None or not isinstance(runtime.provisioned_instances, list):
        raise ValueError("Range guest unavailable")
    runtime.request = Request.objects.select_for_update().get(pk=runtime.request_id)
    matches = [item for item in runtime.provisioned_instances if isinstance(item, dict) and item.get("uuid") == member]
    if len(matches) != 1:
        raise ValueError("Range guest changed")
    return project_range_authority(runtime.request.request_id)


def require_native_range_action(credential: CredentialContext, request_uuid: UUID, action: str) -> Range:
    """Enforce runtime authority independently of the CMS/HTTP caller."""
    from engine.models import Range

    try:
        current = resolve_current_credential(credential)
        projection = project_range_authority(request_uuid)
        target = TargetRef("range", projection.public_uuid)
        if resolve_credential_scope(target) != projection.scope:
            raise ValueError("Range projection changed")
        require_authorization(
            AuthorizationRequest(
                current.principal, action, target, projection.scope, current.ceiling, parent=projection.parent
            )
        )
        row = (
            Range.objects.select_related("request")
            .filter(
                pk=projection.native_id,
                uuid=projection.native_uuid,
                cms_range_uuid=projection.public_uuid,
                owner_principal_uuid=projection.owner_principal_uuid,
                request__request_id=request_uuid,
            )
            .first()
        )
        if row is None or project_range_authority(request_uuid) != projection:
            raise ValueError("Range projection changed")
        return row
    except Exception:  # noqa: BLE001  # fail-closed range authority; only bounded denial escapes
        raise PermissionError(_RANGE_UNAVAILABLE) from None


def authorized_instance_range(
    credential: CredentialContext, instance_uuid: str, *, range_uuid: UUID | None = None
) -> Range:
    """Resolve exactly one realized guest, then authorize before secrets or I/O."""
    from engine.models import Range

    try:
        if not isinstance(instance_uuid, str) or not instance_uuid:
            raise ValueError("Missing guest identifier")
        identifier = instance_uuid
        selected = {"cms_range_uuid": range_uuid} if range_uuid is not None else {}
        candidates = tuple(
            Range.objects.select_related("request")
            .filter(
                provisioned_instances__contains=[{"uuid": identifier}],
                status=Range.Status.READY,
                **selected,
            )
            .only("id", "request__request_id")[:2]
        )
        if len(candidates) != 1 or candidates[0].request is None:
            raise ValueError("Missing guest binding")
        return require_native_range_action(credential, candidates[0].request.request_id, "range.access")
    except Exception:  # noqa: BLE001  # fail-closed authority check; every failure is one bounded denial
        raise PermissionError("Instance unavailable") from None
