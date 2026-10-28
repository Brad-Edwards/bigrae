"""Provider admission precedes aggregate locks; local state remains fenced."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def _assert_provider_outside_locks(monkeypatch):
    from shared.authorization import configured_authorization_provider

    provider = configured_authorization_provider()
    original = provider.check
    calls = []

    def check(request):
        assert not connection.in_atomic_block, request.action
        calls.append(request.action)
        return original(request)

    monkeypatch.setattr(provider, "check", check)
    return calls


def test_hydration_and_nested_graph_writes_admit_policy_before_locks(ctf_event, organizer_credential, monkeypatch):
    from ctf.services.content_hydration import hydrate_event_ctf_content
    from ctf.services.content_refresh import refresh_event_ctf_content
    from tests.ctf.test_content_hydration import _resolved

    resolved = _resolved()
    ctf_event.scenario_id = resolved.bundle.scenario_id
    ctf_event.save(update_fields=["scenario_id", "updated_at"])
    calls = _assert_provider_outside_locks(monkeypatch)
    result = hydrate_event_ctf_content(ctf_event.pk, resolved, actor_id=organizer_credential)
    assert result.created and result.challenge_count == 2
    replay = refresh_event_ctf_content(
        ctf_event.pk, resolved, actor_id=organizer_credential, expected_current_digest=resolved.evidence.declared_digest
    )
    assert replay.outcome == "noop"
    assert "event.manage_content" in calls and "event.manage_challenges" in calls


def test_communication_creation_and_release_keep_policy_outside_aggregate_locks(
    ctf_event, organizer_credential, monkeypatch
):
    from ctf.services.communication import AdmissionActor, CampaignDraft, create_campaign, release_campaign
    from workspaces.services import resource_scope_from_ids

    calls = _assert_provider_outside_locks(monkeypatch)
    scope = resource_scope_from_ids(
        kind=ctf_event.scope_kind,
        account_id=ctf_event.account_id,
        organization_id=ctf_event.organization_id,
        workspace_id=ctf_event.workspace_id,
    )
    campaign = create_campaign(
        organizer_credential,
        scope.account_uuid,
        CampaignDraft(
            title="Synthetic notice",
            origin="organizer_staff",
            target_event_ids=[ctf_event.pk],
            audience_spec={"kind": "event", "event_ids": [str(ctf_event.pk)]},
            trigger_spec={"kind": "manual"},
            channels=["in_app"],
            subject="Notice",
            body="Synthetic notice",
        ),
        parent_type="account",
    )
    released = release_campaign(
        campaign, occurrence_key="one", admission=AdmissionActor(credential=organizer_credential)
    )
    assert released.status == "released"
    assert calls.count("event.manage_communications") >= 2


def test_generated_seats_prepare_both_permissions_before_locks(ctf_event, organizer_credential, monkeypatch):
    from ctf.services.participant.accounts import create_participant_accounts

    calls = _assert_provider_outside_locks(monkeypatch)
    created = create_participant_accounts(ctf_event.pk, count=1, actor=organizer_credential)
    assert len(created) == 1
    assert "event.manage_participants" in calls
    assert "event.manage_ranges" in calls


def test_provisioning_enqueue_admits_before_locks_and_holds_pack_fence(ctf_event, organizer_credential, monkeypatch):
    from ctf.services.range.tasks import request_event_provisioning

    assert not connection.in_atomic_block
    calls = _assert_provider_outside_locks(monkeypatch)
    task = request_event_provisioning(ctf_event.pk, actor_id=organizer_credential)
    assert task.event_id == ctf_event.pk
    assert task.initiating_principal_uuid == organizer_credential.principal.uuid
    assert task.status == "pending"
    assert "event.manage_ranges" in calls
    assert request_event_provisioning(ctf_event.pk, actor_id=organizer_credential).pk == task.pk


def test_scenario_update_admits_before_locks_and_holds_pack_fence(ctf_event_draft, organizer_credential, monkeypatch):
    from ctf.services.event import update_event

    def admit_scenario(credential, scenario_id, *, scope):
        assert not connection.in_atomic_block
        assert credential == organizer_credential
        assert scenario_id == "synthetic-replacement"

    monkeypatch.setattr("ctf.services.event._update.validate_content_scenario_access", admit_scenario)
    calls = _assert_provider_outside_locks(monkeypatch)
    assert not connection.in_atomic_block
    updated = update_event(ctf_event_draft.pk, {"scenario_id": "synthetic-replacement"}, actor_id=organizer_credential)
    assert updated.scenario_id == "synthetic-replacement"
    assert "event.manage_config" in calls
