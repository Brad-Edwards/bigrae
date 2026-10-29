"""Mounted Mission Control resource adapters preserve native proof and custody."""

import pytest
from rest_framework.test import APIClient

from tests.cms._device_fixtures import _ngfw_catalog as _ngfw_catalog
from tests.cms.services.test_principal_content import content_actor as content_actor
from tests.cms.services.test_principal_devices import device_actor as device_actor
from tests.cms.services.test_principal_devices import scoped_devices as scoped_devices
from tests.cms.services.test_principal_ingestion import content_administrator as content_administrator
from tests.cms.services.test_principal_uploads import storage_client as storage_client

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["human", "service"])
def test_native_credential_creation_uses_explicit_customer_scope(
    content_actor,
    kind,
    live_session_credential,
):
    from cms.models import Credential
    from management.models import Principal, PrincipalSession, ServiceCredentialAdmission
    from workspaces.models import Account

    actor = content_actor.credential
    if kind == "human":
        owner = Account.objects.get(uuid=content_actor.scope.account_uuid).individual_principal_uuid
        user = Principal.objects.get(uuid=owner).user
        assert not user.is_staff and not user.is_superuser
        actor = live_session_credential(user)
    client = APIClient()
    client.force_authenticate(user=None, token=actor)
    body = {
        "credential_type": "deployment_profile",
        "name": "Native resource credential",
        "authcode": "SYNTHETIC_SECRET_MARKER",
        "account_uuid": str(content_actor.scope.account_uuid),
    }
    response = client.post("/api/v1/mission-control/credentials/", body, format="json")
    assert response.status_code == 201, response.data
    row = Credential.objects.get(pk=response.data["id"])
    assert row.owner_principal_uuid == actor.principal.uuid
    assert row.account_uuid == content_actor.scope.account_uuid
    assert "SYNTHETIC_SECRET_MARKER" not in str(response.data)
    body["account_uuid"] = str(content_actor.sibling.account_uuid)
    assert client.post("/api/v1/mission-control/credentials/", body, format="json").status_code in {400, 403}
    assert Credential.objects.filter(name="Native resource credential").count() == 1
    if kind == "human":
        PrincipalSession.objects.filter(credential_uuid=actor.credential_uuid).delete()
    else:
        ServiceCredentialAdmission.objects.filter(uuid=actor.credential_uuid).update(is_active=False)
    assert client.post(f"/api/v1/mission-control/credentials/{row.pk}/delete/", {}, format="json").status_code == 403
    row.refresh_from_db()
    assert not row.is_deleted


def test_service_device_list_uses_live_scope_policy_instead_of_contact_user(device_actor, scoped_devices):
    client = APIClient()
    client.force_authenticate(user=None, token=device_actor.credential)
    response = client.get("/api/v1/mission-control/ngfw/list/", {"account_uuid": str(device_actor.scope.account_uuid)})
    assert response.status_code == 200, response.data
    assert [row["id"] for row in response.data["ngfws"]] == [str(scoped_devices[0].pk)]
    assert "PRIVATE_DEVICE_MARKER" not in str(response.data)


def test_service_upload_reserves_native_owner_and_scope_without_a_browser_session(
    content_administrator,
    storage_client,
):
    from cms.models import AgentUpload

    actor = content_administrator.credential
    client = APIClient()
    client.force_authenticate(user=None, token=actor)
    response = client.post(
        "/api/v1/mission-control/upload/initiate/",
        {
            "name": "Synthetic native installer",
            "filename": "synthetic.msi",
            "file_size": 1000,
            "account_uuid": str(content_administrator.scope.account_uuid),
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    row = AgentUpload.objects.get()
    assert row.initiating_principal_uuid == actor.principal.uuid
    assert row.authority_account_uuid == content_administrator.scope.account_uuid
    token = response.data["upload_token"]
    assert (
        client.post("/api/v1/mission-control/upload/cancel/", {"upload_token": token}, format="json").status_code == 200
    )
