"""Event-staff roles and authority-topology mutations (CTF-607, #1922, #1923).

The owning organizer (``CTFEvent.created_by``) is the single canonical owner and
always holds every capability. ``CTFEventStaff`` rows grant other organizer-tier
users a role-scoped slice of one event's management surface, keyed by a closed
:class:`ctf.enums.EventCapability` vocabulary and a fail-closed role map:

- ``moderator``: ``participants`` and ``notifications``
- ``judge``: ``awards`` and ``submissions``
- ``co_organizer``: every operational capability (configuration, challenges,
  participants, teams, ranges, scoring, notifications, awards, submissions,
  content, lifecycle, deletion)

Authority is resolved centrally by ``ctf.services.authorization`` (ADR-052): the
least authority that admits the actor, in the order owner, delegated event-staff
capability, then the platform-admin override. Authority-topology operations —
listing/assigning/re-roling/revoking staff and transferring canonical ownership —
are owner-only (``capability=None``): delegated staff can never exercise them, so
a co-organizer cannot escalate itself or remove the owner's control; the
platform administrator may, as legitimate global administration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from ctf.enums import EventCapability, EventStaffRole
from ctf.exceptions import CTFNotFoundError, CTFValidationError
from ctf.models import CTFEvent, CTFEventAuthorityTransition, CTFEventStaff
from shared.authorization import PolicyEffect
from shared.log_sanitize import safe_log_value

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import AnonymousUser
    from django.db.models import QuerySet

    from ctf.services.actors import EventActor

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


def _publish_staff_role(
    event: CTFEvent,
    actor: EventActor,
    principal_uuid: UUID,
    previous_role: str | None,
    role: str | None,
    *,
    transition_id: UUID,
    ownership_handoff: bool = False,
) -> None:
    """Journal and read back every action delta before updating the SQL projection."""
    from ctf.exceptions import CTFStateError
    from ctf.services.actors import event_actor_credential
    from ctf.services.credential_scope import event_credential_scope
    from shared.authorization import (
        PolicyEffect,
        RelationshipSubject,
        TargetRef,
        configured_authorization_provider,
    )
    from workspaces.services import (
        AUTHORIZATION_MUTATION_CONFIRMED,
        EventOwnershipHandoffPolicyMutationRequest,
        PolicyMutationRequest,
        apply_policy_mutation,
    )

    credential = event_actor_credential(actor)
    provider = configured_authorization_provider()
    scope = event_credential_scope(event.pk)
    old_actions = set(actions_for_role(previous_role))
    new_actions = set(actions_for_role(role))
    for index, action in enumerate(sorted(old_actions | new_actions)):
        effect = PolicyEffect.GRANT if action in new_actions else PolicyEffect.REVOKE
        request_type = EventOwnershipHandoffPolicyMutationRequest if ownership_handoff else PolicyMutationRequest
        result = apply_policy_mutation(
            request_type(
                actor=credential.principal,
                credential=credential.ceiling,
                subject=RelationshipSubject("principal", principal_uuid),
                action=action,
                target=TargetRef("event", event.pk),
                scope=scope,
                effect=effect,
                idempotency_key=f"event-staff-{transition_id.hex}-{index}",
                model_id=provider.model_id,
            ),
            provider,
        )
        if result.state != AUTHORIZATION_MUTATION_CONFIRMED:
            raise CTFStateError("Event staff authority is unavailable", code="CTF_EVENT_AUTHORITY_UNAVAILABLE")


def _publish_event_administrator(
    event: CTFEvent,
    actor: EventActor,
    principal_uuid: UUID,
    effect: PolicyEffect,
    *,
    transition_id: UUID,
    step: str,
) -> None:
    """Journal and confirm one canonical event-administrator relationship."""
    from ctf.exceptions import CTFStateError
    from ctf.services.actors import event_actor_credential
    from ctf.services.credential_scope import event_credential_scope
    from shared.authorization import (
        AdministrativeRoleChange,
        RelationshipSubject,
        TargetRef,
        configured_authorization_provider,
    )
    from workspaces.services import (
        AUTHORIZATION_MUTATION_CONFIRMED,
        NativeRelationshipMutationRequest,
        apply_native_relationship_mutation,
    )

    credential = event_actor_credential(actor)
    provider = configured_authorization_provider()
    result = apply_native_relationship_mutation(
        NativeRelationshipMutationRequest(
            actor=credential.principal,
            credential=credential.ceiling,
            change=AdministrativeRoleChange(
                RelationshipSubject("principal", principal_uuid),
                "event_administrator",
                TargetRef("event", event.pk),
                effect,
            ),
            scope=event_credential_scope(event.pk),
            idempotency_key=f"event-owner-{transition_id.hex}-{step}",
            model_id=provider.model_id,
        ),
        provider,
    )
    if result.state != AUTHORIZATION_MUTATION_CONFIRMED:
        raise CTFStateError("Event ownership authority is unavailable", code="CTF_EVENT_AUTHORITY_UNAVAILABLE")


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
        or pending.actor_principal_uuid != credential.principal.uuid
        or pending.actor_credential_uuid != credential.credential_uuid
    ):
        raise CTFValidationError("Event authority changed concurrently", code="CTF_STAFF_CONFLICT")
    return pending


def _create_transition(
    event: CTFEvent,
    actor: EventActor,
    *,
    kind: str,
    target_principal_uuid: UUID,
    previous_role: str = "",
    requested_role: str = "",
    previous_owner_principal_uuid: UUID | None = None,
) -> CTFEventAuthorityTransition:
    """Persist one provider-bound intent while the caller holds the event lock."""
    from ctf.services.actors import event_actor_credential

    credential = event_actor_credential(actor)
    return CTFEventAuthorityTransition.objects.create(
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
    """User ids of live full co-organizers on ``event`` who are still eligible.

    Filters live ``co_organizer`` rows to accounts that remain active and hold
    the global CTF Organizer role, so a demoted account stops appearing in
    organizer-directed recipient and subscription projections (#1922 review).
    """
    from shared.auth import CTF_ORGANIZER_GROUP

    return list(
        CTFEventStaff.objects.filter(
            event=event,
            role=EventStaffRole.CO_ORGANIZER.value,
            deleted_at__isnull=True,
            user__is_active=True,
            user__groups__name=CTF_ORGANIZER_GROUP,
        ).values_list("user_id", flat=True)
    )


def actor_has_event_capability(actor: User | AnonymousUser, event: CTFEvent, capability: str) -> bool:
    """Return whether ``actor`` may exercise ``capability`` on ``event``.

    Admits the owning organizer, a live staff row whose role grants the
    capability, or the platform-admin override (ADR-052). Delegates to the
    service-owned authority resolver so callers share one least-authority policy.
    """
    from ctf.services.authorization import resolve_event_authority

    return resolve_event_authority(actor, event, capability=capability) is not None


def _resolve_owned_event_for_staff(event_id: UUID, actor: EventActor, action: str) -> CTFEvent:
    """Load the event and require owner or platform-admin authority (ADR-052).

    Staff management is an owner-only
    capability that delegated moderators/judges/co-organizers cannot exercise
    (``capability=None``), but administering an existing event's delegation graph
    is legitimate platform administration, so the platform-admin override is
    admitted alongside the owner. The override never auto-synthesizes the
    administrator as staff; it only authorizes an explicit, audited mutation.
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
    """Assign (or re-role) a staff member on an owned event.

    Owner-or-platform-admin and locked. The target is resolved by email and must
    be an active organizer-tier platform user — the organizer API surface requires
    that platform role, so a standard user could never exercise the delegation.
    Assigning an existing staff member changes their role; re-submitting the
    current role is an idempotent no-op. Strictly audited on a real mutation.
    """
    from ctf.services.audit import audit_event_staff_change
    from management.services import PrincipalConflictError, human_principal_user_id, resolve_principal_uuid

    if role not in _ROLE_CAPABILITIES:
        raise CTFValidationError(
            "Invalid staff role",
            code="CTF_INVALID_STAFF_ROLE",
            details={"role": role, "valid": sorted(_ROLE_CAPABILITIES)},
        )
    try:
        target_principal = resolve_principal_uuid(principal_uuid)
    except PrincipalConflictError:
        raise CTFNotFoundError("Active principal not found", details={"principal_uuid": str(principal_uuid)}) from None
    target_user = None
    if target_principal.kind == "human":
        from django.contrib.auth import get_user_model

        target_user = (
            get_user_model().objects.filter(pk=human_principal_user_id(target_principal), is_active=True).first()
        )
        if target_user is None:
            raise CTFNotFoundError("Active principal not found", details={"principal_uuid": str(principal_uuid)})
    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.ASSIGN,
        target_principal_uuid=target_principal.uuid,
        requested_role=role,
    )
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, "event.manage_staff")
        if target_principal.uuid == event.owner_principal_uuid:
            raise CTFValidationError(
                "The event organizer already holds every capability",
                code="CTF_STAFF_IS_ORGANIZER",
            )
        with transaction.atomic():
            locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
            if locked_event.owner_principal_uuid != event.owner_principal_uuid:
                raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
            staff = CTFEventStaff.objects.filter(
                event=locked_event, principal_uuid=target_principal.uuid, deleted_at__isnull=True
            ).first()
            previous_role = "" if staff is None else staff.role
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
            )
        event = locked_event
    else:
        event = transition_row.event
    previous_role = transition_row.previous_role or None
    _publish_staff_role(
        event,
        actor,
        target_principal.uuid,
        previous_role,
        role,
        transition_id=transition_row.pk,
    )
    with transaction.atomic():
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
        current = CTFEventStaff.objects.filter(
            event=locked_event, principal_uuid=target_principal.uuid, deleted_at__isnull=True
        ).first()
        if (None if current is None else current.role) != previous_role:
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
            previous_role=previous_role,
        )
        _confirm_transition(transition_row)
    logger.info(
        "%s staff %s on event %s as %s",
        "Assigned" if action == "assigned" else "Re-roled",
        target_principal.uuid,
        event.pk,
        role,
    )
    return staff


