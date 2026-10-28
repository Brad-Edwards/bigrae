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
        change = self.change
        if (
            not isinstance(self.plan, RetainedEventAuthorityPlan)
            or self.plan.kind != "transfer"
            or not isinstance(change, AdministrativeRoleChange)
            or change.policy_code != "event_administrator"
            or change.effect != PolicyEffect.GRANT
            or change.target != TargetRef("event", self.plan.event_uuid)
            or change.subject != RelationshipSubject("principal", self.plan.target_uuid)
            or self.scope != self.plan.scope
            or self.idempotency_key != f"event-owner-{self.plan.transition_uuid.hex}-grant-target"
            or self.model_id != self.plan.model_id
            or not any(
                effect.key == self.idempotency_key
                and effect.subject_uuid == change.subject.uuid
                and effect.policy_code == change.policy_code
                and not effect.action
                and effect.effect == change.effect
                for effect in self.plan.effects
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
        if (
            not isinstance(self.plan, RetainedEventAuthorityPlan)
            or self.target != TargetRef("event", self.plan.event_uuid)
            or self.subject.uuid
            != (self.plan.previous_owner_uuid if self.plan.kind == "transfer" else self.plan.target_uuid)
            or self.subject.kind != "principal"
            or self.scope != self.plan.scope
            or (self.plan.kind == "transfer" and self.effect != PolicyEffect.GRANT)
            or (self.plan.kind == "revoke" and self.effect != PolicyEffect.REVOKE)
            or self.model_id != self.plan.model_id
            or not any(
                effect.key == self.idempotency_key
                and effect.subject_uuid == self.subject.uuid
                and effect.action == self.action
                and not effect.policy_code
                and effect.effect == self.effect
                for effect in self.plan.effects
            )
        ):
            raise ValueError("Retained event authority effect does not match")
