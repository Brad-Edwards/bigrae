"""Mounted range model-source routes preserve bounded denial and outage responses."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["principal_range"]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("operation", ["get", "put", "revoke"])
@pytest.mark.parametrize("failure", ["denied", "outage"])
def test_model_source_routes_translate_real_range_authority_errors(principal_range, monkeypatch, operation, failure):
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential_reference
    from shared.api_tokens.scopes import MODEL_ACCESS_RANGE_WRITE
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    case = principal_range
    admission = ServiceCredentialAdmission.objects.get(uuid=case.credential.credential_uuid)
    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=admission.audience,
        scopes=[*admission.scopes, MODEL_ACCESS_RANGE_WRITE],
    )
    credential = resolve_service_credential_reference(admission.uuid, audience=admission.audience)
    calls = []

    def provider():
        calls.append(True)
        if failure == "outage":
            raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_ERROR")
        return SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied"))

    monkeypatch.setattr(port, "_provider_factory", provider)
    client = APIClient()
    client.force_authenticate(user=None, token=credential)
    path = f"/api/v1/cms/ranges/{case.request.request_id}/model-access/"
    if operation == "get":
        response = client.get(path)
    elif operation == "put":
        response = client.put(path, {"expected_revision": 0, "selection": {"aliases": []}}, format="json")
    else:
        response = client.post(path + "revoke/")
    assert calls
    assert response.status_code == (503 if failure == "outage" else 403), response.data
    assert response.data["error"]["code"] == "range_model_sources_unavailable"
    assert "SYNTHETIC_PRIVATE_PROVIDER_ERROR" not in str(response.data)
    case.instance.refresh_from_db()
    assert case.instance.model_source_policy_revision == 0
