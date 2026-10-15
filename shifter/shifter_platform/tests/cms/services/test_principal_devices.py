"""Device projections authorize the persisted request's customer scope."""

from uuid import uuid4

import pytest

from cms.exceptions import CMSError
from tests.cms.services.test_principal_content import content_actor
from tests.cms.test_services_ngfws import _ngfw_catalog

pytestmark = [pytest.mark.django_db, pytest.mark.postgres]
__all__ = ["_ngfw_catalog", "content_actor"]


@pytest.fixture
def device_actor(content_actor):
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential

    old = ServiceCredentialAdmission.objects.select_related("binding").get(
        uuid=content_actor.credential.credential_uuid
    )
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=content_actor.credential.principal.uuid),
        issuer=old.binding.issuer,
        subject=str(uuid4()),
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=old.audience,
        scopes=["authorization:account.read_devices"],
    )
    content_actor.credential = resolve_service_credential(
        issuer=binding.issuer, subject=binding.subject, audience=admission.audience
    )
    return content_actor


@pytest.fixture
def scoped_devices(device_actor, _ngfw_catalog):
    from cms.models import App, AppType, Instance, InstanceType, Request
    from workspaces.services import resolve_resource_scope

    rows = []
    for index, scope in enumerate((device_actor.scope, device_actor.sibling)):
        resolved = resolve_resource_scope(scope)
        request = Request.objects.create(
            request_id=uuid4(),
            request_type="ngfw",
            user=None,
            owner_principal_uuid=device_actor.credential.principal.uuid,
            scope_kind=scope.kind,
            account_id=resolved.account_id,
        )
        instance = Instance.objects.create(
            request=request,
            name=f"Synthetic device {index}",
            instance_type=InstanceType.objects.get(slug="panw-ngfw"),
        )
        rows.append(
            App.objects.create(
                instance=instance,
                name=instance.name,
                app_type=AppType.objects.get(slug="panw-ngfw"),
                data={"serial_number": f"synthetic-{index}", "private_hint": "PRIVATE_DEVICE_MARKER"},
            )
        )
    return rows


def test_service_lists_and_reads_only_authorized_devices(device_actor, scoped_devices):
    from cms.services import get_ngfw, list_ngfws

    rows = list_ngfws(device_actor.credential, scope=device_actor.scope)
    assert [row.app_id for row in rows] == [scoped_devices[0].pk]
    assert "PRIVATE_DEVICE_MARKER" not in repr(rows)
    assert get_ngfw(device_actor.credential, scoped_devices[0].pk).app_id == scoped_devices[0].pk
    with pytest.raises(CMSError):
        get_ngfw(device_actor.credential, scoped_devices[1].pk)
    with pytest.raises(CMSError):
        list_ngfws(device_actor.credential, scope=device_actor.sibling)


def test_revoked_service_cannot_read_device_status(device_actor, scoped_devices):
    from cms.services import get_ngfw
    from management.models import ServiceCredentialAdmission

    ServiceCredentialAdmission.objects.filter(uuid=device_actor.credential.credential_uuid).update(is_active=False)
    with pytest.raises(CMSError):
        get_ngfw(device_actor.credential, scoped_devices[0].pk)


@pytest.fixture
def device_administrator(content_actor, _ngfw_catalog, settings):
    from cms.models import Credential, CredentialType
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from workspaces.services import add_account_member

    actor = content_actor
    admission = ServiceCredentialAdmission.objects.select_related("binding").get(uuid=actor.credential.credential_uuid)
    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=admission.audience,
        scopes=[
            f"authorization:account.{op}"
            for op in ("read_devices", "manage_devices", "access_devices", "manage_credentials")
        ],
    )
    actor.credential = resolve_service_credential(
        issuer=binding.issuer, subject=binding.subject, audience=admission.audience
    )
    add_account_member(actor.scope.account_uuid, actor.credential.principal)
    actor.profile = Credential.objects.create(
        user=None,
        owner_principal_uuid=actor.credential.principal.uuid,
        scope_kind="account",
        account_uuid=actor.scope.account_uuid,
        credential_type=CredentialType.objects.get(slug="deployment_profile"),
        name="Synthetic device profile",
        data={"authcode": "SYNTHETIC_DEVICE_REGISTRATION"},
    )
    settings.CLOUD_PROVIDER = "aws"
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    return actor


