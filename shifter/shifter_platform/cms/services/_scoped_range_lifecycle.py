"""Principal branch of CMS's incumbent pause/resume lifecycle entry points."""

from uuid import UUID

from django.db import transaction
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import RangeInstance
from engine.services import (
    PreparedRangeLifecycle,
    commit_prepared_range_lifecycle,
    prepare_range_lifecycle,
    prepare_system_range_cleanup,
)
from shared.audit import AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import TargetRef
from shared.credentials import CredentialContext

from ._unified_authorization import require_range_action

_RANGE_OPERATION_UNAVAILABLE = "Range operation unavailable"


_BINDING_FIELDS = (
    "request_id",
    "range_id",
    "scope_kind",
    "account_id",
    "organization_id",
    "workspace_id",
    "owner_principal_uuid",
)


_RANGE_UNAVAILABLE = "Range unavailable"


_MANAGE_EVENT_RANGES = "event.manage_ranges"


def run_scoped_range_lifecycle(
    credential: CredentialContext,
    operation: str,
    *,
    instance_pk: int | None = None,
    request_id: str | None = None,
    authority_action: str = "range.manage",
    authority_target: TargetRef | None = None,
) -> None:
    """Authorize before locking; neither historical owner nor user ID vetoes policy."""
    from ._range_lifecycle import PAUSE_OP, RESUME_OP, _assert_lifecycle_supported

    try:
        candidate = _scoped_lifecycle_candidate(instance_pk, request_id)
        snapshot = require_range_action(
            credential,
            candidate.uuid,
            authority_action,
            include_terminal=operation == "destroy",
            authority_target=authority_target,
        )
        if operation in {"destroy", "cancel"}:
            return _destroy_scoped_range(
                credential,
                snapshot,
                operation,
                authority_action=authority_action,
                authority_target=authority_target,
            )
        if snapshot.request is None:
            raise CMSError(_RANGE_UNAVAILABLE)
        op = PAUSE_OP if operation == "pause" else RESUME_OP
        _assert_lifecycle_supported(snapshot, op)
        if operation == "resume":
            from ._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch

            if needs_model_preparation(snapshot.request.request_id):
                from engine.services import resolve_model_access_range_views

                (range_view,) = resolve_model_access_range_views(request_uuids=(snapshot.request.request_id,))
                prepare_model_access_for_dispatch(
                    snapshot.request.request_id,
                    range_id=range_view.range_uuid,
                    renew=True,
                    credential=credential,
                    authority_action=authority_action,
                    authority_target=authority_target,
                )
        prepared = prepare_range_lifecycle(
            credential,
            snapshot.request.request_id,
            operation,
            authority_action=authority_action if authority_target is not None else None,
            authority_target=authority_target,
        )
        fence = {field: getattr(snapshot, field) for field in _BINDING_FIELDS}
        with transaction.atomic():
            row = (
                RangeInstance.objects.select_for_update()
                .filter(pk=snapshot.pk, status=snapshot.status, **fence)
                .first()
            )
            if row is None or not commit_prepared_range_lifecycle(prepared):
                raise CMSError(_RANGE_OPERATION_UNAVAILABLE)
            target = _scoped_lifecycle_target(operation, prepared.status, op.target_status)
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
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise CMSError(_RANGE_OPERATION_UNAVAILABLE) from None


def _destroy_scoped_range(
    credential: CredentialContext,
    snapshot: RangeInstance,
    operation: str,
    *,
    authority_action: str,
    authority_target: TargetRef | None,
) -> None:
    """Commit admitted destruction against the unchanged CMS runtime binding."""
    if snapshot.request is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    prepared = prepare_range_lifecycle(
        credential,
        snapshot.request.request_id,
        operation,
        authority_action=authority_action if authority_target is not None else None,
        authority_target=authority_target,
    )
    fence = {field: getattr(snapshot, field) for field in _BINDING_FIELDS}
    with transaction.atomic():
        row = (
            RangeInstance.all_objects.select_for_update()
            .filter(pk=snapshot.pk, status=snapshot.status, deleted_at=snapshot.deleted_at, **fence)
            .first()
        )
        if row is None or not commit_prepared_range_lifecycle(prepared):
            raise CMSError(_RANGE_OPERATION_UNAVAILABLE)
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


