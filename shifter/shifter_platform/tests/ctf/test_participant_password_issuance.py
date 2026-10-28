"""One-time participant password issuance behavior (issue #1924)."""

from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.signals import got_request_exception
from django.test import Client
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter

from ctf.exceptions import CTFPermissionError, CTFValidationError
from ctf.services.participant.accounts import create_participant_accounts
from ctf.services.participant.credentials import reset_participant_password
from management.services import get_user_profile, set_ctf_password_change_required
from shared.api_tokens.models import ApiToken
from shared.audit import AuditAction, AuditEntityType, RequestAudit
from shared.models import AuditLog

pytestmark = pytest.mark.django_db

User = get_user_model()

_GENERATED_PASSWORD = "Generated-Participant-Password-42"  # nosec B105
_SECOND_GENERATED_PASSWORD = "Generated-Participant-Password-84"  # nosec B105
_SUPPLIED_PASSWORD = "Organizer-Supplied-Password-42"  # nosec B105
_PLATFORM_PASSWORD = "Platform-Default-Must-Be-Ignored-42"  # nosec B105


@pytest.fixture(autouse=True)
def _disable_provisioning(monkeypatch):
    monkeypatch.setattr("ctf.services.participant.accounts.request_event_provisioning", lambda *_a, **_kw: None)
    caches["launch_rate_limit"].clear()


def _create_participant(event, actor):
    return create_participant_accounts(event.id, count=1, actor=actor)[0]


def test_account_creation_generates_a_fresh_password_when_event_has_no_shared_policy(
    ctf_event,
    organizer_credential,
    monkeypatch,
    settings,
):
    ctf_event.participant_password_override = ""
    ctf_event.save(update_fields=["participant_password_override", "updated_at"])
    settings.CTF_DEFAULT_PARTICIPANT_PASSWORD = _PLATFORM_PASSWORD
    monkeypatch.setattr(
        "ctf.services.participant.accounts.generate_participant_password",
        lambda *, user=None: _GENERATED_PASSWORD,
    )

    participant = _create_participant(ctf_event, organizer_credential)

    assert participant.user.check_password(_GENERATED_PASSWORD)
    assert not participant.user.check_password(_PLATFORM_PASSWORD)


def test_account_creation_uses_only_an_explicit_event_shared_password(ctf_event, organizer_credential, monkeypatch):
    shared_password = "Explicit-Event-Shared-Password-42"  # nosec B105
    ctf_event.participant_password_override = shared_password
    ctf_event.save(update_fields=["participant_password_override", "updated_at"])
    generated = []
    monkeypatch.setattr(
        "ctf.services.participant.accounts.generate_participant_password",
        lambda *, user=None: generated.append(True) or _GENERATED_PASSWORD,
    )

    participant = _create_participant(ctf_event, organizer_credential)

    assert participant.user.check_password(shared_password)
    assert generated == []


def test_generated_reset_updates_security_state_and_writes_secret_free_audit(
    ctf_event,
    organizer_user,
    organizer_credential,
    monkeypatch,
):
    participant = _create_participant(ctf_event, organizer_credential)
    set_ctf_password_change_required(participant.user, False)
    token, _raw = ApiToken.create_token(
        name="defensive participant token",
        created_by=participant.user,
        scopes=["ctf:event:read"],
    )
    monkeypatch.setattr(
        "ctf.services.participant.accounts.generate_participant_password",
        lambda *, user=None: _GENERATED_PASSWORD,
    )

    result = reset_participant_password(
        participant.id,
        actor=organizer_credential,
        kind="generated",
        request_audit=RequestAudit(source_ip="192.0.2.10", user_agent="pytest", request_id="request-1924"),
    )

    participant.user.refresh_from_db()
    token.refresh_from_db()
    audit = AuditLog.objects.get(
        entity_type=AuditEntityType.USER,
        entity_id=participant.user_id,
        action=AuditAction.UPDATE,
        context="ctf_participant_password_generated",
    )
    assert result.password == _GENERATED_PASSWORD
    assert result.kind == "generated"
    assert participant.user.check_password(_GENERATED_PASSWORD)
    assert get_user_profile(participant.user).must_change_password is True
    assert token.revoked_at is not None
    assert audit.actor_id is None
    assert audit.actor_principal_uuid == organizer_credential.principal.uuid
    assert audit.source_ip == "192.0.2.10"
    assert audit.request_id == "request-1924"
    audit_text = json.dumps({"previous": audit.previous_state, "new": audit.new_state})
    assert _GENERATED_PASSWORD not in audit_text
    assert "password" not in audit_text.lower()


