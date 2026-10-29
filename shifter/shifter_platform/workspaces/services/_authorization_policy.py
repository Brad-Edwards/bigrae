"""Scoped authorization policy responsibilities (ADR-066, #2315)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal, cast
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from shared.authorization import (
    AdministrativeRoleChange,
    AuthorizationContractError,
    AuthorizationDescendantResolutionError,
    AuthorizationProvider,
    AuthorizationProviderError,
    AuthorizationRequest,
    CredentialCeiling,
    DecisionKind,
    PolicyEffect,
    PolicyRelationshipChange,
    RelationshipState,
    TargetRef,
)
from shared.identity_scope import PrincipalRef, ResourceScope
from shared.principal_port import PrincipalResolutionError, resolve_principal
from workspaces.models import (
    AuthorizationOperation,
)

from ._account import resolve_resource_scope
from ._authorization_commands import (
    AuthorizationMutationConflict as AuthorizationMutationConflict,
)
from ._authorization_commands import (
    EventOwnershipHandoffPolicyMutationRequest as EventOwnershipHandoffPolicyMutationRequest,
)
from ._authorization_commands import (
    MutationRequest as MutationRequest,
)
from ._authorization_commands import (
    NativeRelationshipMutationRequest as NativeRelationshipMutationRequest,
)
from ._authorization_commands import (
    PolicyMutationRequest as PolicyMutationRequest,
)
from ._authorization_commands import (
    PolicyMutationResult as PolicyMutationResult,
)
from ._authorization_delegation import (
    _delegation_requests,
    _native_delegation_requests,
    _resolve_concrete_target,
    _resolve_native_change,
    _resolve_policy_change,
    _scope_target,
)
from ._authorization_journal import (
    _assert_operation_provider,
    _assert_request_provider,
    _base_change_from_operation,
    _change_from_operation,
    _clear_reconcile_lease,
    _hash,
    _idempotency_namespace,
    _operation_result,
    _request_payload,
    _reserve,
    _set_outcome,
)

logger = logging.getLogger(__name__)

_AUTHORIZATION_EVALUATOR_UNAVAILABLE = "Authorization evaluator unavailable"

_RECONCILE_LEASE = timedelta(seconds=30)
_RECOVERABLE_STATES = {AuthorizationOperation.State.REQUESTED, AuthorizationOperation.State.UNRESOLVED}


def confirmed_authorization_effects(keys: tuple[str, ...], *, store_id: str, model_id: str) -> int:
    """Bounded journal progress for an owning aggregate's server-generated effects."""
    if len(keys) > 100 or len(set(keys)) != len(keys):
        raise AuthorizationMutationConflict("authorization effect inventory is invalid")
    return (
        AuthorizationOperation.objects.filter(
            idempotency_key__in=keys,
            provider_store_id=store_id,
            model_id=model_id,
            state=AuthorizationOperation.State.CONFIRMED,
        )
        .values("idempotency_key")
        .distinct()
        .count()
    )


def _matches(effect: str, state: RelationshipState) -> bool:
    """Require the intended tuple and absence of its opposing relationship."""
    if effect == PolicyEffect.GRANT:
        return bool(state.grant_present and not state.deny_present)
    return bool(state.deny_present and not state.grant_present)


def apply_policy_mutation(request: PolicyMutationRequest, provider: AuthorizationProvider) -> PolicyMutationResult:
    """Authorize, journal, apply and read back one serialized tuple change."""
    resolve_resource_scope(request.scope)
    _assert_request_provider(request, provider)
    allowed = check_authorization_mutation(request, provider)
    return _apply_authorized_mutation(request, provider, allowed)


def check_authorization_mutation(request: MutationRequest, provider: AuthorizationProvider) -> bool:
    """Preflight one exact effect without reserving a key or publishing a tuple.

    An evaluator failure is retryable and cannot become an immutable denial.
    Owning aggregate commands use this same admission before retaining intent.
    """
    resolve_resource_scope(request.scope)
    _assert_request_provider(request, provider)
    try:
        if isinstance(request, NativeRelationshipMutationRequest):
            _resolve_native_change(request)
            checks = _native_delegation_requests(request)
        else:
            _resolve_policy_change(request)
            checks = _delegation_requests(request)
    except (
        AuthorizationMutationConflict,
        AuthorizationContractError,
        AuthorizationDescendantResolutionError,
        PrincipalResolutionError,
        ValueError,
    ):
        return False
    return _evaluate_delegation(checks, provider)