def test_service_creates_device_with_native_generation_and_revocable_authority(device_administrator):
    from cms.models import App
    from cms.services import create_ngfw, get_ngfw
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import Instance, ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    actor = device_administrator
    created = create_ngfw(
        actor.credential,
        "Synthetic device",
        actor.profile.pk,
        "otp",
        otp_value="synthetic-otp",
        otp_folder="synthetic-folder",
        scope=actor.scope,
    )
    app = App.objects.select_related("instance__request").get(pk=created.app_id)
    native = Instance.objects.select_related("request").get(request__request_id=app.instance.request.request_id)
    assert app.instance.request.user_id is native.request.user_id is None
    assert (
        app.instance.request.owner_principal_uuid
        == native.request.owner_principal_uuid
        == actor.credential.principal.uuid
    )
    assert get_ngfw(actor.credential, created.app_id).status == "provisioning"
    queued = ProvisionerLaunchIntent.objects.get()
    assert queued.authority_action == "account.manage_devices"
    assert queued.credential_uuid == actor.credential.credential_uuid
    authorize_provisioner_payload(queued.payload, expected_operation_id=queued.operation_id)
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    with pytest.raises(PermissionError):
        authorize_provisioner_payload(queued.payload, expected_operation_id=queued.operation_id)


def test_service_cannot_provision_with_another_accounts_source_credential(device_administrator):
    from cms.models import App
    from cms.services import create_ngfw
    from engine.models import ProvisionerLaunchIntent

    actor = device_administrator
    with pytest.raises(CMSError):
        create_ngfw(
            actor.credential,
            "Foreign device",
            actor.profile.pk,
            "otp",
            otp_value="synthetic",
            otp_folder="synthetic",
            scope=actor.sibling,
        )
    assert not App.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.fixture
def provisioned_device(device_administrator):
    from cms.models import App
    from cms.services import create_ngfw
    from engine.models import Instance

    actor = device_administrator
    created = create_ngfw(
        actor.credential,
        "Synthetic device",
        actor.profile.pk,
        "otp",
        otp_value="synthetic-otp",
        otp_folder="synthetic-folder",
        scope=actor.scope,
    )
    actor.app = App.objects.select_related("instance__request").get(pk=created.app_id)
    actor.native = Instance.objects.select_related("request").get(
        request__request_id=actor.app.instance.request.request_id
    )
    actor.app.status = actor.app.instance.status = actor.native.status = "ready"
    actor.app.save(update_fields=["status"])
    actor.app.instance.save(update_fields=["status"])
    actor.native.save(update_fields=["status"])
    actor.native.apps.update(status="ready")
    return actor


def test_service_teardown_uses_native_outbox_and_exact_custody(provisioned_device):
    from cms.services import destroy_ngfw
    from engine.models import ProvisionerLaunchIntent

    actor = provisioned_device
    result = destroy_ngfw(actor.credential, actor.app.pk, actor.app.name)
    assert result.is_deleted
    queued = ProvisionerLaunchIntent.objects.get(payload__operation="deprovision")
    assert queued.authority_resource_uuid == actor.app.pk
    assert queued.authority_account_uuid == actor.scope.account_uuid
    assert queued.credential_uuid == actor.credential.credential_uuid


def test_native_device_specs_keep_registration_secrets_encrypted(provisioned_device):
    from django.db import connection

    actor = provisioned_device
    with connection.cursor() as cursor:
        cursor.execute("SELECT spec FROM engine_instance WHERE id = %s", [actor.native.pk])
        assert "SYNTHETIC_DEVICE_REGISTRATION" not in str(cursor.fetchone())
        cursor.execute("SELECT spec FROM engine_app WHERE instance_id = %s", [actor.native.pk])
        assert "SYNTHETIC_DEVICE_REGISTRATION" not in str(cursor.fetchone())
        cursor.execute("SELECT data FROM cms_instance WHERE id = %s", [actor.app.instance_id])
        raw = str(cursor.fetchone())
        assert "SYNTHETIC_DEVICE_REGISTRATION" not in raw
        assert "synthetic-otp" not in raw
        assert "enc:v1:" in raw


