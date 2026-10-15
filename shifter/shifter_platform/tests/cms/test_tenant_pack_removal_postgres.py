"""PostgreSQL lock semantics of the pack admission fence (ADR-034-R13, #2520)."""

import threading
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.db import connection, transaction

from cms.models import RaesPackageSource, TenantPackArchive
from cms.services import PackRemovalConflict, fence_pack_reference, request_tenant_pack_removal
from tests.cms.test_tenant_pack_upload import PackStorage
from workspaces.models import Organization, OrganizationMembership

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def tenant_pack(settings, monkeypatch):
    settings.RAES_PACKAGE_BUCKET = "test-private-pack-storage"
    storage = PackStorage()
    monkeypatch.setattr("shared.cloud.get_object_storage", lambda: storage)
    actor = User.objects.create_user(username="fence-admin")
    organization = Organization.objects.create(name="Fence tenant")
    OrganizationMembership.objects.create(user=actor, organization=organization, role="admin")
    ref = f"tenant-packs/{organization.uuid}/{uuid4()}.tar.gz"
    storage.objects[(settings.RAES_PACKAGE_BUCKET, ref)] = b"archive"
    source = RaesPackageSource.objects.create(
        scenario_id="pack-" + uuid4().hex,
        organization_uuid=organization.uuid,
        package_name="fenced",
        source_kind="object",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref=ref,
        package_version="1",
        package_digest="sha256:" + "a" * 64,
        registered_by=actor,
    )
    TenantPackArchive.objects.create(
        organization_uuid=organization.uuid,
        scenario_id=source.scenario_id,
        package_ref=ref,
        bucket=settings.RAES_PACKAGE_BUCKET,
        object_key=ref,
    )
    return actor, organization, source, storage


def test_removal_never_waits_behind_a_reference_writer_and_then_wins(tenant_pack):
    actor, organization, source, storage = tenant_pack
    fenced, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def writer():
        try:
            with transaction.atomic():
                fence_pack_reference(source.scenario_id, expected_digest=source.package_digest)
                fenced.set()
                release.wait(timeout=30)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=writer)
    thread.start()
    assert fenced.wait(timeout=30)
    try:
        with pytest.raises(PackRemovalConflict) as busy:
            request_tenant_pack_removal(actor, organization.uuid, source.scenario_id, source.package_digest)
        assert busy.value.code == "pack_busy"
        assert RaesPackageSource.objects.filter(pk=source.pk).exists()
    finally:
        release.set()
        thread.join(timeout=30)
    assert not failures

    result = request_tenant_pack_removal(actor, organization.uuid, source.scenario_id, source.package_digest)
    assert result.state == "completed"
    assert not storage.objects

    # Once the removal committed, a reference writer is refused instead of admitted.
    from cms.exceptions import CMSError

    with transaction.atomic(), pytest.raises(CMSError):
        fence_pack_reference(source.scenario_id, required=False)
