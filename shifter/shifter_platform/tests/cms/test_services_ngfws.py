"""Native device service contracts replace retired User-owned helper branches."""

import pytest

from cms.exceptions import CMSError
from shared.schemas.app import NGFWOneTimeRegistration
from tests.cms.services.test_principal_devices import (
    _ngfw_catalog,
    content_actor,
    device_administrator,
    provisioned_device,
)

__all__ = ["_ngfw_catalog", "content_actor", "device_administrator", "provisioned_device"]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["human", "service"])
def test_native_device_create_read_and_destroy_preserve_the_exact_credential(
    device_administrator, live_session_credential, django_user_model, kind
):
    from cms.models import App
    from cms.services import create_ngfw, destroy_ngfw, get_ngfw, list_ngfws
    from engine.models import ProvisionerLaunchIntent

    actor = device_administrator
    credential = actor.credential
    if kind == "human":
        credential = live_session_credential(django_user_model.objects.get(username="content-owner-0"))
    created = create_ngfw(
        credential,
        "Native device",
        actor.profile.pk,
        "otp",
        scope=actor.scope,
        otp=NGFWOneTimeRegistration("synthetic-otp", "synthetic-folder"),
    )
    assert get_ngfw(credential, created.app_id).name == "Native device"
    assert [row.app_id for row in list_ngfws(credential, scope=actor.scope)] == [created.app_id]
    app = App.objects.select_related("instance").get(pk=created.app_id)
    assert app.instance.request.owner_principal_uuid == credential.principal.uuid
    assert ProvisionerLaunchIntent.objects.get().credential_uuid == credential.credential_uuid
    with pytest.raises(CMSError):
        destroy_ngfw(credential, created.app_id, "Another name")
    assert ProvisionerLaunchIntent.objects.count() == 1


@pytest.mark.parametrize("invalid", ["name", "method", "profile", "pin", "otp"])
def test_native_device_rejects_invalid_inputs_before_any_request_or_launch(device_administrator, invalid):
    from cms.models import App, Request
    from cms.services import create_ngfw
    from engine.models import ProvisionerLaunchIntent

    actor = device_administrator
    values = {
        "name": "Native device",
        "deployment_profile_id": actor.profile.pk,
        "registration_method": "otp",
        "scope": actor.scope,
        "otp": NGFWOneTimeRegistration("synthetic-otp", "synthetic-folder"),
    }
    if invalid == "name":
        values["name"] = " "
    elif invalid == "method":
        values["registration_method"] = "unsupported"
    elif invalid == "profile":
        values["deployment_profile_id"] = 999999
    elif invalid == "pin":
        values["registration_method"] = "pin"
    else:
        values["otp"] = None
    with pytest.raises(CMSError):
        create_ngfw(actor.credential, **values)
    assert not App.objects.exists() and not Request.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


def test_native_device_confirmed_destroy_is_durable_and_cross_scope_is_hidden(provisioned_device):
    from cms.services import destroy_ngfw, get_ngfw, list_ngfws
    from engine.models import ProvisionerLaunchIntent

    actor = provisioned_device
    with pytest.raises(CMSError):
        list_ngfws(actor.credential, scope=actor.sibling)
    result = destroy_ngfw(actor.credential, actor.app.pk, actor.app.name)
    assert result.is_deleted
    assert (
        ProvisionerLaunchIntent.objects.get(payload__operation="deprovision").credential_uuid
        == actor.credential.credential_uuid
    )
    with pytest.raises(CMSError):
        get_ngfw(actor.credential, actor.app.pk)
