"""CMS-owned device status uses its native request and resolved parent scope."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from cms.exceptions import CMSError
from cms.models import App
from workspaces.services import resolve_resource_scope, resource_scope_from_ids

from ._content_authority import require_content_scope

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet

    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope
    from shared.schemas.app import NGFWAppContext, NGFWAppRef


_DEVICE_UNAVAILABLE = "Device unavailable"


def _device_rows(*, for_destruction: bool = False) -> QuerySet[App]:
    """Select device request custody without exposing private runtime fields."""
    from django.db.models import Q

    rows = (
        App.all_objects.filter(
            app_type__slug="panw-ngfw",
            instance__instance_type__slug="panw-ngfw",
            instance__request__deleted_at__isnull=True,
            instance__request__request_type="ngfw",
            instance__request__owner_principal_uuid__isnull=False,
        )
        .exclude(instance__request__owner_principal_uuid=UUID(int=0))
        .select_related("instance", "instance__request")
    )
    active = Q(deleted_at__isnull=True, instance__deleted_at__isnull=True)
    if for_destruction:
        active |= Q(status__in=["failed", "destroying"], instance__status__in=["failed", "destroying"])
    return rows.filter(active)


def _request_scope(row: App) -> ResourceScope:
    """Resolve customer ancestry from the device owning request."""
    try:
        if row.instance is None:
            raise CMSError(_DEVICE_UNAVAILABLE)
        request = row.instance.request
        return resource_scope_from_ids(
            kind=request.scope_kind,
            account_id=request.account_id,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        )
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise CMSError(_DEVICE_UNAVAILABLE) from None


def list_scoped_devices(credential: CredentialContext, scope: ResourceScope | None) -> list[NGFWAppContext]:
    """List only device projections admitted by the current parent decision."""
    from ._ngfws import _app_to_ngfw_context

    if scope is None:
        raise CMSError("Content unavailable")
    require_content_scope(credential, scope, "read_devices")
    resolved = resolve_resource_scope(scope)
    rows = _device_rows().filter(
        instance__request__scope_kind=resolved.kind,
        instance__request__account_id=resolved.account_id,
        instance__request__organization_id=resolved.organization_id,
        instance__request__workspace_id=resolved.workspace_id,
    )
    return [_app_to_ngfw_context(row) for row in rows]


def get_scoped_device(credential: CredentialContext, identifier: UUID | str) -> NGFWAppContext:
    """Read one device after checking its persisted customer ancestry."""
    from ._ngfws import _app_to_ngfw_context

    try:
        snapshot = _device_rows().filter(pk=UUID(str(identifier))).first()
    except (ValueError, TypeError):
        raise CMSError(_DEVICE_UNAVAILABLE) from None
    if snapshot is None or snapshot.instance is None:
        raise CMSError(_DEVICE_UNAVAILABLE)
    scope = _request_scope(snapshot)
    if scope is None:
        raise CMSError("Content unavailable")
    require_content_scope(credential, scope, "read_devices")
    request = snapshot.instance.request
    row = (
        _device_rows()
        .filter(
            pk=snapshot.pk,
            instance_id=snapshot.instance.pk,
            instance__request_id=request.pk,
            instance__request__request_id=request.request_id,
            instance__request__owner_principal_uuid=request.owner_principal_uuid,
            instance__request__scope_kind=request.scope_kind,
            instance__request__account_id=request.account_id,
            instance__request__organization_id=request.organization_id,
            instance__request__workspace_id=request.workspace_id,
        )
        .first()
    )
    if row is None:
        raise CMSError(_DEVICE_UNAVAILABLE)
    return _app_to_ngfw_context(row)


def destroy_scoped_device(credential: CredentialContext, identifier: UUID | str, confirm_name: str) -> NGFWAppRef:
    """Admit native destruction of the exact scoped device identity."""
    from django.db import transaction
    from django.utils import timezone

    from engine.services import commit_ngfw_lifecycle, prepare_ngfw_lifecycle
    from shared.credentials import resolve_local_credential
    from shared.schemas.app import NGFWAppRef

    try:
        snapshot = _device_rows(for_destruction=True).get(pk=UUID(str(identifier)))
        if snapshot.instance is None:
            raise CMSError(_DEVICE_UNAVAILABLE)
        scope = _request_scope(snapshot)
        current = require_content_scope(credential, scope, "manage_devices")
        if confirm_name != snapshot.name:
            raise ValueError("Device confirmation does not match")
        prepared = prepare_ngfw_lifecycle(snapshot.instance.request.request_id, current, "deprovision")
        fields = dict(prepared[2])
        if (
            fields["authority_resource_uuid"] != snapshot.pk
            or fields["authority_owner_uuid"] != snapshot.instance.request.owner_principal_uuid
        ):
            raise ValueError("Device runtime binding changed")
        with transaction.atomic():
            row = _device_rows(for_destruction=True).select_for_update(of=("self",)).get(pk=snapshot.pk)
            if row.instance is None:
                raise CMSError(_DEVICE_UNAVAILABLE)
            if (
                _request_scope(row) != scope
                or row.name != confirm_name
                or row.instance_id != snapshot.instance_id
                or resolve_local_credential(current) != current
            ):
                raise ValueError("Device changed")
            commit_ngfw_lifecycle(prepared)
            now = timezone.now()
            row.status = row.instance.status = "destroying"
            row.deleted_at = row.instance.deleted_at = now
            row.save(update_fields=["status", "deleted_at"])
            row.instance.save(update_fields=["status", "deleted_at"])
        return NGFWAppRef(app_id=row.pk, instance_id=row.instance.pk, is_deleted=True)
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise CMSError("Device operation unavailable") from None


def admit_device_remote_authority(credential: CredentialContext, identifier: UUID | str) -> dict[str, object]:
    """Bind device access to the original credential and runtime custody."""
    from engine.services import admit_native_device_access

    try:
        snapshot = _device_rows().get(pk=UUID(str(identifier)), status="ready", instance__status="ready")
        if snapshot.instance is None:
            raise CMSError(_DEVICE_UNAVAILABLE)
        scope = _request_scope(snapshot)
        current = require_content_scope(credential, scope, "access_devices")
        fields = admit_native_device_access(current, identifier)
        if (
            fields["authority_resource_uuid"] != snapshot.instance.pk
            or fields["authority_owner_uuid"] != snapshot.instance.request.owner_principal_uuid
            or fields["authority_scope_kind"] != scope.kind
            or fields["authority_account_uuid"] != scope.account_uuid
            or fields["authority_organization_uuid"] != scope.organization_uuid
            or fields["authority_workspace_uuid"] != scope.workspace_uuid
        ):
            raise ValueError("Device projections changed")
        return fields
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise CMSError("Device access unavailable") from None
