"""Mounted preparation routes carry real credentials through native policy."""

import pytest
from rest_framework.test import APIClient

from engine.models import PreparationAdapter
from tests.engine.services.test_preparation_adapters import grant as grant
from tests.shared.raes.test_preparation_contract import manifest_payload

pytestmark = pytest.mark.django_db
URL = "/api/v1/cms/preparation-adapters/"


@pytest.mark.parametrize("kind", ["human", "service"])
def test_policy_authorizes_adapter_install_without_django_role_flags(
    kind,
    grant,
    django_user_model,
    live_session_credential,
    allow_all_policy,
    settings,
):
    from management.services import create_service_principal
    from shared.authorization import CredentialCeiling
    from shared.credentials import CredentialContext
    from tests._credential_helpers import persisted_credential

    if kind == "human":
        user = django_user_model.objects.create_user(username="native-preparation-admin")
        credential = live_session_credential(user)
        assert not user.is_staff and not user.is_superuser
    else:
        from uuid import uuid4

        credential = persisted_credential(
            CredentialContext(
                create_service_principal("Native preparation administrator"),
                "service",
                uuid4(),
                CredentialCeiling(frozenset({"installation.manage_adapters"})),
                frozenset({"cms:preparation-adapters:write", "cms:preparation-adapters:read"}),
            )
        )
        user = None
    client = APIClient()
    client.force_authenticate(user=user, token=credential)
    response = client.post(URL, {"grant_id": str(grant.pk), "manifest": manifest_payload()}, format="json")
    assert response.status_code == 201, response.data
    assert PreparationAdapter.objects.get().installed_by_principal_uuid == credential.principal.uuid
    assert client.get(URL).status_code == 200


def test_staff_flags_do_not_override_explicit_policy_denial(
    grant, django_user_model, live_session_credential, monkeypatch
):
    from types import SimpleNamespace

    from shared.authorization import AuthorizationDecision, DecisionKind, port

    user = django_user_model.objects.create_user(username="legacy-preparation-admin", is_staff=True, is_superuser=True)
    credential = live_session_credential(user)
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(
            check=lambda _request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied"),
        ),
    )
    client = APIClient()
    client.force_authenticate(user=user, token=credential)
    assert (
        client.post(URL, {"grant_id": str(grant.pk), "manifest": manifest_payload()}, format="json").status_code == 403
    )
    assert not PreparationAdapter.objects.exists()


def test_adapter_install_rejects_revocation_after_policy(
    grant,
    django_user_model,
    live_session_credential,
    allow_all_policy,
    monkeypatch,
):
    from engine.services import install_preparation_adapter
    from management.models import PrincipalSession
    from shared.exceptions import ValidationError

    actor = live_session_credential(django_user_model.objects.create_user(username="revoked-preparation-admin"))
    original = allow_all_policy.check

    def revoke(request):
        result = original(request)
        PrincipalSession.objects.filter(credential_uuid=actor.credential_uuid).delete()
        return result

    monkeypatch.setattr(allow_all_policy, "check", revoke)
    with pytest.raises(ValidationError):
        install_preparation_adapter(actor, grant.pk, manifest_payload())
    assert not PreparationAdapter.objects.exists()
