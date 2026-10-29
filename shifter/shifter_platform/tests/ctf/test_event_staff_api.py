"""Delegated event-staff roles: assignment and capability boundaries (CTF-607)."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from shared.auth import CTF_ORGANIZER_GROUP
from tests.ctf._api_flow_helpers import call_json

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("during_policy", [False, True])
def test_staff_assignment_fences_revoked_native_session(
    ctf_event, organizer_credential, helper_organizer, monkeypatch, during_policy
):
    from ctf.exceptions import CTFPermissionError
    from ctf.models import CTFEventAuthorityTransition, CTFEventStaff
    from ctf.services.event import assign_event_staff
    from management.models import PrincipalSession
    from shared.authorization import configured_authorization_provider

    def revoke():
        PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).delete()

    provider = configured_authorization_provider()
    check = provider.check

    def revoke_after_check(request):
        decision = check(request)
        if during_policy:
            revoke()
        return decision

    monkeypatch.setattr(provider, "check", revoke_after_check)
    if not during_policy:
        revoke()
    with pytest.raises(CTFPermissionError):
        assign_event_staff(ctf_event.pk, organizer_credential, helper_organizer.identity_principal.uuid, "co_organizer")
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event).exists()
    assert not CTFEventStaff.objects.filter(event=ctf_event).exists()


def test_current_owner_can_revoke_a_deactivated_staff_principal(ctf_event, organizer_credential, helper_organizer):
    from ctf.models import CTFEventStaff
    from ctf.services.event import assign_event_staff, revoke_event_staff

    subject = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, subject, "co_organizer")
    helper_organizer.is_active = False
    helper_organizer.save(update_fields=["is_active"])
    assert revoke_event_staff(ctf_event.pk, organizer_credential, subject)
    assert not CTFEventStaff.objects.filter(event=ctf_event, principal_uuid=subject).exists()


def test_current_administrator_recovers_handoff_after_previous_owner_is_deactivated(
    ctf_event, organizer_user, organizer_credential, helper_organizer, admin_user, live_session_credential, monkeypatch
):
    from ctf.exceptions import CTFStateError
    from ctf.models import CTFEventAuthorityTransition
    from ctf.services.event import assign_event_staff, transfer_event_ownership
    from shared.authorization import AuthorizationProviderError, PolicyEffect, configured_authorization_provider

    subject = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, subject, "co_organizer")
    provider = configured_authorization_provider()
    original = provider.write_relationships

    def interrupt_source_revoke(change):
        if change.effect == PolicyEffect.REVOKE and change.grant_tuple.relation == "direct_administrator":
            raise AuthorizationProviderError("Synthetic source revoke outage")
        return original(change)

    monkeypatch.setattr(provider, "write_relationships", interrupt_source_revoke)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, subject)
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    organizer_user.is_active = False
    organizer_user.save(update_fields=["is_active"])
    monkeypatch.setattr(provider, "write_relationships", original)
    result = transfer_event_ownership(ctf_event.pk, live_session_credential(admin_user), subject)
    assert result.owner_principal_uuid == subject
    transition.refresh_from_db()
    assert transition.state == "confirmed"
    assert transition.actor_credential_uuid == organizer_credential.credential_uuid


def test_same_principal_can_recover_handoff_with_a_fresh_bounded_personal_proof(
    ctf_event, organizer_user, organizer_credential, helper_organizer, monkeypatch
):
    from ctf.exceptions import CTFStateError
    from ctf.services.event import assign_event_staff, transfer_event_ownership
    from shared.api_tokens.models import ApiToken
    from shared.api_tokens.policy import personal_credential_for_token
    from shared.authorization import (
        AuthorizationProviderError,
        TargetRef,
        configured_authorization_provider,
        predefined_policy_definition,
    )

    subject = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, subject, "co_organizer")
    provider = configured_authorization_provider()
    original = provider.read_relationships

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic readback outage")

    monkeypatch.setattr(provider, "read_relationships", unavailable)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, subject)
    monkeypatch.setattr(provider, "read_relationships", original)
    actions = predefined_policy_definition("event_administrator").actions
    token, _raw = ApiToken.create_token(
        name="Bounded handoff recovery",
        created_by=organizer_user,
        scopes=[f"authorization:{action}" for action in sorted(actions)],
        target=TargetRef("event", ctf_event.pk),
    )
    current = personal_credential_for_token(token)
    assert current.credential_uuid != organizer_credential.credential_uuid
    assert transfer_event_ownership(ctf_event.pk, current, subject).owner_principal_uuid == subject


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("failure", ["readback", "write"])
def test_ownership_handoff_recovers_admitted_source_revoke_without_replaying_it(
    ctf_event, organizer_credential, helper_organizer, monkeypatch, expired, failure
):
    from datetime import timedelta

    from django.utils import timezone

    from ctf.exceptions import CTFPermissionError, CTFStateError
    from ctf.models import CTFEventAuthorityTransition
    from ctf.services.event import assign_event_staff, transfer_event_ownership
    from ctf.services.unified_authorization import require_event_action
    from management.models import PrincipalSession
    from shared.authorization import AuthorizationProviderError, PolicyEffect, configured_authorization_provider

    target = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, target, "co_organizer")
    provider = configured_authorization_provider()
    original_write, original_read = provider.write_relationships, provider.read_relationships
    failed = False
    revoke_writes = []

    def source_revoke(change):
        return (
            change.effect == PolicyEffect.REVOKE
            and change.grant_tuple.relation == "direct_administrator"
            and change.grant_tuple.user == f"principal:{organizer_credential.principal.uuid}"
        )

    def write(change):
        nonlocal failed
        if source_revoke(change):
            revoke_writes.append(change)
            if failure == "write" and not failed:
                failed = True
                raise AuthorizationProviderError("synthetic failed write")
        return original_write(change)

    def read(change):
        nonlocal failed
        if source_revoke(change) and failure == "readback" and not failed:
            failed = True
            raise AuthorizationProviderError("synthetic lost readback")
        return original_read(change)

    monkeypatch.setattr(provider, "write_relationships", write)
    monkeypatch.setattr(provider, "read_relationships", read)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, target)
    if failure == "readback":
        with pytest.raises(CTFPermissionError):
            require_event_action(organizer_credential, ctf_event.pk, "event.transfer_ownership")
    assert len(revoke_writes) == 1
    if expired:
        PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(CTFPermissionError):
            transfer_event_ownership(ctf_event.pk, organizer_credential, target)
        ctf_event.refresh_from_db()
        assert ctf_event.owner_principal_uuid == organizer_credential.principal.uuid
    else:
        if failure == "readback":
            failed = False
            with pytest.raises(CTFStateError):
                transfer_event_ownership(ctf_event.pk, organizer_credential, target)
            assert len(revoke_writes) == 1
        result = transfer_event_ownership(ctf_event.pk, organizer_credential, target)
        assert result.owner_principal_uuid == target
        assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()
    assert len(revoke_writes) == (2 if failure == "write" and not expired else 1)


def _organizer(email: str) -> User:
    from management.services import get_user_profile

    user = User.objects.create_user(username=email, email=email, password="testpass123")  # nosec B106
    group, _ = Group.objects.get_or_create(name=CTF_ORGANIZER_GROUP)
    user.groups.add(group)
    profile = get_user_profile(user)
    profile.user_type = "ctf_organizer"
    profile.save(update_fields=["user_type"])
    return user


@pytest.fixture
def helper_organizer():
    return _organizer("helper@test.com")


@pytest.fixture
def helper_client(helper_organizer):
    fresh = Client()
    fresh.force_login(helper_organizer)
    return fresh


def _assign(client, event, email, role):
    from management.services import principal_for_user

    target = User.objects.filter(email=email).first()
    principal_uuid = principal_for_user(target).uuid if target is not None else "00000000-0000-0000-0000-000000000001"
    return call_json(
        client,
        "post",
        "api_event_staff",
        kwargs={"event_id": event.id},
        body={"principal_uuid": str(principal_uuid), "role": role},
    )


class TestStaffAssignment:
    def test_assign_list_and_revoke(self, ctf_event, authenticated_organizer_client, helper_organizer):
        resp = _assign(authenticated_organizer_client, ctf_event, "helper@test.com", "moderator")
        assert resp.status_code == 201
        assert resp.json()["role"] == "moderator"

        listing = call_json(authenticated_organizer_client, "get", "api_event_staff", kwargs={"event_id": ctf_event.id})
        assert [s["principal_uuid"] for s in listing.json()["staff"]] == [str(helper_organizer.identity_principal.uuid)]

        gone = call_json(
            authenticated_organizer_client,
            "delete",
            "api_event_staff_member",
            kwargs={"event_id": ctf_event.id, "principal_uuid": helper_organizer.identity_principal.uuid},
        )
        assert gone.status_code == 204
        listing = call_json(authenticated_organizer_client, "get", "api_event_staff", kwargs={"event_id": ctf_event.id})
        assert listing.json()["staff"] == []

    def test_reassign_changes_role(self, ctf_event, authenticated_organizer_client, helper_organizer):
        _assign(authenticated_organizer_client, ctf_event, "helper@test.com", "moderator")
        resp = _assign(authenticated_organizer_client, ctf_event, "helper@test.com", "judge")
        assert resp.status_code == 201
        listing = call_json(authenticated_organizer_client, "get", "api_event_staff", kwargs={"event_id": ctf_event.id})
        assert [s["role"] for s in listing.json()["staff"]] == ["judge"]

    def test_assign_rejects_unknown_principal_and_bad_role(self, ctf_event, authenticated_organizer_client):
        assert _assign(authenticated_organizer_client, ctf_event, "nobody@test.com", "moderator").status_code == 404
        assert _assign(authenticated_organizer_client, ctf_event, "organizer@test.com", "sheriff").status_code == 400

    def test_legacy_email_selector_reveals_no_account_presence(self, ctf_event, authenticated_organizer_client):
        """The UUID-only endpoint gives identical errors for known and unknown email selectors (#2617)."""
        errors = []
        for email in ("organizer@test.com", "nobody@test.com"):
            response = call_json(
                authenticated_organizer_client,
                "post",
                "api_event_staff",
                kwargs={"event_id": ctf_event.pk},
                body={"email": email, "role": "moderator"},
            )
            assert response.status_code == 400
            error = response.json()["error"]
            errors.append({key: error.get(key) for key in ("code", "message", "details")})
            assert email not in response.content.decode()
        assert errors[0] == errors[1]

    def test_assign_rejects_owner_and_accepts_active_human_principal(
        self, ctf_event, authenticated_organizer_client, participant_user
    ):
        assert _assign(authenticated_organizer_client, ctf_event, "organizer@test.com", "judge").status_code == 400
        assert _assign(authenticated_organizer_client, ctf_event, participant_user.email, "judge").status_code == 201


class TestModeratorCapabilities:
    @pytest.fixture
    def moderator_client(self, ctf_event, authenticated_organizer_client, helper_client):
        _assign(authenticated_organizer_client, ctf_event, "helper@test.com", "moderator")
        return helper_client

    def test_moderator_manages_participants(self, ctf_event, moderator_client, monkeypatch):
        monkeypatch.setattr("ctf.services.participant.accounts.request_event_provisioning", lambda *_a, **_kw: None)
        listing = call_json(moderator_client, "get", "api_participant_list", kwargs={"event_id": ctf_event.id})
        assert listing.status_code == 200
        invited = call_json(
            moderator_client,
            "post",
            "api_participant_list",
            kwargs={"event_id": ctf_event.id},
            body={"email": "new@test.com", "name": "Newbie"},
        )
        assert invited.status_code == 201

    def test_moderator_cannot_touch_challenges_or_staff(self, ctf_event, moderator_client):
        challenges = call_json(moderator_client, "get", "api_challenge_list", kwargs={"event_id": ctf_event.id})
        assert challenges.status_code == 403
        staff = call_json(moderator_client, "get", "api_event_staff", kwargs={"event_id": ctf_event.id})
        assert staff.status_code == 403


class TestJudgeCapabilities:
    @pytest.fixture
    def judge_client(self, ctf_event, authenticated_organizer_client, helper_client):
        _assign(authenticated_organizer_client, ctf_event, "helper@test.com", "judge")
        return helper_client

    @pytest.fixture
    def target_participant(self, ctf_event, participant_user):
        from django.utils import timezone

        from ctf.models import CTFParticipant

        return CTFParticipant.objects.create(
            event=ctf_event,
            user=participant_user,
            email=participant_user.email,
            name="Scored Player",
            status="active",
            registered_at=timezone.now(),
        )

    def test_judge_grants_awards_but_cannot_manage_roster(self, ctf_event, judge_client, target_participant):
        granted = call_json(
            judge_client,
            "post",
            "api_participant_awards",
            kwargs={"participant_id": target_participant.id},
            body={"points": 25, "reason": "style points"},
        )
        assert granted.status_code == 200

        roster = call_json(judge_client, "get", "api_participant_list", kwargs={"event_id": ctf_event.id})
        assert roster.status_code == 403
        banned = call_json(
            judge_client, "post", "api_participant_ban", kwargs={"participant_id": target_participant.id}
        )
        assert banned.status_code == 403

    def test_judge_reads_participant_detail_and_timeline(self, judge_client, target_participant):
        detail = call_json(
            judge_client, "get", "api_participant_detail", kwargs={"participant_id": target_participant.id}
        )
        assert detail.status_code == 200
        timeline = call_json(
            judge_client, "get", "api_score_timeline", kwargs={"participant_id": target_participant.id}
        )
        assert timeline.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_staff_http_keeps_durable_intent_when_policy_readback_is_lost(
    ctf_event,
    authenticated_admin_client,
    helper_organizer,
    monkeypatch,
):
    from django.db import connection

    from ctf.models import CTFEventAuthorityTransition
    from shared.authorization import AuthorizationProviderError, configured_authorization_provider
    from workspaces.models import AuthorizationOperation

    provider = configured_authorization_provider()
    original_check = provider.check

    def check(request):
        assert not connection.in_atomic_block
        return original_check(request)

    def read(change):
        assert not connection.in_atomic_block
        raise AuthorizationProviderError("synthetic readback unavailable")

    monkeypatch.setattr(provider, "check", check)
    monkeypatch.setattr(provider, "read_relationships", read)
    response = _assign(authenticated_admin_client, ctf_event, helper_organizer.email, "judge")
    assert response.status_code == 503
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event)
    assert transition.state == "pending"
    assert AuthorizationOperation.objects.filter(state="unresolved").exists()


