"""Scoped authorization commands responsibilities (ADR-066, #2315)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self
from uuid import UUID

from shared.audit import RequestAudit
from shared.authorization import (
    AdministrativeRoleChange,
    CredentialCeiling,
    GroupMembershipChange,
    PolicyEffect,
    PolicyRelationshipChange,
    RelationshipSubject,
    RoleAssignmentChange,
    TargetRef,
    action_definition,
)
from shared.authorization.inventory import RetainedEventAuthorityPlan
from shared.identity_scope import PrincipalRef, ResourceScope

AUTHORIZATION_MUTATION_CONFIRMED = "confirmed"


class AuthorizationMutationConflict(RuntimeError):
    """A different or unresolved mutation owns the serialization fence."""


@dataclass(frozen=True, slots=True)
class PolicyMutationRequest:
    """Validated action-assignment intent with trusted actor and audit context."""

    actor: PrincipalRef
    credential: CredentialCeiling
    subject: RelationshipSubject
    action: str
    target: TargetRef
    scope: ResourceScope
    effect: PolicyEffect
    idempotency_key: str
    model_id: str
    audit: RequestAudit = field(default_factory=RequestAudit)

    def __post_init__(self) -> None:
        PolicyRelationshipChange(self.subject, self.action, self.target, self.effect)
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 128:
            raise ValueError("idempotency key must contain 1 to 128 characters")
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("authorization model id is required")
        if not isinstance(self.audit, RequestAudit):
            raise ValueError("trusted audit attribution is required")


@dataclass(frozen=True, slots=True)
class EventOwnershipHandoffPolicyMutationRequest(PolicyMutationRequest):
    """Legacy journal request, accepted only for already-admitted readback/recovery."""

    def __post_init__(self) -> None:
        super(EventOwnershipHandoffPolicyMutationRequest, self).__post_init__()
        if (
            self.target.type != "event"
            or self.target.uuid is None
            or self.subject.kind != "principal"
            or self.subject.uuid != self.actor.uuid
            or self.effect != PolicyEffect.GRANT
            or not action_definition(self.action).delegable
        ):
            raise ValueError("event ownership handoff must grant a closed staff action set to the former owner")


@dataclass(frozen=True, slots=True)
class PolicyMutationResult:
    """Durable operation identity and its current externally visible state."""

    operation_id: UUID
    state: str


type NativeRelationshipChange = GroupMembershipChange | RoleAssignmentChange | AdministrativeRoleChange


@dataclass(frozen=True, slots=True)
class NativeRelationshipMutationRequest:
    """A native group membership or role assignment mutation."""

    actor: PrincipalRef
    credential: CredentialCeiling
    change: NativeRelationshipChange
    scope: ResourceScope
    idempotency_key: str
    model_id: str
    audit: RequestAudit = field(default_factory=RequestAudit)

    def __post_init__(self) -> None:
        if not isinstance(self.change, (GroupMembershipChange, RoleAssignmentChange, AdministrativeRoleChange)):
            raise ValueError("native relationship change is required")
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 128:
            raise ValueError("idempotency key must contain 1 to 128 characters")
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("authorization model id is required")
        if not isinstance(self.audit, RequestAudit):
            raise ValueError("trusted audit attribution is required")


type MutationRequest = PolicyMutationRequest | NativeRelationshipMutationRequest


@dataclass(frozen=True, slots=True)
class RetainedEventOwnershipTransferMutationRequest(NativeRelationshipMutationRequest):
    """Settle one admitted handoff without requiring its recipient to remain active."""

    plan: RetainedEventAuthorityPlan = field(kw_only=True)

    @classmethod
    def from_request(cls, request: NativeRelationshipMutationRequest, plan: RetainedEventAuthorityPlan) -> Self:
        return cls(
            actor=request.actor,
            credential=request.credential,
            change=request.change,
            scope=request.scope,
            idempotency_key=request.idempotency_key,
            model_id=request.model_id,
            audit=request.audit,
            plan=plan,
        )

    def __post_init__(self) -> None:
        super(RetainedEventOwnershipTransferMutationRequest, self).__post_init__()
        if not isinstance(self.plan, RetainedEventAuthorityPlan):
            raise ValueError("Retained event ownership transfer does not match")
        if (
            self.plan.kind != "transfer"
            or not _transfer_target_grant(self.change, self.plan)
            or (self.scope, self.idempotency_key, self.model_id)
            != (self.plan.scope, f"event-owner-{self.plan.transition_uuid.hex}-grant-target", self.plan.model_id)
            or not _retained_effect_matches(
                self.plan, self.idempotency_key, self.plan.target_uuid, "", "event_administrator", PolicyEffect.GRANT
            )
        ):
            raise ValueError("Retained event ownership transfer does not match")


@dataclass(frozen=True, slots=True)
class RetainedEventAuthorityPolicyMutationRequest(PolicyMutationRequest):
    """One exact effect of a retained domain-owned staff or ownership plan."""

    plan: RetainedEventAuthorityPlan = field(kw_only=True)

    @classmethod
    def from_request(cls, request: PolicyMutationRequest, plan: RetainedEventAuthorityPlan) -> Self:
        return cls(
            actor=request.actor,
            credential=request.credential,
            subject=request.subject,
            action=request.action,
            target=request.target,
            scope=request.scope,
            effect=request.effect,
            idempotency_key=request.idempotency_key,
            model_id=request.model_id,
            audit=request.audit,
            plan=plan,
        )

    def __post_init__(self) -> None:
        super(RetainedEventAuthorityPolicyMutationRequest, self).__post_init__()
        if not isinstance(self.plan, RetainedEventAuthorityPlan):
            raise ValueError("Retained event authority effect does not match")
        subject_uuid = self.plan.previous_owner_uuid if self.plan.kind == "transfer" else self.plan.target_uuid
        if (
            (self.target, self.subject.kind, self.subject.uuid, self.scope, self.model_id)
            != (
                TargetRef("event", self.plan.event_uuid),
                "principal",
                subject_uuid,
                self.plan.scope,
                self.plan.model_id,
            )
            or not _retained_policy_effect_allowed(self.plan, self.effect)
            or not _retained_effect_matches(
                self.plan, self.idempotency_key, self.subject.uuid, self.action, "", self.effect
            )
        ):
            raise ValueError("Retained event authority effect does not match")


def _transfer_target_grant(change: NativeRelationshipChange, plan: RetainedEventAuthorityPlan) -> bool:
    """Only the exact planned recipient administrator grant can settle a retained transfer."""
    return isinstance(change, AdministrativeRoleChange) and (
        change.policy_code,
        change.effect,
        change.target,
        change.subject,
    ) == (
        "event_administrator",
        PolicyEffect.GRANT,
        TargetRef("event", plan.event_uuid),
        RelationshipSubject("principal", plan.target_uuid),
    )


def _retained_effect_matches(
    plan: RetainedEventAuthorityPlan, key: str, subject_uuid: UUID, action: str, policy_code: str, effect: PolicyEffect
) -> bool:
    """Bind a settlement to one exact enumerated effect of the immutable domain-owned plan."""
    expected = (key, subject_uuid, action, policy_code, effect)
    return any(
        (item.key, item.subject_uuid, item.action, item.policy_code, item.effect) == expected for item in plan.effects
    )


def _retained_policy_effect_allowed(plan: RetainedEventAuthorityPlan, effect: PolicyEffect) -> bool:
    """Retained transfer/revoke kinds keep their closed grant/revoke semantics."""
    return (plan.kind != "transfer" or effect == PolicyEffect.GRANT) and (
        plan.kind != "revoke" or effect == PolicyEffect.REVOKE
    )
