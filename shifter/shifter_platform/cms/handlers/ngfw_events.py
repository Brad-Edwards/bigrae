"""NGFW event handler — updates CMS Instance and App for ngfw.event messages."""

from __future__ import annotations

import logging
from typing import cast

from cms.models import App, Instance
from shared.enums import ResourceStatus
from shared.messages.envelope import parse_sns_message
from shared.messages.events import EVENT_TYPE_NGFW
from shared.messages.payloads import NGFWEventPayload

logger = logging.getLogger(__name__)


class _NgfwAbort(Exception):
    """Internal: raised by an entity update helper when a DB error has been
    logged and the rest of the event handler should bail out cleanly."""


def process_ngfw_event(message: str | dict) -> None:
    """Process unified NGFW event from SNS/SQS.

    Updates CMS Instance and App models based on the event:
    - Looks up Instance by instance_id (UUID)
    - Looks up App by app_id (UUID)
    - Updates status on both if provided
    - EntityBase.save() auto-sets deleted_at on terminal status

    Args:
        message: SNS-wrapped message containing NGFW event data.

    Returns:
        None. Errors are logged and handled gracefully.
    """
    event = parse_sns_message(message)
    event_type = event.get("event_type")

    if event_type != EVENT_TYPE_NGFW:
        logger.debug("Ignoring NGFW event_type=%s", event_type)
        return

    # event_type confirmed; narrow to the typed payload. Required-field and
    # status validation below is retained — the payload is still untrusted input.
    payload = cast(NGFWEventPayload, event)

    event_id = payload.get("event_id", "unknown")
    instance_id = payload.get("instance_id")
    app_id = payload.get("app_id")
    status = payload.get("status")
    serial_number = payload.get("serial_number")

    if not _validate_required_fields(event_id, instance_id, app_id) or not _validate_status(event_id, status):
        return
    # _validate_required_fields confirms app_id is truthy; narrow str | None -> str.
    assert app_id is not None

    if _principal_device_event(payload):
        return
    try:
        previous_instance_status = _update_instance(event_id, instance_id, status)
        previous_app_status = _update_app(event_id, app_id, status, serial_number)
    except _NgfwAbort:
        return

    logger.info(
        "CMS processed NGFW event: instance_id=%s (%s->%s) app_id=%s (%s->%s) serial=%s event_id=%s",
        instance_id,
        previous_instance_status,
        status or previous_instance_status,
        app_id,
        previous_app_status,
        status or previous_app_status,
        serial_number or "N/A",
        event_id,
    )


def _principal_device_event(payload):
    """Mirror only an accepted native event for the same principal device custody."""
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone

    from engine.services import verified_device_status_projection
    from workspaces.services import resource_scope_from_ids

    try:
        # Either named side being principal-owned keeps mixed-identity events
        # out of the legacy update path as well.
        candidates = Instance.all_objects.select_related("request").filter(
            Q(pk=payload["instance_id"]) | Q(apps__pk=payload["app_id"])
        )
        if not candidates.filter(request__owner_principal_uuid__isnull=False).exists():
            return False
        proof = verified_device_status_projection(dict(payload))
        if proof is None:
            return True
        with transaction.atomic():
            instance = candidates.select_for_update(of=("self",)).get(pk=payload["instance_id"])
            app = App.all_objects.select_for_update().get(pk=payload["app_id"], instance=instance)
            request = instance.request
            scope = resource_scope_from_ids(
                kind=request.scope_kind,
                account_id=request.account_id,
                organization_id=request.organization_id,
                workspace_id=request.workspace_id,
            )
            if proof != (request.request_id, instance.pk, request.owner_principal_uuid, scope):
                return True
            instance.status = app.status = payload["status"]
            instance.save(update_fields=["status"])
            app.save(update_fields=["status"])
            if payload["status"] == "destroyed":
                request.deleted_at = timezone.now()
                request.save(update_fields=["deleted_at"])
        return True
    except Exception:
        return True


def _validate_required_fields(event_id: str, instance_id: str | None, app_id: str | None) -> bool:
    """Return True when both instance_id and app_id are present; log and return False otherwise."""
    if instance_id and app_id:
        return True
    logger.warning(
        "NGFW event missing required fields: instance_id=%s app_id=%s event_id=%s",
        instance_id,
        app_id,
        event_id,
    )
    return False


def _validate_status(event_id: str, status: str | None) -> bool:
    """Return True when status is empty or a valid ResourceStatus; log and return False otherwise."""
    if not status:
        return True
    try:
        ResourceStatus(status)
    except ValueError:
        logger.error("Invalid status value: %s event_id=%s", status, event_id)
        return False
    return True


def _update_instance(event_id: str, instance_id: str, status: str | None) -> str | None:
    """Look up Instance and apply status. Returns previous status, or None if
    the row is missing. Raises `_NgfwAbort` on DB error."""
    try:
        instance = Instance.objects.get(id=instance_id)
    except Instance.DoesNotExist:
        logger.warning(
            "CMS Instance not found: instance_id=%s event_id=%s",
            instance_id,
            event_id,
        )
        return None
    except Exception as exc:
        logger.exception(
            "DB error loading CMS Instance: instance_id=%s event_id=%s",
            instance_id,
            event_id,
        )
        raise _NgfwAbort from exc

    previous = instance.status
    if not status:
        return previous

    instance.status = status
    try:
        instance.save(update_fields=["status"])
    except Exception as exc:
        logger.exception(
            "DB error saving CMS Instance: instance_id=%s event_id=%s",
            instance_id,
            event_id,
        )
        raise _NgfwAbort from exc
    return previous


def _update_app(
    event_id: str,
    app_id: str,
    status: str | None,
    serial_number: str | None,
) -> str | None:
    """Look up App and apply status / serial_number. Returns previous status,
    or None if the row is missing. Raises `_NgfwAbort` on DB error."""
    try:
        app = App.objects.get(id=app_id)
    except App.DoesNotExist:
        logger.warning("CMS App not found: app_id=%s event_id=%s", app_id, event_id)
        return None
    except Exception as exc:
        logger.exception("DB error loading CMS App: app_id=%s event_id=%s", app_id, event_id)
        raise _NgfwAbort from exc

    previous = app.status
    update_fields: list[str] = []

    if status:
        app.status = status
        update_fields.append("status")

    if serial_number:
        app.data = {**app.data, "serial_number": serial_number}
        update_fields.append("data")

    if not update_fields:
        return previous

    try:
        app.save(update_fields=update_fields)
    except Exception as exc:
        logger.exception("DB error saving CMS App: app_id=%s event_id=%s", app_id, event_id)
        raise _NgfwAbort from exc
    return previous
