"""Source administration uses session authority and secret-free readback."""

import json

import pytest
from rest_framework.test import APIClient

from shared.api_tokens.models import ApiToken
from shared.api_tokens.scopes import AUTHORIZATION_ACTION_SCOPES
from tests.engine.services.test_model_sources import configuration, model_source_credential, source_tenant

__all__ = ["source_tenant"]

pytestmark = pytest.mark.django_db


def source_client(user):
    _token, raw = ApiToken.create_token(
        name="model source api",
        created_by=user,
        scopes=[AUTHORIZATION_ACTION_SCOPES["organization.manage_model_sources"]],
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def test_source_api_registers_lists_and_conflicts_without_echoing_credentials(source_tenant):
    admin, member, org, _store = source_tenant
    client = source_client(admin)
    url = f"/api/v1/cms/organizations/{org.uuid}/model-sources/"
    payload = {"configuration": configuration(), "credential": {"api_key": "synthetic-secret"}}
    response = client.post(url, payload, format="json")
    assert response.status_code == 201
    assert "synthetic-secret" not in response.content.decode()
    source_id = response.json()["id"]
    assert len(client.get(url).json()["results"]) == 1
    edited = client.put(f"{url}{source_id}/", {"expected_revision": 0, "configuration": configuration()}, format="json")
    assert edited.status_code == 409
    client = source_client(member)
    assert client.post(url, payload, format="json").status_code == 403
    assert client.get(url).status_code == 403
    assert client.get(f"{url}available/").status_code == 403


def test_source_api_rejects_duplicate_and_unknown_members_before_mutation(source_tenant):
    admin, _, org, store = source_tenant
    client = source_client(admin)
    url = f"/api/v1/cms/organizations/{org.uuid}/model-sources/"
    raw = '{"configuration":' + json.dumps(configuration()) + ',"configuration":{}}'
    assert client.post(url, raw, content_type="application/json").status_code == 400
    assert client.post(url, {"configuration": configuration(), "owner_id": 99}, format="json").status_code == 400
    store.create_owned_secret.assert_not_called()


def test_credential_cleanup_is_tenant_admin_only_and_has_no_client_secret_reference(source_tenant):
    from uuid import uuid4

    from engine.services import create_model_source

    admin, member, org, store = source_tenant
    source = create_model_source(
        model_source_credential(admin), org.uuid, configuration(), credential={"api_key": "synthetic-secret"}
    )
    url = f"/api/v1/cms/organizations/{org.uuid}/model-sources/{source.id}/retire-credentials/"
    client = source_client(member)
    assert client.post(url, {}, format="json").status_code == 403
    client = source_client(admin)
    assert client.post(url.replace(str(org.uuid), str(uuid4())), {}, format="json").status_code == 403
    assert client.post(url, {"credential_reference": "foreign-secret"}, format="json").status_code == 400
    result = client.post(url, {}, format="json")
    assert result.status_code == 200 and result.json() == {"retired": 0}
    store.retire_owned_secret.assert_not_called()


def test_usable_source_metadata_contains_no_embedded_user_grants(source_tenant):
    from engine.services import create_model_source

    admin, member, org, _ = source_tenant
    create_model_source(
        model_source_credential(admin),
        org.uuid,
        configuration(),
        credential={"api_key": "synthetic-secret"},
    )
    member.grant_model_sources()
    client = source_client(member)
    response = client.get(f"/api/v1/cms/organizations/{org.uuid}/model-sources/available/")
    assert response.status_code == 200
    configuration_payload = response.json()["results"][0]["configuration"]
    assert "allowed_user_ids" not in configuration_payload
    assert "allow_organization_members" not in configuration_payload