def revoke_event_staff(event_id: UUID, actor: EventActor, principal_uuid: UUID) -> bool:
    """Remove a staff assignment from an owned event (owner-or-admin, locked, audited).

    The staff-revocation command never targets ``created_by`` (the owner holds no
    staff row), so it can never leave an event without an owner.
    """
    from ctf.services.audit import audit_event_staff_change

    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.REVOKE,
        target_principal_uuid=principal_uuid,
    )
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, "event.manage_staff")
        with transaction.atomic():
            locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
            if locked_event.owner_principal_uuid != event.owner_principal_uuid:
                raise CTFValidationError("Event ownership changed concurrently", code="CTF_STAFF_CONFLICT")
            staff = CTFEventStaff.objects.filter(
                event=locked_event,
                principal_uuid=principal_uuid,
                deleted_at__isnull=True,
            ).first()
            if staff is None:
                raise CTFNotFoundError(
                    "Staff assignment not found", details={"principal_uuid": str(principal_uuid)}
                )
            transition_row = _create_transition(
                locked_event,
                actor,
                kind=CTFEventAuthorityTransition.Kind.REVOKE,
                target_principal_uuid=principal_uuid,
                previous_role=staff.role,
            )
        event = locked_event
    else:
        event = transition_row.event
    revoked_role = transition_row.previous_role
    _publish_staff_role(
        event,
        actor,
        principal_uuid,
        revoked_role,
        None,
        transition_id=transition_row.pk,
    )
    with transaction.atomic():
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
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
    logger.info("Revoked staff %s on event %s", safe_log_value(principal_uuid), event.pk)
    return True


