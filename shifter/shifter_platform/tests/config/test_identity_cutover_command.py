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
