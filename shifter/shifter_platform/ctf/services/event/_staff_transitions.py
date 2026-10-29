"""Retained staff authority receipts and credential fences for command recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from ctf.exceptions import CTFPermissionError, CTFValidationError
from ctf.models import CTFEvent, CTFEventAuthorityTransition, CTFEventStaff

from ._staff_policy import _EVENT_MANAGE_STAFF, _resolve_owned_event_for_staff
from .authority_plan import AuthorityPlan

if TYPE_CHECKING:
    from uuid import UUID

    from ctf.services.actors import EventActor
    from shared.credentials import CredentialContext


def _admit_staff_actor(actor: EventActor) -> EventActor:
    """Revalidate native proofs before both initial commands and recovery."""
    from ctf.exceptions import CTFError
    from ctf.services.actors import current_event_actor_credential
    from shared.credentials import CredentialContext, CredentialUnavailable

    try:
        current = current_event_actor_credential(actor)
        return current if isinstance(actor, CredentialContext) else actor
    except CredentialUnavailable:
        raise CTFError("Event authority unavailable", code="CTF_AUTHORITY_UNAVAILABLE") from None
    except ValueError:
        raise CTFPermissionError("Event authority denied") from None


def _fence_staff_actor(actor: EventActor) -> None:
    """Fence the current proof without provider I/O while committing the projection."""
    from ctf.services.actors import fence_event_actor

    try:
        fence_event_actor(actor)
    except ValueError:
        raise CTFPermissionError("Event authority recovery denied") from None


def _is_original_transition_credential(transition_id: UUID, credential: CredentialContext) -> bool:
    """Only the original live proof may use confirmation without a replay grant."""
    return CTFEventAuthorityTransition.objects.filter(
        pk=transition_id,
        actor_principal_uuid=credential.principal.uuid,
        actor_credential_uuid=credential.credential_uuid,
    ).exists()


def _pending_transition(
    event_id: UUID,
    actor: EventActor,
    *,
    kind: str,
    target_principal_uuid: UUID,
    requested_role: str = "",
) -> CTFEventAuthorityTransition | None:
    """Return an exact resumable intent or reject a conflicting pending change."""
    from ctf.services.actors import event_actor_credential

    credential = event_actor_credential(actor)
    pending = CTFEventAuthorityTransition.objects.filter(
        event_id=event_id,
        state=CTFEventAuthorityTransition.State.PENDING,
    ).first()
    if pending is None:
        return None
    if (
        pending.kind != kind
        or pending.target_principal_uuid != target_principal_uuid
        or pending.requested_role != requested_role
    ):
        raise CTFValidationError("Event authority changed concurrently", code="CTF_STAFF_CONFLICT")
    if (pending.actor_principal_uuid, pending.actor_credential_uuid) != (
        credential.principal.uuid,
        credential.credential_uuid,
    ):
        action = (
            "event.transfer_ownership" if kind == CTFEventAuthorityTransition.Kind.TRANSFER else _EVENT_MANAGE_STAFF
        )
        _resolve_owned_event_for_staff(event_id, actor, action)
    return pending


def pending_event_authority(event_id: UUID, actor: EventActor) -> dict[str, object] | None:
    """Expose only an authorized command receipt; status reads publish no effects."""
    from workspaces.services import confirmed_authorization_effects

    from .authority_plan import retained_plan

    actor = _admit_staff_actor(actor)
    _resolve_owned_event_for_staff(event_id, actor, _EVENT_MANAGE_STAFF)
    transition = CTFEventAuthorityTransition.objects.filter(event_id=event_id, state="pending").first()
    if transition is None:
        return None
    plan = retained_plan(transition)
    return {
        "id": str(transition.pk),
        "kind": transition.kind,
        "target_principal_uuid": str(transition.target_principal_uuid),
        "requested_role": transition.requested_role,
        "created_at": transition.created_at.isoformat(),
        "total_effects": len(plan.effects),
        "completed_effects": confirmed_authorization_effects(
            tuple(effect.key for effect in plan.effects), store_id=plan.store_id, model_id=plan.model_id
        ),
    }


def _create_transition(
    event: CTFEvent,
    actor: EventActor,
    *,
    kind: str,
    target_principal_uuid: UUID,
    previous_role: str = "",
    requested_role: str = "",
    prepared_plan: AuthorityPlan,
) -> CTFEventAuthorityTransition:
    """Persist one provider-bound intent while the caller holds the event lock."""
    from ctf.services.actors import event_actor_credential

    credential = event_actor_credential(actor)
    previous_owner_principal_uuid = event.owner_principal_uuid if kind == "transfer" else None
    _fence_staff_actor(actor)
    if CTFEventAuthorityTransition.objects.filter(event=event, state="pending").exists():
        raise CTFValidationError("Event authority changed concurrently", code="CTF_STAFF_CONFLICT")
    if (
        prepared_plan.event_uuid,
        prepared_plan.kind,
        prepared_plan.target_uuid,
        prepared_plan.previous_role,
        prepared_plan.requested_role,
        prepared_plan.previous_owner_uuid,
    ) != (event.pk, kind, target_principal_uuid, previous_role, requested_role, previous_owner_principal_uuid):
        raise CTFValidationError("Event authority intent changed", code="CTF_STAFF_CONFLICT")
    if kind in {CTFEventAuthorityTransition.Kind.TRANSFER, CTFEventAuthorityTransition.Kind.ASSIGN}:
        from management.services import PrincipalConflictError, resolve_principal_uuid

        try:
            resolve_principal_uuid(target_principal_uuid)
        except PrincipalConflictError:
            raise CTFValidationError(
                "Ownership can only transfer to an active principal", code="CTF_TRANSFER_TARGET_INELIGIBLE"
            ) from None
    return CTFEventAuthorityTransition.objects.create(
        id=prepared_plan.transition_uuid,
        effect_plan=prepared_plan.payload(),
        effect_plan_digest=prepared_plan.digest,
        event=event,
        kind=kind,
        actor_principal_uuid=credential.principal.uuid,
        actor_credential_uuid=credential.credential_uuid,
        target_principal_uuid=target_principal_uuid,
        previous_owner_principal_uuid=previous_owner_principal_uuid,
        previous_role=previous_role,
        requested_role=requested_role,
    )


def _confirm_transition(transition: CTFEventAuthorityTransition) -> None:
    """Mark a locked transition complete in the same commit as its SQL projection."""
    transition.state = CTFEventAuthorityTransition.State.CONFIRMED
    transition.confirmed_at = timezone.now()
    transition.save(update_fields=["state", "confirmed_at", "updated_at"])


def _retain_assignment(
    event: CTFEvent,
    actor: EventActor,
    target_uuid: UUID,
    role: str,
    expected_role: str,
    prepared_plan: AuthorityPlan,
) -> CTFEventStaff | CTFEventAuthorityTransition:
    """Fence ownership and role before retaining one accepted assignment plan."""
    from django.db import transaction

    with transaction.atomic():
        _fence_staff_actor(actor)
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
        if locked_event.owner_principal_uuid != event.owner_principal_uuid:
            raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
        staff = CTFEventStaff.objects.filter(
            event=locked_event, principal_uuid=target_uuid, deleted_at__isnull=True
        ).first()
        previous_role = "" if staff is None else staff.role
        if previous_role != expected_role:
            raise CTFValidationError("Event staff changed concurrently", code="CTF_STAFF_CONFLICT")
        if staff is not None and staff.role == role:
            # Idempotent re-submission performs no provider, SQL, or audit write.
            return staff
        transition_row = _create_transition(
            locked_event,
            actor,
            kind=CTFEventAuthorityTransition.Kind.ASSIGN,
            target_principal_uuid=target_uuid,
            previous_role=previous_role,
            requested_role=role,
            prepared_plan=prepared_plan,
        )
    return transition_row