def test_supplied_reset_uses_django_validation_and_invalidates_the_old_session(
    ctf_event_active,
    organizer_user,
    organizer_credential,
):
    participant = _create_participant(ctf_event_active, organizer_credential)
    participant_client = Client()
    participant_client.force_login(participant.user)

    result = reset_participant_password(
        participant.id,
        actor=organizer_credential,
        kind="set",
        password=_SUPPLIED_PASSWORD,
    )
    participant.user.refresh_from_db()
    response = participant_client.get(reverse("ctf:participant_dashboard"))

    assert result.password == _SUPPLIED_PASSWORD
    assert participant.user.check_password(_SUPPLIED_PASSWORD)
    assert response.status_code == 302
    assert "_auth_user_id" not in participant_client.session

    with pytest.raises(CTFValidationError):
        reset_participant_password(
            participant.id,
            actor=organizer_credential,
            kind="set",
            password="short",
        )
    participant.user.refresh_from_db()
    assert participant.user.check_password(_SUPPLIED_PASSWORD)


def test_service_rechecks_event_capability(ctf_event, organizer_credential, standard_user, live_session_credential):
    participant = _create_participant(ctf_event, organizer_credential)

    with pytest.raises(CTFPermissionError) as exc:
        reset_participant_password(
            participant.id,
            actor=live_session_credential(standard_user),
            kind="generated",
        )

    assert exc.value.code == "CTF_PERMISSION_DENIED"


def test_reset_api_returns_each_password_once_and_never_from_a_read(
    authenticated_organizer_client,
    ctf_event,
    organizer_credential,
    monkeypatch,
):
    participant = _create_participant(ctf_event, organizer_credential)
    generated = iter([_GENERATED_PASSWORD, _SECOND_GENERATED_PASSWORD])
    monkeypatch.setattr(
        "ctf.services.participant.accounts.generate_participant_password",
        lambda *, user=None: next(generated),
    )
    reset_url = reverse("v1:ctf:api_participant_password_reset", kwargs={"participant_id": participant.id})

    first = authenticated_organizer_client.post(
        reset_url,
        data=json.dumps({"kind": "generated"}),
        content_type="application/json",
    )
    detail = authenticated_organizer_client.get(
        reverse("v1:ctf:api_participant_detail", kwargs={"participant_id": participant.id})
    )
    retrieval = authenticated_organizer_client.get(reset_url)
    second = authenticated_organizer_client.post(
        reset_url,
        data=json.dumps({"kind": "generated"}),
        content_type="application/json",
    )

    assert first.status_code == 200
    assert first.json()["password"] == _GENERATED_PASSWORD
    assert first["Cache-Control"] == "private, no-store"
    assert _GENERATED_PASSWORD not in detail.content.decode()
    assert retrieval.status_code == 405
    assert second.status_code == 200
    assert second.json()["password"] == _SECOND_GENERATED_PASSWORD
    assert second.json()["password"] != first.json()["password"]


def test_reset_api_supports_supplied_password_and_denies_cross_event_actor(
    authenticated_organizer_client,
    ctf_event,
    ctf_event_active,
    organizer_credential,
    second_organizer_user,
    live_session_credential,
    monkeypatch,
):
    participant = _create_participant(ctf_event, organizer_credential)
    supplied = authenticated_organizer_client.post(
        reverse("v1:ctf:api_participant_password_reset", kwargs={"participant_id": participant.id}),
        data=json.dumps({"kind": "set", "password": _SUPPLIED_PASSWORD}),
        content_type="application/json",
    )

    other = _create_participant(ctf_event_active, organizer_credential)
    from ctf.enums import EventStaffRole
    from ctf.services.event import assign_event_staff, revoke_event_staff, transfer_event_ownership
    from shared.principal_port import principal_for_user

    new_owner = principal_for_user(second_organizer_user)
    assign_event_staff(
        other.event_id,
        organizer_credential,
        new_owner.uuid,
        EventStaffRole.CO_ORGANIZER.value,
    )
    transfer_event_ownership(other.event_id, organizer_credential, new_owner.uuid)
    revoke_event_staff(
        other.event_id,
        live_session_credential(second_organizer_user),
        principal_for_user(ctf_event.created_by).uuid,
    )
    denied = authenticated_organizer_client.post(
        reverse("v1:ctf:api_participant_password_reset", kwargs={"participant_id": other.id}),
        data=json.dumps({"kind": "generated"}),
        content_type="application/json",
    )

    participant.user.refresh_from_db()
    assert supplied.status_code == 200
    assert supplied.json()["password"] == _SUPPLIED_PASSWORD
    assert participant.user.check_password(_SUPPLIED_PASSWORD)
    assert denied.status_code == 403


