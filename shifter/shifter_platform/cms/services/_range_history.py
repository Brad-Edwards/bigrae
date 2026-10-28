"""Safe Mission Control range history, including terminal native generations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cms.models import RangeInstance
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from workspaces.services import WorkspaceOperation

from ._common import _validate_caller_user
from ._range_workspace import authorized_range_workspace_ids

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


def list_mission_control_range_history(
    user: User | CredentialContext, *, scope: ResourceScope | None = None
) -> list[RangeInstance]:
    """Return the user's Mission Control range history, newest first (#1370).

    Unlike :func:`list_ranges` (the generic, source-agnostic query through the
    soft-delete ``objects`` manager), this backs the Mission Control
    range-history surface, so it:

    - reads through ``all_objects`` to INCLUDE soft-deleted rows. Terminal
      ranges (DESTROYED/FAILED) set ``deleted_at`` on save, and those are
      exactly the historical rows a history view exists to list; the default
      manager would hide them and leave history near-empty.
    - scopes to ``range_source == MISSION_CONTROL`` so CTF-sourced ranges for
      the same user never leak into the Mission Control product surface
      (provenance is server-derived, never user-supplied).

    ``request`` is eager-loaded because the caller projects the durable
    ``request.request_id`` correlation key for every row.

    Args:
        user: User whose Mission Control range history to retrieve.

    Returns:
        List of ``RangeInstance`` rows (active and soft-deleted), newest first.

    Raises:
        TypeError: If user is None or an invalid type.
        ValueError: If user is unsaved (id is None).
    """
    from shared.enums import RangeSource

    if isinstance(user, CredentialContext):
        return _principal_range_history(user, scope)
    _validate_caller_user(user, "list_mission_control_range_history")

    logger.debug("list_mission_control_range_history called for user_id=%s", user.id)

    return list(
        RangeInstance.all_objects.filter(
            user_id=user.id,
            range_source=RangeSource.MISSION_CONTROL.value,
            workspace_id__in=authorized_range_workspace_ids(user, WorkspaceOperation.READ_RANGE),
        )
        .select_related("request")
        .order_by("-created_at")
    )


def _principal_range_history(user: CredentialContext, scope: ResourceScope | None) -> list[RangeInstance]:
    """Project native personal or explicitly selected history through current leaf policy."""
    from shared.enums import RangeSource

    from ._scoped_range_queries import list_scoped_ranges

    if scope is None:
        from shared.credentials import resolve_current_credential
        from shared.enums import RangeSource

        current = resolve_current_credential(user)
        candidates = RangeInstance.all_objects.filter(
            owner_principal_uuid=current.principal.uuid,
            range_source=RangeSource.MISSION_CONTROL.value,
        ).order_by("-created_at")
        from ._scoped_range_queries import authorized_range_rows

        return authorized_range_rows(current, candidates, include_terminal=True)
    return list_scoped_ranges(user, scope, include_terminal=True, range_source=RangeSource.MISSION_CONTROL.value)