@pytest.mark.django_db(transaction=True)
def test_native_sql_reader_decodes_persisted_service_device_specs(provisioned_device, settings):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from django.db import connection

    root = Path(__file__).resolve().parents[4]
    db = connection.settings_dict
    environment = {
        "PATH": os.environ["PATH"],
        "TESTING": "1",
        "PYTHONPATH": os.pathsep.join(map(str, [root / "engine/provisioner", root, root / "shifter_platform"])),
        "DB_HOST": str(db["HOST"]),
        "DB_PORT": str(db["PORT"]),
        "DB_NAME": str(db["NAME"]),
        "DB_USER": str(db["USER"]),
        "DB_PASSWORD": str(db["PASSWORD"]),
        "FIELD_ENCRYPTION_KEY": settings.FIELD_ENCRYPTION_KEY,
    }
    script = """
import json, sys
from provisioner_db_ngfw import get_ngfw_data_by_request_id
record = get_ngfw_data_by_request_id(sys.argv[1])
print(json.dumps({"spec": record["spec"], "app_spec": record["app_spec"]}))
"""
    result = subprocess.run(  # noqa: S603 # nosec B603
        [sys.executable, "-c", script, str(provisioned_device.native.request.request_id)],
        cwd=root / "engine/provisioner",
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    decoded = json.loads(result.stdout)
    assert decoded["app_spec"]["authcode"] == "SYNTHETIC_DEVICE_REGISTRATION"
    assert decoded["app_spec"]["user_id"] is None
    assert decoded["spec"]["ngfw_app"]["authcode"] == "SYNTHETIC_DEVICE_REGISTRATION"


def test_failed_device_can_be_destroyed_before_its_reserved_slot_is_reused(provisioned_device):
    from cms.handlers.ngfw_events import process_ngfw_event
    from cms.services import create_ngfw, destroy_ngfw, get_ngfw
    from engine.models import RangeEventOutbox
    from engine.services._operation_apply_effects import _enqueue_ngfw_status_event

    actor = provisioned_device
    actor.native.status = "failed"
    actor.native.save(update_fields=["status"])
    actor.native.apps.update(status="failed")
    _enqueue_ngfw_status_event(actor.native, actor.native.apps.get(), "failed", str(actor.native.request.request_id))
    process_ngfw_event(RangeEventOutbox.objects.get().payload)
    with pytest.raises(CMSError):
        get_ngfw(actor.credential, actor.app.pk)
    with pytest.raises(CMSError):
        create_ngfw(
            actor.credential,
            "Replacement",
            actor.profile.pk,
            "otp",
            otp_value="synthetic",
            otp_folder="synthetic",
            scope=actor.scope,
        )
    destroy_ngfw(actor.credential, actor.app.pk, actor.app.name)
    actor.native.refresh_from_db()
    actor.native.status = "destroyed"
    actor.native.save(update_fields=["status"])
    actor.native.apps.update(status="destroyed")
    _enqueue_ngfw_status_event(actor.native, actor.native.apps.get(), "destroyed", str(actor.native.request.request_id))
    process_ngfw_event(RangeEventOutbox.objects.get(payload__status="destroyed").payload)
    replacement = create_ngfw(
        actor.credential,
        "Replacement",
        actor.profile.pk,
        "otp",
        otp_value="synthetic",
        otp_folder="synthetic",
        scope=actor.scope,
    )
    assert replacement.app_id != actor.app.pk


@pytest.mark.parametrize("retired", [False, True])
def test_nested_secret_migration_encrypts_retained_and_retired_cms_specs(provisioned_device, retired):
    import importlib
    import json
    from types import SimpleNamespace

    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    instance = provisioned_device.app.instance
    if retired:
        instance.status = "failed"
        instance.save(update_fields=["status"])
    plain = {"ngfw_app": {"authcode": "MIGRATION_AUTH", "items": [{"otp_value": "MIGRATION_OTP"}]}}
    with connection.cursor() as cursor:
        cursor.execute("UPDATE cms_instance SET data = %s WHERE id = %s", [json.dumps(plain), instance.pk])
    historical = MigrationExecutor(connection).loader.project_state([("cms", "0064_device_custody_guards")]).apps
    migration = importlib.import_module("cms.migrations.0065_encrypt_nested_device_secrets")
    migration.encrypt_retained_specs(historical, SimpleNamespace(connection=connection))
    with connection.cursor() as cursor:
        cursor.execute("SELECT data FROM cms_instance WHERE id = %s", [instance.pk])
        raw = str(cursor.fetchone())
    assert "MIGRATION_AUTH" not in raw and "MIGRATION_OTP" not in raw
    assert "enc:v1:" in raw
    instance.refresh_from_db()
    assert instance.data == plain


@pytest.mark.parametrize("retired", [False, True])
def test_native_secret_migration_preserves_nested_specs_and_is_idempotent(provisioned_device, retired):
    import importlib
    import json
    from types import SimpleNamespace

    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    native = provisioned_device.native
    app = native.apps.get()
    if retired:
        native.status = app.status = "failed"
        native.save(update_fields=["status"])
        app.save(update_fields=["status"])
    plain = {
        "ngfw_app": {
            "authcode": "MIGRATION_AUTH",
            "items": [{"scm_pin_value": "MIGRATION_PIN", "otp_value": "MIGRATION_OTP"}],
        }
    }
    tables = (("engine_instance", native), ("engine_app", app))
    with connection.cursor() as cursor:
        for table, row in tables:
            cursor.execute(f"UPDATE {table} SET spec = %s WHERE id = %s", [json.dumps(plain), row.pk])  # nosec B608
    historical = MigrationExecutor(connection).loader.project_state([("engine", "0113_device_request_scope")]).apps
    migration = importlib.import_module("engine.migrations.0114_device_request_custody_guards")
    migration.encrypt_native_specs(historical, SimpleNamespace(connection=connection))
    with connection.cursor() as cursor:
        stored = []
        for table, row in tables:
            cursor.execute(f"SELECT spec FROM {table} WHERE id = %s", [row.pk])  # nosec B608
            raw = cursor.fetchone()[0]
            assert all(marker not in str(raw) for marker in ("MIGRATION_AUTH", "MIGRATION_PIN", "MIGRATION_OTP"))
            assert "enc:v1:" in str(raw)
            stored.append(raw)
            row.refresh_from_db()
            assert row.spec == plain
    migration.encrypt_native_specs(historical, SimpleNamespace(connection=connection))
    with connection.cursor() as cursor:
        for (table, row), expected in zip(tables, stored, strict=True):
            cursor.execute(f"SELECT spec FROM {table} WHERE id = %s", [row.pk])  # nosec B608
            assert cursor.fetchone()[0] == expected


def test_bulk_write_cannot_rehome_native_device_custody(provisioned_device):
    from django.db import IntegrityError, transaction

    from engine.models import Request
    from workspaces.services import resolve_resource_scope

    destination = resolve_resource_scope(provisioned_device.sibling)
    with pytest.raises(IntegrityError), transaction.atomic():
        Request.objects.filter(pk=provisioned_device.native.request_id).update(account_id=destination.account_id)


def test_unaccepted_callback_cannot_change_a_principal_device(provisioned_device):
    from cms.handlers.ngfw_events import process_ngfw_event
    from shared.messages.events import EVENT_TYPE_NGFW

    actor = provisioned_device
    process_ngfw_event(
        {
            "event_type": EVENT_TYPE_NGFW,
            "event_id": str(uuid4()),
            "instance_id": str(actor.app.instance_id),
            "app_id": str(actor.app.pk),
            "request_id": str(actor.app.instance.request.request_id),
            "status": "destroyed",
        }
    )
    actor.app.refresh_from_db()
    actor.app.instance.refresh_from_db()
    assert actor.app.status == actor.app.instance.status == "ready"


def test_accepted_native_device_event_releases_the_same_cms_custody(provisioned_device):
    from cms.handlers.ngfw_events import process_ngfw_event
    from engine.models import RangeEventOutbox
    from engine.services._operation_apply_effects import _enqueue_ngfw_status_event

    actor = provisioned_device
    actor.native.status = "destroyed"
    actor.native.save(update_fields=["status"])
    actor.native.apps.update(status="destroyed")
    _enqueue_ngfw_status_event(actor.native, actor.native.apps.get(), "destroyed", str(actor.native.request.request_id))
    event = RangeEventOutbox.objects.get().payload
    process_ngfw_event(dict(event, status="ready"))
    actor.app.refresh_from_db()
    assert actor.app.status == "ready"
    process_ngfw_event(event)
    actor.app.refresh_from_db()
    actor.app.instance.request.refresh_from_db()
    assert actor.app.status == "destroyed"
    assert actor.app.instance.request.deleted_at is not None


@pytest.mark.django_db(transaction=True)
def test_device_remote_access_retains_original_credential_and_closes_on_revocation(
    provisioned_device, settings, monkeypatch
):
    from management.models import ServiceCredentialAdmission
    from mission_control.guacamole_authority import renew_gateway_authority, revalidate_guacamole_sessions
    from mission_control.guacamole_session import launch_guacamole_session
    from mission_control.models import GuacamoleBootstrapRequest
    from tests.mission_control.test_principal_guacamole import Gateway

    actor = provisioned_device
    actor.native.state = {"management_ip": "192.0.2.10", "ssh_key_secret_id": "synthetic-device-key"}
    actor.native.save(update_fields=["state"])
    monkeypatch.setattr("engine.secrets.get_ssh_key", lambda reference: "SYNTHETIC_TEST_KEY")
    settings.GUACAMOLE_BOOTSTRAP_INLINE = True
    gateway = Gateway()
    result = launch_guacamole_session(
        user=actor.credential, protocol="ngfw_ssh", target_id=str(actor.app.pk), guacamole_client=gateway
    )
    row = GuacamoleBootstrapRequest.objects.get(pk=result.bootstrap_id)
    assert row.status == "succeeded"
    assert gateway.last_identity == "access-" + str(row.pk)
    assert renew_gateway_authority(str(row.pk))
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    assert not renew_gateway_authority(str(row.pk))
    assert revalidate_guacamole_sessions(client=gateway) == 1
    assert gateway.closed == ["SYNTHETIC_NATIVE_TOKEN"]


def test_native_device_lifecycle_accepts_service_authority(provisioned_device):
    from engine.models import ProvisionerLaunchIntent
    from engine.services import stop_ngfw

    actor = provisioned_device
    assert stop_ngfw(actor.native.request.request_id, credential=actor.credential)
    queued = ProvisionerLaunchIntent.objects.get(payload__operation="stop")
    assert queued.credential_uuid == actor.credential.credential_uuid
    assert queued.authority_resource_uuid == actor.app.pk


def test_bulk_write_cannot_rehome_cms_device_custody(provisioned_device):
    from django.db import IntegrityError, transaction

    from cms.models import Request
    from workspaces.services import resolve_resource_scope

    destination = resolve_resource_scope(provisioned_device.sibling)
    with pytest.raises(IntegrityError), transaction.atomic():
        Request.objects.filter(pk=provisioned_device.app.instance.request_id).update(account_id=destination.account_id)


def test_device_teardown_cleanup_requires_native_absence_after_revocation(provisioned_device, monkeypatch):
    from types import SimpleNamespace

    from cms.services import destroy_ngfw
    from engine.launch_intents import authorize_provisioner_payload, enqueue_provisioner_launch
    from engine.models import ProvisionerLaunchIntent, ProvisionerLaunchStatus
    from management.models import ServiceCredentialAdmission
    from shared.cloud.types import TaskInterruptDisposition

    actor = provisioned_device
    destroy_ngfw(actor.credential, actor.app.pk, actor.app.name)
    origin = ProvisionerLaunchIntent.objects.get(payload__operation="deprovision")
    origin.status = ProvisionerLaunchStatus.SUCCEEDED
    origin.save(update_fields=["status"])
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    observed = []

    def interrupt(namespace, task_ref, expected):
        observed.append((namespace, task_ref, expected))
        return TaskInterruptDisposition.TERMINAL_ABSENT

    monkeypatch.setattr("engine.ecs.get_task_runner", lambda: SimpleNamespace(interrupt_task=interrupt))
    identifier = enqueue_provisioner_launch(
        ["ngfw", "deprovision", "--request-id", str(actor.native.request.request_id)], cleanup_for=origin.operation_id
    )
    cleanup = ProvisionerLaunchIntent.objects.get(intent_id=identifier)
    assert cleanup.authority_kind == "cleanup"
    assert cleanup.cleanup_origin_operation_uuid == origin.operation_id
    assert observed[0][1] == origin.task_ref
    assert observed[0][2]["image"] == origin.executing_workload["image"]
    authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)


