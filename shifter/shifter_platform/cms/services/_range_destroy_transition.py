"""Range destroy/cancel entrypoints (by range_id and by request_id)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import RangeInstance
from shared.audit import (
    AuditAction,
    AuditActorType,
    AuditEntityType,
)
from shared.enums import ResourceStatus

from ._range_destroy_dispatch import (
    engine_cancel_range_by_request as _engine_cancel_range_by_request_call,
)
from ._range_destroy_dispatch import (
    engine_destroy_range_by_request as _engine_destroy_range_by_request_call,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

# Shared error message for "Range not found" so we don't duplicate the literal (python:S1192).
_RANGE_NOT_FOUND_MSG = "Range not found"
_MISSING_REQUEST_MSG = "Range has no associated request"


_TransitionSpec = tuple[str, Callable[[UUID], bool], AuditAction, str, str, bool]
_DESTROY_TRANSITION: _TransitionSpec = (
    ResourceStatus.DESTROYING.value,
    _engine_destroy_range_by_request_call,
    AuditAction.DEPROVISION,
    "Range cannot be destroyed in current state",
    "destroy_range",
    True,
)
_CANCEL_TRANSITION: _TransitionSpec = (
    ResourceStatus.DESTROYING.value,
    _engine_cancel_range_by_request_call,
    AuditAction.CANCEL,
    "Range cannot be cancelled in current state",
    "cancel_range",
    False,
)


def _audit_log_call(**kwargs: Any) -> None:  # NOSONAR
    """Late-bound call to ``cms.services.audit_log`` so test patches apply."""
    from cms import services as _cs

    _cs.audit_log(_cs.AuditEvent(**kwargs))


def _get_range_call(user: User, range_id: int) -> RangeInstance:
    """Look up range through the package so test patches apply."""
    from cms import services as _cs

    return _cs.get_range(user, range_id)


def _transition_then_dispatch(
    *,
    instance: RangeInstance,
    request_id: UUID,
    user: User | None,
    audit_entity_id: int,
    transition: _TransitionSpec,
    audit_actor_type: AuditActorType = AuditActorType.USER,
    audit_context: str | None = None,
) -> None:
    """Apply a CMS lifecycle transition, dispatch engine cleanup, and revert on rejection."""
    target_status, engine_call, audit_action, failure_message, label, soft_delete = transition
    previous_status = instance.status
    previous_deleted_at = instance.deleted_at
    status_changed = previous_status != target_status or (soft_delete and previous_deleted_at is None)

    if status_changed:
        instance.status = target_status
        update_fields = ["status"]
        if soft_delete:
            instance.deleted_at = timezone.now()
            update_fields.append("deleted_at")
        instance.save(update_fields=update_fields)

    try:
        accepted = engine_call(request_id)
    except Exception:
        _restore_range_instance_status(instance, previous_status, previous_deleted_at)
        raise

    if not accepted:
        _restore_range_instance_status(instance, previous_status, previous_deleted_at)
        logger.warning("%s: engine rejected cleanup request_id=%s", label, request_id)
        raise CMSError(failure_message)

    if status_changed:
        _audit_log_call(
            entity_type=AuditEntityType.RANGE,
            entity_id=audit_entity_id,
            action=audit_action,
            actor_type=audit_actor_type,
            actor_id=user.id if user is not None and audit_actor_type == AuditActorType.USER else None,
            previous_state={
                "status": previous_status,
                "scenario": instance.scenario_id,
            },
            new_state={"status": target_status},
            request_id=str(request_id),
            context=audit_context or "",
        )


def _restore_range_instance_status(
    instance: RangeInstance,
    previous_status: str,
    previous_deleted_at: datetime | None,
) -> None:
    """Restore CMS status/deleted_at after engine cleanup dispatch rejects."""
    instance.status = previous_status
    instance.deleted_at = previous_deleted_at
    instance.save(update_fields=["status", "deleted_at"])
