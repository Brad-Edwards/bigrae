"""Durable one-time cutover state, provider pinning and activation receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from django.db import connection
from django.utils import timezone

from workspaces.models import IdentityCutoverState


class IdentityCutoverConflict(RuntimeError):
    """Persisted facts changed after the cutover plan was created."""


@dataclass(frozen=True, slots=True)
class IdentityCutoverStateView:
    """Persistence-neutral projection of the singleton cutover state."""

    state: str
    plan_digest: str
    plan_payload: dict[str, object]
    relationship_intent: list[dict[str, str]]
    tuple_digest: str
    provider_store_id: str
    model_id: str
    bootstrap_principal_uuid: UUID | None
    bootstrap_verified_at: object | None
    admission_invalidated_at: object | None


def _state_view(state: IdentityCutoverState) -> IdentityCutoverStateView:
    """Project the retained singleton without exposing the persistence model."""
    return IdentityCutoverStateView(
        state.state,
        state.plan_digest,
        state.plan_payload,
        state.relationship_intent,
        state.tuple_digest,
        state.provider_store_id,
        state.model_id,
        state.bootstrap_principal_uuid,
        state.bootstrap_verified_at,
        state.admission_invalidated_at,
    )


def get_identity_cutover_state(
    *, plan_digest: str | None = None, allow_missing_schema: bool = False
) -> IdentityCutoverStateView | None:
    """Read the singleton state, optionally pinned to one plan digest."""
    if allow_missing_schema and IdentityCutoverState._meta.db_table not in connection.introspection.table_names():
        return None
    query = IdentityCutoverState.objects.filter(key=1)
    if plan_digest is not None:
        query = query.filter(plan_digest=plan_digest)
    state = query.first()
    return _state_view(state) if state is not None else None


def begin_identity_cutover(plan: Mapping[str, object]) -> IdentityCutoverStateView:
    """Create or lock the durable cutover state for one exact plan."""
    digest = str(plan["plan_digest"])
    state, _ = IdentityCutoverState.objects.select_for_update().get_or_create(
        key=1,
        defaults={"state": IdentityCutoverState.State.PLANNED, "plan_digest": digest, "plan_payload": dict(plan)},
    )
    if state.plan_digest != digest:
        raise IdentityCutoverConflict("another cutover plan owns the state")
    return _state_view(state)


def record_identity_cutover_policy_intent(
    plan_digest: str, intent: list[dict[str, str]], tuple_digest: str
) -> IdentityCutoverStateView:
    """Commit the complete relationship intent before provider writes."""
    state = IdentityCutoverState.objects.select_for_update().get(key=1, plan_digest=plan_digest)
    if state.relationship_intent and state.relationship_intent != intent:
        raise IdentityCutoverConflict("cutover relationship intent changed")
    state.state = IdentityCutoverState.State.POLICY_PENDING
    state.relationship_intent = intent
    state.tuple_digest = tuple_digest
    state.save(update_fields=["state", "relationship_intent", "tuple_digest", "updated_at"])
    return _state_view(state)


def bind_identity_cutover_provider(plan_digest: str, store_id: str, model_id: str) -> IdentityCutoverStateView:
    """Pin pending work to the exact policy provider deployment."""
    state = IdentityCutoverState.objects.get(key=1, plan_digest=plan_digest)
    if state.provider_store_id and (state.provider_store_id != store_id or state.model_id != model_id):
        raise IdentityCutoverConflict("authorization provider binding changed")
    if not state.provider_store_id:
        state.provider_store_id = store_id
        state.model_id = model_id
        state.save(update_fields=["provider_store_id", "model_id", "updated_at"])
    return _state_view(state)


def record_identity_cutover_bootstrap(plan_digest: str, principal_uuid: UUID) -> IdentityCutoverStateView:
    """Atomically bind the one verified initial application administrator."""
    state = IdentityCutoverState.objects.select_for_update().get(key=1, plan_digest=plan_digest)
    if state.state != IdentityCutoverState.State.POLICY_PENDING:
        raise IdentityCutoverConflict("cutover policy state changed during bootstrap")
    if state.bootstrap_principal_uuid not in {None, principal_uuid}:
        raise IdentityCutoverConflict("bootstrap principal changed")
    state.bootstrap_principal_uuid = principal_uuid
    state.bootstrap_verified_at = timezone.now()
    state.save(update_fields=["bootstrap_principal_uuid", "bootstrap_verified_at", "updated_at"])
    return _state_view(state)


def activate_identity_cutover(plan_digest: str) -> IdentityCutoverStateView:
    """Activate only the locked, fully verified pending state."""
    state = IdentityCutoverState.objects.select_for_update().get(key=1, plan_digest=plan_digest)
    if state.state == IdentityCutoverState.State.ACTIVATED:
        return _state_view(state)
    if state.state != IdentityCutoverState.State.POLICY_PENDING:
        raise IdentityCutoverConflict("cutover state changed during verification")
    state.state = IdentityCutoverState.State.VERIFIED
    state.save(update_fields=["state", "updated_at"])
    state.admission_invalidated_at = timezone.now()
    state.state = IdentityCutoverState.State.ACTIVATED
    state.save(update_fields=["state", "admission_invalidated_at", "updated_at"])
    return _state_view(state)
