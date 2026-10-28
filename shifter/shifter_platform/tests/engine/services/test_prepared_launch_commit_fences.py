"""Native queue admission rechecks local proof after every prepared policy decision."""

import pytest

from tests.cms.services.test_principal_content import content_actor
from tests.cms.services.test_principal_devices import _ngfw_catalog, device_administrator, provisioned_device
from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["_ngfw_catalog", "content_actor", "device_administrator", "principal_range", "provisioned_device"]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("operation", ["pause", "resume", "destroy"])
def test_native_range_prepared_lifecycle_cannot_commit_after_local_revocation(principal_range, operation):
    from engine.models import OperationInput, ProvisionerLaunchIntent
    from engine.services import commit_prepared_range_lifecycle, prepare_range_lifecycle
    from management.models import ServiceCredentialAdmission

    actor = principal_range
    if operation == "resume":
        actor.realized.status = "paused"
        actor.realized.save(update_fields=["status"])
    before = actor.realized.status
    prepared = prepare_range_lifecycle(actor.credential, actor.request.request_id, operation)
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    with pytest.raises((ValueError, PermissionError)):
        commit_prepared_range_lifecycle(prepared)
    actor.realized.refresh_from_db()
    assert actor.realized.status == before
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()


def test_native_device_prepared_lifecycle_cannot_commit_after_local_revocation(provisioned_device):
    from engine.models import OperationInput, ProvisionerLaunchIntent
    from engine.services import commit_ngfw_lifecycle, prepare_ngfw_lifecycle
    from management.models import ServiceCredentialAdmission

    actor = provisioned_device
    prepared = prepare_ngfw_lifecycle(actor.native.request.request_id, actor.credential, "deprovision")
    before = ProvisionerLaunchIntent.objects.count(), OperationInput.objects.count()
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    with pytest.raises((ValueError, PermissionError)):
        commit_ngfw_lifecycle(prepared)
    actor.native.refresh_from_db()
    assert actor.native.status == "ready"
    assert (ProvisionerLaunchIntent.objects.count(), OperationInput.objects.count()) == before
