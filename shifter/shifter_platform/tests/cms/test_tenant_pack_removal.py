"""Tenant pack removal drives the real upload, guard, storage cleanup and API boundaries."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from cms.models import RaesPackageSource, RangeInstance, ScenarioMetadata, TenantPackArchive, TenantPackRemoval
from ctf.enums import EventStatus, ScheduledTaskStatus, ScheduledTaskType
from ctf.models import CTFEvent, CTFScheduledTask
from engine.models import RuntimePluginInstallation, RuntimePluginPackBinding
from engine.services import install_runtime_plugin
from shared.cloud.exceptions import CloudStorageError
from shared.models import AuditLog
from tests.cms.test_runtime_plugins import manifest as manifest
from tests.cms.test_tenant_pack_upload import _post, _post_revision
from tests.cms.test_tenant_pack_upload import tenant as tenant
from tests.cms.test_tenant_pack_upload import upload as upload
from workspaces.models import Organization, OrganizationMembership, Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def installed(upload, make_pack):
    """A tenant pack with two uploaded revisions and an adapter binding."""
    client, organization, root, storage = upload
    first = _post(client, organization, root)
    assert first.status_code == 201, first.data
    second = _post_revision(client, organization, root, make_pack, first.data["package_digest"])
    assert second.status_code == 201, second.data
    assert len(storage.objects) == 2
    return RaesPackageSource.objects.get(scenario_id=first.data["scenario_id"])


@pytest.fixture
def workspace(tenant):
    organization = tenant[1]
    workspace = Workspace.objects.create(organization=organization, name="Pack workspace")
    WorkspaceMembership.objects.create(workspace=workspace, user=tenant[0], role="owner")
    return workspace


def _remove(client, organization, source, **body):
    return client.post(
        f"/api/v1/cms/organizations/{organization.uuid}/packs/{source.scenario_id}/removal/",
        {"pack_digest": source.package_digest, **body},
        format="json",
    )


def _range(source, workspace, status, user_id=1):
    return RangeInstance.all_objects.create(
        scenario_id=source.scenario_id, user_id=user_id, workspace_id=workspace.pk, status=status
    )


def _event(source, owner, workspace, status, name="Pack event"):
    now = timezone.now()
    return CTFEvent.objects.create(
        name=name,
        created_by=owner,
        scope_kind="account",
        account_id=workspace.organization.account_id,
        organization_id=workspace.organization_id,
        workspace_id=workspace.pk,
        status=status,
        event_start=now - timedelta(days=2),
        event_end=now - timedelta(days=1),
        scenario_id=source.scenario_id,
    )


def _bind(source, tenant, manifest):
    actor, organization = tenant
    plugin = install_runtime_plugin(actor.credential, organization.uuid, manifest)
    RuntimePluginInstallation.objects.filter(pk=plugin.id).update(state="ready")
    return RuntimePluginPackBinding.objects.create(
        organization_uuid=organization.uuid,
        pack_id=source.scenario_id,
        pack_digest=source.package_digest,
        installation_id=plugin.id,
        bindings={"targets": {"server": "server"}},
    )


def _storage_inaccessible(*_args, **_kwargs):
    raise AssertionError("history must not read pack archives")


def test_admin_removes_unreferenced_pack_with_all_revisions_and_keeps_history(
    upload, installed, tenant, manifest, workspace, monkeypatch, allow_all_policy, live_session_credential
):
    client, organization, _root, storage = upload
    actor, _ = tenant
    client.force_authenticate(user=actor, token=live_session_credential(actor))
    monkeypatch.setattr("ctf.api.organizer._base._event_action_allowed", lambda *_args: True)
    _bind(installed, tenant, manifest)
    ScenarioMetadata.objects.create(scenario_id=installed.scenario_id, enabled=True, updated_by=actor)
    destroyed = _range(installed, workspace, "destroyed", user_id=actor.id)
    ended = _event(installed, actor, workspace, EventStatus.ENDED.value)

    response = _remove(client, organization, installed)

    assert response.status_code == 200, response.data
    assert response.data["state"] == "completed"
    assert response.data["remaining_archives"] == 0
    assert not RaesPackageSource.objects.exists()
    assert not ScenarioMetadata.objects.exists()
    assert not RuntimePluginPackBinding.objects.exists()
    assert RuntimePluginInstallation.objects.exists()
    assert not storage.objects
    assert len(storage.deleted) == 2
    assert not TenantPackArchive.objects.exists()
    removal = TenantPackRemoval.objects.get()
    assert removal.state == "completed" and removal.package_digest == installed.package_digest
    assert client.get(f"/api/v1/cms/organizations/{organization.uuid}/plugin-packs/").json()["count"] == 0
    audits = AuditLog.objects.filter(entity_ref=installed.scenario_id, action="delete")
    assert audits.count() == 1
    assert audits.get().previous_state["package_digest"] == installed.package_digest
    assert audits.get().actor_id is None
    assert audits.get().actor_principal_uuid == actor.identity_principal.uuid
    assert AuditLog.objects.filter(entity_ref=str(removal.id), new_state__result="removal_completed").exists()

    # History renders from retained scalar identities while storage is inaccessible.
    monkeypatch.setattr(storage, "download_object", _storage_inaccessible)
    assert RangeInstance.all_objects.get(pk=destroyed.pk).scenario_id == destroyed.scenario_id
    event = client.get(f"/api/v1/ctf/events/{ended.pk}/")
    assert event.status_code == 200, event.data
    assert event.json()["scenario_id"] == installed.scenario_id

    # A repeated authorized request returns the same completed removal.
    again = _remove(client, organization, installed)
    assert again.status_code == 200 and again.data["removal_id"] == str(removal.id)


def test_removal_refuses_active_references_without_using_legacy_role_visibility(upload, installed, tenant, workspace):
    client, organization, _root, storage = upload
    actor, _ = tenant
    _range(installed, workspace, "provisioning")
    _event(installed, actor, workspace, EventStatus.DRAFT.value, name="Draft pack event")
    ended = _event(installed, actor, workspace, EventStatus.ENDED.value, name="Ended pack event")
    CTFScheduledTask.objects.create(
        event=ended,
        task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
        scheduled_for=timezone.now(),
        status=ScheduledTaskStatus.PENDING.value,
    )
    foreign = Workspace.objects.create(organization=Organization.objects.create(name="Elsewhere"), name="Hidden")
    _range(installed, foreign, "paused", user_id=2)
    # Organization administration does not grant CTF read access to another organizer's event.
    other_organizer = User.objects.create_user(username="other-organizer")
    _event(installed, other_organizer, workspace, EventStatus.REGISTRATION.value, name="Private organizer event")

    response = _remove(client, organization, installed)

    assert response.status_code == 409, response.data
    error = response.data["error"]
    assert error["code"] == "pack_in_use"
    blockers = error["details"]["blockers"]
    assert error["details"]["blocker_count"] == 5
    assert {row["kind"] for row in blockers} == {"range", "ctf_event", "ctf_scheduled_launch"}
    labels = {row["label"] for row in blockers}
    assert labels == {None}
    assert "Private organizer event" not in labels
    assert "Private organizer event" not in error["message"]
    assert "Draft pack event" not in error["message"]
    assert RaesPackageSource.objects.filter(pk=installed.pk).exists()
    assert len(storage.objects) == 2
    assert not TenantPackRemoval.objects.exists()


def test_operator_warm_pool_bucket_blocks_removal(upload, installed, settings):
    from shared.warm_pool.policy import WarmPoolBucketPolicy, WarmPoolRuntimePolicy

    client, organization, _root, _storage = upload
    settings.WARM_POOL_POLICY = WarmPoolRuntimePolicy(
        enabled=True,
        buckets=(
            WarmPoolBucketPolicy(
                id="pack-bucket",
                backend="gce",
                scenario=installed.scenario_id,
                capacity_partition="default",
                target=1,
                minimum=0,
                maximum=1,
                idle_ttl_seconds=60,
            ),
        ),
    )
    response = _remove(client, organization, installed)
    assert response.status_code == 409
    assert response.data["error"]["details"]["blockers"][0]["kind"] == "warm_pool_bucket"


def test_removal_is_denied_for_foreign_platform_stale_and_malformed_requests(upload, installed, tenant):
    client, organization, _root, storage = upload
    actor, _ = tenant

    stale = _remove(client, organization, installed, pack_digest="sha256:" + "0" * 64)
    assert stale.status_code == 409 and stale.data["error"]["code"] == "pack_changed"
    assert _remove(client, organization, installed, force=True).status_code == 400

    outsider = User.objects.create_user(username="foreign-pack-admin", is_staff=True)
    other = Organization.objects.create(name="Other pack org")
    OrganizationMembership.objects.create(user=outsider, organization=other, role="admin")
    client.force_authenticate(user=outsider)
    assert _remove(client, organization, installed).status_code == 403
    foreign = _remove(client, other, installed)
    assert foreign.status_code == 403 and foreign.data["error"]["code"] == "pack_access_denied"

    platform = RaesPackageSource.objects.create(
        scenario_id="platform-pack",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="platform-pack",
        package_version="1",
        package_digest="sha256:" + "b" * 64,
        registered_by=actor,
    )
    client.force_authenticate(user=actor)
    denied = _remove(client, organization, platform)
    assert denied.status_code == 400 and denied.data["error"]["message"] == "The pack is unavailable"

    assert RaesPackageSource.objects.count() == 2
    assert len(storage.objects) == 2
    assert not TenantPackRemoval.objects.exists()


def test_failed_storage_cleanup_stays_pending_and_the_reconciler_completes_it(upload, installed, monkeypatch):
    from cms.services import reconcile_tenant_pack_removals

    client, organization, _root, storage = upload
    delete = storage.delete_object
    failures = {"left": 1}

    def flaky(bucket, key):
        if failures["left"]:
            failures["left"] -= 1
            raise CloudStorageError("transient")
        delete(bucket, key)

    monkeypatch.setattr(storage, "delete_object", flaky)
    response = _remove(client, organization, installed)

    assert response.status_code == 202, response.data
    assert response.data["state"] == "pending"
    assert response.data["remaining_archives"] == 1
    assert not RaesPackageSource.objects.exists()
    removal = TenantPackRemoval.objects.get()
    assert removal.last_failure_code == "storage_delete_failed" and removal.attempts == 1
    # Backoff defers the retry; a pass before it is due does nothing.
    assert reconcile_tenant_pack_removals() == 0
    TenantPackRemoval.objects.filter(pk=removal.pk).update(next_attempt_at=timezone.now())
    assert reconcile_tenant_pack_removals() == 1
    assert TenantPackRemoval.objects.get().state == "completed"
    assert not storage.objects
    assert _remove(client, organization, installed).data["state"] == "completed"


def test_removed_pack_admits_no_new_reference(upload, installed, tenant):
    from cms.exceptions import CMSError
    from cms.services import fence_pack_reference

    client, organization, _root, _storage = upload
    assert _remove(client, organization, installed).status_code == 200
    with pytest.raises(CMSError):
        fence_pack_reference(installed.scenario_id)
    from cms.services import create_raes_native_range

    with pytest.raises(CMSError):
        create_raes_native_range(tenant[0], installed.scenario_id)


def test_backfilled_legacy_archive_is_bound_to_storage_and_deleted(upload, installed, settings):
    from uuid import uuid4

    client, organization, _root, storage = upload
    legacy = f"tenant-packs/{organization.uuid}/{uuid4()}.tar.gz"
    key = f"{settings.RAES_PACKAGE_PREFIX}/{legacy}"
    storage.objects[(settings.RAES_PACKAGE_BUCKET, key)] = b"legacy revision"
    TenantPackArchive.objects.create(
        organization_uuid=organization.uuid, scenario_id=installed.scenario_id, package_ref=legacy
    )

    assert _remove(client, organization, installed).status_code == 200
    assert (settings.RAES_PACKAGE_BUCKET, key) in storage.deleted
    assert not storage.objects


def test_reconciler_command_completes_pending_removals_and_rejects_bad_bounds(upload, installed, monkeypatch):
    from django.core.management import CommandError, call_command

    client, organization, _root, storage = upload
    delete = storage.delete_object
    monkeypatch.setattr(storage, "delete_object", lambda bucket, key: (_ for _ in ()).throw(CloudStorageError("down")))
    assert _remove(client, organization, installed).status_code == 202
    monkeypatch.setattr(storage, "delete_object", delete)
    TenantPackRemoval.objects.update(next_attempt_at=timezone.now())

    call_command("reconcile_tenant_pack_removals")

    assert TenantPackRemoval.objects.get().state == "completed"
    assert not storage.objects
    with pytest.raises(CommandError):
        call_command("reconcile_tenant_pack_removals", "--batch-size", "0")


@pytest.mark.parametrize("operator_action", ["relocate", "confirm_absent"])
def test_legacy_archive_under_an_earlier_prefix_is_never_reported_deleted_by_guess(
    upload, installed, settings, operator_action
):
    """A backfilled archive missing at its inferred location keeps removal pending for the operator."""
    from uuid import uuid4

    from django.core.management import call_command

    client, organization, _root, storage = upload
    legacy = f"tenant-packs/{organization.uuid}/{uuid4()}.tar.gz"
    original = (settings.RAES_PACKAGE_BUCKET, f"previous-prefix/{legacy}")
    storage.objects[original] = b"legacy revision under an earlier prefix"
    archive = TenantPackArchive.objects.create(
        organization_uuid=organization.uuid, scenario_id=installed.scenario_id, package_ref=legacy
    )

    response = _remove(client, organization, installed)

    assert response.status_code == 202 and response.data["remaining_archives"] == 1
    removal = TenantPackRemoval.objects.get()
    assert removal.last_failure_code == "legacy_location_unverified"
    assert original in storage.objects
    if operator_action == "relocate":
        call_command(
            "reconcile_tenant_pack_removals",
            "--relocate-archive",
            str(archive.pk),
            "--bucket",
            original[0],
            "--object-key",
            original[1],
        )
        assert original not in storage.objects
    else:
        call_command("reconcile_tenant_pack_removals", "--confirm-archive-absent", str(archive.pk))
    assert TenantPackRemoval.objects.get().state == "completed"
    assert AuditLog.objects.filter(entity_ref=str(removal.id), context="operator reconciliation").exists()
