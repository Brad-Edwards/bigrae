"""CMS-owned device status uses its native request and resolved parent scope."""

from uuid import UUID

from cms.exceptions import CMSError
from cms.models import App
from workspaces.services import resolve_resource_scope, resource_scope_from_ids

from ._content_authority import require_content_scope


def _device_rows(*, for_destruction=False):
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


def _request_scope(row):
    try:
        request = row.instance.request
        return resource_scope_from_ids(
            kind=request.scope_kind,
            account_id=request.account_id,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        )
    except Exception:
        raise CMSError("Device unavailable") from None


def list_scoped_devices(credential, scope):
    from ._ngfws import _app_to_ngfw_context

    require_content_scope(credential, scope, "read_devices")
    resolved = resolve_resource_scope(scope)
    rows = _device_rows().filter(
        instance__request__scope_kind=resolved.kind,
        instance__request__account_id=resolved.account_id,
        instance__request__organization_id=resolved.organization_id,
        instance__request__workspace_id=resolved.workspace_id,
    )
    return [_app_to_ngfw_context(row) for row in rows]


def get_scoped_device(credential, identifier):
    from ._ngfws import _app_to_ngfw_context

    try:
        snapshot = _device_rows().filter(pk=UUID(str(identifier))).first()
    except (ValueError, TypeError):
        raise CMSError("Device unavailable") from None
    if snapshot is None:
        raise CMSError("Device unavailable")
    scope = _request_scope(snapshot)
    require_content_scope(credential, scope, "read_devices")
    request = snapshot.instance.request
    row = (
        _device_rows()
        .filter(
            pk=snapshot.pk,
            instance_id=snapshot.instance_id,
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
        raise CMSError("Device unavailable")
    return _app_to_ngfw_context(row)


def destroy_scoped_device(credential, identifier, confirm_name):
    from django.db import transaction
    from django.utils import timezone

    from engine.services import commit_ngfw_lifecycle, prepare_ngfw_lifecycle
    from shared.credentials import resolve_local_credential
    from shared.schemas.app import NGFWAppRef

    try:
        snapshot = _device_rows(for_destruction=True).get(pk=UUID(str(identifier)))
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
        return NGFWAppRef(app_id=row.pk, instance_id=row.instance_id, is_deleted=True)
    except Exception:
        raise CMSError("Device operation unavailable") from None


def admit_device_remote_authority(credential, identifier):
    from engine.services import admit_native_device_access

    try:
        snapshot = _device_rows().get(pk=UUID(str(identifier)), status="ready", instance__status="ready")
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
    except Exception:
        raise CMSError("Device access unavailable") from None