def check_authorization_delegation(requests: tuple[MutationRequest, ...], provider: AuthorizationProvider) -> bool:
    """Batch prospective effects without admitting subjects, keys or writes."""
    if not requests or len(requests) > 100:
        raise AuthorizationMutationConflict("authorization effect inventory is invalid")
    checks: list[AuthorizationRequest] = []
    try:
        for request in requests:
            resolve_resource_scope(request.scope)
            _assert_request_provider(request, provider)
            resolve_principal(request.actor)
            target = _delegation_target(request)
            _resolve_concrete_target(target, request.scope)
            effect_checks = (
                _native_delegation_requests(request)
                if isinstance(request, NativeRelationshipMutationRequest)
                else _delegation_requests(request)
            )
            if not effect_checks:
                return False
            checks.extend(effect_checks)
    except (
        AuthorizationContractError,
        AuthorizationMutationConflict,
        AuthorizationDescendantResolutionError,
        PrincipalResolutionError,
        ValueError,
    ):
        return False
    return _evaluate_delegation(tuple(dict.fromkeys(checks)), provider)


def _evaluate_delegation(checks: tuple[AuthorizationRequest, ...], provider: AuthorizationProvider) -> bool:
    """Evaluate every required delegation check against the pinned provider."""
    if not checks:
        return False
    try:
        decisions = provider.batch_check(checks)
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise AuthorizationProviderError(_AUTHORIZATION_EVALUATOR_UNAVAILABLE) from None
    if len(decisions) != len(checks) or any(item.kind == DecisionKind.EVALUATOR_ERROR for item in decisions):
        raise AuthorizationProviderError(_AUTHORIZATION_EVALUATOR_UNAVAILABLE)
    return all(item.allowed for item in decisions)


def _apply_authorized_mutation(
    request: MutationRequest, provider: AuthorizationProvider, allowed: bool
) -> PolicyMutationResult:
    """Persist the authorization decision before attempting a provider write."""
    if not allowed:
        operation, _ = _reserve(
            request,
            provider,
            state=AuthorizationOperation.State.DENIED,
            reason="delegation_denied",
            block=False,
        )
        return _operation_result(operation)
    operation, created = _reserve(
        request,
        provider,
        state=AuthorizationOperation.State.REQUESTED,
        reason="accepted",
        block=True,
    )
    if not created and operation.state in _RECOVERABLE_STATES:
        return reconcile_policy_mutation(operation.id, provider)
    if created and operation.state == AuthorizationOperation.State.REQUESTED:
        operation = _write_and_confirm(operation, provider)
    return _operation_result(operation)


def _write_and_confirm(operation: AuthorizationOperation, provider: AuthorizationProvider) -> AuthorizationOperation:
    """Record confirmation only after the intended relationship is read back."""
    change = _change_from_operation(operation)
    try:
        provider.write_relationships(change)
        observed = provider.read_relationships(change)
    except AuthorizationProviderError:
        return _set_outcome(operation, AuthorizationOperation.State.UNRESOLVED, "provider_outcome_unknown")
    if not _matches(operation.effect, observed):
        return _set_outcome(operation, AuthorizationOperation.State.UNRESOLVED, "readback_mismatch")
    return _set_outcome(operation, AuthorizationOperation.State.CONFIRMED, "readback_confirmed")


def apply_native_relationship_mutation(
    request: NativeRelationshipMutationRequest,
    provider: AuthorizationProvider,
) -> PolicyMutationResult:
    """Apply a serialized native group membership or role assignment."""
    resolve_resource_scope(request.scope)
    _assert_request_provider(request, provider)
    allowed = check_authorization_mutation(request, provider)
    return _apply_authorized_mutation(request, provider, allowed)


