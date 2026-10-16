"""Gateway identity cannot substitute for recorded application authority."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def gateway_claims(settings, monkeypatch):
    settings.GUACAMOLE_CONTROL_AUDIENCE = "https://portal.example.test/guacamole-control"
    settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_EMAIL = "gateway@synthetic.iam.gserviceaccount.com"
    settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_ID = "123456789012345678901"
    claims = {
        "email": settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_EMAIL,
        "sub": settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_ID,
        "email_verified": True,
    }

    def verify(token, request, audience=None):
        assert audience == settings.GUACAMOLE_CONTROL_AUDIENCE
        if token != "synthetic-signed-assertion":
            raise ValueError("Signature invalid")
        return dict(claims)

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", verify)
    return claims


def post(body, assertion="synthetic-signed-assertion"):
    client = APIClient()
    if assertion is not None:
        client.credentials(HTTP_AUTHORIZATION="Bearer " + assertion)
    return client.post(reverse("v1:mission_control:guacamole-control-authority"), body, format="json")


@pytest.mark.parametrize(
    "claim",
    [{"sub": "100000000000000000001"}, {"email": "other@synthetic.iam.gserviceaccount.com"}, {"email_verified": False}],
)
def test_other_gateway_identity_is_denied(gateway_claims, claim):
    gateway_claims.update(claim)
    assert post({"bootstrap": "00000000-0000-0000-0000-000000000001"}).status_code == 401


@pytest.mark.parametrize("assertion", [None, "forged"])
def test_missing_or_forged_workload_assertion_is_denied(gateway_claims, assertion):
    assert post({}, assertion).status_code == 401


def test_unknown_bootstrap_returns_only_closed_denial(gateway_claims):
    response = post({"bootstrap": "00000000-0000-0000-0000-000000000001"})
    assert response.status_code == 200
    assert response.json() == {"allowed": False}


@pytest.mark.parametrize(
    "body",
    [{}, [], {"bootstrap": "invalid"}, {"bootstrap": "00000000-0000-0000-0000-000000000001", "principal": "override"}],
)
def test_control_payload_cannot_select_another_proof(gateway_claims, body):
    assert post(body).status_code == 400
