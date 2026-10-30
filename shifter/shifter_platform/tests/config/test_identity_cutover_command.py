"""Operator command tests for the bounded S8 identity cutover."""

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.utils import timezone

from management.models import Principal, ProviderBinding
from management.principals import ensure_human_principal
from workspaces.models import Organization, OrganizationMembership, Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def test_status_probe_recognizes_an_absent_legacy_table(monkeypatch):
    from django.db import connection

    monkeypatch.setattr(connection.introspection, "table_names", lambda: [])
    output = StringIO()
    call_command("identity_cutover", "status", probe=True, stdout=output)
    # #2679: before the first migration no user table exists, so the installation is empty.
    assert json.loads(output.getvalue()) == {"state": "absent", "users": 0}


def test_status_probe_counts_users_for_the_fresh_install_decision():
    get_user_model().objects.create_user(username="retained")
    output = StringIO()
    call_command("identity_cutover", "status", probe=True, stdout=output)
    assert json.loads(output.getvalue()) == {"state": "absent", "users": 1}
    plain = StringIO()
    call_command("identity_cutover", "status", stdout=plain)
    assert "users" not in json.loads(plain.getvalue())


def test_status_probe_does_not_convert_database_failure_to_absence(monkeypatch):
    from django.db import OperationalError, connection

    def unavailable():
        raise OperationalError("synthetic unavailable database")

    monkeypatch.setattr(connection.introspection, "table_names", unavailable)
    with pytest.raises(OperationalError):
        call_command("identity_cutover", "status", probe=True, stdout=StringIO())


class _PolicyProvider:
    """Small primary-backed provider double for cutover replay tests."""

    store_id = "01J00000000000000000000001"
    model_id = "01J00000000000000000000000"

    def __init__(self, *, lose_first_ack: bool = False) -> None:
        self.relationships = set()
        self.lose_first_ack = lose_first_ack

    def write_relationships(self, change) -> None:
        self.relationships.update(change.writes)
        if self.lose_first_ack:
            self.lose_first_ack = False
            raise RuntimeError("provider response was lost")

    def read_relationships(self, change):
        from shared.authorization import RelationshipState

        return RelationshipState(change.grant_tuple in self.relationships, False)

    def read_relationship_subjects(self, relation, object_value, *, limit=2):
        return tuple(
            item.user for item in self.relationships if item.relation == relation and item.object == object_value
        )[:limit]


def _facts(tmp_path, **extra):
    path = tmp_path / "facts.json"
    path.write_text(
        json.dumps(
            {
                "schema": "shifter.identity-cutover-facts/v1",
                "account_types_by_organization": {},
                "owner_user_ids_by_workspace": {},
                **extra,
            }
        )
    )
    path.chmod(0o600)
    return path


def _bootstrap_facts(tmp_path, *, issuer="https://issuer.example", subject="provider-subject"):
    path = tmp_path / "bootstrap-facts.json"
    path.write_text(
        json.dumps(
            {
                "schema": "shifter.identity-bootstrap-facts/v1",
                "issuer": issuer,
                "subject": subject,
            }
        )
    )
    path.chmod(0o600)
    return path


def _bind_bootstrap(principal, *, issuer="https://issuer.example", subject="provider-subject"):
    ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid),
        issuer=issuer,
        subject=subject,
    )


def test_plan_is_deterministic_and_does_not_mutate_cutover_state(tmp_path) -> None:
    from workspaces.models import IdentityCutoverState

    first = StringIO()
    second = StringIO()
    facts = _facts(tmp_path)

    call_command("identity_cutover", "plan", facts=str(facts), stdout=first)
    call_command("identity_cutover", "plan", facts=str(facts), stdout=second)

    first_payload = json.loads(first.getvalue())
    second_payload = json.loads(second.getvalue())
    assert first_payload == second_payload
    assert first_payload["schema"] == "shifter.identity-cutover-plan/v1"
    assert first_payload["blockers"] == []
    assert len(first_payload["plan_digest"]) == 64
    assert not IdentityCutoverState.objects.exists()


