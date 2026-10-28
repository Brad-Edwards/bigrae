"""Bounded native discovery shared by the new console and workspace projection."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import transaction

import workspaces.services as ws
from ctf.enums import EventStatus
from ctf.exceptions import CTFCommunicationError, CTFError, CTFPermissionError
from ctf.models import CTFEvent
from ctf.services.authorization import admit_event_actions, assert_event_action
from ctf.services.communication.admission import AdmissionActor, prepare_collection_actor
from shared.api_tokens.scopes import CTF_COMMUNICATION_READ
from shared.identity_scope import ResourceScope

_MAX_CANDIDATES = 10000


def _parent(scope: ResourceScope) -> tuple[str, UUID]:
    """Select the most specific live account ancestry for the communication scope."""
    if scope.workspace_uuid is not None:
        return "workspace", scope.workspace_uuid
    if scope.organization_uuid is not None:
        return "organization", scope.organization_uuid
    if scope.account_uuid is not None:
        return "account", scope.account_uuid
    raise ws.AccountScopeError("Scope unavailable")


def list_target_options(
    actor: AdmissionActor,
    *,
    parent_type: str | None = None,
    parent_uuid: UUID | None = None,
    limit: int,
    offset: int,
    workspace_only: bool = False,
) -> dict[str, Any]:
    """Authorize every candidate before pagination; parent labels follow event admission.

    A parent filter selects direct placement, never descendant events. Credential
    and evaluator failures propagate even when no eligible event is returned.
    """
    actor = prepare_collection_actor(actor, required_scope=CTF_COMMUNICATION_READ)
    if actor.credential is None or actor.system:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_ACTOR_DENIED")
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000 or (parent_type is None) != (parent_uuid is None):
        raise CTFCommunicationError("Invalid target page", code="CTF_COMMUNICATION_TARGET_DENIED")
    candidates = _target_candidates(parent_type, parent_uuid, workspace_only)
    rows: list[dict[str, Any]] = []
    parents: dict[tuple[str, UUID], dict[str, Any]] = {}
    for event in candidates:
        row = _target_option(actor, event, parents)
        if row is not None:
            rows.append(row)
    # A revoked or narrowed actor is not an ordinary target-level policy denial.
    # Recheck outside SQL before disclosing any accumulated parents or page.
    current_actor = prepare_collection_actor(actor, required_scope=CTF_COMMUNICATION_READ)
    if current_actor.credential != actor.credential:
        raise CTFCommunicationError("Communication credential changed", code="CTF_COMMUNICATION_ACTOR_DENIED")
    return {
        "parents": sorted(parents.values(), key=lambda row: (row["name"], row["kind"], str(row["id"]))),
        "events": rows[offset : offset + limit],
        "next_offset": offset + limit if len(rows) > offset + limit else None,
    }


def list_targets(actor: AdmissionActor, *, workspace_uuid: UUID | None, limit: int, offset: int) -> dict[str, Any]:
    """Preserve the published workspace-only shape using the native discovery owner."""
    result = list_target_options(
        actor,
        parent_type="workspace" if workspace_uuid is not None else None,
        parent_uuid=workspace_uuid,
        limit=limit,
        offset=offset,
        workspace_only=True,
    )
    return {
        "workspaces": [{"id": row["id"], "name": row["name"]} for row in result["parents"]],
        "events": [
            {**{key: value for key, value in row.items() if key != "parent"}, "workspace_id": row["parent"]["id"]}
            for row in result["events"]
        ],
        "next_offset": result["next_offset"],
    }


def _target_option(
    actor: AdmissionActor, event: CTFEvent, parents: dict[tuple[str, UUID], dict[str, Any]]
) -> dict[str, Any] | None:
    """Recheck one live event before projecting its name and owning parent."""
    if actor.credential is None:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_ACTOR_DENIED")
    try:
        admission = admit_event_actions(
            actor.compatibility_user_id if actor.compatibility_user_id is not None else actor.credential,
            event,
            "event.manage_communications",
        )
        # Read fresh local fields after policy; never disclose a stale binding.
        with transaction.atomic():
            current = CTFEvent.objects.filter(pk=event.pk).exclude(status=EventStatus.CANCELLED.value).first()
            if current is None:
                return None
            assert_event_action(admission, current, "event.manage_communications")
            scope = ws.resource_scope_from_ids(
                kind=current.scope_kind,
                account_id=current.account_id,
                organization_id=current.organization_id,
                workspace_id=current.workspace_id,
            )
            kind, identity = _parent(scope)
            if (kind, identity) not in parents:
                parents[kind, identity] = {
                    "kind": kind,
                    "id": identity,
                    "name": ws.resource_scope_parent_name(scope),
                }
    except (CTFPermissionError, ws.AccountScopeError):
        return None
    except CTFError:
        raise CTFCommunicationError(
            "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
        ) from None
    return {
        "id": current.pk,
        "name": current.name,
        "status": current.status,
        "event_timezone": current.event_timezone,
        "event_start": current.event_start,
        "event_end": current.event_end,
        "parent": {"kind": kind, "id": identity},
    }


def _target_candidates(parent_type: str | None, parent_uuid: UUID | None, workspace_only: bool) -> list[CTFEvent]:
    """Read a bounded set at the exact selected placement before per-event policy."""
    queryset = CTFEvent.objects.exclude(status=EventStatus.CANCELLED.value)
    if workspace_only:
        queryset = queryset.filter(workspace_id__isnull=False)
    if parent_type is not None and parent_uuid is not None:
        try:
            scope = ws.event_placement_scope(parent_type, parent_uuid)
            binding = ws.resolve_resource_scope(scope)
        except ws.AccountScopeError:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_WORKSPACE_DENIED") from None
        queryset = queryset.filter(
            scope_kind=scope.kind,
            account_id=binding.account_id,
            organization_id=binding.organization_id,
            workspace_id=binding.workspace_id,
        )
    candidates = list(queryset.order_by("event_start", "pk")[: _MAX_CANDIDATES + 1])
    if len(candidates) > _MAX_CANDIDATES:
        raise CTFCommunicationError("Target collection is too large", code="CTF_COMMUNICATION_COLLECTION_BOUNDED")
    return candidates