@pytest.mark.parametrize("kind", ["human", "service"])
def test_third_administrator_transfers_ownership_without_using_a_self_grant(
    ctf_event,
    organizer_credential,
    helper_organizer,
    admin_user,
    live_session_credential,
    settings,
    kind,
):
    from ctf.services.event import assign_event_staff, transfer_event_ownership
    from shared.authorization import (
        ACTION_CATALOG,
        AdministrativeRoleChange,
        PolicyEffect,
        RelationshipSubject,
        TargetRef,
        configured_authorization_provider,
    )
    from tests.cms._warm_pool_fixtures import service_credential

    target = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, target, "co_organizer")
    actor = (
        live_session_credential(admin_user)
        if kind == "human"
        else service_credential(settings, scopes=[f"authorization:{item.code}" for item in ACTION_CATALOG])
    )
    if kind == "service":
        configured_authorization_provider().write_relationships(
            AdministrativeRoleChange(
                RelationshipSubject("principal", actor.principal.uuid),
                "event_administrator",
                TargetRef("event", ctf_event.pk),
                PolicyEffect.GRANT,
            )
        )
    event = transfer_event_ownership(ctf_event.pk, actor, target)
    assert event.owner_principal_uuid == target


def test_current_administrator_recovers_the_original_ownership_intent_after_credential_expiry(
    ctf_event,
    organizer_credential,
    helper_organizer,
    admin_user,
    live_session_credential,
    monkeypatch,
):
    from datetime import timedelta

    from django.utils import timezone

    from ctf.exceptions import CTFStateError
    from ctf.models import CTFEventAuthorityTransition
    from ctf.services.event import assign_event_staff, transfer_event_ownership
    from management.models import PrincipalSession
    from shared.authorization import AuthorizationProviderError, configured_authorization_provider

    target = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, target, "co_organizer")
    provider = configured_authorization_provider()
    original = provider.read_relationships

    def unavailable(change):
        raise AuthorizationProviderError("synthetic readback unavailable")

    monkeypatch.setattr(provider, "read_relationships", unavailable)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, target)
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )
    monkeypatch.setattr(provider, "read_relationships", original)
    event = transfer_event_ownership(ctf_event.pk, live_session_credential(admin_user), target)
    assert event.owner_principal_uuid == target
    transition.refresh_from_db()
    assert transition.state == "confirmed"
    assert transition.actor_principal_uuid == organizer_credential.principal.uuid
    assert transition.actor_credential_uuid == organizer_credential.credential_uuid
