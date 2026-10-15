"""Range destroy/cancel entrypoints (by range_id and by request_id)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cms.exceptions import CMSError
from cms.models import RangeInstance
from shared.audit import (
    AuditActorType,
)
from shared.constants import USER_CANNOT_BE_NONE
from shared.credentials import CredentialContext
from workspaces.services import WorkspaceOperation

from ._common import _validate_caller_user
from ._range_destroy_query import destroyable_instances as _destroyable_instances
from ._range_destroy_transition import (
    _CANCEL_TRANSITION,
    _DESTROY_TRANSITION,
    _get_range_call,
    _transition_then_dispatch,
)
from ._range_workspace import authorize_range_workspace

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

# Shared error message for "Range not found" so we don't duplicate the literal (python:S1192).
_RANGE_NOT_FOUND_MSG = "Range not found"
_MISSING_REQUEST_MSG = "Range has no associated request"


def destroy_range(user: User | CredentialContext, range_instance_pk: int) -> None:
    """Tear down an owned range by its ``RangeInstance`` primary key.

    The primary key is the stable identifier held by callers; the legacy
    nullable ``range_id`` engine field is not used for lookup (issue #1139).
    """
    if isinstance(user, CredentialContext):
        from ._scoped_range_lifecycle import run_scoped_range_lifecycle

        return run_scoped_range_lifecycle(user, "destroy", instance_pk=range_instance_pk)
    _validate_caller_user(user, "destroy_range")

    if range_instance_pk is None:
        logger.error(
            "destroy_range called with None range_instance_pk for user_id=%s",
            user.id,
        )
        raise TypeError("range_instance_pk cannot be None")

    if not isinstance(range_instance_pk, int):
        logger.error(
            "destroy_range called with invalid range_instance_pk type: %s",
            type(range_instance_pk).__name__,
        )
        msg = f"range_instance_pk must be an int, got {type(range_instance_pk).__name__}"
        raise TypeError(msg)

    if range_instance_pk < 0:
        logger.error(
            "destroy_range called with negative range_instance_pk=%s for user_id=%s",
            range_instance_pk,
            user.id,
        )
        raise ValueError("range_instance_pk must be non-negative")

    logger.debug(
        "destroy_range called for user_id=%s, range_instance_pk=%s",
        user.id,
        range_instance_pk,
    )

    try:
        instance = _destroyable_instances().get(pk=range_instance_pk)
    except RangeInstance.DoesNotExist:
        logger.warning(
            "destroy_range: range not found for user_id=%s, range_instance_pk=%s",
            user.id,
            range_instance_pk,
        )
        raise CMSError(f"Range {range_instance_pk} not found") from None

    if instance.user_id != user.id:
        logger.error(
            "destroy_range: access denied - range_instance_pk=%s owned by user_id=%s, requested by user_id=%s",
            range_instance_pk,
            instance.user_id,
            user.id,
        )
        raise CMSError(f"Range {range_instance_pk} not found")
    authorize_range_workspace(user, instance.workspace_id, WorkspaceOperation.MANAGE_RANGE)

    try:
        request_id = instance.request.request_id if instance.request else None
        if request_id is None:
            logger.error(
                "destroy_range: no request_id for range_instance_pk=%s, cannot destroy",
                range_instance_pk,
            )
            raise CMSError(f"Range {range_instance_pk} has no associated request")

        _transition_then_dispatch(
            instance=instance,
            request_id=request_id,
            user=user,
            audit_entity_id=range_instance_pk,
            transition=_DESTROY_TRANSITION,
        )

        logger.debug(
            "destroy_range completed for range_instance_pk=%s request_id=%s user_id=%s",
            range_instance_pk,
            request_id,
            user.id,
        )

    except (TypeError, ValueError, CMSError):
        raise
    except Exception:
        logger.exception(
            "Error in destroy_range for user_id=%s, range_instance_pk=%s",
            user.id,
            range_instance_pk,
        )
        raise


def destroy_expired_range(instance: RangeInstance) -> None:
    """Destroy a server-expired range through the canonical Engine lifecycle."""
    if instance.owner_principal_uuid is not None:
        from ._scoped_range_lifecycle import destroy_expired_scoped_range

        destroy_expired_scoped_range(instance.uuid)
        return
    request_id = instance.request.request_id if instance.request else None
    if request_id is None:
        raise CMSError(_MISSING_REQUEST_MSG)
    _transition_then_dispatch(
        instance=instance,
        request_id=request_id,
        user=None,
        audit_entity_id=instance.pk,
        transition=_DESTROY_TRANSITION,
        audit_actor_type=AuditActorType.SYSTEM,
        audit_context="range_lease_expired",
    )


def cancel_range(user: User | CredentialContext, range_id: int) -> None:
    """Cancel provisioning range.

    Verifies ownership via get_range, marks the CMS row destroying, and delegates
    engine cancellation.
    """
    if isinstance(user, CredentialContext):
        from ._scoped_range_lifecycle import run_scoped_range_lifecycle

        return run_scoped_range_lifecycle(user, "cancel", instance_pk=range_id)
    _validate_caller_user(user, "cancel_range")

    if range_id is None:
        logger.error(
            "cancel_range called with None range_id for user_id=%s",
            user.id,
        )
        raise TypeError("range_id cannot be None")

    if not isinstance(range_id, int):
        logger.error(
            "cancel_range called with invalid range_id type: %s",
            type(range_id).__name__,
        )
        msg = f"range_id must be an int, got {type(range_id).__name__}"
        raise TypeError(msg)

    if range_id < 0:
        logger.error(
            "cancel_range called with negative range_id=%s for user_id=%s",
            range_id,
            user.id,
        )
        raise ValueError("range_id must be non-negative")

    logger.debug(
        "cancel_range called for user_id=%s, range_id=%s",
        user.id,
        range_id,
    )

    instance = None

    try:
        instance = _get_range_call(user, range_id)
        if instance is None:
            logger.warning(
                "cancel_range: range not found for user_id=%s, range_id=%s",
                user.id,
                range_id,
            )
            raise CMSError(_RANGE_NOT_FOUND_MSG)
    except (TypeError, ValueError, CMSError):
        logger.error(
            "cancel_range: user and range mismatch for user_id=%s, range_id=%s",
            user.id,
            range_id,
        )
        raise

    try:
        request_id = instance.request.request_id if instance.request else None
        if request_id is None:
            logger.error(
                "cancel_range: no request_id for range_id=%s, cannot cancel",
                range_id,
            )
            raise CMSError(f"Range {range_id} has no associated request")

        _transition_then_dispatch(
            instance=instance,
            request_id=request_id,
            user=user,
            audit_entity_id=range_id,
            transition=_CANCEL_TRANSITION,
        )
    except (TypeError, ValueError, CMSError):
        raise
    except Exception:
        logger.exception(
            "Error in cancel_range for user_id=%s, range_id=%s",
            user.id,
            range_id,
        )
        raise


def destroy_range_by_request_id(user: User | CredentialContext, request_id: str) -> None:
    """Tear down range by request_id.

    Fetches RangeInstance by request_id, verifies ownership, updates CMS status
    to DESTROYING, then delegates to engine.services.destroy_range.

    Args:
        user: User requesting destruction
        request_id: UUID string of the request

    Returns:
        None

    Raises:
        TypeError: If user is None or invalid type
        CMSError: If range not found or not owned by user
    """
    if isinstance(user, CredentialContext):
        from ._scoped_range_lifecycle import run_scoped_range_lifecycle

        return run_scoped_range_lifecycle(user, "destroy", request_id=request_id)
    if user is None:
        logger.error("destroy_range_by_request_id called with None user")
        raise TypeError(USER_CANNOT_BE_NONE)

    if not hasattr(user, "id"):
        logger.error(
            "destroy_range_by_request_id called with invalid user type: %s",
            type(user).__name__,
        )
        msg = f"user must be a User instance, got {type(user).__name__}"
        raise TypeError(msg)

    if not request_id:
        logger.error("destroy_range_by_request_id called with empty request_id")
        raise CMSError("request_id is required")

    logger.debug(
        "destroy_range_by_request_id called: user_id=%s request_id=%s",
        user.id,
        request_id,
    )

    instance = (
        _destroyable_instances()
        .filter(
            request__request_id=request_id,
            user_id=user.id,
        )
        .first()
    )

    if not instance:
        logger.warning(
            "destroy_range_by_request_id: not found: request_id=%s user_id=%s",
            request_id,
            user.id,
        )
        raise CMSError(_RANGE_NOT_FOUND_MSG)
    authorize_range_workspace(user, instance.workspace_id, WorkspaceOperation.MANAGE_RANGE)

    if instance.request is None:
        raise CMSError(_MISSING_REQUEST_MSG)

    try:
        _transition_then_dispatch(
            instance=instance,
            request_id=instance.request.request_id,
            user=user,
            audit_entity_id=instance.range_id or 0,
            transition=_DESTROY_TRANSITION,
        )

        logger.debug(
            "destroy_range_by_request_id completed: request_id=%s user_id=%s",
            request_id,
            user.id,
        )
    except (TypeError, ValueError, CMSError):
        raise
    except Exception:
        logger.exception(
            "Error in destroy_range_by_request_id: user_id=%s request_id=%s",
            user.id,
            request_id,
        )
        raise


def cancel_range_by_request_id(user: User | CredentialContext, request_id: str) -> None:
    """Cancel provisioning range by request_id.

    Fetches RangeInstance by request_id, verifies ownership, updates status,
    then delegates to engine.orchestration.cancel().

    Args:
        user: User requesting cancellation
        request_id: UUID string of the request

    Returns:
        None

    Raises:
        TypeError: If user is None or invalid type
        CMSError: If range not found or not owned by user
    """
    if isinstance(user, CredentialContext):
        from ._scoped_range_lifecycle import run_scoped_range_lifecycle

        return run_scoped_range_lifecycle(user, "cancel", request_id=request_id)
    if user is None:
        logger.error("cancel_range_by_request_id called with None user")
        raise TypeError(USER_CANNOT_BE_NONE)

    if not hasattr(user, "id"):
        logger.error(
            "cancel_range_by_request_id called with invalid user type: %s",
            type(user).__name__,
        )
        msg = f"user must be a User instance, got {type(user).__name__}"
        raise TypeError(msg)

    if not request_id:
        logger.error("cancel_range_by_request_id called with empty request_id")
        raise CMSError("request_id is required")

    logger.debug(
        "cancel_range_by_request_id called: user_id=%s request_id=%s",
        user.id,
        request_id,
    )

    instance = RangeInstance.objects.filter(
        request__request_id=request_id,
        user_id=user.id,
    ).first()

    if not instance:
        logger.warning(
            "cancel_range_by_request_id: not found: request_id=%s user_id=%s",
            request_id,
            user.id,
        )
        raise CMSError(_RANGE_NOT_FOUND_MSG)
    authorize_range_workspace(user, instance.workspace_id, WorkspaceOperation.MANAGE_RANGE)

    if instance.request is None:
        raise CMSError(_MISSING_REQUEST_MSG)

    try:
        _transition_then_dispatch(
            instance=instance,
            request_id=instance.request.request_id,
            user=user,
            audit_entity_id=instance.id,
            transition=_CANCEL_TRANSITION,
        )

        logger.debug(
            "cancel_range_by_request_id completed: request_id=%s user_id=%s",
            request_id,
            user.id,
        )
    except (TypeError, ValueError, CMSError):
        raise
    except Exception:
        logger.exception(
            "Error in cancel_range_by_request_id: user_id=%s request_id=%s",
            user.id,
            request_id,
        )
        raise
