"""Unknown upload outcomes retain exact cleanup custody across crashes and late writes."""

import pytest
from django.utils import timezone

from cms.models import RaesPackageSource, TenantPackArchive, TenantPackRemoval
from cms.services import reconcile_tenant_pack_removals
from tests.cms.test_tenant_pack_upload import _archive, _post, tenant, upload

__all__ = ["tenant", "upload"]
pytestmark = pytest.mark.django_db


class LostUploader(BaseException):
    """A process exit bypasses the request's compensating error handler."""


def _make_due(removal):
    TenantPackRemoval.objects.filter(pk=removal.pk).update(
        lease_expires_at=timezone.now(), next_attempt_at=timezone.now()
    )


@pytest.mark.django_db(transaction=True)
def test_crashed_upload_is_known_before_storage_and_cleanup_keeps_unknown_custody(upload, tenant, monkeypatch):
    from cms.services._tenant_pack_upload import upload_tenant_pack

    _client, organization, root, storage = upload
    original = storage.upload_file

    def stored_then_process_lost(*args, **kwargs):
        removal = TenantPackRemoval.objects.get()
        archive = TenantPackArchive.objects.get(removal=removal)
        assert archive.bucket == args[1] and archive.object_key == args[2]
        assert removal.upload_outcome == "unknown"
        original(*args, **kwargs)
        raise LostUploader

    monkeypatch.setattr(storage, "upload_file", stored_then_process_lost)
    with pytest.raises(LostUploader):
        upload_tenant_pack(
            user=tenant[0].credential, organization_uuid=organization.uuid, name=root.name, archive=_archive(root)
        )
    assert not RaesPackageSource.objects.exists()
    removal = TenantPackRemoval.objects.get()
    assert reconcile_tenant_pack_removals() == 0
    assert storage.objects and not storage.deleted
    _make_due(removal)
    assert reconcile_tenant_pack_removals() == 0
    assert not storage.objects
    removal.refresh_from_db()
    assert removal.state == "pending" and removal.last_failure_code == "upload_outcome_unknown"
    assert TenantPackArchive.objects.filter(removal=removal).count() == 1


def test_ambiguous_upload_failure_and_late_server_write_stay_under_exact_cleanup(upload, monkeypatch):
    client, organization, root, storage = upload
    original = storage.upload_file

    def written_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("Synthetic ambiguous acknowledgment")

    monkeypatch.setattr(storage, "upload_file", written_then_timeout)
    assert _post(client, organization, root).status_code == 400
    assert not RaesPackageSource.objects.exists() and not storage.objects
    removal = TenantPackRemoval.objects.get()
    archive = TenantPackArchive.objects.get(removal=removal)
    assert removal.state == "pending" and removal.upload_outcome == "unknown"
    # The provider finishes after cleanup's earlier successful delete/absence.
    storage.objects[(archive.bucket, archive.object_key)] = b"late server write"
    _make_due(removal)
    assert reconcile_tenant_pack_removals() == 0
    assert not storage.objects
    assert TenantPackArchive.objects.filter(pk=archive.pk, removal=removal).exists()
    removal.refresh_from_db()
    assert removal.state == "pending" and removal.upload_outcome == "unknown"


@pytest.mark.parametrize("reap_while_uploading", [False, True])
def test_expired_uploader_cannot_publish_even_after_successful_storage_return(
    upload, monkeypatch, reap_while_uploading
):
    client, organization, root, storage = upload
    original = storage.upload_file

    def delayed_upload(*args, **kwargs):
        removal = TenantPackRemoval.objects.get()
        _make_due(removal)
        if reap_while_uploading:
            assert reconcile_tenant_pack_removals() == 0
            removal.refresh_from_db()
            assert removal.state == "pending" and removal.upload_outcome == "unknown"
            assert TenantPackArchive.objects.filter(removal=removal).exists()
        original(*args, **kwargs)

    monkeypatch.setattr(storage, "upload_file", delayed_upload)
    assert _post(client, organization, root).status_code == 400
    assert not RaesPackageSource.objects.exists()
    assert not storage.objects and not TenantPackArchive.objects.exists()
    removal = TenantPackRemoval.objects.get()
    assert removal.state == "completed" and removal.upload_outcome == "settled"


def test_failed_compensating_delete_remains_durable_until_storage_recovers(upload, monkeypatch):
    client, organization, root, storage = upload
    original_delete = storage.delete_object

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("Synthetic delete outage")

    monkeypatch.setattr(storage, "delete_object", unavailable)
    # A stale revision precondition fails after an acknowledged storage upload.
    assert _post(client, organization, root, expected_digest="sha256:" + "f" * 64).status_code == 400
    assert not RaesPackageSource.objects.exists() and storage.objects
    removal = TenantPackRemoval.objects.get()
    archive = TenantPackArchive.objects.get(removal=removal)
    assert removal.state == "pending" and removal.upload_outcome == "settled"
    monkeypatch.setattr(storage, "delete_object", original_delete)
    _make_due(removal)
    assert reconcile_tenant_pack_removals() == 1
    assert not storage.objects and not TenantPackArchive.objects.filter(pk=archive.pk).exists()
