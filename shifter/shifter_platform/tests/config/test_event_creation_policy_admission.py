"""Mounted organizer grants distinguish unavailable evaluation from explicit denial."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from shared.authorization import ACTION_CATALOG, AuthorizationProviderError
from tests.workspaces.test_account_policy_admin import Provider, _actor
from workspaces.models import AuthorizationOperation, Organization
from workspaces.services import create_account

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize("failure", ["outage", "deny"])
def test_event_creation_grant_retry_preserves_unavailability_and_denial(monkeypatch, settings, kind, failure):
    actor = _actor(kind, actions=frozenset(item.code for item in ACTION_CATALOG))
    subject = get_user_model().objects.create_user(username="event-creation-subject")
    account = create_account(kind="team", name="Synthetic event scope")
    organization = Organization.objects.get(account=account.id)

    class RecoveringProvider(Provider):
        admission = failure

        def batch_check(self, requests):
            if self.admission == "outage":
                raise AuthorizationProviderError("Synthetic evaluator failure")
            if self.admission == "deny":
                from shared.authorization import AuthorizationDecision, DecisionKind

                return tuple(AuthorizationDecision(DecisionKind.DENIED, "policy_denied") for _ in requests)
            return super().batch_check(requests)

    provider = RecoveringProvider()
    settings.OPENFGA_MODEL_ID = provider.model_id
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
    client = APIClient()
    user = get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid) if kind == "human" else None
    client.force_authenticate(user=user, token=actor)
    url = f"/api/v1/administer/principals/{subject.identity_principal.uuid}/event-creation-grants/"
    command = {
        "target_type": "organization",
        "target_uuid": str(organization.uuid),
        "idempotency_key": "same-grant-command",
    }
    response = client.post(url, command, format="json")
    if failure == "outage":
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "administration_unavailable"
        assert not AuthorizationOperation.objects.exists()
    else:
        assert response.status_code == 202
        assert AuthorizationOperation.objects.get().state == "denied"
    assert provider.changes == []
    provider.admission = "allowed"
    response = client.post(url, command, format="json")
    assert response.status_code == 202
    assert AuthorizationOperation.objects.get().state == ("confirmed" if failure == "outage" else "denied")
    assert len(provider.changes) == (1 if failure == "outage" else 0)