def test_plan_rejects_unknown_fact_fields(tmp_path) -> None:
    with pytest.raises(CommandError, match="facts document is invalid"):
        call_command("identity_cutover", "plan", facts=str(_facts(tmp_path, unexpected=True)))


def test_plan_blocks_pending_work_without_initiating_credential(tmp_path) -> None:
    from ctf.enums import ScheduledTaskType
    from ctf.models import CTFEvent, CTFScheduledTask
    from tests.ctf.conftest import individual_event_scope, make_ctf_event

    owner = get_user_model().objects.create_user(username="queued-work-owner")
    principal = ensure_human_principal(owner)
    ctf_event = make_ctf_event(
        created_by_id=owner.pk,
        creator_principal_uuid=principal.uuid,
        owner_principal_uuid=principal.uuid,
        **individual_event_scope(owner),
    )
    CTFEvent.objects.bulk_create([ctf_event])

    CTFScheduledTask.objects.create(
        event=ctf_event,
        task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
        scheduled_for=timezone.now(),
    )
    output = StringIO()

    call_command("identity_cutover", "plan", facts=str(_facts(tmp_path)), stdout=output)

    payload = json.loads(output.getvalue())
    assert payload["dispositions"]["credential_bound_work"] == {
        "pending": 1,
        "action": "retain_and_reauthorize_exact_initiator_after_cutover",
    }
    assert {item["reason"] for item in payload["blockers"]} >= {"pending_operation_credential_missing"}


def test_empty_fresh_install_apply_and_verify_reaches_bound_activation(tmp_path, monkeypatch) -> None:
    from management.models import PrincipalSession
    from shared.api_tokens import scopes
    from shared.api_tokens.models import ApiToken
    from workspaces.models import IdentityCutoverState

    facts = _facts(tmp_path)
    user = get_user_model().objects.create_user(username="first-admin")
    principal = ensure_human_principal(user)
    _bind_bootstrap(principal)
    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]

    applied = StringIO()
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest, stdout=applied)
    assert json.loads(applied.getvalue())["state"] == IdentityCutoverState.State.POLICY_PENDING

    provider = _PolicyProvider()
    monkeypatch.setattr(
        "config.management.commands.identity_cutover.configured_authorization_provider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "config.management.commands.identity_cutover_bootstrap.configured_authorization_provider",
        lambda: provider,
    )
    call_command(
        "identity_cutover_bootstrap",
        facts=str(_bootstrap_facts(tmp_path)),
        plan_digest=digest,
    )
    current, _raw = ApiToken.create_token(
        name="pre-cutover",
        created_by=user,
        scopes=[scopes.MISSION_CONTROL_RANGE_READ],
    )
    historical = ApiToken.objects.create(
        name="legacy",
        token_id="legacy-token",
        verifier_hash="a" * 64,
        created_by=user,
        scopes=[scopes.MISSION_CONTROL_RANGE_READ],
    )
    PrincipalSession.objects.create(
        session_key="pre-cutover-session",
        session_data="e30=",
        expire_date=timezone.now() + timedelta(hours=1),
        principal_uuid=principal.uuid,
    )
    verified = StringIO()
    call_command("identity_cutover", "verify", facts=str(facts), plan_digest=digest, stdout=verified)

    state = IdentityCutoverState.objects.get()
    assert json.loads(verified.getvalue())["state"] == IdentityCutoverState.State.ACTIVATED
    assert state.state == IdentityCutoverState.State.ACTIVATED
    assert state.provider_store_id == provider.store_id
    assert state.model_id == provider.model_id
    assert state.admission_invalidated_at is not None
    assert not PrincipalSession.objects.exists()
    current.refresh_from_db()
    historical.refresh_from_db()
    assert current.revoked_at is not None and current.knox_token_id is None and current.requires_reissue
    assert historical.revoked_at is not None and not historical.verifier_hash and historical.requires_reissue


