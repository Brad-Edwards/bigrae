"""Real service bearer HTTP admission cannot enter interactive early release."""

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from ctf.exceptions import CTFCommunicationError
from ctf.models import CTFScheduledTask, RecipientSnapshot
from ctf.services.communication import AdmissionActor, release_due_declaration, request_early_release
from tests.ctf.test_communication_scheduling import _scheduled

pytestmark = pytest.mark.django_db


@pytest.fixture
def native_service_http(ctf_event, settings, monkeypatch):
    from config import service_identity
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, resolve_service_credential
    from shared.authorization import (
        AdministrativeRoleChange,
        PolicyEffect,
        RelationshipSubject,
        TargetRef,
        configured_authorization_provider,
    )

    principal = create_service_principal("Synthetic communication worker")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid),
        issuer="https://accounts.google.com",
        subject="123456789012345678901",
    )
    settings.CLOUD_PROVIDER = "gcp"
    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["ctf:event:write", "ctf:communication:write", "ctf:communication:read"],
    )
    credential = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)
    configured_authorization_provider().write_relationships(
        AdministrativeRoleChange(
            RelationshipSubject("principal", principal.uuid),
            "event_administrator",
            TargetRef("event", ctf_event.pk),
            PolicyEffect.GRANT,
        )
    )
    claims = {
        "iss": binding.issuer,
        "sub": binding.subject,
        "aud": admission.audience,
        "email": "communication@synthetic-project.iam.gserviceaccount.com",
        "email_verified": True,
    }
    monkeypatch.setattr(service_identity.id_token, "verify_oauth2_token", lambda *args, **kwargs: claims)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer synthetic.service.proof")
    return client, credential


def test_service_bearer_cannot_run_scheduled_communication_early(organizer_user, ctf_event, native_service_http):
    client, _credential = native_service_http
    _, intent, _ = _scheduled(organizer_user, ctf_event, due_at=timezone.now() + timezone.timedelta(hours=1))
    task = CTFScheduledTask.objects.get(metadata__intent_id=str(intent.pk))
    response = client.post(f"/api/v1/ctf/events/{ctf_event.pk}/tasks/{task.pk}/run/", {}, format="json")
    assert response.status_code == 404, response.content
    assert response.json()["error"]["code"] == "not_found"
    intent.refresh_from_db()
    task.refresh_from_db()
    assert intent.status == "scheduled" and task.status == "pending"
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()


@pytest.mark.parametrize("entrypoint", ["request", "due_command"])
def test_direct_service_early_release_is_denied(organizer_user, ctf_event, native_service_http, entrypoint):
    _client, credential = native_service_http
    _, intent, _ = _scheduled(organizer_user, ctf_event, due_at=timezone.now() + timezone.timedelta(hours=1))
    actor = AdmissionActor(credential=credential, allow_early_release=True)
    with pytest.raises(CTFCommunicationError) as error:
        if entrypoint == "request":
            request_early_release(intent, actor=actor)
        else:
            release_due_declaration(intent, actor=actor, allow_early=True)
    assert error.value.code == "CTF_COMMUNICATION_EARLY_RELEASE_DENIED"
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()


def test_service_keeps_normal_due_time_release(organizer_user, ctf_event, native_service_http):
    _client, credential = native_service_http
    _, intent, _ = _scheduled(organizer_user, ctf_event, due_at=timezone.now() - timezone.timedelta(minutes=1))
    assert release_due_declaration(intent, actor=AdmissionActor(credential=credential)) == "released"
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1