def test_device_cleanup_cannot_borrow_a_provision_or_running_operation(provisioned_device):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import ProvisionerLaunchIntent

    actor = provisioned_device
    origin = ProvisionerLaunchIntent.objects.get()
    with pytest.raises(ValueError):
        enqueue_provisioner_launch(
            ["ngfw", "deprovision", "--request-id", str(actor.native.request.request_id)],
            cleanup_for=origin.operation_id,
        )


@pytest.mark.parametrize("terminal", ["IDENTITY_MISMATCH", "EXHAUSTED"])
def test_device_cleanup_cannot_overwrite_a_concurrent_terminal_interrupt(provisioned_device, monkeypatch, terminal):
    from types import SimpleNamespace

    from cms.services import destroy_ngfw
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import InterruptState, ProvisionerLaunchIntent
    from shared.cloud.types import TaskInterruptDisposition

    actor = provisioned_device
    destroy_ngfw(actor.credential, actor.app.pk, actor.app.name)
    origin = ProvisionerLaunchIntent.objects.get(payload__operation="deprovision")
    final_state = getattr(InterruptState, terminal)

    def interrupt(*args):
        ProvisionerLaunchIntent.objects.filter(pk=origin.pk).update(interrupt_state=final_state)
        return TaskInterruptDisposition.TERMINAL_ABSENT

    monkeypatch.setattr("engine.ecs.get_task_runner", lambda: SimpleNamespace(interrupt_task=interrupt))
    with pytest.raises(ValueError, match="native identity boundary"):
        enqueue_provisioner_launch(
            ["ngfw", "deprovision", "--request-id", str(actor.native.request.request_id)],
            cleanup_for=origin.operation_id,
        )
    origin.refresh_from_db()
    assert origin.interrupt_state == final_state
    assert not ProvisionerLaunchIntent.objects.filter(authority_kind="cleanup").exists()


