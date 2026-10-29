"""Private image bytes require current native proof and exact event authority."""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from ctf.models import CTFParticipant
from ctf.services.content_images import can_read_content_image, image_url
from shared.api_tokens.scopes import CTF_EVENT_READ, CTF_PLAY_READ
from shared.authorization import (
    AuthorizationDecision,
    CredentialCeiling,
    DecisionKind,
    TargetRef,
    configured_authorization_provider,
)
from tests.ctf._api_flow_helpers import call_json
from tests.ctf.test_content_images_api import PNG, images, participant_client, storage
from tests.workspaces.test_account_policy_admin import _actor

__all__ = ["images", "participant_client", "storage"]
pytestmark = pytest.mark.django_db


def _native_client(kind, actions):
    credential = replace(_actor(kind, actions=frozenset(actions)), scopes=frozenset({CTF_EVENT_READ, CTF_PLAY_READ}))
    user = get_user_model().objects.get(identity_principal__uuid=credential.principal.uuid) if kind == "human" else None
    client = APIClient()
    client.force_authenticate(user=user, token=credential)
    return client, credential, user


def _allow_action(monkeypatch, event, action):
    def check(request):
        permitted = request.action == action and request.target == TargetRef("event", event.pk)
        return AuthorizationDecision(
            DecisionKind.ALLOWED if permitted else DecisionKind.DENIED,
            "policy_allowed" if permitted else "policy_denied",
        )

    monkeypatch.setattr(configured_authorization_provider(), "check", check)


@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize("proof", ["live", "empty", "other_event", "revoked"])
def test_hidden_image_requires_exact_current_native_staff_proof(images, ctf_event_active, monkeypatch, kind, proof):
    from management.models import PrincipalSession, ServiceCredentialAdmission

    client, credential, user = _native_client(kind, {"event.manage_challenges"})
    _allow_action(monkeypatch, ctf_event_active, "event.manage_challenges")
    if proof == "empty":
        credential = replace(credential, ceiling=CredentialCeiling(frozenset()))
    elif proof == "other_event":
        credential = replace(
            credential, ceiling=CredentialCeiling(credential.ceiling.actions, TargetRef("event", uuid4()))
        )
    elif proof == "revoked":
        if kind == "human":
            PrincipalSession.objects.filter(credential_uuid=credential.credential_uuid).delete()
        else:
            ServiceCredentialAdmission.objects.filter(uuid=credential.credential_uuid).update(is_active=False)
    client.force_authenticate(user=user, token=credential)
    response = client.get(image_url(images["https://img.example.test/later.png"].pk))
    assert response.status_code == (200 if proof == "live" else 404)
    if proof == "live":
        assert response.content == PNG
        assert response["Cache-Control"] == "private, no-store"
    else:
        assert PNG not in response.content


@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize(
    "status,registered", [("active", True), ("disqualified", True), ("banned", True), ("active", False)]
)
def test_image_participation_matches_exact_eligible_principal(
    images, ctf_event_active, monkeypatch, kind, status, registered
):
    client, credential, user = _native_client(kind, {"event.participate"})
    CTFParticipant.objects.create(
        event=ctf_event_active,
        principal_uuid=credential.principal.uuid,
        user=user,
        name="Synthetic native viewer",
        status=status,
        registered_at=timezone.now() if registered else None,
    )
    _allow_action(monkeypatch, ctf_event_active, "event.participate")
    visible = client.get(image_url(images["https://img.example.test/logo.png"].pk))
    hidden = client.get(image_url(images["https://img.example.test/later.png"].pk))
    assert visible.status_code == (200 if registered and status != "banned" else 404)
    assert hidden.status_code == 404


def test_sql_creator_and_legacy_admin_flags_cannot_grant_image_access(images, organizer_user, monkeypatch):
    organizer_user.is_staff = True
    organizer_user.is_superuser = True
    organizer_user.save(update_fields=["is_staff", "is_superuser"])
    monkeypatch.setattr(
        configured_authorization_provider(),
        "check",
        lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied"),
    )
    client = Client()
    client.force_login(organizer_user)
    response = client.get(image_url(images["https://img.example.test/later.png"].pk))
    assert response.status_code == 404
    assert can_read_content_image(organizer_user, images["https://img.example.test/later.png"]) is False


def test_policy_outage_cannot_fall_back_to_participant_membership(images, participant_client, monkeypatch):
    monkeypatch.setattr(
        configured_authorization_provider(),
        "check",
        lambda request: AuthorizationDecision(
            DecisionKind.EVALUATOR_ERROR if request.action == "event.manage_challenges" else DecisionKind.ALLOWED,
            "evaluator_unavailable" if request.action == "event.manage_challenges" else "policy_allowed",
        ),
    )
    response = participant_client.get(image_url(images["https://img.example.test/logo.png"].pk))
    assert response.status_code == 503
    assert PNG not in response.content


def test_participant_image_map_rechecks_live_event_permission(images, participant_client, monkeypatch):
    monkeypatch.setattr(
        configured_authorization_provider(),
        "check",
        lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied"),
    )
    response = call_json(participant_client, "get", "api_me_content_images")
    assert response.status_code == 404
    assert b"source_url" not in response.content


def test_service_does_not_inherit_responsible_human_participation(
    images, ctf_event_active, participant_client, participant_user, monkeypatch
):
    from management.models import Principal

    client, credential, _ = _native_client("service", {"event.participate"})
    Principal.objects.filter(uuid=credential.principal.uuid).update(responsible_user=participant_user)
    _allow_action(monkeypatch, ctf_event_active, "event.participate")
    response = client.get(image_url(images["https://img.example.test/logo.png"].pk))
    assert response.status_code == 404
