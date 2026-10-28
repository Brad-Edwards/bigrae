"""CTF-owned, immutable effect plans for SQL/policy authority transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from ctf.exceptions import CTFPermissionError, CTFStateError
from ctf.models import CTFEvent, CTFEventAuthorityTransition
from ctf.services.actors import EventActor, event_actor_credential
from ctf.services.credential_scope import event_credential_scope
from shared.authorization import (
    AdministrativeRoleChange,
    AuthorizationProviderBindingError,
    AuthorizationProviderError,
    PolicyEffect,
    RelationshipSubject,
    TargetRef,
    configured_authorization_provider,
)
from shared.authorization.inventory import EventAuthorityEffect, RetainedEventAuthorityPlan
from shared.identity_scope import ResourceScope

if TYPE_CHECKING:
    from workspaces.services import NativeRelationshipMutationRequest, PolicyMutationRequest


def unavailable() -> CTFStateError:
    """Create the opaque domain error for an unresolved authority transition."""
    return CTFStateError("Event authority is unavailable", code="CTF_EVENT_AUTHORITY_UNAVAILABLE")


@dataclass(frozen=True)
class AuthorityPlan:
    """Immutable effects and provider binding for one event authority transition."""

    transition_uuid: UUID
    event_uuid: UUID
    kind: str
    target_uuid: UUID
    previous_owner_uuid: UUID | None
    previous_role: str
    requested_role: str
    scope: ResourceScope
    store_id: str
    model_id: str
    effects: tuple[EventAuthorityEffect, ...]

    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "transition_uuid": str(self.transition_uuid),
            "event_uuid": str(self.event_uuid),
            "kind": self.kind,
            "target_uuid": str(self.target_uuid),
            "previous_owner_uuid": str(self.previous_owner_uuid) if self.previous_owner_uuid else None,
            "previous_role": self.previous_role,
            "requested_role": self.requested_role,
            "scope": {
                "kind": self.scope.kind,
                "account_uuid": str(self.scope.account_uuid) if self.scope.account_uuid else None,
                "organization_uuid": str(self.scope.organization_uuid) if self.scope.organization_uuid else None,
                "workspace_uuid": str(self.scope.workspace_uuid) if self.scope.workspace_uuid else None,
            },
            "store_id": self.store_id,
            "model_id": self.model_id,
            "effects": [
                {
                    "key": effect.key,
                    "subject_uuid": str(effect.subject_uuid),
                    "effect": effect.effect,
                    "action": effect.action,
                    "policy_code": effect.policy_code,
                }
                for effect in self.effects
            ],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def admission(self) -> RetainedEventAuthorityPlan:
        return RetainedEventAuthorityPlan(
            transition_uuid=self.transition_uuid,
            event_uuid=self.event_uuid,
            kind=self.kind,
            target_uuid=self.target_uuid,
            previous_owner_uuid=self.previous_owner_uuid,
            scope=self.scope,
            effects=self.effects,
            store_id=self.store_id,
            model_id=self.model_id,
        )


def prepare_plan(
    event: CTFEvent,
    actor: EventActor,
    *,
    kind: str,
    target_uuid: UUID,
    previous_role: str = "",
    requested_role: str = "",
) -> AuthorityPlan:
    """Preflight every prospective effect before retaining a pending aggregate."""
    from workspaces.services import check_authorization_delegation

    try:
        provider = configured_authorization_provider()
    except AuthorizationProviderBindingError:
        raise unavailable() from None
    transition_uuid = uuid4()
    effects = _plan_effects(transition_uuid, event, kind, target_uuid, previous_role, requested_role)
    plan = AuthorityPlan(
        transition_uuid,
        event.pk,
        kind,
        target_uuid,
        event.owner_principal_uuid if kind == "transfer" else None,
        previous_role,
        requested_role,
        event_credential_scope(event.pk),
        provider.store_id,
        provider.model_id,
        effects,
    )
    if target_uuid == event_actor_credential(actor).principal.uuid and (
        kind == "transfer" or any(effect.effect == "grant" for effect in effects)
    ):
        raise CTFPermissionError("Self-assignment is not permitted", code="CTF_EVENT_ACTION_DENIED")
    try:
        requests = tuple(effect_request(plan, effect, actor) for effect in effects)
        if not check_authorization_delegation(requests, provider):
            raise CTFPermissionError("Event authority denied", code="CTF_EVENT_ACTION_DENIED")
    except AuthorizationProviderError:
        raise unavailable() from None
    return plan


def effect_request(
    plan: AuthorityPlan, effect: EventAuthorityEffect, actor: EventActor
) -> NativeRelationshipMutationRequest | PolicyMutationRequest:
    """Build the exact lower-level request without consulting a mutable role catalog."""
    from workspaces.services import NativeRelationshipMutationRequest, PolicyMutationRequest

    credential = event_actor_credential(actor)
    subject = RelationshipSubject("principal", effect.subject_uuid)
    target = TargetRef("event", plan.event_uuid)
    if effect.policy_code:
        return NativeRelationshipMutationRequest(
            actor=credential.principal,
            credential=credential.ceiling,
            scope=plan.scope,
            idempotency_key=effect.key,
            model_id=plan.model_id,
            change=AdministrativeRoleChange(subject, effect.policy_code, target, PolicyEffect(effect.effect)),
        )
    return PolicyMutationRequest(
        actor=credential.principal,
        credential=credential.ceiling,
        scope=plan.scope,
        idempotency_key=effect.key,
        model_id=plan.model_id,
        subject=subject,
        action=effect.action,
        target=target,
        effect=PolicyEffect(effect.effect),
    )


def retained_plan(transition: CTFEventAuthorityTransition) -> AuthorityPlan:
    """Reject unknown, altered or unbound plans; never infer an old command's effects."""
    try:
        payload = transition.effect_plan
        if payload["version"] != 1:
            raise ValueError("Unknown effect plan version")
        scope = payload["scope"]
        plan = AuthorityPlan(
            UUID(payload["transition_uuid"]),
            UUID(payload["event_uuid"]),
            payload["kind"],
            UUID(payload["target_uuid"]),
            UUID(payload["previous_owner_uuid"]) if payload["previous_owner_uuid"] else None,
            payload["previous_role"],
            payload["requested_role"],
            ResourceScope(
                scope["kind"],
                *(
                    UUID(scope[key]) if scope[key] else None
                    for key in ("account_uuid", "organization_uuid", "workspace_uuid")
                ),
            ),
            payload["store_id"],
            payload["model_id"],
            tuple(
                EventAuthorityEffect(
                    item["key"], UUID(item["subject_uuid"]), item["effect"], item["action"], item["policy_code"]
                )
                for item in payload["effects"]
            ),
        )
        if (
            plan.digest != transition.effect_plan_digest
            or plan.payload() != payload
            or (
                plan.transition_uuid,
                plan.event_uuid,
                plan.kind,
                plan.target_uuid,
                plan.previous_owner_uuid,
                plan.previous_role,
                plan.requested_role,
            )
            != (
                transition.pk,
                transition.event_id,
                transition.kind,
                transition.target_principal_uuid,
                transition.previous_owner_principal_uuid,
                transition.previous_role,
                transition.requested_role,
            )
            or plan.scope != event_credential_scope(transition.event_id)
            or len({effect.key for effect in plan.effects}) != len(plan.effects)
        ):
            raise ValueError("Effect plan changed")
        return plan
    except (KeyError, TypeError, ValueError):
        raise unavailable() from None


