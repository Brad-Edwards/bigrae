"""CTF-owned projections and mutations for the one-time identity hard cut."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf.models import CTFEvent

from collections.abc import Mapping
from uuid import UUID

from ctf.enums import ScheduledTaskStatus, ScheduledTaskType
from shared.authorization.model import grant_relation_for_action
from shared.operation_authority import credential_reference_from_record
from workspaces.services import resource_scope_from_ids


def identity_cutover_disposition() -> tuple[dict[str, object], list[dict[str, object]]]:
    """Classify retained CTF rows and principal-binding blockers."""
    from ctf.models import CTFEvent, CTFEventStaff, CTFParticipant
    from ctf.services.event.staff import actor_is_active_ctf_organizer

    blockers: list[dict[str, object]] = []
    event_gaps = CTFEvent.objects.filter(created_by__identity_principal__isnull=True).count()
    participant_gaps = CTFParticipant.objects.filter(principal_uuid__isnull=True, user__isnull=True).count()
    if event_gaps:
        blockers.append({"reason": "ctf_event_creator_principal_missing", "entity_count": event_gaps})
    if participant_gaps:
        blockers.append({"reason": "ctf_participant_principal_missing", "entity_count": participant_gaps})
    stale_staff = sum(
        1
        for user_id in CTFEventStaff.objects.filter(deleted_at__isnull=True)
        .exclude(user_id__isnull=True)
        .values_list("user_id", flat=True)
        if not actor_is_active_ctf_organizer(user_id)
    )
    if stale_staff:
        blockers.append({"reason": "ctf_staff_organizer_eligibility_revoked", "entity_count": stale_staff})
    return (
        {
            "ctf": {
                "events": CTFEvent.all_objects.count(),
                "participants": CTFParticipant.all_objects.count(),
                "action": "retain_and_bind_principal_scope",
            }
        },
        blockers,
    )


def pending_identity_cutover_work() -> tuple[int, int]:
    """Count pending CTF work and rows lacking a complete initiator reference."""
    from ctf.models import CommunicationIntent, CTFScheduledTask

    querysets = (
        CTFScheduledTask.objects.filter(
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            status__in=(ScheduledTaskStatus.PENDING.value, ScheduledTaskStatus.RUNNING.value),
        ),
        CommunicationIntent.objects.filter(status="scheduled"),
    )
    pending = missing = 0
    for queryset in querysets:
        for row in queryset.iterator():
            pending += 1
            try:
                if not credential_reference_from_record(row).ceiling.actions:
                    raise ValueError("empty ceiling")
            except ValueError:
                missing += 1
    return pending, missing


def apply_identity_cutover_resources(
    mappings: Mapping[int, tuple[int, int | None, int | None]], principal_by_user: Mapping[int, UUID]
) -> None:
    """Bind retained events and participants to planned account/principal facts."""
    from ctf.models import CTFEvent, CTFParticipant

    for workspace_id, (account_id, organization_id, retained_workspace_id) in mappings.items():
        CTFEvent.all_objects.filter(workspace_id=workspace_id).update(
            scope_kind="account",
            account_id=account_id,
            organization_id=organization_id,
            workspace_id=retained_workspace_id,
        )
    for user_id, principal_uuid in principal_by_user.items():
        CTFParticipant.all_objects.filter(principal_uuid__isnull=True, user_id=user_id).update(
            principal_uuid=principal_uuid
        )


def identity_cutover_relationships() -> set[tuple[str, str, str]]:
    """Project event ancestry and explicit event grants through public identities."""
    from ctf.models import CTFEvent, CTFEventStaff, CTFParticipant
    from ctf.services.event.staff import actions_for_role, actor_is_active_ctf_organizer

    relationships: set[tuple[str, str, str]] = set()
    for event in CTFEvent.all_objects.order_by("pk"):
        relationships.add(_event_parent_relationship(event))
        if event.owner_principal_uuid is None:
            raise ValueError("cutover event owner principal is incomplete")
        relationships.add((f"principal:{event.owner_principal_uuid}", "direct_administrator", f"event:{event.pk}"))
    for participant in CTFParticipant.all_objects.exclude(principal_uuid__isnull=True).order_by("pk"):
        relationships.add(
            (
                f"principal:{participant.principal_uuid}",
                grant_relation_for_action("event.participate"),
                f"event:{participant.event_id}",
            )
        )
    for staff in CTFEventStaff.all_objects.filter(deleted_at__isnull=True).order_by("pk"):
        if staff.principal_uuid is None:
            raise ValueError("cutover event staff principal is incomplete")
        if staff.user_id is not None and not actor_is_active_ctf_organizer(staff.user_id):
            continue
        for action in actions_for_role(staff.role):
            relationships.add(
                (
                    f"principal:{staff.principal_uuid}",
                    grant_relation_for_action(action),
                    f"event:{staff.event_id}",
                )
            )
    return relationships


def confirm_identity_cutover_publication(store_id: str, model_id: str) -> None:
    """Record read-back confirmation for every retained event."""
    from ctf.models import CTFEvent

    CTFEvent.all_objects.update(
        authorization_publication_state="confirmed",
        authorization_provider_store_id=store_id,
        authorization_model_id=model_id,
        authorization_publication_reason="cutover_primary_readback_confirmed",
    )


def _event_parent_relationship(event: CTFEvent) -> tuple[str, str, str]:
    """Project only complete canonical SQL ancestry for a retained event."""
    scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    if scope.workspace_uuid is not None:
        parent_kind, parent_uuid = "workspace", scope.workspace_uuid
    elif scope.organization_uuid is not None:
        parent_kind, parent_uuid = "organization", scope.organization_uuid
    elif scope.account_uuid is not None:
        parent_kind, parent_uuid = "account", scope.account_uuid
    else:
        raise ValueError("cutover event scope is incomplete")
    return (f"{parent_kind}:{parent_uuid}", parent_kind, f"event:{event.pk}")