def test_empty_database_prepares_the_bootstrap_principal_before_the_plan(tmp_path, monkeypatch) -> None:
    """#2671: a first install has no user; the bootstrap facts create and bind the first administrator."""
    from workspaces.models import IdentityCutoverState

    assert not get_user_model().objects.exists()
    facts, bootstrap = _facts(tmp_path), _bootstrap_facts(tmp_path)
    prepared = StringIO()
    call_command("identity_cutover_bootstrap", facts=str(bootstrap), prepare=True, stdout=prepared)
    assert json.loads(prepared.getvalue()) == {"prepared": True}
    binding = ProviderBinding.objects.get(issuer="https://issuer.example", subject="provider-subject")
    user = binding.principal.user
    assert user.username.startswith("principal-") and "provider-subject" not in user.username
    assert user.email == "" and user.is_active
    again = StringIO()
    call_command("identity_cutover_bootstrap", facts=str(bootstrap), prepare=True, stdout=again)
    assert json.loads(again.getvalue()) == {"prepared": True}
    assert get_user_model().objects.count() == 1

    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest, stdout=StringIO())
    provider = _PolicyProvider()
    for module in ("identity_cutover", "identity_cutover_bootstrap"):
        monkeypatch.setattr(f"config.management.commands.{module}.configured_authorization_provider", lambda: provider)
    call_command("identity_cutover_bootstrap", facts=str(bootstrap), plan_digest=digest, stdout=StringIO())
    verified = StringIO()
    call_command("identity_cutover", "verify", facts=str(facts), plan_digest=digest, stdout=verified)
    assert json.loads(verified.getvalue())["state"] == IdentityCutoverState.State.ACTIVATED
    assert IdentityCutoverState.objects.get().bootstrap_principal_uuid == binding.principal.uuid
    with pytest.raises(CommandError, match="only before the plan"):
        call_command("identity_cutover_bootstrap", facts=str(bootstrap), prepare=True, stdout=StringIO())


def test_preparation_never_creates_an_administrator_beside_existing_users(tmp_path) -> None:
    get_user_model().objects.create_user(username="retained-user")
    prepared = StringIO()
    call_command("identity_cutover_bootstrap", facts=str(_bootstrap_facts(tmp_path)), prepare=True, stdout=prepared)
    assert json.loads(prepared.getvalue()) == {"prepared": False}
    assert not ProviderBinding.objects.exists()
    assert get_user_model().objects.count() == 1


def _activated_fresh_install(tmp_path, monkeypatch):
    """Activate an empty database through the generated fresh-install flow (#2679)."""
    facts, bootstrap = _facts(tmp_path, bootstrap_personal_token_eligibility=True), _bootstrap_facts(tmp_path)
    call_command("identity_cutover_bootstrap", facts=str(bootstrap), prepare=True, stdout=StringIO())
    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest, stdout=StringIO())
    provider = _PolicyProvider()
    for module in ("identity_cutover", "identity_cutover_bootstrap"):
        monkeypatch.setattr(f"config.management.commands.{module}.configured_authorization_provider", lambda: provider)
    call_command("identity_cutover_bootstrap", facts=str(bootstrap), plan_digest=digest, stdout=StringIO())
    call_command("identity_cutover", "verify", facts=str(facts), plan_digest=digest, stdout=StringIO())
    return bootstrap