def run_event_range_lifecycle(
    credential: CredentialContext, event_uuid: UUID, range_instance_pk: int, operation: str
) -> None:
    """Run lifecycle work using the owning event's exact range-management grant."""
    return run_scoped_range_lifecycle(
        credential,
        operation,
        instance_pk=range_instance_pk,
        authority_action=_MANAGE_EVENT_RANGES,
        authority_target=TargetRef("event", event_uuid),
    )


def destroy_expired_scoped_range(range_uuid: UUID) -> None:
    """Accept only a current server-expired lease and an existing native cleanup origin."""
    from shared.audit import AuditActorType

    from ._unified_authorization import _range_binding

    try:
        snapshot, _, _ = _range_binding(range_uuid, include_terminal=True)
        if snapshot.expires_at is None or snapshot.expires_at > timezone.now():
            raise CMSError("Range lease has not expired")
        if snapshot.request is None:
            raise CMSError(_RANGE_UNAVAILABLE)
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
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise CMSError("Range cleanup unavailable") from None


def destroy_event_range_for_deletion(
    credential: CredentialContext, event_uuid: UUID, instance_pk: int | None, *, request_id: UUID | None = None
) -> None:
    """Retain exact generation cleanup under current event deletion authority."""
    _destroy_event_range_with_native_cleanup(
        credential, event_uuid, instance_pk, request_id=request_id, authority_action="event.delete"
    )


def destroy_event_range_for_recovery(
    credential: CredentialContext, event_uuid: UUID, instance_pk: int | None, *, request_id: UUID | None = None
) -> bool:
    """Retain old-generation cleanup before a recovery removes its live pointer."""
    return _destroy_event_range_with_native_cleanup(
        credential, event_uuid, instance_pk, request_id=request_id, authority_action=_MANAGE_EVENT_RANGES
    )


def _destroy_event_range_with_native_cleanup(
    credential: CredentialContext,
    event_uuid: UUID,
    instance_pk: int | None,
    *,
    request_id: UUID | None = None,
    authority_action: str,
) -> bool:
    """Authorize exact event deletion, then retain native generation-bound cleanup."""
    from shared.authorization import AuthorizationRequest
    from shared.authorization.policy import require_authorization
    from shared.credentials import resolve_current_credential

    from ._unified_authorization import _range_binding

    try:
        current = resolve_current_credential(credential)
        if (instance_pk is None) == (request_id is None):
            raise ValueError(_RANGE_UNAVAILABLE)
        candidate = _scoped_lifecycle_candidate(instance_pk, str(request_id) if request_id is not None else None)
        if _cancel_unrealized_event_intent(current, event_uuid, candidate, authority_action):
            return True
        snapshot, scope, parent = _range_binding(candidate.uuid, include_terminal=True)
        if parent != TargetRef("event", event_uuid) or snapshot.request is None:
            raise ValueError(_RANGE_UNAVAILABLE)
        require_authorization(AuthorizationRequest(current.principal, authority_action, parent, scope, current.ceiling))
        from engine.services import is_cleanup_verified_absent

        verified_absent = snapshot.status == "destroyed" and is_cleanup_verified_absent(snapshot.request.request_id)
        prepared = None if verified_absent else prepare_system_range_cleanup(snapshot.request.request_id)
        return _commit_event_cleanup(current, snapshot, prepared, authority_action)
    except Exception:
        raise CMSError("Event range cleanup unavailable") from None


