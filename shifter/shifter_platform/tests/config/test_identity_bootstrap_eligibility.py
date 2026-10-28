"""First-install eligibility is explicit, plan-bound and verified before activation."""

import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from config.identity_bootstrap import PERSONAL_CREDENTIAL_ACTION, bootstrap_grants
from shared.authorization import RelationshipState
from shared.models import AuditLog
from tests.config.test_identity_cutover_command import _bind_bootstrap, _bootstrap_facts, _facts, _PolicyProvider
from workspaces.models import IdentityCutoverState

pytestmark = pytest.mark.django_db


@pytest.fixture
def bootstrap_candidate(tmp_path, monkeypatch, django_user_model):
    from management.services import principal_for_user

    def prepare(*, eligible=False):
        user = django_user_model.objects.create_user(username="first-eligible-admin")
        principal = principal_for_user(user)
        _bind_bootstrap(principal)
        facts = _facts(tmp_path, bootstrap_personal_token_eligibility=eligible)
        output = StringIO()
        call_command("identity_cutover", "plan", facts=str(facts), stdout=output)
        plan = json.loads(output.getvalue())
        provider = _PolicyProvider()
        for command in ("identity_cutover", "identity_cutover_bootstrap"):
            monkeypatch.setattr(
                f"config.management.commands.{command}.configured_authorization_provider", lambda: provider
            )
        call_command("identity_cutover", "apply", facts=str(facts), plan_digest=plan["plan_digest"])
        return principal, facts, plan, provider

    return prepare


@pytest.mark.parametrize("eligible", [False, True])
def test_bootstrap_seeds_only_explicit_plan_grants(tmp_path, bootstrap_candidate, eligible):
    principal, facts, plan, provider = bootstrap_candidate(eligible=eligible)
    command = {"facts": str(_bootstrap_facts(tmp_path)), "plan_digest": plan["plan_digest"]}
    call_command("identity_cutover_bootstrap", **command)
    first_relationships = set(provider.relationships)
    call_command("identity_cutover_bootstrap", **command)
    assert provider.relationships == first_relationships
    expected = bootstrap_grants(principal.uuid, plan)
    assert all(change.grant_tuple in provider.relationships for change in expected)
    assert len(expected) == (2 if eligible else 1)
    assert any(getattr(change, "action", None) == PERSONAL_CREDENTIAL_ACTION for change in expected) is eligible
    row = AuditLog.objects.filter(entity_ref="identity-cutover-bootstrap").latest("sequence")
    assert row.new_state["personal_token_eligibility"] is eligible
    assert row.new_state["plan_digest"] == plan["plan_digest"]
    call_command("identity_cutover", "verify", facts=str(facts), plan_digest=plan["plan_digest"])
    assert IdentityCutoverState.objects.get().state == "activated"


def test_eligibility_changes_the_reviewed_plan_digest(tmp_path):
    plans = []
    for eligible in (False, True):
        output = StringIO()
        call_command(
            "identity_cutover",
            "plan",
            facts=str(_facts(tmp_path, bootstrap_personal_token_eligibility=eligible)),
            stdout=output,
        )
        plans.append(json.loads(output.getvalue()))
    assert plans[0]["plan_digest"] != plans[1]["plan_digest"]


@pytest.mark.parametrize("value", ["true", 1, None])
def test_eligibility_never_coerces_non_boolean_facts(tmp_path, value):
    with pytest.raises(CommandError, match="facts document is invalid"):
        call_command(
            "identity_cutover", "plan", facts=str(_facts(tmp_path, bootstrap_personal_token_eligibility=value))
        )


@pytest.mark.parametrize("failure", ["denied", "write_lost", "ack_lost"])
def test_eligibility_needs_readback_and_preserves_denials(tmp_path, bootstrap_candidate, monkeypatch, failure):
    principal, _facts_path, plan, provider = bootstrap_candidate(eligible=True)
    eligibility = bootstrap_grants(principal.uuid, plan)[1]
    original_read = provider.read_relationships
    original_write = provider.write_relationships

    def read(change):
        if failure == "denied" and change == eligibility:
            return RelationshipState(False, True)
        return original_read(change)

    def write(change):
        if change != eligibility:
            original_write(change)
        elif failure == "ack_lost":
            original_write(change)
            raise TimeoutError("synthetic lost acknowledgement")
        else:
            raise TimeoutError("synthetic unavailable write")

    monkeypatch.setattr(provider, "read_relationships", read)
    monkeypatch.setattr(provider, "write_relationships", write)
    command = {"facts": str(_bootstrap_facts(tmp_path)), "plan_digest": plan["plan_digest"]}
    if failure == "ack_lost":
        call_command("identity_cutover_bootstrap", **command)
        assert IdentityCutoverState.objects.get().bootstrap_verified_at is not None
    else:
        with pytest.raises(CommandError, match="grant is unresolved"):
            call_command("identity_cutover_bootstrap", **command)
        assert IdentityCutoverState.objects.get().bootstrap_verified_at is None
        assert eligibility.grant_tuple not in provider.relationships


def test_removed_eligibility_blocks_activation(tmp_path, bootstrap_candidate):
    principal, facts, plan, provider = bootstrap_candidate(eligible=True)
    call_command("identity_cutover_bootstrap", facts=str(_bootstrap_facts(tmp_path)), plan_digest=plan["plan_digest"])
    provider.relationships.remove(bootstrap_grants(principal.uuid, plan)[1].grant_tuple)
    with pytest.raises(CommandError, match="grant is unresolved"):
        call_command("identity_cutover", "verify", facts=str(facts), plan_digest=plan["plan_digest"])
    assert IdentityCutoverState.objects.get().state == "policy_pending"