def test_installation_catalog_acts_only_as_the_bootstrap_administrator(tmp_path, monkeypatch) -> None:
    """#2679: the in-box catalog registers with a short-lived credential that is always revoked."""
    from shared.api_tokens.models import ApiToken

    bootstrap = _activated_fresh_install(tmp_path, monkeypatch)
    command = "config.management.commands.initialize_installation_catalog"
    actors = []
    monkeypatch.setattr(f"{command}.credential_from_proof", lambda proof: ("actor", proof))
    monkeypatch.setattr(f"{command}.register_inbox_catalog", lambda *, actor: actors.append(actor) or ["pack"])
    output = StringIO()
    call_command("initialize_installation_catalog", bootstrap_facts=str(bootstrap), stdout=output)
    assert "Registered 1 in-box pack(s)." in output.getvalue()
    token = ApiToken.objects.get(name="installation-catalog")
    assert token.revoked_at is not None and actors[0][0] == "actor"
    assert token.expires_at is not None and token.expires_at <= token.created_at + timedelta(minutes=15)
    assert set(token.scopes) == {
        f"authorization:installation.{action}"
        for action in ("read_content", "manage_content", "manage_sources", "prepare_content", "publish_content")
    }

    def broken(*, actor):
        raise ValueError("pack qualification pending")

    monkeypatch.setattr(f"{command}.register_inbox_catalog", broken)
    with pytest.raises(CommandError, match="Catalog initialization incomplete"):
        call_command("initialize_installation_catalog", bootstrap_facts=str(bootstrap), stdout=StringIO())
    assert not ApiToken.objects.filter(name="installation-catalog", revoked_at__isnull=True).exists()
    (tmp_path / "other").mkdir()
    other = _bootstrap_facts(tmp_path / "other", subject="another-subject")
    with pytest.raises(CommandError, match="do not name the installation's bootstrap administrator"):
        call_command("initialize_installation_catalog", bootstrap_facts=str(other), stdout=StringIO())


def test_installation_catalog_requires_the_activated_cutover(tmp_path) -> None:
    with pytest.raises(CommandError, match="requires the activated identity cutover"):
        call_command("initialize_installation_catalog", bootstrap_facts=str(_bootstrap_facts(tmp_path)))


def test_apply_maps_unambiguous_personal_hierarchy_without_deleting_it(tmp_path, monkeypatch) -> None:
    from workspaces.models import Account, IdentityCutoverState, WorkspaceOwnership

    user = get_user_model().objects.create_user(username="legacy-owner")
    principal = ensure_human_principal(user)
    retained_account = Account.objects.create(
        kind=Account.Kind.INDIVIDUAL,
        name="Retained individual account",
        individual_principal_uuid=principal.uuid,
    )
    retained_account_uuid = retained_account.uuid
    _bind_bootstrap(principal)
    organization = Organization.objects.create(name="Personal")
    workspace = Workspace.objects.create(organization=organization, name="Personal", personal_for_user=user)
    OrganizationMembership.objects.create(organization=organization, user=user, role="admin")
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role="owner")
    facts = _facts(tmp_path)
    facts.write_text(
        json.dumps(
            {
                "schema": "shifter.identity-cutover-facts/v1",
                "account_types_by_organization": {},
                "owner_user_ids_by_workspace": {str(workspace.pk): [user.pk]},
            }
        )
    )

    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]
    provider = _PolicyProvider()
    monkeypatch.setattr(
        "config.management.commands.identity_cutover.configured_authorization_provider",
        lambda: provider,
    )
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest)

    account = Account.objects.get(individual_principal_uuid=principal.uuid)
    workspace.refresh_from_db()
    assert account.kind == Account.Kind.INDIVIDUAL
    assert account.uuid == retained_account_uuid
    assert workspace.archived_at is not None
    assert workspace.personal_for_user_id is None
    assert WorkspaceOwnership.objects.filter(workspace=workspace, principal_uuid=principal.uuid).exists()
    assert IdentityCutoverState.objects.get().state == IdentityCutoverState.State.POLICY_PENDING


