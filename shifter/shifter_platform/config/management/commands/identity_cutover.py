"""Run the bounded S8 identity cutover plan, apply, or verify phase."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from cms.services import (
    apply_identity_cutover_resources as apply_cms_cutover_resources,
)
from config.identity_cutover import (
    IdentityCutoverFacts,
    build_identity_cutover_plan,
    load_identity_cutover_facts,
)
from config.openfga_authorization import configured_authorization_provider
from ctf.services import (
    apply_identity_cutover_resources as apply_ctf_cutover_resources,
)
from ctf.services import (
    confirm_identity_cutover_publication,
)
from ctf.services import (
    identity_cutover_relationships as ctf_cutover_relationships,
)
from engine.services import apply_identity_cutover_resources as apply_engine_cutover_resources
from management.services import (
    PrincipalConflictError,
    apply_identity_provider_bindings,
    identity_cutover_principals,
    invalidate_pre_cutover_admission,
)
from shared.authorization import CutoverRelationshipChange
from workspaces.services import (
    IdentityCutoverConflict,
    IdentityCutoverStateView,
    activate_identity_cutover,
    apply_identity_cutover_sql,
    begin_identity_cutover,
    bind_identity_cutover_provider,
    get_identity_cutover_state,
    identity_cutover_resource_mappings,
    record_identity_cutover_policy_intent,
    verify_identity_cutover_hierarchy,
)
from workspaces.services import (
    identity_cutover_relationships as workspace_cutover_relationships,
)


class Command(BaseCommand):
    """Plan the coordinated identity cutover without implicit facts."""

    help = "Plan, apply, or verify the coordinated identity cutover"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("phase", choices=("status", "plan", "apply", "verify"))
        parser.add_argument("--facts")
        parser.add_argument("--plan-digest", default="")
        parser.add_argument(
            "--probe", action="store_true", help="Allow an absent legacy schema; verify activated runtime"
        )

    def handle(self, *args, **options) -> None:
        phase = options["phase"]
        if phase == "status":
            state = get_identity_cutover_state(allow_missing_schema=options["probe"])
            result = {"state": "absent"} if state is None else _state_result(state)
            if options["probe"] and result["state"] == "activated":
                from config.identity_runtime import IdentityRuntimeNotReady, require_identity_runtime_ready

                try:
                    require_identity_runtime_ready()
                except IdentityRuntimeNotReady:
                    raise CommandError("Identity runtime is unavailable") from None
            self.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
            return
        if not options["facts"]:
            raise CommandError("facts document is required")
        try:
            facts = load_identity_cutover_facts(options["facts"])
        except ValueError as exc:
            raise CommandError("facts document is invalid") from exc
        payload = build_identity_cutover_plan(facts)
        if phase == "plan":
            result = payload
        else:
            supplied_digest = options["plan_digest"]
            if supplied_digest != payload["plan_digest"]:
                existing = get_identity_cutover_state(plan_digest=supplied_digest)
                if existing is None or not isinstance(existing.plan_payload, dict):
                    raise CommandError("cutover plan digest does not match")
                payload = existing.plan_payload
            if payload["blockers"]:
                raise CommandError("cutover plan contains blockers")
            result = self._apply(payload, facts) if phase == "apply" else self._verify(payload)
        self.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))

    @staticmethod
    def _apply(plan: dict[str, object], facts) -> dict[str, object]:
        """Commit SQL and durable policy intent before replaying provider writes."""
        with transaction.atomic():
            try:
                state = begin_identity_cutover(json.loads(json.dumps(plan, default=str)))
            except IdentityCutoverConflict as exc:
                raise CommandError(str(exc)) from exc
            if state.state == "activated":
                return _state_result(state)
            try:
                _apply_provider_bindings(plan, facts)
                apply_identity_cutover_sql(plan)
                _apply_retained_resource_mapping(plan)
            except (IdentityCutoverConflict, PrincipalConflictError) as exc:
                raise CommandError("cutover SQL facts changed") from exc
            intent = _relationship_intent(plan)
            tuple_digest = _digest(intent)
            try:
                state = record_identity_cutover_policy_intent(str(plan["plan_digest"]), intent, tuple_digest)
            except IdentityCutoverConflict as exc:
                raise CommandError(str(exc)) from exc
        if state.relationship_intent:
            provider = configured_authorization_provider()
            state = _bind_provider(state.plan_digest, provider)
            for item in state.relationship_intent:
                change = _relationship_change(item)
                try:
                    provider.write_relationships(change)
                except Exception:
                    try:
                        recovered = provider.read_relationships(change).grant_present
                    except Exception as exc:
                        raise CommandError("cutover policy write is unresolved") from exc
                    if not recovered:
                        raise CommandError("cutover policy write is unresolved") from None
        return _state_result(state)

    @staticmethod
    def _verify(plan: dict[str, object]) -> dict[str, object]:
        """Read every durable relationship from the pinned provider before activation."""
        provider = configured_authorization_provider()
        state = get_identity_cutover_state()
        if state is None:
            raise CommandError("cutover has not been applied")
        if state.plan_digest != plan["plan_digest"] or state.state not in {"policy_pending", "activated"}:
            raise CommandError("cutover state is not verifiable")
        state = _bind_provider(state.plan_digest, provider)
        for item in state.relationship_intent:
            try:
                present = provider.read_relationships(_relationship_change(item)).grant_present
            except Exception as exc:
                raise CommandError("cutover policy verification is unavailable") from exc
            if not present:
                raise CommandError("cutover policy verification does not match intent")
        confirm_identity_cutover_publication(provider.store_id, provider.model_id)
        if _digest(state.relationship_intent) != state.tuple_digest:
            raise CommandError("cutover relationship digest does not match")
        if state.bootstrap_principal_uuid is None or state.bootstrap_verified_at is None:
            raise CommandError("cutover administrator bootstrap is incomplete")
        with transaction.atomic():
            state = get_identity_cutover_state(plan_digest=state.plan_digest)
            if state is None:
                raise CommandError("cutover state changed during verification")
            if state.state == "activated":
                return _state_result(state)
            if state.state != "policy_pending":
                raise CommandError("cutover state changed during verification")
            try:
                verify_identity_cutover_hierarchy(plan)
            except IdentityCutoverConflict as exc:
                raise CommandError("cutover hierarchy verification failed") from exc
            invalidate_pre_cutover_admission()
            try:
                state = activate_identity_cutover(state.plan_digest)
            except IdentityCutoverConflict as exc:
                raise CommandError(str(exc)) from exc
        return _state_result(state)


def _state_result(state: IdentityCutoverStateView) -> dict[str, object]:
    """Return bounded state without exposing relationship payloads."""
    return {"state": state.state, "plan_digest": state.plan_digest, "tuple_digest": state.tuple_digest}


def _apply_provider_bindings(plan: Mapping[str, object], facts: IdentityCutoverFacts) -> None:
    """Create only exact fact-bound provider tuples; never infer identity from email."""
    digests = sorted(
        hashlib.sha256(f"{user_id}\0{item.issuer}\0{item.subject}".encode()).hexdigest()
        for user_id, item in facts.provider_bindings_by_user.items()
    )
    dispositions = plan.get("dispositions")
    if not isinstance(dispositions, Mapping):
        raise CommandError("cutover disposition plan is invalid")
    binding_plan = dispositions.get("provider_bindings")
    if (
        not isinstance(binding_plan, Mapping)
        or binding_plan.get("intent_digest") != hashlib.sha256("".join(digests).encode()).hexdigest()
    ):
        raise CommandError("provider binding facts changed")
    apply_identity_provider_bindings(
        {user_id: (item.issuer, item.subject) for user_id, item in facts.provider_bindings_by_user.items()}
    )


def _apply_retained_resource_mapping(plan: Mapping[str, object]) -> None:
    """Move legacy workspace-scoped rows to explicit account ancestry in owning tables."""
    principal_by_user = identity_cutover_principals()
    mappings = identity_cutover_resource_mappings(plan)
    apply_cms_cutover_resources(mappings, principal_by_user)
    apply_ctf_cutover_resources(mappings, principal_by_user)
    apply_engine_cutover_resources(mappings, principal_by_user)


def _digest(intent: object) -> str:
    encoded = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _bind_provider(plan_digest: str, provider: object):
    """Bind pending work to one exact store/model pair."""
    store_id = str(getattr(provider, "store_id", ""))
    model_id = str(getattr(provider, "model_id", ""))
    if not store_id or not model_id:
        raise CommandError("authorization provider binding is invalid")
    try:
        return bind_identity_cutover_provider(plan_digest, store_id, model_id)
    except IdentityCutoverConflict as exc:
        raise CommandError(str(exc)) from exc


def _relationship_intent(plan: Mapping[str, object]) -> list[dict[str, str]]:
    """Derive the complete closed hierarchy and explicit legacy-admin disposition."""
    relationships = workspace_cutover_relationships(plan)
    relationships.update(ctf_cutover_relationships())
    return [
        {"user": user, "relation": relation, "object": object_value}
        for user, relation, object_value in sorted(relationships)
    ]


def _relationship_change(item: object) -> CutoverRelationshipChange:
    if not isinstance(item, Mapping):
        raise CommandError("cutover relationship intent is invalid")
    try:
        return CutoverRelationshipChange(str(item["user"]), str(item["relation"]), str(item["object"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise CommandError("cutover relationship intent is invalid") from exc
