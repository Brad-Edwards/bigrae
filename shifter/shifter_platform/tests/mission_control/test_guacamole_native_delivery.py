"""Native Guacamole proof and exact handoff fences at the owning boundary."""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_direct_guacamole_launch_requires_native_proof(native_guacamole):
    from mission_control.guacamole_bootstrap import BootstrapFailure
    from mission_control.guacamole_session import launch_guacamole_session
    from mission_control.models import GuacamoleBootstrapRequest

    fixture = native_guacamole
    before = GuacamoleBootstrapRequest.objects.count()
    with pytest.raises(BootstrapFailure) as error:
        launch_guacamole_session(
            user=fixture.user,
            protocol="ngfw_ssh",
            target_id=str(fixture.runtime.test_app_uuid),
            guacamole_client=fixture.gateway,
        )
    assert error.value.status_code == 403
    assert GuacamoleBootstrapRequest.objects.count() == before


def test_direct_guacamole_launch_rejects_revoked_native_proof(native_guacamole):
    from management.models import PrincipalSession
    from mission_control.guacamole_bootstrap import BootstrapFailure
    from mission_control.guacamole_session import launch_guacamole_session
    from mission_control.models import GuacamoleBootstrapRequest

    fixture = native_guacamole
    before = GuacamoleBootstrapRequest.objects.count()
    PrincipalSession.objects.filter(credential_uuid=fixture.credential.credential_uuid).delete()
    with pytest.raises(BootstrapFailure) as error:
        launch_guacamole_session(
            user=fixture.credential,
            protocol="ngfw_ssh",
            target_id=str(fixture.runtime.test_app_uuid),
            guacamole_client=fixture.gateway,
        )
    assert error.value.status_code == 403
    assert GuacamoleBootstrapRequest.objects.count() == before


def test_proof_revoked_after_handoff_admission_does_not_release_url(native_guacamole, monkeypatch):
    from management.models import PrincipalSession
    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest

    fixture = native_guacamole
    lock = GuacamoleBootstrapRequest.objects.select_for_update

    def revoke_before_lock(*args, **kwargs):
        PrincipalSession.objects.filter(credential_uuid=fixture.credential.credential_uuid).delete()
        return lock(*args, **kwargs)

    monkeypatch.setattr(GuacamoleBootstrapRequest.objects, "select_for_update", revoke_before_lock)
    assert consume_ready_url(request_id=fixture.row.pk, credential=fixture.credential) is None
    fixture.row.refresh_from_db()
    assert fixture.row.delivered_at is None
    assert fixture.row.result_url


@pytest.mark.parametrize("field", ["target_id", "authority_owner_uuid", "credential_uuid"])
def test_durable_handoff_authority_cannot_be_rewritten(native_guacamole, field):
    from django.db import IntegrityError, transaction

    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest

    fixture = native_guacamole
    value = "different-device" if field == "target_id" else uuid4()
    with pytest.raises(IntegrityError), transaction.atomic():
        GuacamoleBootstrapRequest.objects.filter(pk=fixture.row.pk).update(**{field: value})
    assert consume_ready_url(request_id=fixture.row.pk, credential=fixture.credential)


def test_native_handoff_cannot_use_legacy_user_id(native_guacamole):
    from mission_control.guacamole_bootstrap import consume_ready_url

    fixture = native_guacamole
    assert consume_ready_url(request_id=fixture.row.pk, user_id=fixture.user.pk) is None
    assert consume_ready_url(request_id=fixture.row.pk, credential=fixture.credential)
    assert consume_ready_url(request_id=fixture.row.pk, credential=fixture.credential) is None


def test_legacy_authority_free_bootstrap_cannot_deliver(native_guacamole):
    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest

    fixture = native_guacamole
    row = GuacamoleBootstrapRequest.objects.create(
        user_id=fixture.user.pk,
        protocol="ngfw_ssh",
        target_id=str(fixture.runtime.test_app_uuid),
        status="succeeded",
        result_url="https://example.test/#/client/synthetic?token=SYNTHETIC_LEGACY_TOKEN",
        expires_at=fixture.row.expires_at,
    )
    assert consume_ready_url(request_id=row.pk, user_id=fixture.user.pk) is None
    assert consume_ready_url(request_id=row.pk, credential=fixture.credential) is None
    row.refresh_from_db()
    assert row.delivered_at is None


@pytest.mark.parametrize("change", ["native_status", "native_spec", "cms_deleted"])
def test_changed_source_after_handoff_decision_does_not_release_url(native_guacamole, monkeypatch, change):
    from django.db.models import QuerySet
    from django.utils import timezone

    from cms.models import App
    from engine.models import Instance
    from mission_control.guacamole_bootstrap import consume_ready_url

    fixture = native_guacamole
    lock = QuerySet.select_for_update

    def change_before_source_lock(query, *args, **kwargs):
        if query.model is App:
            if change == "native_status":
                Instance.objects.filter(pk=fixture.runtime.pk).update(status="paused")
            elif change == "native_spec":
                Instance.objects.filter(pk=fixture.runtime.pk).update(spec={"changed_generation": True})
            else:
                App.objects.filter(pk=fixture.runtime.test_app_uuid).update(deleted_at=timezone.now())
        return lock(query, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", change_before_source_lock)
    assert consume_ready_url(request_id=fixture.row.pk, credential=fixture.credential) is None
    fixture.row.refresh_from_db()
    assert fixture.row.delivered_at is None
    assert fixture.row.result_url


def test_handoff_policy_checks_finish_before_source_locks(native_guacamole, monkeypatch):
    from types import SimpleNamespace

    from django.db import transaction

    from mission_control.guacamole_bootstrap import consume_ready_url
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    calls = []

    def check(request):
        assert not transaction.get_connection().in_atomic_block
        calls.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    fixture = native_guacamole
    assert consume_ready_url(request_id=fixture.row.pk, credential=fixture.credential)
    assert calls