def test_apply_binds_shared_hierarchy_and_converts_memberships(tmp_path, monkeypatch) -> None:
    from workspaces.models import Account, WorkspaceOwnership

    user = get_user_model().objects.create_user(username="business-owner")
    principal = ensure_human_principal(user)
    _bind_bootstrap(principal)
    organization = Organization.objects.create(name="Business")
    workspace = Workspace.objects.create(organization=organization, name="Primary")
    OrganizationMembership.objects.create(organization=organization, user=user, role="admin")
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role="owner")
    OrganizationMembership.objects.create(organization=organization, principal_uuid=principal.uuid, role="")
    WorkspaceMembership.objects.create(workspace=workspace, principal_uuid=principal.uuid, role="")
    facts = _facts(tmp_path)
    facts.write_text(
        json.dumps(
            {
                "schema": "shifter.identity-cutover-facts/v1",
                "account_types_by_organization": {str(organization.uuid): "enterprise"},
                "owner_user_ids_by_workspace": {str(workspace.pk): [user.pk]},
            }
        )
    )

    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]
    provider = _PolicyProvider()
    monkeypatch.setattr(
        "config.management.commands.identity_cutover.configured_authorization_provider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "config.management.commands.identity_cutover_bootstrap.configured_authorization_provider",
        lambda: provider,
    )
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest)

    organization.refresh_from_db()
    workspace.refresh_from_db()
    assert organization.account.kind == Account.Kind.ENTERPRISE
    assert organization.is_default
    assert workspace.is_default
    assert OrganizationMembership.objects.get(organization=organization).principal_uuid == principal.uuid
    assert WorkspaceMembership.objects.get(workspace=workspace).principal_uuid == principal.uuid
    assert OrganizationMembership.objects.filter(organization=organization).count() == 1
    assert WorkspaceMembership.objects.filter(workspace=workspace).count() == 1
    assert WorkspaceOwnership.objects.filter(workspace=workspace, principal_uuid=principal.uuid).exists()
    expected = {
        ("installation:root", "installation", f"account:{organization.account.uuid}"),
        (f"account:{organization.account.uuid}", "account", f"organization:{organization.uuid}"),
        (f"organization:{organization.uuid}", "organization", f"workspace:{workspace.uuid}"),
        (f"principal:{principal.uuid}", "direct_administrator", f"organization:{organization.uuid}"),
        (f"principal:{principal.uuid}", "direct_administrator", f"workspace:{workspace.uuid}"),
    }
    assert {(item.user, item.relation, item.object) for item in provider.relationships} == expected

    call_command(
        "identity_cutover_bootstrap",
        facts=str(_bootstrap_facts(tmp_path)),
        plan_digest=digest,
    )
    call_command("identity_cutover", "verify", facts=str(facts), plan_digest=digest)


@pytest.mark.parametrize("late_legacy_member", [False, True])
def test_cutover_disposes_memberships_under_prepared_accounts_before_activation(
    tmp_path, monkeypatch, late_legacy_member
):
    from workspaces.models import Account, IdentityCutoverState, WorkspaceOwnership

    user = get_user_model().objects.create_user(username="prepared-account-owner")
    principal = ensure_human_principal(user)
    _bind_bootstrap(principal)
    account = Account.objects.create(kind="team", name="Prepared account")
    organization = Organization.objects.create(account=account, name="Prepared organization", is_default=True)
    workspace = Workspace.objects.create(organization=organization, name="Prepared workspace", is_default=True)
    OrganizationMembership.objects.create(organization=organization, user=user, role="admin")
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role="owner")
    facts = _facts(tmp_path)
    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    plan = json.loads(planned.getvalue())
    assert not plan["blockers"]
    assert len(plan["memberships"]) == 2
    provider = _PolicyProvider()
    monkeypatch.setattr(
        "config.management.commands.identity_cutover.configured_authorization_provider", lambda: provider
    )
    monkeypatch.setattr(
        "config.management.commands.identity_cutover_bootstrap.configured_authorization_provider", lambda: provider
    )
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=plan["plan_digest"])
    assert not OrganizationMembership.objects.filter(user_id__isnull=False).exists()
    assert not WorkspaceMembership.objects.filter(user_id__isnull=False).exists()
    assert WorkspaceOwnership.objects.filter(workspace=workspace, principal_uuid=principal.uuid).exists()
    assert (f"principal:{principal.uuid}", "direct_administrator", f"workspace:{workspace.uuid}") in {
        (item.user, item.relation, item.object) for item in provider.relationships
    }
    call_command("identity_cutover_bootstrap", facts=str(_bootstrap_facts(tmp_path)), plan_digest=plan["plan_digest"])
    if late_legacy_member:
        WorkspaceMembership.objects.create(workspace=workspace, user=user, role="member")
        with pytest.raises(CommandError, match="hierarchy verification failed"):
            call_command("identity_cutover", "verify", facts=str(facts), plan_digest=plan["plan_digest"])
        assert IdentityCutoverState.objects.get().state == "policy_pending"
    else:
        call_command("identity_cutover", "verify", facts=str(facts), plan_digest=plan["plan_digest"])
        assert IdentityCutoverState.objects.get().state == "activated"


