"""Principal branch of CMS's incumbent pause/resume lifecycle entry points."""

from uuid import UUID

from django.db import transaction
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import RangeInstance
from engine.services import commit_prepared_range_lifecycle, prepare_range_lifecycle, prepare_system_range_cleanup
from shared.audit import AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext

from ._unified_authorization import require_range_action

_BINDING_FIELDS = (
    "request_id",
    "range_id",
    "scope_kind",
    "account_id",
    "organization_id",
    "workspace_id",
    "owner_principal_uuid",
)


def run_scoped_range_lifecycle(
    credential: CredentialContext, operation: str, *, instance_pk: int | None = None, request_id: str | None = None
) -> None:
    """Authorize before locking; neither historical owner nor user ID vetoes policy."""
    from ._range_lifecycle import PAUSE_OP, RESUME_OP, _assert_lifecycle_supported

    try:
        if request_id is not None:
            candidate = RangeInstance.all_objects.filter(request__request_id=UUID(request_id)).first()
        else:
            candidate = RangeInstance.all_objects.filter(pk=instance_pk).first()
        if candidate is None:
            raise CMSError("Range unavailable")
        snapshot = require_range_action(
            credential, candidate.uuid, "range.manage", include_terminal=operation == "destroy"
        )
        if operation in {"destroy", "cancel"}:
            return _destroy_scoped_range(credential, snapshot, operation)
        op = PAUSE_OP if operation == "pause" else RESUME_OP
        _assert_lifecycle_supported(snapshot, op)
        prepared = prepare_range_lifecycle(credential, snapshot.request.request_id, operation)
        fence = {field: getattr(snapshot, field) for field in _BINDING_FIELDS}
        with transaction.atomic():
            row = (
                RangeInstance.objects.select_for_update()
                .filter(pk=snapshot.pk, status=snapshot.status, **fence)
                .first()
            )
            if row is None or not commit_prepared_range_lifecycle(prepared):
                raise CMSError("Range operation unavailable")
            target = "paused" if operation == "pause" and prepared.status == "paused" else op.target_status
            if operation == "resume" and prepared.status == "ready":
                target = "ready"
            if row.status == target:
                return
            row.status = target
            row.save(update_fields=["status"])
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.RANGE,
                    entity_id=row.pk,
                    entity_ref=str(row.uuid),
                    action=op.audit_action,
                    **principal_actor_fields(credential.principal),
                    request_id=str(prepared.request_id),
                    new_state={"status": row.status},
                ),
                strict=True,
            )
    except CMSError:
        raise
    except Exception:
        raise CMSError("Range operation unavailable") from None


def _destroy_scoped_range(credential: CredentialContext, snapshot: RangeInstance, operation: str) -> None:
    prepared = prepare_range_lifecycle(credential, snapshot.request.request_id, operation)
    fence = {field: getattr(snapshot, field) for field in _BINDING_FIELDS}
    with transaction.atomic():
        row = (
            RangeInstance.all_objects.select_for_update()
            .filter(pk=snapshot.pk, status=snapshot.status, deleted_at=snapshot.deleted_at, **fence)
            .first()
        )
        if row is None or not commit_prepared_range_lifecycle(prepared):
            raise CMSError("Range operation unavailable")
        if row.status == "destroying" and (operation == "cancel" or row.deleted_at is not None):
            return
        row.status = "destroying"
        if operation == "destroy":
            row.deleted_at = timezone.now()
        row.save(update_fields=["status", "deleted_at"])
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.uuid),
                action="cancel" if operation == "cancel" else "deprovision",
                **principal_actor_fields(credential.principal),
                request_id=str(prepared.request_id),
                new_state={"status": row.status},
            ),
            strict=True,
        )


def destroy_expired_scoped_range(range_uuid: UUID) -> None:
    """Accept only a current server-expired lease and an existing native cleanup origin."""
    from shared.audit import AuditActorType

    from ._unified_authorization import _range_binding

    try:
        snapshot, _, _ = _range_binding(range_uuid, include_terminal=True)
        if snapshot.expires_at is None or snapshot.expires_at > timezone.now():
            raise CMSError("Range lease has not expired")
        prepared = prepare_system_range_cleanup(snapshot.request.request_id)
        fence = {field: getattr(snapshot, field) for field in _BINDING_FIELDS}
        with transaction.atomic():
            row = (
                RangeInstance.all_objects.select_for_update()
                .filter(pk=snapshot.pk, status=snapshot.status, expires_at=snapshot.expires_at, **fence)
                .first()
            )
            if row is None or not commit_prepared_range_lifecycle(prepared):
                raise CMSError("Range cleanup unavailable")
            if row.status == "destroying" and row.deleted_at is not None:
                return
            row.status = "destroying"
            row.deleted_at = row.deleted_at or timezone.now()
            row.save(update_fields=["status", "deleted_at"])
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.RANGE,
                    entity_id=row.pk,
                    entity_ref=str(row.uuid),
                    action="deprovision",
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    request_id=str(prepared.request_id),
                    new_state={"status": row.status},
                    context="range_lease_expired",
                ),
                strict=True,
            )
    except CMSError:
        raise
    except Exception:
        raise CMSError("Range cleanup unavailable") from None