def _confirm_admitted_mutation(
    request: MutationRequest, provider: AuthorizationProvider
) -> PolicyMutationResult | None:
    """Read back an exact admitted native mutation without authorizing or replaying a write."""
    resolve_resource_scope(request.scope)
    _assert_request_provider(request, provider)
    operation = AuthorizationOperation.objects.filter(
        idempotency_namespace=_idempotency_namespace(request), idempotency_key=request.idempotency_key
    ).first()
    if operation is None or operation.state == AuthorizationOperation.State.DENIED:
        return None
    if operation.request_digest != _hash(_request_payload(request)):
        raise AuthorizationMutationConflict("admitted mutation does not match")
    _assert_operation_provider(operation, provider)
    if isinstance(request, NativeRelationshipMutationRequest):
        _resolve_native_change(request, admitted_subject=True)
    else:
        _resolve_policy_change(request, admitted_subject=True)
    if operation.state in _RECOVERABLE_STATES or operation.state == AuthorizationOperation.State.CONFIRMED:
        operation = _readback_admitted_mutation(operation, provider)
    return _operation_result(operation) if operation is not None else None


def _readback_admitted_mutation(
    operation: AuthorizationOperation, provider: AuthorizationProvider
) -> AuthorizationOperation | None:
    """Read back exact retained intent without a provider write or a new admission."""
    try:
        observed = provider.read_relationships(_change_from_operation(operation))
    except AuthorizationProviderError:
        return None
    if not _matches(operation.effect, observed):
        return None
    if operation.state != AuthorizationOperation.State.CONFIRMED:
        operation = _set_outcome(operation, AuthorizationOperation.State.CONFIRMED, "admitted_primary_read")
    return operation


def confirm_admitted_native_mutation(
    request: NativeRelationshipMutationRequest, provider: AuthorizationProvider
) -> PolicyMutationResult | None:
    """Read back one exact native intent without admitting another write."""
    return _confirm_admitted_mutation(request, provider)


def confirm_admitted_policy_mutation(
    request: PolicyMutationRequest, provider: AuthorizationProvider
) -> PolicyMutationResult | None:
    """Read back one exact action intent without replaying a former actor's grant."""
    return _confirm_admitted_mutation(request, provider)


def settle_authorization_mutation(
    request: MutationRequest, provider: AuthorizationProvider, *, readback_first: bool = False
) -> PolicyMutationResult:
    """Confirm an original effect or reconcile/admit it using current authority."""
    result = _confirm_admitted_mutation(request, provider) if readback_first else None
    if result is None:
        result = reconcile_admitted_mutation(request, provider)
    if result is None:
        result = (
            apply_native_relationship_mutation(request, provider)
            if isinstance(request, NativeRelationshipMutationRequest)
            else apply_policy_mutation(request, provider)
        )
    return result


def reconcile_admitted_mutation(
    request: MutationRequest, provider: AuthorizationProvider
) -> PolicyMutationResult | None:
    """Recover an exact durable command using a currently authorized administrator.

    The original operation keeps its initiating actor. The supplied request must
    describe the same server-generated key, scope, model and relationship effect;
    its actor must independently retain all authority required to replay it.
    """
    resolve_resource_scope(request.scope)
    _assert_request_provider(request, provider)
    expected = (
        request.change
        if isinstance(request, NativeRelationshipMutationRequest)
        else PolicyRelationshipChange(request.subject, request.action, request.target, request.effect)
    )
    operations = (
        AuthorizationOperation.objects.filter(idempotency_key=request.idempotency_key)
        .exclude(state=AuthorizationOperation.State.DENIED)
        .order_by("created_at", "pk")
    )
    selected = None
    for operation in operations:
        if operation.scope_kind not in {"installation", "account"}:
            raise AuthorizationMutationConflict("admitted mutation scope is invalid")
        operation_scope = ResourceScope(
            "installation" if operation.scope_kind == "installation" else "account",
            operation.account_uuid,
            operation.organization_uuid,
            operation.workspace_uuid,
        )
        if operation_scope != request.scope or _base_change_from_operation(operation) != expected:
            raise AuthorizationMutationConflict("admitted mutation does not match")
        authorize_operation_reconciliation(operation.pk, request.actor, request.credential, provider)
        selected = reconcile_policy_mutation(operation.pk, provider)
        if selected.state != AuthorizationOperation.State.CONFIRMED:
            return selected
    return selected