def test_apply_reconciles_lost_acknowledgement_and_is_idempotent(tmp_path, monkeypatch) -> None:
    from workspaces.models import IdentityCutoverState

    user = get_user_model().objects.create_user(username="retry-owner")
    principal = ensure_human_principal(user)
    _bind_bootstrap(principal)
    organization = Organization.objects.create(name="Retry business")
    workspace = Workspace.objects.create(organization=organization, name="Primary")
    OrganizationMembership.objects.create(organization=organization, user=user, role="admin")
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role="owner")
    facts = _facts(tmp_path)
    facts.write_text(
        json.dumps(
            {
                "schema": "shifter.identity-cutover-facts/v1",
                "account_types_by_organization": {str(organization.uuid): "team"},
                "owner_user_ids_by_workspace": {str(workspace.pk): [user.pk]},
            }
        )
    )
    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]
    provider = _PolicyProvider(lose_first_ack=True)
    monkeypatch.setattr(
        "config.management.commands.identity_cutover.configured_authorization_provider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "config.management.commands.identity_cutover_bootstrap.configured_authorization_provider",
        lambda: provider,
    )

    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest)
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest)
    call_command(
        "identity_cutover_bootstrap",
        facts=str(_bootstrap_facts(tmp_path)),
        plan_digest=digest,
    )
    call_command("identity_cutover", "verify", facts=str(facts), plan_digest=digest)

    assert IdentityCutoverState.objects.get().state == IdentityCutoverState.State.ACTIVATED


def test_bootstrap_refuses_an_existing_different_administrator(tmp_path, monkeypatch) -> None:
    from shared.authorization import AdministrativeRoleChange, PolicyEffect, RelationshipSubject, TargetRef

    first = get_user_model().objects.create_user(username="selected-admin")
    selected = ensure_human_principal(first)
    _bind_bootstrap(selected)
    other = ensure_human_principal(get_user_model().objects.create_user(username="other-admin"))
    _bind_bootstrap(other, subject="other-provider-subject")
    facts = _facts(tmp_path)
    planned = StringIO()
    call_command("identity_cutover", "plan", facts=str(facts), stdout=planned)
    digest = json.loads(planned.getvalue())["plan_digest"]
    call_command("identity_cutover", "apply", facts=str(facts), plan_digest=digest)
    provider = _PolicyProvider()
    provider.write_relationships(
        AdministrativeRoleChange(
            RelationshipSubject("principal", other.uuid),
            "application_administrator",
            TargetRef("installation"),
            PolicyEffect.GRANT,
        )
    )
    monkeypatch.setattr(
        "config.management.commands.identity_cutover_bootstrap.configured_authorization_provider",
        lambda: provider,
    )

    with pytest.raises(CommandError, match="already has an administrator"):
        call_command(
            "identity_cutover_bootstrap",
            facts=str(_bootstrap_facts(tmp_path)),
            plan_digest=digest,
        )


def test_cutover_adds_a_new_exact_provider_tuple_to_an_already_bound_principal():
    from management.identity_cutover import apply_identity_provider_bindings, identity_cutover_disposition

    user = get_user_model().objects.create_user(username="multi-provider-principal")
    principal = ensure_human_principal(user)
    _bind_bootstrap(principal, issuer="https://original.example", subject="original-subject")
    supplied = {user.pk: ("https://replacement.example", "replacement-subject")}
    _, blockers = identity_cutover_disposition(supplied)
    assert not blockers
    apply_identity_provider_bindings(supplied)
    apply_identity_provider_bindings(supplied)
    assert set(ProviderBinding.objects.filter(principal__uuid=principal.uuid).values_list("issuer", "subject")) == {
        ("https://original.example", "original-subject"),
        ("https://replacement.example", "replacement-subject"),
    }
