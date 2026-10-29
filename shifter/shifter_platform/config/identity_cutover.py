"""Bounded composition for the one-time S8 identity cutover."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cms.services import (
    identity_cutover_contract_gaps as cms_contract_gaps,
)
from cms.services import (
    identity_cutover_resource_disposition as cms_resource_disposition,
)
from cms.services import (
    pending_identity_cutover_work as cms_pending_work,
)
from ctf.services import (
    identity_cutover_disposition as ctf_cutover_disposition,
)
from ctf.services import (
    pending_identity_cutover_work as ctf_pending_work,
)
from engine.services import identity_cutover_contract_gaps as engine_contract_gaps
from engine.services import identity_cutover_resource_disposition as engine_resource_disposition
from engine.services import pending_identity_cutover_work as engine_pending_work
from management.services import (
    identity_cutover_disposition as management_cutover_disposition,
)
from management.services import (
    identity_cutover_principals,
    identity_cutover_token_count,
)
from shared.audit import retained_audit_event_count
from shared.model_access.messages import ContractError, strict_json
from workspaces.services import identity_cutover_disposition, plan_identity_cutover_accounts

_FACTS_DOCUMENT_IS_INVALID = "facts document is invalid"

_MAX_FACTS_BYTES = 65_536


class ProviderIdentityFact(BaseModel):
    """One exact immutable provider tuple; email is deliberately not accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=255)


class IdentityCutoverFacts(BaseModel):
    """Closed operator facts that cannot be derived from retained SQL state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["shifter.identity-cutover-facts/v1"] = Field(alias="schema")
    account_types_by_organization: dict[UUID, Literal["team", "enterprise"]]
    bootstrap_personal_token_eligibility: bool = Field(default=False, strict=True)
    owner_user_ids_by_workspace: dict[int, set[int]]
    provider_bindings_by_user: dict[int, ProviderIdentityFact] = Field(default_factory=dict)


class IdentityBootstrapFacts(BaseModel):
    """Exact immutable provider identity selected for the one-time first grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["shifter.identity-bootstrap-facts/v1"] = Field(alias="schema")
    issuer: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=255)


def load_identity_cutover_facts(path_value: str) -> IdentityCutoverFacts:
    """Load a bounded owner-only facts file without exposing its content."""
    path = Path(path_value)
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("mode")
        payload = strict_json(path.read_bytes(), limit=_MAX_FACTS_BYTES)
        facts = IdentityCutoverFacts.model_validate(payload)
    except (OSError, ValueError, ValidationError, ContractError) as exc:
        raise ValueError(_FACTS_DOCUMENT_IS_INVALID) from exc
    _validate_cutover_identifiers(facts)
    return facts


def load_identity_bootstrap_facts(path_value: str) -> IdentityBootstrapFacts:
    """Load one owner-only bootstrap selection without accepting email lookup."""
    path = Path(path_value)
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("mode")
        payload = strict_json(path.read_bytes(), limit=_MAX_FACTS_BYTES)
        facts = IdentityBootstrapFacts.model_validate(payload)
    except (OSError, ValueError, ValidationError, ContractError) as exc:
        raise ValueError("bootstrap facts document is invalid") from exc
    if facts.issuer != facts.issuer.strip() or facts.subject != facts.subject.strip():
        raise ValueError("bootstrap facts document is invalid")
    return facts


def build_identity_cutover_plan(facts: IdentityCutoverFacts) -> dict[str, object]:
    """Build one deterministic, non-mutating plan from database and operator facts."""
    principal_by_user = identity_cutover_principals()
    account_plan = plan_identity_cutover_accounts(
        principal_by_user=principal_by_user,
        owner_ids_by_workspace={key: set(value) for key, value in facts.owner_user_ids_by_workspace.items()},
        account_types_by_organization=facts.account_types_by_organization,
    )
    bindings = {user_id: (item.issuer, item.subject) for user_id, item in facts.provider_bindings_by_user.items()}
    management_disposition, management_blockers = management_cutover_disposition(bindings)
    tenancy_disposition, tenancy_blockers = identity_cutover_disposition()
    ctf_disposition, ctf_blockers = ctf_cutover_disposition()
    cms_resources, cms_gaps = cms_resource_disposition()
    engine_resources, engine_gaps = engine_resource_disposition()
    pending_counts = [cms_pending_work(), ctf_pending_work(), engine_pending_work()]
    pending_work = sum(item[0] for item in pending_counts)
    missing_work = sum(item[1] for item in pending_counts)
    domain_blockers = management_blockers + tenancy_blockers + ctf_blockers
    resource_gaps = cms_gaps + engine_gaps
    contract_gaps = cms_contract_gaps() + engine_contract_gaps()
    if resource_gaps:
        domain_blockers.append({"reason": "resource_owner_principal_missing", "entity_count": resource_gaps})
    if missing_work:
        domain_blockers.append({"reason": "pending_operation_credential_missing", "entity_count": missing_work})
    if contract_gaps:
        domain_blockers.append({"reason": "legacy_authority_contract_unresolved", "entity_count": contract_gaps})
    dispositions = {
        **management_disposition,
        **tenancy_disposition,
        **ctf_disposition,
        "resources": {
            "count": cms_resources + engine_resources,
            "action": "retain_stable_ids_and_bind_principal_scope",
        },
        "credentials": {"tokens": identity_cutover_token_count(), "action": "revoke_and_require_reissue"},
        "credential_bound_work": {
            "pending": pending_work,
            "action": "retain_and_reauthorize_exact_initiator_after_cutover",
        },
        "legacy_authority_contracts": {
            "count": contract_gaps,
            "action": "block_until_reissued_under_principal_credential_contract",
        },
        "audit": {"events": retained_audit_event_count(), "action": "retain_immutable"},
    }
    account_blockers = account_plan["blockers"]
    if not isinstance(account_blockers, list):
        raise ValueError("Cutover account plan is invalid")
    payload: dict[str, object] = {
        "schema": "shifter.identity-cutover-plan/v1",
        "bootstrap_personal_token_eligibility": facts.bootstrap_personal_token_eligibility,
        "individuals": account_plan["individuals"],
        "organizations": account_plan["organizations"],
        "memberships": account_plan["memberships"],
        "dispositions": dispositions,
        "blockers": account_blockers + domain_blockers,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    payload["plan_digest"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _validate_cutover_identifiers(facts: IdentityCutoverFacts) -> None:
    """Reject invalid local IDs or whitespace-altered provider identities."""
    if any(workspace_id <= 0 for workspace_id in facts.owner_user_ids_by_workspace):
        raise ValueError(_FACTS_DOCUMENT_IS_INVALID)
    if any(user_id <= 0 for values in facts.owner_user_ids_by_workspace.values() for user_id in values):
        raise ValueError(_FACTS_DOCUMENT_IS_INVALID)
    if any(user_id <= 0 for user_id in facts.provider_bindings_by_user):
        raise ValueError(_FACTS_DOCUMENT_IS_INVALID)
    if any(
        item.issuer != item.issuer.strip() or item.subject != item.subject.strip()
        for item in facts.provider_bindings_by_user.values()
    ):
        raise ValueError(_FACTS_DOCUMENT_IS_INVALID)
