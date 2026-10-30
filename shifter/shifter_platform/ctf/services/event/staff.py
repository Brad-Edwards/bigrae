"""CTF-owned staff roles and durable authority publication (ADR-066).

SQL records canonical ownership and role display. Current policy and credential
ceilings authorize commands; those SQL facts do not grant permission. Each
mutation retains an exact effect plan before publication and projects SQL only
after the shared policy journal confirms every effect.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from ctf.enums import EventStaffRole
from ctf.exceptions import CTFNotFoundError, CTFValidationError
from ctf.models import CTFEvent, CTFEventAuthorityTransition, CTFEventStaff
from shared.log_sanitize import safe_log_value

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet

    from ctf.services.actors import EventActor

from ._staff_policy import (
    _CAPABILITY_ACTIONS,
    _EVENT_MANAGE_STAFF,
    _ROLE_CAPABILITIES,
    ALL_DELEGABLE_CAPABILITIES,
    _live_staff_role,
    _resolve_owned_event_for_staff,
    actions_for_role,
    actor_has_event_capability,
    actor_is_active_ctf_organizer,
    capabilities_for_role,
    capability_action_pairs,
    eligible_co_organizer_ids,
    event_notification_organizer_ids,
    event_notification_organizers,
    staff_row_grants_capability,
)
from ._staff_projection import _assignment_target, _commit_assignment, _commit_ownership_transfer, _transfer_target
from ._staff_transitions import (
    _admit_staff_actor,
    _confirm_transition,
    _create_transition,
    _fence_staff_actor,
    _is_original_transition_credential,
    _pending_transition,
    _retain_assignment,
    pending_event_authority,
)
from .authority_plan import prepare_plan, publish_plan

__all__ = [
    "ALL_DELEGABLE_CAPABILITIES",
    "_CAPABILITY_ACTIONS",
    "_EVENT_MANAGE_STAFF",
    "_ROLE_CAPABILITIES",
    "_admit_staff_actor",
    "_confirm_transition",
    "_create_transition",
    "_fence_staff_actor",
    "_is_original_transition_credential",
    "_live_staff_role",
    "_pending_transition",
    "_resolve_owned_event_for_staff",
    "actions_for_role",
    "actor_has_event_capability",
    "actor_is_active_ctf_organizer",
    "assign_event_staff",
    "capabilities_for_role",
    "capability_action_pairs",
    "eligible_co_organizer_ids",
    "event_notification_organizer_ids",
    "event_notification_organizers",
    "list_event_staff",
    "pending_event_authority",
    "revoke_event_staff",
    "staff_row_grants_capability",
    "transfer_event_ownership",
]

_EVENT_OWNERSHIP_CHANGED_CONCURRENTLY = "Event ownership changed concurrently"
_EVENT_STAFF_CHANGED_CONCURRENTLY = "Event staff changed concurrently"

logger = logging.getLogger(__name__)

# Fail-closed role -> capability map. Co-organizers receive the full operational
# set as an explicit, enumerated grant (never a wildcard), so a newly added
# capability is authorized only when intentionally added here.


# Closed set of delegable capability nouns, sorted for deterministic projections.
# Owner and platform-admin authority cover all of them; a staff role covers a
# subset (a full co-organizer covers all). Advisory only — the server re-checks
# per operation (ADR-052-R2).


def assign_event_staff(event_id: UUID, actor: EventActor, principal_uuid: UUID, role: str) -> CTFEventStaff:
    """Assign an active human or service principal by UUID under native policy.

    Validate the complete role delta before retaining its immutable plan. Publish
    through the policy journal, then atomically update staff and audit facts.
    Exact accepted plans remain settleable if their subject later deactivates;
    current actor proof and delegation are still required. An unchanged role is
    an idempotent no-op.
    """
    actor = _admit_staff_actor(actor)

    if role not in _ROLE_CAPABILITIES:
        raise CTFValidationError(
            "Invalid staff role",
            code="CTF_INVALID_STAFF_ROLE",
            details={"role": role, "valid": sorted(_ROLE_CAPABILITIES)},
        )
    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.ASSIGN,
        target_principal_uuid=principal_uuid,
        requested_role=role,
    )
    target_principal, target_user = _assignment_target(principal_uuid, transition_row is not None)
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, _EVENT_MANAGE_STAFF)
        if target_principal.uuid == event.owner_principal_uuid:
            raise CTFValidationError(
                "The event organizer already holds every capability",
                code="CTF_STAFF_IS_ORGANIZER",
            )
        expected_staff = CTFEventStaff.objects.filter(event=event, principal_uuid=target_principal.uuid).first()
        expected_role = expected_staff.role if expected_staff else ""
        if expected_staff is not None and expected_role == role:
            return expected_staff
        prepared_plan = prepare_plan(
            event,
            actor,
            kind="assign",
            target_uuid=target_principal.uuid,
            previous_role=expected_role,
            requested_role=role,
        )
        accepted = _retain_assignment(event, actor, target_principal.uuid, role, expected_role, prepared_plan)
        if isinstance(accepted, CTFEventStaff):
            return accepted
        transition_row = accepted
        event = transition_row.event
    else:
        event = transition_row.event
    publish_plan(transition_row, actor)
    staff, action = _commit_assignment(event, actor, transition_row, target_principal, target_user, role)
    logger.info(
        "%s staff %s on event %s as %s",
        "Assigned" if action == "assigned" else "Re-roled",
        target_principal.uuid,
        event.pk,
        role,
    )
    return staff


def revoke_event_staff(event_id: UUID, actor: EventActor, principal_uuid: UUID) -> bool:
    """Revoke an exact staff assignment with current native policy authority.

    Retain and confirm the complete revocation plan before deleting the staff
    projection. Revoking staff never changes the canonical owner principal.
    """
    from ctf.services.audit import audit_event_staff_change

    actor = _admit_staff_actor(actor)

    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.REVOKE,
        target_principal_uuid=principal_uuid,
    )
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, _EVENT_MANAGE_STAFF)
        expected_staff = CTFEventStaff.objects.filter(event=event, principal_uuid=principal_uuid).first()
        if expected_staff is None:
            raise CTFNotFoundError("Staff assignment not found", details={"principal_uuid": str(principal_uuid)})
        prepared_plan = prepare_plan(
            event, actor, kind="revoke", target_uuid=principal_uuid, previous_role=expected_staff.role
        )
        with transaction.atomic():
            _fence_staff_actor(actor)
            locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
            if locked_event.owner_principal_uuid != event.owner_principal_uuid:
                raise CTFValidationError(_EVENT_OWNERSHIP_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
            staff = CTFEventStaff.objects.filter(
                event=locked_event,
                principal_uuid=principal_uuid,
                deleted_at__isnull=True,
            ).first()
            if staff is None:
                raise CTFNotFoundError("Staff assignment not found", details={"principal_uuid": str(principal_uuid)})
            if staff.role != expected_staff.role:
                raise CTFValidationError(_EVENT_STAFF_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
            transition_row = _create_transition(
                locked_event,
                actor,
                kind=CTFEventAuthorityTransition.Kind.REVOKE,
                target_principal_uuid=principal_uuid,
                previous_role=staff.role,
                prepared_plan=prepared_plan,
            )
        event = locked_event
    else:
        event = transition_row.event
    revoked_role = transition_row.previous_role
    publish_plan(transition_row, actor)
    with transaction.atomic():
        _fence_staff_actor(actor)
        CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        staff = CTFEventStaff.objects.filter(
            event=event, principal_uuid=principal_uuid, deleted_at__isnull=True, role=revoked_role
        ).first()
        if staff is None:
            raise CTFValidationError(_EVENT_STAFF_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
        staff.delete(soft=True)
        from ctf.services.communication import on_organizer_removed

        on_organizer_removed(event, principal_uuid)
        audit_event_staff_change(
            actor_id=actor,
            event_id=event.pk,
            target_principal_uuid=principal_uuid,
            action="revoked",
            previous_role=revoked_role,
        )
        _confirm_transition(transition_row)
        _fence_staff_actor(actor)
    logger.info("Revoked staff %s on event %s", safe_log_value(principal_uuid), event.pk)
    return True


def transfer_event_ownership(event_id: UUID, actor: EventActor, new_owner_principal_uuid: UUID) -> CTFEvent:
    """Transfer ownership to an active human or service co-organizer principal.

    Current transfer authority and full delegation admit an immutable effect
    plan. After policy publication is confirmed, one SQL transaction changes
    canonical ownership, retires the recipient's staff row and retains the
    previous owner as co-organizer. Accepted plans can settle inactive subjects
    using a live authorized administrator. Related resource ownership is a
    separate command and does not move implicitly.
    """
    from management.services import (
        human_principal_identity_user_id,
        principal_identity_for_uuid,
    )

    actor = _admit_staff_actor(actor)

    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.TRANSFER,
        target_principal_uuid=new_owner_principal_uuid,
    )
    target_principal, target_user_id = _transfer_target(new_owner_principal_uuid, transition_row is not None)
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, "event.transfer_ownership")
        previous_owner_uuid = event.owner_principal_uuid
        if target_principal.uuid == previous_owner_uuid:
            raise CTFValidationError(
                "The target already owns this event",
                code="CTF_ALREADY_OWNER",
            )
        prepared_plan = prepare_plan(event, actor, kind="transfer", target_uuid=target_principal.uuid)
        with transaction.atomic():
            _fence_staff_actor(actor)
            locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
            if locked_event.owner_principal_uuid != previous_owner_uuid:
                raise CTFValidationError(_EVENT_OWNERSHIP_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
            target_staff = CTFEventStaff.objects.filter(
                event=locked_event,
                principal_uuid=target_principal.uuid,
                role=EventStaffRole.CO_ORGANIZER.value,
                deleted_at__isnull=True,
            ).first()
            if target_staff is None:
                raise CTFValidationError(
                    "Ownership can only transfer to a current co-organizer",
                    code="CTF_TRANSFER_TARGET_INVALID",
                )
            transition_row = _create_transition(
                locked_event,
                actor,
                kind=CTFEventAuthorityTransition.Kind.TRANSFER,
                target_principal_uuid=target_principal.uuid,
                prepared_plan=prepared_plan,
            )
    else:
        retained_owner_uuid = transition_row.previous_owner_principal_uuid
        if retained_owner_uuid is None:
            raise CTFValidationError("Event ownership transition is invalid", code="CTF_STAFF_CONFLICT")
        previous_owner_uuid = retained_owner_uuid
    previous_owner = principal_identity_for_uuid(previous_owner_uuid)
    previous_owner_user_id = (
        human_principal_identity_user_id(previous_owner) if previous_owner.kind == "human" else None
    )
    publish_plan(transition_row, actor)
    event = _commit_ownership_transfer(
        actor,
        transition_row,
        target_principal,
        target_user_id,
        previous_owner_uuid,
        previous_owner_user_id,
        new_owner_principal_uuid,
    )
    logger.info(
        "Transferred ownership of event %s to principal %s",
        event.pk,
        safe_log_value(new_owner_principal_uuid),
    )
    return event


def list_event_staff(event_id: UUID) -> QuerySet[CTFEventStaff]:
    """List live staff assignments for an event."""
    return (
        CTFEventStaff.objects.filter(event_id=event_id, deleted_at__isnull=True)
        .select_related("user")
        .order_by("created_at")
    )
