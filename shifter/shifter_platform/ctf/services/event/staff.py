"""CTF-owned staff roles and durable authority publication (ADR-066).

SQL records canonical ownership and role display. Current policy and credential
ceilings authorize commands; those SQL facts do not grant permission. Each
mutation retains an exact effect plan before publication and projects SQL only
after the shared policy journal confirms every effect.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from ctf.enums import EventCapability, EventStaffRole
from ctf.exceptions import CTFNotFoundError, CTFPermissionError, CTFValidationError
from ctf.models import CTFEvent, CTFEventAuthorityTransition, CTFEventStaff
from shared.log_sanitize import safe_log_value

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import AnonymousUser
    from django.db.models import QuerySet

    from ctf.services.actors import EventActor

from .authority_plan import AuthorityPlan, prepare_plan, publish_plan

logger = logging.getLogger(__name__)

# Fail-closed role -> capability map. Co-organizers receive the full operational
# set as an explicit, enumerated grant (never a wildcard), so a newly added
# capability is authorized only when intentionally added here.
_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    EventStaffRole.MODERATOR.value: frozenset(
        {EventCapability.PARTICIPANTS.value, EventCapability.NOTIFICATIONS.value}
    ),
    EventStaffRole.JUDGE.value: frozenset({EventCapability.AWARDS.value, EventCapability.SUBMISSIONS.value}),
    EventStaffRole.CO_ORGANIZER.value: frozenset(cap.value for cap in EventCapability),
}

_CAPABILITY_ACTIONS = {
    EventCapability.CONFIG.value: "event.manage_config",
    EventCapability.CHALLENGES.value: "event.manage_challenges",
    EventCapability.PARTICIPANTS.value: "event.manage_participants",
    EventCapability.TEAMS.value: "event.manage_teams",
    EventCapability.RANGES.value: "event.manage_ranges",
    EventCapability.SCORING.value: "event.manage_scoring",
    EventCapability.NOTIFICATIONS.value: "event.manage_communications",
    EventCapability.AWARDS.value: "event.manage_awards",
    EventCapability.SUBMISSIONS.value: "event.manage_submissions",
    EventCapability.CONTENT.value: "event.manage_content",
    EventCapability.LIFECYCLE.value: "event.manage_lifecycle",
    EventCapability.DELETE.value: "event.delete",
}

# Closed set of delegable capability nouns, sorted for deterministic projections.
# Owner and platform-admin authority cover all of them; a staff role covers a
# subset (a full co-organizer covers all). Advisory only — the server re-checks
# per operation (ADR-052-R2).
ALL_DELEGABLE_CAPABILITIES: tuple[str, ...] = tuple(sorted(set().union(*_ROLE_CAPABILITIES.values())))


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


def _is_original_transition_credential(transition_id: UUID, credential) -> bool:
    """Only the original live proof may use confirmation without a replay grant."""
    return CTFEventAuthorityTransition.objects.filter(
        pk=transition_id,
        actor_principal_uuid=credential.principal.uuid,
        actor_credential_uuid=credential.credential_uuid,
    ).exists()


def capabilities_for_role(role: str | None) -> tuple[str, ...]:
    """Return the sorted delegable capabilities a staff ``role`` grants (empty if none)."""
    return tuple(sorted(_ROLE_CAPABILITIES.get(role or "", frozenset())))


def actions_for_role(role: str | None) -> tuple[str, ...]:
    """Return the exact OpenFGA application actions granted by a staff role."""
    capabilities = capabilities_for_role(role)
    actions = {_CAPABILITY_ACTIONS[item] for item in capabilities}
    if EventCapability.CONFIG.value in capabilities:
        actions.add("event.read")
    return tuple(sorted(actions))


def capability_action_pairs() -> tuple[tuple[str, str], ...]:
    """Return the closed UI capability-to-policy-action mapping."""
    return tuple(sorted(_CAPABILITY_ACTIONS.items()))


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
            "event.transfer_ownership" if kind == CTFEventAuthorityTransition.Kind.TRANSFER else "event.manage_staff"
        )
        _resolve_owned_event_for_staff(event_id, actor, action)
    return pending


def pending_event_authority(event_id: UUID, actor: EventActor) -> dict[str, object] | None:
    """Expose only an authorized command receipt; status reads publish no effects."""
    from workspaces.services import confirmed_authorization_effects

    from .authority_plan import retained_plan

    actor = _admit_staff_actor(actor)
    _resolve_owned_event_for_staff(event_id, actor, "event.manage_staff")
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
    previous_owner_principal_uuid: UUID | None = None,
    prepared_plan: AuthorityPlan,
) -> CTFEventAuthorityTransition:
    """Persist one provider-bound intent while the caller holds the event lock."""
    from ctf.services.actors import event_actor_credential

    credential = event_actor_credential(actor)
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


def _live_staff_role(event: CTFEvent, user_id: int) -> str | None:
    """Return the live (non-revoked) staff role for ``user_id`` on ``event``, or None."""
    return (
        CTFEventStaff.objects.filter(event=event, user_id=user_id, deleted_at__isnull=True)
        .values_list("role", flat=True)
        .first()
    )


def actor_is_active_ctf_organizer(user_id: int | None) -> bool:
    """Return whether ``user_id`` is an active account with the global CTF organizer role.

    Staff-derived authority is only valid while the account still holds the
    platform CTF Organizer role: a live ``CTFEventStaff`` row must never be an
    ambient source of authority after the global role is revoked or the account
    is deactivated (#1922 review — stale-row bypass). The owner and platform-admin
    branches do not use this; canonical ownership and the superuser override are
    separate invariants.
    """
    if user_id is None:
        return False
    from ctf.bridges import get_user_role

    user = User.objects.filter(pk=user_id, is_active=True).first()
    return user is not None and get_user_role(user).is_ctf_organizer


def staff_row_grants_capability(actor_pk: int, event: CTFEvent, capability: str) -> bool:
    """Return whether a live staff row for ``actor_pk`` on ``event`` grants ``capability``.

    The raw delegation check only: no owner or platform-admin fallback. The
    authority resolver composes this with owner and override authority so the
    least-authority order lives in one place (ADR-052-R2). A live row grants a
    capability only while the account still holds the global CTF Organizer role
    and is active, so a demoted account keeps no authority through a stale row
    (#1922 review).
    """
    role = _live_staff_role(event, actor_pk)
    if role is None or capability not in _ROLE_CAPABILITIES.get(role, frozenset()):
        return False
    return actor_is_active_ctf_organizer(actor_pk)


def eligible_co_organizer_ids(event: CTFEvent) -> list[int]:
    """Project active human co-organizer candidates; delivery checks native policy."""
    from management.services import active_human_user_ids

    principal_uuids = tuple(
        CTFEventStaff.objects.filter(event=event, role=EventStaffRole.CO_ORGANIZER.value)
        .order_by("principal_uuid")
        .values_list("principal_uuid", flat=True)
    )
    return list(active_human_user_ids(principal_uuids))


def event_notification_organizer_ids(event: CTFEvent) -> list[int]:
    """Map current owner and co-organizer principals without remote policy I/O."""
    from management.services import active_human_user_ids

    principal_uuids = (
        event.owner_principal_uuid,
        *CTFEventStaff.objects.filter(event=event, role=EventStaffRole.CO_ORGANIZER.value)
        .order_by("principal_uuid")
        .values_list("principal_uuid", flat=True),
    )
    return list(active_human_user_ids(principal_uuids))


def actor_has_event_capability(actor: User | AnonymousUser, event: CTFEvent, capability: str) -> bool:
    """Return whether ``actor`` may exercise ``capability`` on ``event``.

    Admits the owning organizer, a live staff row whose role grants the
    capability, or the platform-admin override (ADR-052). Delegates to the
    service-owned authority resolver so callers share one least-authority policy.
    """
    from ctf.services.authorization import resolve_event_authority

    return resolve_event_authority(actor, event, capability=capability) is not None


def _resolve_owned_event_for_staff(event_id: UUID, actor: EventActor, action: str) -> CTFEvent:
    """Resolve the live event and require the exact native policy action.

    Ownership and staff rows describe the event; current policy and the actor's
    credential ceiling authorize this command. Full effect delegation is checked
    separately before retaining a new transition and while settling its effects.
    """
    from ctf.services.authorization import assert_event_action

    try:
        event = CTFEvent.objects.get(pk=event_id, deleted_at__isnull=True)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(f"Event {event_id} not found", details={"event_id": str(event_id)}) from None
    from ctf.exceptions import CTFPermissionError

    try:
        assert_event_action(actor, event, action)
    except CTFPermissionError as exc:
        raise CTFPermissionError("Event staff authority denied") from exc
    return event


def assign_event_staff(event_id: UUID, actor: EventActor, principal_uuid: UUID, role: str) -> CTFEventStaff:
    """Assign an active human or service principal by UUID under native policy.

    Validate the complete role delta before retaining its immutable plan. Publish
    through the policy journal, then atomically update staff and audit facts.
    Exact accepted plans remain settleable if their subject later deactivates;
    current actor proof and delegation are still required. An unchanged role is
    an idempotent no-op.
    """
    from ctf.services.audit import audit_event_staff_change
    from management.services import (
        PrincipalConflictError,
        human_principal_identity_user_id,
        human_principal_user_id,
        principal_identity_for_uuid,
        resolve_principal_uuid,
    )

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
    try:
        target_principal = (
            principal_identity_for_uuid(principal_uuid)
            if transition_row is not None
            else resolve_principal_uuid(principal_uuid)
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
                    if transition_row is not None
                    else human_principal_user_id(target_principal)
                )
            )
            .first()
        )
        if target_user is None:
            raise CTFNotFoundError("Active principal not found", details={"principal_uuid": str(principal_uuid)})
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, "event.manage_staff")
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
        with transaction.atomic():
            _fence_staff_actor(actor)
            locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
            if locked_event.owner_principal_uuid != event.owner_principal_uuid:
                raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
            staff = CTFEventStaff.objects.filter(
                event=locked_event, principal_uuid=target_principal.uuid, deleted_at__isnull=True
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
                target_principal_uuid=target_principal.uuid,
                previous_role=previous_role,
                requested_role=role,
                prepared_plan=prepared_plan,
            )
        event = locked_event
    else:
        event = transition_row.event
    retained_previous_role = transition_row.previous_role or None
    publish_plan(transition_row, actor)
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
            raise CTFValidationError("Event staff changed concurrently", code="CTF_STAFF_CONFLICT")
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
        event = _resolve_owned_event_for_staff(event_id, actor, "event.manage_staff")
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
                raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
            staff = CTFEventStaff.objects.filter(
                event=locked_event,
                principal_uuid=principal_uuid,
                deleted_at__isnull=True,
            ).first()
            if staff is None:
                raise CTFNotFoundError("Staff assignment not found", details={"principal_uuid": str(principal_uuid)})
            if staff.role != expected_staff.role:
                raise CTFValidationError("Event staff changed concurrently", code="CTF_STAFF_CONFLICT")
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
            raise CTFValidationError("Event staff changed concurrently", code="CTF_STAFF_CONFLICT")
        staff.delete(soft=True)
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
    from ctf.services.audit import audit_event_ownership_transferred
    from management.services import (
        PrincipalConflictError,
        human_principal_identity_user_id,
        human_principal_user_id,
        principal_identity_for_uuid,
        resolve_principal_uuid,
    )

    actor = _admit_staff_actor(actor)

    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.TRANSFER,
        target_principal_uuid=new_owner_principal_uuid,
    )
    try:
        target_principal = (
            principal_identity_for_uuid(new_owner_principal_uuid)
            if transition_row is not None
            else resolve_principal_uuid(new_owner_principal_uuid)
        )
    except PrincipalConflictError:
        raise CTFValidationError(
            "Ownership can only transfer to an active principal", code="CTF_TRANSFER_TARGET_INELIGIBLE"
        ) from None
    target_user_id = None
    if target_principal.kind == "human":
        target_user_id = (
            human_principal_identity_user_id(target_principal)
            if transition_row is not None
            else human_principal_user_id(target_principal)
        )
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
                raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
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
                previous_owner_principal_uuid=previous_owner_uuid,
                prepared_plan=prepared_plan,
            )
        event = locked_event
    else:
        event = transition_row.event
        retained_owner_uuid = transition_row.previous_owner_principal_uuid
        if retained_owner_uuid is None:
            raise CTFValidationError("Event ownership transition is invalid", code="CTF_STAFF_CONFLICT")
        previous_owner_uuid = retained_owner_uuid
    previous_owner = principal_identity_for_uuid(previous_owner_uuid)
    previous_owner_user_id = (
        human_principal_identity_user_id(previous_owner) if previous_owner.kind == "human" else None
    )
    publish_plan(transition_row, actor)
    with transaction.atomic():
        _fence_staff_actor(actor)
        event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        if event.owner_principal_uuid != previous_owner_uuid:
            raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
        target_staff = CTFEventStaff.objects.filter(
            event=event,
            principal_uuid=new_owner_principal_uuid,
            role=EventStaffRole.CO_ORGANIZER.value,
            deleted_at__isnull=True,
        ).first()
        if target_staff is None:
            raise CTFValidationError("Event staff changed concurrently", code="CTF_STAFF_CONFLICT")
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