def _cancel_unrealized_event_intent(
    current: CredentialContext, event_uuid: UUID, candidate: RangeInstance, action: str
) -> bool:
    """Fence native creation before releasing a reservation with no runtime."""
    from engine.services import has_range_runtime
    from shared.authorization import AuthorizationRequest
    from shared.authorization.policy import require_authorization
    from shared.credentials import resolve_local_credential
    from workspaces.services import release_workspace_concurrent_range

    from ._unified_authorization import _intent_scope_binding

    if candidate.request is None or has_range_runtime(candidate.request.request_id, candidate.uuid):
        return False
    scope, parent = _intent_scope_binding(candidate)
    if parent != TargetRef("event", event_uuid):
        raise ValueError("Event range cleanup unavailable")
    require_authorization(AuthorizationRequest(current.principal, action, parent, scope, current.ceiling))
    with transaction.atomic():
        row = RangeInstance.all_objects.select_for_update(of=("self",)).select_related("request").get(pk=candidate.pk)
        request = row.request
        if request is None:
            raise ValueError("Event range cleanup changed")
        if (
            resolve_local_credential(current) != current
            or row.status != candidate.status
            or _intent_scope_binding(row) != (scope, parent)
            or has_range_runtime(request.request_id, row.uuid)
        ):
            raise ValueError("Event range cleanup changed")
        row.status = "failed"
        row.save(update_fields=["status"])
        if row.workspace_id is not None:
            release_workspace_concurrent_range(row.workspace_id, request.request_id)
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.uuid),
                action="deprovision",
                **principal_actor_fields(current.principal),
                request_id=str(request.request_id),
                new_state={"status": "failed"},
                context="event_unrealized_intent_cleanup",
            ),
            strict=True,
        )
    return True


def _scoped_lifecycle_candidate(instance_pk: int | None, request_id: str | None) -> RangeInstance:
    """Select the existing CMS intent before taking lifecycle locks."""
    if request_id is not None:
        candidate = RangeInstance.all_objects.filter(request__request_id=UUID(request_id)).first()
    elif instance_pk is not None:
        candidate = RangeInstance.all_objects.filter(pk=instance_pk).first()
    else:
        raise CMSError(_RANGE_UNAVAILABLE)
    if candidate is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    return candidate


def _scoped_lifecycle_target(operation: str, status: str, default: str) -> str:
    """Preserve an already-settled native transition in the CMS status projection."""
    target = "paused" if operation == "pause" and status == "paused" else default
    if operation == "resume" and status == "ready":
        target = "ready"
    return target


def dispatch_bound_event_range(credential: CredentialContext, event_uuid: UUID, instance_pk: int) -> None:
    """Make a CTF launch runnable only after its owning aggregate records the binding."""
    from engine.services import dispatch_created_raes_range

    instance = _scoped_lifecycle_candidate(instance_pk, None)
    admitted = require_range_action(
        credential, instance.uuid, _MANAGE_EVENT_RANGES, authority_target=TargetRef("event", event_uuid)
    )
    if admitted.request is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    try:
        dispatch_created_raes_range(admitted.request.request_id, credential=credential)
    except (ValueError, PermissionError):
        raise CMSError("Event range dispatch unavailable") from None


def _commit_event_cleanup(
    current: CredentialContext, snapshot: RangeInstance, prepared: PreparedRangeLifecycle | None, authority_action: str
) -> bool:
    """Commit accepted exact native cleanup against unchanged CMS custody and live caller proof."""
    from engine.services import is_cleanup_verified_absent

    assert snapshot.request is not None
    fence = {field: getattr(snapshot, field) for field in _BINDING_FIELDS}
    with transaction.atomic():
        row = (
            RangeInstance.all_objects.select_for_update()
            .filter(pk=snapshot.pk, status=snapshot.status, deleted_at=snapshot.deleted_at, **fence)
            .first()
        )
        from shared.credentials import resolve_local_credential

        if row is None or resolve_local_credential(current) != current:
            raise ValueError(_RANGE_OPERATION_UNAVAILABLE)
        if prepared is None:
            if not is_cleanup_verified_absent(snapshot.request.request_id):
                raise ValueError(_RANGE_OPERATION_UNAVAILABLE)
            return True
        if not commit_prepared_range_lifecycle(prepared):
            raise ValueError(_RANGE_OPERATION_UNAVAILABLE)
        row.status = "destroying"
        row.deleted_at = row.deleted_at or timezone.now()
        row.save(update_fields=["status", "deleted_at"])
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.uuid),
                action="deprovision",
                **principal_actor_fields(current.principal),
                request_id=str(prepared.request_id),
                new_state={"status": "destroying"},
                context="event_deletion_cleanup" if authority_action == "event.delete" else "event_recovery_cleanup",
            ),
            strict=True,
        )
    return False
