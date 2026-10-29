"""Filter customer range queries before returning private model projections."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.schemas import RangeContext

from cms.exceptions import CMSError
from cms.models import RangeInstance
from shared.credentials import CredentialContext, resolve_current_credential
from shared.identity_scope import ResourceScope
from workspaces.services import resolve_resource_scope

from ._unified_authorization import require_range_action

_RANGE_UNAVAILABLE = "Range unavailable"
_RANGE_READ = "range.read"


PRIVATE_RANGE_FIELDS = ("range_spec", "model_launch_scope", "model_sources", "model_source_sponsorship")


def list_scoped_ranges(
    credential: CredentialContext,
    scope: ResourceScope | None,
    *,
    include_terminal: bool = False,
    range_source: str | None = None,
) -> list[RangeInstance]:
    """The caller's scope selects candidates; only live leaf decisions disclose rows."""
    try:
        if scope is None:
            raise ValueError(_RANGE_UNAVAILABLE)
        current = resolve_current_credential(credential)
        resolved = resolve_resource_scope(scope)
        if resolved.kind != "account":
            raise ValueError("Missing customer scope")
    except Exception:
        raise CMSError(_RANGE_UNAVAILABLE) from None
    filters = {"scope_kind": "account", "account_id": resolved.account_id, "owner_principal_uuid__isnull": False}
    if resolved.organization_id is not None:
        filters["organization_id"] = resolved.organization_id
    if resolved.workspace_id is not None:
        filters["workspace_id"] = resolved.workspace_id
    if range_source is not None:
        filters["range_source"] = range_source
    manager = RangeInstance.all_objects if include_terminal else RangeInstance.objects
    rows = manager.defer(*PRIVATE_RANGE_FIELDS).filter(**filters).order_by("-created_at")
    return authorized_range_rows(current, rows, include_terminal=include_terminal)


def active_scoped_range(
    credential: CredentialContext, scope: ResourceScope | None, range_source: str
) -> RangeInstance | None:
    """Select the most recent visible active range in the requested customer scope."""
    from shared.enums import ResourceStatus

    if scope is None:
        return _active_owned_range(credential, range_source)
    rows = list_scoped_ranges(credential, scope, range_source=range_source)
    return next((row for row in rows if row.status != ResourceStatus.DESTROYING.value), None)


def get_scoped_range(credential: CredentialContext, range_id: int) -> RangeInstance:
    """Authorize the public range before returning its CMS projection."""
    row = RangeInstance.objects.defer(*PRIVATE_RANGE_FIELDS).filter(range_id=range_id).first()
    if row is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    return require_range_action(credential, row.uuid, _RANGE_READ)


def get_scoped_range_by_request(
    credential: CredentialContext, request_id: str, *, include_terminal: bool = False
) -> RangeContext:
    """Authorize before hydrating guest addresses or content projections."""
    from uuid import UUID

    from shared.enums import ResourceStatus
    from shared.schemas import InstanceContext, RangeContext

    from ._common import _instance_contexts_from_range_spec, _resolve_pause_resume_supported, _resolve_runtime_ips

    try:
        identifier = UUID(request_id)
    except (ValueError, TypeError, AttributeError):
        raise CMSError(_RANGE_UNAVAILABLE) from None
    manager = RangeInstance.all_objects if include_terminal else RangeInstance.objects
    candidate = manager.defer(*PRIVATE_RANGE_FIELDS).filter(request__request_id=identifier).first()
    if candidate is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    admitted = require_range_action(credential, candidate.uuid, _RANGE_READ, include_terminal=include_terminal)
    if admitted.request_id is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    row = (
        manager.select_related("request", "agent")
        .filter(
            pk=admitted.pk,
            request_id=admitted.request_id,
            range_id=admitted.range_id,
            scope_kind=admitted.scope_kind,
            account_id=admitted.account_id,
            organization_id=admitted.organization_id,
            workspace_id=admitted.workspace_id,
            owner_principal_uuid=admitted.owner_principal_uuid,
        )
        .first()
    )
    if row is None or row.request is None:
        raise CMSError(_RANGE_UNAVAILABLE)
    ips = _resolve_runtime_ips(row.range_id)
    supported = _resolve_pause_resume_supported(row.range_id)
    return RangeContext(
        request_id=row.request.request_id,
        range_id=row.range_id,
        scenario_id=row.scenario_id,
        user_id=row.user_id,
        owner_principal_uuid=row.owner_principal_uuid,
        status=ResourceStatus(row.status),
        instances=_instance_contexts_from_range_spec(row.range_spec, InstanceContext, ips),
        agent_name=row.agent.name if row.agent else None,
        pause_supported=supported,
        resume_supported=supported,
    )


def _active_owned_range(credential: CredentialContext, range_source: str) -> RangeInstance | None:
    """Resolve the exact principal-owned candidate, then require the current leaf read."""
    from shared.enums import ResourceStatus

    try:
        current = resolve_current_credential(credential)
    except Exception:
        raise CMSError(_RANGE_UNAVAILABLE) from None
    candidate = (
        RangeInstance.objects.defer(*PRIVATE_RANGE_FIELDS)
        .filter(owner_principal_uuid=current.principal.uuid, range_source=range_source)
        .exclude(status=ResourceStatus.DESTROYING.value)
        .order_by("-created_at")
        .first()
    )
    if candidate is None:
        return None
    try:
        return require_range_action(current, candidate.uuid, _RANGE_READ)
    except CMSError as exc:
        if exc.details and exc.details.get("code") == "authority-unavailable":
            raise
        return None


def authorized_range_rows(
    current: CredentialContext, rows: Iterable[RangeInstance], *, include_terminal: bool
) -> list[RangeInstance]:
    """Filter each candidate by current native leaf policy; dependency failure never becomes an empty page."""
    result = []
    for row in rows:
        try:
            authorized = require_range_action(current, row.uuid, _RANGE_READ, include_terminal=include_terminal)
        except CMSError as exc:
            if exc.details and exc.details.get("code") == "authority-unavailable":
                raise
        else:
            result.append(authorized)
    return result