def test_reset_api_requires_authentication_and_csrf(ctf_event, organizer_credential):
    participant = _create_participant(ctf_event, organizer_credential)
    reset_url = reverse("v1:ctf:api_participant_password_reset", kwargs={"participant_id": participant.id})
    anonymous = Client().post(
        reset_url,
        data=json.dumps({"kind": "generated"}),
        content_type="application/json",
    )
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(ctf_event.created_by)
    missing_csrf = csrf_client.post(
        reset_url,
        data=json.dumps({"kind": "generated"}),
        content_type="application/json",
    )

    assert anonymous.status_code in {401, 403}
    assert missing_csrf.status_code == 403
    assert "password" not in anonymous.content.decode().lower()
    assert "password" not in missing_csrf.content.decode().lower()


def test_reset_api_redacts_form_encoded_password_from_exception_reports(
    authenticated_organizer_client,
    ctf_event,
    organizer_credential,
    monkeypatch,
    settings,
):
    participant = _create_participant(ctf_event, organizer_credential)
    captured_requests = []

    def capture_request(*, request, **_kwargs):
        captured_requests.append(request)

    def fail_after_parsing(*_args, **_kwargs):
        raise RuntimeError("injected response-path failure")

    settings.DEBUG = False
    authenticated_organizer_client.raise_request_exception = False
    monkeypatch.setattr("ctf.services.reset_participant_password", fail_after_parsing)
    got_request_exception.connect(capture_request, weak=False)
    try:
        response = authenticated_organizer_client.post(
            reverse("v1:ctf:api_participant_password_reset", kwargs={"participant_id": participant.id}),
            data=urlencode({"kind": "set", "password": _SUPPLIED_PASSWORD}),
            content_type="application/x-www-form-urlencoded",
        )
    finally:
        got_request_exception.disconnect(capture_request)

    assert response.status_code == 500
    assert captured_requests
    failed_request = captured_requests[-1]
    assert failed_request.POST["password"] == _SUPPLIED_PASSWORD
    assert failed_request.sensitive_post_parameters == ("password",)
    filtered_post = SafeExceptionReporterFilter().get_post_parameters(failed_request)
    assert filtered_post["password"] != _SUPPLIED_PASSWORD
    assert _SUPPLIED_PASSWORD not in repr(filtered_post)


def test_reset_api_rate_limit_fails_without_mutating_password(
    authenticated_organizer_client,
    ctf_event,
    organizer_credential,
    monkeypatch,
):
    participant = _create_participant(ctf_event, organizer_credential)
    participant.user.set_password(_SUPPLIED_PASSWORD)
    participant.user.save(update_fields=["password"])
    monkeypatch.setattr("ctf.views._access._check_credential_delivery_rate_limit", lambda _actor_id: False)

    response = authenticated_organizer_client.post(
        reverse("v1:ctf:api_participant_password_reset", kwargs={"participant_id": participant.id}),
        data=json.dumps({"kind": "generated"}),
        content_type="application/json",
    )

    participant.user.refresh_from_db()
    assert response.status_code == 429
    assert participant.user.check_password(_SUPPLIED_PASSWORD)


def test_legacy_resend_no_longer_mutates_or_emails_password(
    authenticated_organizer_client,
    ctf_event,
    organizer_credential,
    monkeypatch,
):
    participant = _create_participant(ctf_event, organizer_credential)
    participant.user.set_password(_SUPPLIED_PASSWORD)
    participant.user.save(update_fields=["password"])
    sent: list[dict[str, str]] = []
    monkeypatch.setattr("ctf.services.notification._send_email", lambda **kwargs: sent.append(kwargs))

    response = authenticated_organizer_client.post(
        reverse("v1:ctf:api_participant_resend_invite", kwargs={"participant_id": participant.id})
    )

    participant.user.refresh_from_db()
    assert response.status_code == 410
    assert participant.user.check_password(_SUPPLIED_PASSWORD)
    assert all(_SUPPLIED_PASSWORD not in message.get("text_content", "") for message in sent)
    assert all(_SUPPLIED_PASSWORD not in message.get("html_content", "") for message in sent)