def authorize_operation_reconciliation(
    operation_id: UUID,
    actor: PrincipalRef,
    credential: CredentialCeiling,
    provider: AuthorizationProvider,
) -> None:
    """Require current caller authority for every effect a recovery can replay."""
    operation = AuthorizationOperation.objects.get(pk=operation_id)
    _assert_operation_provider(operation, provider)
    if operation.state == AuthorizationOperation.State.DENIED:
        raise AuthorizationMutationConflict("authorization recovery denied")
    scope = ResourceScope(
        cast(Literal["installation", "account"], operation.scope_kind),
        operation.account_uuid,
        operation.organization_uuid,
        operation.workspace_uuid,
    )
    resolve_resource_scope(scope)
    change = _base_change_from_operation(operation)
    checks: tuple[AuthorizationRequest, ...]
    if isinstance(change, PolicyRelationshipChange):
        request_type = (
            EventOwnershipHandoffPolicyMutationRequest
            if operation.trusted_context == "event_ownership_handoff" and change.subject.uuid == actor.uuid
            else PolicyMutationRequest
        )
        request = request_type(
            actor=actor,
            credential=credential,
            scope=scope,
            model_id=operation.model_id,
            idempotency_key=operation.idempotency_key,
            subject=change.subject,
            action=change.action,
            target=change.target,
            effect=change.effect,
        )
        _resolve_policy_change(request, admitted_subject=True)
        checks = _delegation_requests(request)
    else:
        native_request = NativeRelationshipMutationRequest(
            actor=actor,
            credential=credential,
            scope=scope,
            model_id=operation.model_id,
            idempotency_key=operation.idempotency_key,
            change=change,
        )
        _resolve_native_change(native_request, allow_admitted_self=True, admitted_subject=True)
        checks = _native_delegation_requests(native_request)
    decisions = provider.batch_check(checks)
    if len(decisions) != len(checks) or any(item.kind == DecisionKind.EVALUATOR_ERROR for item in decisions):
        raise AuthorizationProviderError(_AUTHORIZATION_EVALUATOR_UNAVAILABLE)
    if not checks or not all(item.allowed for item in decisions):
        raise AuthorizationMutationConflict("authorization recovery denied")


def reconcile_policy_mutation(operation_id: UUID, provider: AuthorizationProvider) -> PolicyMutationResult:
    """Lease and recover a requested/ambiguous write using idempotent tuples."""
    with transaction.atomic():
        operation = AuthorizationOperation.objects.select_for_update().get(pk=operation_id)
        if operation.state not in _RECOVERABLE_STATES:
            return _operation_result(operation)
        _assert_operation_provider(operation, provider)
        now = timezone.now()
        if operation.reconcile_lease_until is not None and operation.reconcile_lease_until > now:
            return _operation_result(operation)
        operation.reconcile_lease_until = now + _RECONCILE_LEASE
        operation.save(update_fields=["reconcile_lease_until", "updated_at"])
    return _operation_result(_recover_leased_operation(operation, provider))


def _recover_leased_operation(
    operation: AuthorizationOperation, provider: AuthorizationProvider
) -> AuthorizationOperation:
    """Replay under the lease, retaining the fence until primary readback agrees."""
    change = _change_from_operation(operation)
    try:
        observed = provider.read_relationships(change)
        if not _matches(operation.effect, observed):
            provider.write_relationships(change)
            observed = provider.read_relationships(change)
    except AuthorizationProviderError:
        if operation.state == AuthorizationOperation.State.REQUESTED:
            return _set_outcome(operation, AuthorizationOperation.State.UNRESOLVED, "provider_outcome_unknown")
        observed = None
    if observed is None or not _matches(operation.effect, observed):
        return _clear_reconcile_lease(operation)
    return _set_outcome(operation, AuthorizationOperation.State.CONFIRMED, "reconciled_primary_read")


def _delegation_target(request: MutationRequest) -> TargetRef:
    """Resolve the exact target form carried by a policy or native relationship command."""
    if isinstance(request, PolicyMutationRequest):
        return request.target
    if isinstance(request.change, AdministrativeRoleChange):
        return request.change.target
    return _scope_target(request.scope)