def test_uncertain_device_cleanup_waits_for_existing_interrupt_worker(provisioned_device, monkeypatch):
    from types import SimpleNamespace

    from cms.services import destroy_ngfw
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.launch_interrupt import drain_due_interrupts
    from engine.models import InterruptState, ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission
    from shared.cloud.types import TaskInterruptDisposition

    actor = provisioned_device
    destroy_ngfw(actor.credential, actor.app.pk, actor.app.name)
    origin = ProvisionerLaunchIntent.objects.get(payload__operation="deprovision")
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    disposition = [TaskInterruptDisposition.STOPPING]
    monkeypatch.setattr(
        "engine.ecs.get_task_runner", lambda: SimpleNamespace(interrupt_task=lambda *args: disposition[0])
    )
    with pytest.raises(ValueError, match="awaits native task absence"):
        enqueue_provisioner_launch(
            ["ngfw", "deprovision", "--request-id", str(actor.native.request.request_id)],
            cleanup_for=origin.operation_id,
        )
    assert ProvisionerLaunchIntent.objects.filter(authority_kind="cleanup").count() == 0
    origin.refresh_from_db()
    assert origin.interrupt_state == InterruptState.REQUESTED
    disposition[0] = TaskInterruptDisposition.TERMINAL_ABSENT
    assert drain_due_interrupts(1) == 1
    origin.refresh_from_db()
    assert origin.interrupt_state == InterruptState.DESTROY_ENQUEUED
    assert ProvisionerLaunchIntent.objects.filter(authority_kind="cleanup").count() == 1