def transfer_event_ownership(event_id: UUID, actor: EventActor, new_owner_principal_uuid: UUID) -> CTFEvent:
    """Transfer canonical ownership to an existing co-organizer (owner-or-admin, locked, audited).

    One atomic command: the target must already be a live co-organizer who is
    still an active CTF organizer; in the same transaction the target becomes
    ``created_by``, its now-redundant staff row is soft-deleted, and the previous
    owner is retained as a co-organizer. The non-null owner invariant is never
    transiently broken. Ownership transfer does not move participant accounts,
    teams, ranges, tokens, or provider credentials — those keep their existing
    product-specific owners.
    """
    from ctf.services.audit import audit_event_ownership_transferred
    from management.services import PrincipalConflictError, human_principal_user_id, resolve_principal_uuid

    try:
        target_principal = resolve_principal_uuid(new_owner_principal_uuid)
    except PrincipalConflictError:
        raise CTFValidationError(
            "Ownership can only transfer to an active principal",
            code="CTF_TRANSFER_TARGET_INELIGIBLE",
        ) from None
    target_user_id = None
    if target_principal.kind == "human":
        target_user_id = human_principal_user_id(target_principal)
    transition_row = _pending_transition(
        event_id,
        actor,
        kind=CTFEventAuthorityTransition.Kind.TRANSFER,
        target_principal_uuid=target_principal.uuid,
    )
    if transition_row is None:
        event = _resolve_owned_event_for_staff(event_id, actor, "event.transfer_ownership")
        previous_owner_uuid = event.owner_principal_uuid
        if target_principal.uuid == previous_owner_uuid:
            raise CTFValidationError(
                "The target already owns this event",
                code="CTF_ALREADY_OWNER",
            )
        with transaction.atomic():
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
            )
        event = locked_event
    else:
        event = transition_row.event
        previous_owner_uuid = transition_row.previous_owner_principal_uuid
        if previous_owner_uuid is None:
            raise CTFValidationError("Event ownership transition is invalid", code="CTF_STAFF_CONFLICT")
    previous_owner = resolve_principal_uuid(previous_owner_uuid)
    previous_owner_user_id = human_principal_user_id(previous_owner) if previous_owner.kind == "human" else None
    # Publish the complete new authority graph before changing its SQL display
    # projection. The journal fences each tuple generation and confirms primary
    # readback; no provider call runs while the event row is locked.
    _publish_staff_role(
        event,
        actor,
        previous_owner_uuid,
        None,
        EventStaffRole.CO_ORGANIZER.value,
        transition_id=transition_row.pk,
        ownership_handoff=True,
    )
    _publish_event_administrator(
        event,
        actor,
        target_principal.uuid,
        PolicyEffect.GRANT,
        transition_id=transition_row.pk,
        step="grant-target",
    )
    # Keep the target's existing positive action tuples. They are redundant
    # while the principal is the event administrator and become correct again
    # if a later transfer retains this owner as a co-organizer. Publishing a
    # role REVOKE here would create explicit action denials that override the
    # administrator relationship.
    _publish_event_administrator(
        event,
        actor,
        previous_owner_uuid,
        PolicyEffect.REVOKE,
        transition_id=transition_row.pk,
        step="revoke-source",
    )
    with transaction.atomic():
        transition_row = CTFEventAuthorityTransition.objects.select_for_update().get(
            pk=transition_row.pk,
            state=CTFEventAuthorityTransition.State.PENDING,
        )
        event = CTFEvent.objects.select_for_update().get(pk=event.pk, deleted_at__isnull=True)
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