def publish_plan(transition: CTFEventAuthorityTransition, actor: EventActor) -> None:
    """Reuse the policy journal for every retained effect before changing SQL ownership."""
    from ctf.services.event.staff import _is_original_transition_credential
    from workspaces.services import (
        AuthorizationMutationConflict,
        NativeRelationshipMutationRequest,
        RetainedEventAuthorityPolicyMutationRequest,
        RetainedEventOwnershipTransferMutationRequest,
        settle_authorization_mutation,
    )

    plan = retained_plan(transition)
    try:
        provider = configured_authorization_provider()
    except AuthorizationProviderBindingError:
        raise unavailable() from None
    if (provider.store_id, provider.model_id) != (plan.store_id, plan.model_id):
        raise unavailable()
    for effect in plan.effects:
        try:
            request = effect_request(plan, effect, actor)
            if isinstance(request, NativeRelationshipMutationRequest):
                if plan.kind == "transfer" and effect.effect == "grant":
                    request = RetainedEventOwnershipTransferMutationRequest.from_request(request, plan.admission())
            else:
                request = RetainedEventAuthorityPolicyMutationRequest.from_request(request, plan.admission())
            result = settle_authorization_mutation(
                request,
                provider,
                readback_first=_is_original_transition_credential(transition.pk, event_actor_credential(actor)),
            )
        except (AuthorizationMutationConflict, AuthorizationProviderError, ValueError):
            raise unavailable() from None
        if result.state != "confirmed":
            raise unavailable()


def _plan_effects(
    transition_uuid: UUID,
    event: CTFEvent,
    kind: str,
    target_uuid: UUID,
    previous_role: str,
    requested_role: str,
) -> tuple[EventAuthorityEffect, ...]:
    """Freeze the exact staff delta and any ownership handoff effects."""
    from ctf.services.event.staff import actions_for_role

    old = set(actions_for_role(previous_role or None))
    new = set(actions_for_role(requested_role or None))
    subject_uuid = target_uuid
    if kind == "transfer":
        old, new = set(), set(actions_for_role("co_organizer"))
        subject_uuid = event.owner_principal_uuid
    effects = tuple(
        EventAuthorityEffect(
            f"event-staff-{transition_uuid.hex}-{action}",
            subject_uuid,
            "grant" if action in new else "revoke",
            action=action,
        )
        for action in sorted(old | new)
    )
    if kind == "transfer":
        effects += (
            EventAuthorityEffect(
                f"event-owner-{transition_uuid.hex}-grant-target",
                target_uuid,
                "grant",
                policy_code="event_administrator",
            ),
            EventAuthorityEffect(
                f"event-owner-{transition_uuid.hex}-revoke-source",
                event.owner_principal_uuid,
                "revoke",
                policy_code="event_administrator",
            ),
        )
    return effects
