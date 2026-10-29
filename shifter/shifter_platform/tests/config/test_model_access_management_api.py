"""Retained API authentication, draft and generated contract coverage under ADR-068."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from shared.api_tokens.models import ApiToken
from shared.api_tokens.scopes import CTF_EVENT_READ, MODEL_ACCESS_SHARING_READ

pytestmark = pytest.mark.django_db


@pytest.mark.usefixtures("personal_token_use_grant")
@pytest.mark.parametrize("scope", [CTF_EVENT_READ, MODEL_ACCESS_SHARING_READ])
def test_real_personal_token_cannot_unpark_sharing(scope, django_user_model, settings):
    settings.MODEL_ACCESS_ENABLED = True
    user = django_user_model.objects.create_user(username="parked-personal-token")
    _, raw = ApiToken.create_token(name="parked-sharing", created_by=user, scopes=[scope])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    response = client.post("/api/v1/model-access/bindings/validate/", {"binding": {}, "pool": {}}, format="json")
    assert response.status_code == 503, response.data
    assert response.data["error"]["code"] == "model_access_unavailable"


class TestDraftSealing:
    def test_injects_server_deployment_and_placeholder_publisher(self):
        from config.api_model_access import _binding_and_pool

        deployment_id = uuid.uuid4()
        draft = {
            "binding": {
                "sharing_binding_id": "b1",
                # A client-supplied deployment id must not survive: the server owns it.
                "deployment_id": str(uuid.uuid4()),
                "selector": {"kind": "all_ranges"},
                "membership_mode": "dynamic",
                "profile_id": "coding",
                "sharing_pool_id": "pool-a",
                "facets": ["profile", "spend"],
                "priority": 0,
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_until": "2027-01-01T00:00:00Z",
            },
            "pool": {"sharing_pool_id": "pool-a", "routing_revision": 1, "spend_account_refs": ["acct-1"]},
        }
        binding, pool = _binding_and_pool(draft, deployment_id)
        assert str(binding.deployment_id) == str(deployment_id)
        # The client cannot forge the publisher; publish re-seals it server-side.
        assert binding.authorized_publisher_ref.reference == "draft:unresolved"
        assert pool.sharing_pool_id == "pool-a"


class TestGeneratedContract:
    def test_openapi_publishes_exact_model_access_scopes(self):
        import json
        import pathlib

        spec = json.loads((pathlib.Path(__file__).resolve().parents[2] / "openapi" / "v1.json").read_text())
        expected = {
            "/api/v1/model-access/bindings/validate/": ("post", "model-access:sharing:read"),
            "/api/v1/model-access/bindings/publish/": ("post", "model-access:sharing:write"),
            "/api/v1/model-access/bindings/drain/": ("post", "model-access:sharing:write"),
            "/api/v1/model-access/bindings/policy-preview/": ("post", "model-access:operator:read"),
            "/api/v1/cms/ranges/{request_id}/model-access/revoke/": ("post", "model-access:range:write"),
            "/api/v1/ctf/events/{event_id}/model-access/assessment/": ("get", "model-access:event:read"),
            "/api/v1/ctf/me/model-access/": ("get", "model-access:participant:read"),
        }
        for path, (method, scope) in expected.items():
            operation = spec["paths"][path][method]
            assert operation["x-required-scopes"] == [scope], path
