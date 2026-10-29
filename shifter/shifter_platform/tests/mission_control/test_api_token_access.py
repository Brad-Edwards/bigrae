"""End-to-end API-token tests for the Mission Control DRF API (PLAT-106)."""

from __future__ import annotations

from uuid import UUID

import pytest
from rest_framework.test import APIClient

from shared.api_tokens import scopes
from shared.api_tokens.models import ApiToken
from tests._mission_control_fixtures import native_scopes
from tests.cms.services.test_principal_uploads import storage_client as storage_client

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("personal_token_use_grant")]

RANGE_URL = "/api/v1/mission-control/range/"
UPLOAD_INITIATE_URL = "/api/v1/mission-control/upload/initiate/"
UPLOAD_CANCEL_URL = "/api/v1/mission-control/upload/cancel/"
NGFW_LIST_URL = "/api/v1/mission-control/ngfw/list/"
GUACAMOLE_RDP_URL = "/api/v1/mission-control/guacamole/rdp-url/"


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="mc-token@example.com", email="mc-token@example.com")


@pytest.fixture
def client():
    return APIClient()


def _bearer(client: APIClient, raw: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def _token(user, *granted_scopes: str) -> str:
    from tests._mission_control_fixtures import individual_account

    individual_account(user)
    _, raw = ApiToken.create_token(name="mission-control", created_by=user, scopes=native_scopes(list(granted_scopes)))
    return raw


class TestRangeTokenAccess:
    def test_range_read_token_can_read_current_range(self, client, user):
        raw = _token(user, scopes.MISSION_CONTROL_RANGE_READ)

        response = _bearer(client, raw).get(RANGE_URL)

        assert response.status_code == 200
        assert response.json() == {
            "has_range": False,
            "range": None,
            "connection_urls": [],
            "raes_projection": None,
            "raes_participant_runtime": None,
            "lifecycle": None,
            "vpn_profile_available": False,
        }

    def test_token_without_range_read_scope_is_forbidden(self, client, user):
        raw = _token(user, scopes.MISSION_CONTROL_UPLOAD_WRITE)

        response = _bearer(client, raw).get(RANGE_URL)

        assert response.status_code == 403


class TestUploadTokenAccess:
    def test_upload_requires_upload_write_scope(self, client, user):
        raw = _token(user, scopes.MISSION_CONTROL_RANGE_WRITE)

        response = _bearer(client, raw).post(
            UPLOAD_INITIATE_URL,
            {"name": "Agent", "filename": "agent.msi", "file_size": 10},
            format="json",
        )

        assert response.status_code == 403

    def test_upload_write_token_reaches_upload_validation(self, client, user):
        raw = _token(user, scopes.MISSION_CONTROL_UPLOAD_WRITE)

        response = _bearer(client, raw).post(
            UPLOAD_INITIATE_URL,
            {"name": "", "filename": "agent.msi", "file_size": 10},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid"

    def test_upload_write_token_can_cancel_without_csrf(self, client, storage_client, user):
        raw = _token(user, scopes.MISSION_CONTROL_UPLOAD_WRITE)
        _bearer(client, raw)
        initiated = client.post(
            UPLOAD_INITIATE_URL,
            {"name": "Synthetic installer", "filename": "agent.msi", "file_size": 100},
            format="json",
        )
        assert initiated.status_code == 200, initiated.data
        response = client.post(UPLOAD_CANCEL_URL, {"upload_token": initiated.data["upload_token"]}, format="json")

        assert response.status_code == 200
        assert response.json() == {"success": True}


class TestSubsurfaceTokenAccess:
    def test_ngfw_read_scope_can_list_ngfws(self, client, user):
        raw = _token(user, scopes.MISSION_CONTROL_NGFW_READ)

        response = _bearer(client, raw).get(NGFW_LIST_URL)

        assert response.status_code == 200
        assert response.json() == {"ngfws": []}

    def test_ngfw_list_rejects_range_read_scope(self, client, user):
        raw = _token(user, scopes.MISSION_CONTROL_RANGE_READ)

        response = _bearer(client, raw).get(NGFW_LIST_URL)

        assert response.status_code == 403

    @pytest.mark.postgres
    @pytest.mark.django_db(transaction=True)
    def test_guacamole_bootstrap_response_uses_canonical_urls(
        self, client, user, settings, range_rdp_instance, secrets_boundary, guac_exchange
    ):
        settings.GUACAMOLE_JSON_AUTH_SECRET = "0123456789abcdef0123456789abcdef"  # nosec B105
        settings.GUACAMOLE_BOOTSTRAP_INLINE = True
        _, member = range_rdp_instance(user)
        raw = _token(user, scopes.MISSION_CONTROL_GUACAMOLE_READ)
        with secrets_boundary(), guac_exchange():
            response = _bearer(client, raw).post(GUACAMOLE_RDP_URL, {"instance_uuid": member["uuid"]}, format="json")
        assert response.status_code == 202
        request_id = UUID(response.json()["request_id"])
        assert response["Location"] == f"/api/v1/mission-control/guacamole/bootstrap/{request_id}/"
        assert response.json()["status_url"] == f"/api/v1/mission-control/guacamole/bootstrap/{request_id}/"
        assert response.json()["url"] == f"/api/v1/mission-control/guacamole/bootstrap/{request_id}/open/"