@pytest.mark.django_db(transaction=True)
def test_device_bootstrap_does_not_log_private_secret_provider_errors(
    provisioned_device, settings, monkeypatch, caplog
):
    from mission_control.guacamole_session import launch_guacamole_session
    from mission_control.models import GuacamoleBootstrapRequest
    from tests.mission_control.test_principal_guacamole import Gateway

    actor = provisioned_device
    actor.native.state = {"management_ip": "192.0.2.10", "ssh_key_secret_id": "synthetic-device-key"}
    actor.native.save(update_fields=["state"])

    def unavailable(reference):
        raise RuntimeError("SYNTHETIC_SECRET_PROVIDER_FAILURE")

    monkeypatch.setattr("engine.secrets.get_ssh_key", unavailable)
    settings.GUACAMOLE_BOOTSTRAP_INLINE = True
    result = launch_guacamole_session(
        user=actor.credential, protocol="ngfw_ssh", target_id=str(actor.app.pk), guacamole_client=Gateway()
    )
    row = GuacamoleBootstrapRequest.objects.get(pk=result.bootstrap_id)
    assert row.status == "failed"
    assert "SYNTHETIC_SECRET_PROVIDER_FAILURE" not in row.error_message
    assert "SYNTHETIC_SECRET_PROVIDER_FAILURE" not in caplog.text
