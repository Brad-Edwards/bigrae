"""Locked SQL projections for confirmed CTF staff and ownership effect plans."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from ctf.enums import EventStaffRole
from ctf.exceptions import CTFNotFoundError, CTFValidationError
from ctf.models import CTFEvent, CTFEventAuthorityTransition, CTFEventStaff

from ._staff_transitions import _confirm_transition, _fence_staff_actor

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import User

    from ctf.services.actors import EventActor
    from shared.identity_scope import PrincipalRef

_EVENT_OWNERSHIP_CHANGED_CONCURRENTLY = "Event ownership changed concurrently"
_EVENT_STAFF_CHANGED_CONCURRENTLY = "Event staff changed concurrently"


def _assignment_target(principal_uuid: UUID, retained: bool) -> tuple[PrincipalRef, User | None]:
    """Resolve an active new subject or the exact identity of an accepted retry."""
    from management.services import (
        PrincipalConflictError,
        human_principal_identity_user_id,
        human_principal_user_id,
        principal_identity_for_uuid,
        resolve_principal_uuid,
    )

    try:
        target_principal = (
            principal_identity_for_uuid(principal_uuid) if retained else resolve_principal_uuid(principal_uuid)
        )
    except PrincipalConflictError:
        raise CTFNotFoundError("Active principal not found", details={"principal_uuid": str(principal_uuid)}) from None
    target_user = None
    if target_principal.kind == "human":
        from django.contrib.auth import get_user_model

        target_user = (
            get_user_model()
            .objects.filter(
                pk=(
                    human_principal_identity_user_id(target_principal)
                    if retained
                    else human_principal_user_id(target_principal)
                )
            )
            .first()
        )
        if target_user is None:
            raise CTFNotFoundError("Active principal not found", details={"principal_uuid": str(principal_uuid)})
    return target_principal, target_user


def _commit_assignment(
    event: CTFEvent,
    actor: EventActor,
    transition_row: CTFEventAuthorityTransition,
    target_principal: PrincipalRef,
    target_user: User | None,
    role: str,
) -> tuple[CTFEventStaff, str]:
    """Commit the confirmed role and its audit record under event and journal locks."""
    from ctf.services.audit import audit_event_staff_change

    retained_previous_role = transition_row.previous_role or None
    with transaction.atomic():
        _fence_staff_actor(actor)
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        current = CTFEventStaff.objects.filter(
            event=locked_event, principal_uuid=target_principal.uuid, deleted_at__isnull=True
        ).first()
        if (None if current is None else current.role) != retained_previous_role:
            raise CTFValidationError(_EVENT_STAFF_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
        if current is None:
            staff = CTFEventStaff.objects.create(
                event=locked_event, user=target_user, principal_uuid=target_principal.uuid, role=role
            )
            action = "assigned"
        else:
            current.role = role
            current.save(update_fields=["role", "updated_at"])
            staff = current
            action = "reroled"
            if retained_previous_role == EventStaffRole.CO_ORGANIZER.value and role != retained_previous_role:
                from ctf.services.communication import on_organizer_removed

                on_organizer_removed(locked_event, target_principal.uuid)
        audit_event_staff_change(
            actor_id=actor,
            event_id=event.pk,
            target_principal_uuid=target_principal.uuid,
            action=action,
            role=role,
            previous_role=retained_previous_role,
        )
        _confirm_transition(transition_row)
        _fence_staff_actor(actor)
    return staff, action


def _commit_ownership_transfer(
    actor: EventActor,
    transition_row: CTFEventAuthorityTransition,
    target_principal: PrincipalRef,
    target_user_id: int | None,
    previous_owner_uuid: UUID,
    previous_owner_user_id: int | None,
    new_owner_principal_uuid: UUID,
) -> CTFEvent:
    """Commit confirmed ownership, staff projection and audit in the same transaction."""
    from ctf.services.audit import audit_event_ownership_transferred

    with transaction.atomic():
        _fence_staff_actor(actor)
        event = CTFEvent.objects.select_for_update().get(pk=transition_row.event_id, deleted_at__isnull=True)
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        if event.owner_principal_uuid != previous_owner_uuid:
            raise CTFValidationError(_EVENT_OWNERSHIP_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
        target_staff = CTFEventStaff.objects.filter(
            event=event,
            principal_uuid=new_owner_principal_uuid,
            role=EventStaffRole.CO_ORGANIZER.value,
            deleted_at__isnull=True,
        ).first()
        if target_staff is None:
            raise CTFValidationError(_EVENT_STAFF_CHANGED_CONCURRENTLY, code="CTF_STAFF_CONFLICT")
        event.owner_principal_uuid = target_principal.uuid
        event.created_by_id = target_user_id
        event.save(update_fields=["owner_principal_uuid", "created_by", "updated_at"])
        # The new owner no longer needs a staff row; the previous owner keeps full
        # access as a co-organizer so the change is reversible by the new owner.
        target_staff.delete(soft=True)
        CTFEventStaff.objects.create(
            event=event,
            user_id=previous_owner_user_id,
            principal_uuid=previous_owner_uuid,
            role=EventStaffRole.CO_ORGANIZER.value,
        )
        audit_event_ownership_transferred(
            actor_id=actor,
            event_id=event.pk,
            previous_owner_uuid=previous_owner_uuid,
            new_owner_uuid=new_owner_principal_uuid,
        )
        _confirm_transition(transition_row)
        _fence_staff_actor(actor)
    return event


def _transfer_target(principal_uuid: UUID, retained: bool) -> tuple[PrincipalRef, int | None]:
    """Keep new transfers active-only while resolving exact identities for accepted retries."""
    from management.services import (
        PrincipalConflictError,
        human_principal_identity_user_id,
        human_principal_user_id,
        principal_identity_for_uuid,
        resolve_principal_uuid,
    )

    try:
        target_principal = (
            principal_identity_for_uuid(principal_uuid) if retained else resolve_principal_uuid(principal_uuid)
        )
    except PrincipalConflictError:
        raise CTFValidationError(
            "Ownership can only transfer to an active principal", code="CTF_TRANSFER_TARGET_INELIGIBLE"
        ) from None
    target_user_id = None
    if target_principal.kind == "human":
        target_user_id = (
            human_principal_identity_user_id(target_principal)
            if retained
            else human_principal_user_id(target_principal)
        )
    return target_principal, target_user_id
