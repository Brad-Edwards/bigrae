"""Interrupted provider copies keep exact cleanup custody after timeouts and late writes."""

from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from cms.exceptions import CMSError
from tests.cms._native_asset_fixtures import content_actor, content_administrator, installer_upload, storage_client

__all__ = ["content_actor", "content_administrator", "installer_upload", "storage_client"]
pytestmark = pytest.mark.django_db


class UploaderLost(BaseException):
    """The process stops without reaching its compensating error handler."""


@pytest.mark.parametrize("outcome", ["process-lost", "ambiguous-timeout", "late-acknowledgment"])
def test_copy_outcome_controls_cleanup_after_actual_provider_callback(installer_upload, monkeypatch, outcome):
    from cms.models import AgentConfig, AgentUpload
    from cms.services import cancel_upload, complete_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    case = installer_upload
    objects = {case.row.staging_key: b"synthetic validated installer"}
    clock = {"now": case.row.expires_at + timedelta(minutes=16)}

    def delete_object(**kwargs):
        objects.pop(kwargs["Key"], None)

    def copy_object(**kwargs):
        assert kwargs["CopySourceIfMatch"] == "synthetic-etag"
        assert kwargs["Key"] == case.row.install_key
        if outcome == "process-lost":
            raise UploaderLost
        if outcome == "ambiguous-timeout":
            raise ClientError({"Error": {"Code": "500", "Message": "Synthetic lost acknowledgment"}}, "CopyObject")
        monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: clock["now"])
        assert reconcile_upload_cleanup() == 0
        row = AgentUpload.objects.get()
        assert row.state == "finalizing" and row.cleanup_pending
        objects[case.row.install_key] = b"synthetic late provider copy"
        return {}

    case.client.delete_object.side_effect = delete_object
    case.client.copy_object.side_effect = copy_object
    with pytest.raises(UploaderLost if outcome == "process-lost" else CMSError):
        complete_upload(case.actor.credential, case.result["upload_token"])
    assert not AgentConfig.objects.exists()
    if outcome == "late-acknowledgment":
        row = AgentUpload.objects.get()
        assert row.state == "cancelled" and row.cleanup_pending
        assert reconcile_upload_cleanup() == 1
        assert not objects
        return
    with pytest.raises(CMSError, match=r"outcome.*pending"):
        cancel_upload(case.actor.credential, case.result["upload_token"])
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: clock["now"])
    assert reconcile_upload_cleanup() == 0
    row = AgentUpload.objects.get()
    assert row.state == "finalizing" and row.cleanup_pending
    assert not objects
    # The provider finishes after an earlier delete/absence observation.
    objects[case.row.install_key] = b"synthetic late provider copy"
    clock["now"] += timedelta(minutes=16)
    assert reconcile_upload_cleanup() == 0
    assert not objects
    row.refresh_from_db()
    assert row.state == "finalizing" and row.cleanup_pending


def test_process_loss_before_copy_admission_has_no_unknown_copy_to_retain(installer_upload, monkeypatch):
    from cms.models import AgentUpload
    from cms.services import complete_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    case = installer_upload
    case.client.head_object.side_effect = UploaderLost()
    with pytest.raises(UploaderLost):
        complete_upload(case.actor.credential, case.result["upload_token"])
    assert not AgentUpload.objects.get().validated_identity
    case.client.copy_object.assert_not_called()
    monkeypatch.setattr(
        "cms.services._scoped_uploads.timezone.now", lambda: case.row.expires_at + timedelta(minutes=16)
    )
    assert reconcile_upload_cleanup() == 1
    row = AgentUpload.objects.get()
    assert row.state == "cancelled" and not row.cleanup_pending


@pytest.mark.parametrize("completed", [False, True])
def test_expired_upload_tombstone_reaps_late_browser_put_and_preserves_active_install(
    installer_upload, monkeypatch, completed
):
    from io import StringIO

    from django.core.management import call_command

    from cms.models import AgentConfig, AgentUpload
    from cms.services import cancel_upload, complete_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup
    from shared.models import AuditLog

    case = installer_upload
    if completed:
        agent = complete_upload(case.actor.credential, case.result["upload_token"])
        assert AgentConfig.objects.filter(pk=agent.pk).exists()
    else:
        cancel_upload(case.actor.credential, case.result["upload_token"])
    installed = b"synthetic active immutable installer"
    objects = {case.row.staging_key: b"synthetic original PUT"}
    if completed:
        objects[case.row.install_key] = installed

    def delete_object(**kwargs):
        objects.pop(kwargs["Key"], None)

    case.client.delete_object.side_effect = delete_object
    clock = {"now": case.row.expires_at + timedelta(minutes=16)}
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: clock["now"])
    assert reconcile_upload_cleanup() == 1
    row = AgentUpload.objects.get()
    assert not row.cleanup_pending
    audits = AuditLog.objects.filter(entity_ref=str(row.pk), action="delete").count()
    objects[row.staging_key] = b"synthetic PUT finished after URL expiry and earlier cleanup"
    clock["now"] += timedelta(minutes=16)
    assert not AgentUpload.objects.filter(cleanup_pending=True).exists()
    # The real command still sweeps a retained key when no pending rows exist.
    call_command("reconcile_tenant_pack_removals", batch_size=10, stdout=StringIO())
    assert row.staging_key not in objects
    assert objects == ({row.install_key: installed} if completed else {})
    assert AuditLog.objects.filter(entity_ref=str(row.pk), action="delete").count() == audits
    if completed:
        assert AgentConfig.objects.filter(pk=agent.pk).exists()


def test_deployed_cleanup_command_runs_installer_cleanup_even_when_pack_pass_fails(installer_upload, monkeypatch):
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.db import OperationalError, connection

    from cms.models import AgentUpload
    from cms.services import cancel_upload

    case = installer_upload
    cancel_upload(case.actor.credential, case.result["upload_token"])
    monkeypatch.setattr(
        "cms.services._scoped_uploads.timezone.now", lambda: case.row.expires_at + timedelta(minutes=16)
    )

    def fail_pack_query(execute, sql, params, many, context):
        if sql.startswith("SELECT") and "cms_tenantpackremoval" in sql:
            raise OperationalError("Synthetic pack journal read outage")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_pack_query), pytest.raises(CommandError, match="cleanup pass failed"):
        call_command("reconcile_tenant_pack_removals", batch_size=10, stdout=StringIO())
    assert not AgentUpload.objects.get().cleanup_pending
    assert {call.kwargs["Key"] for call in case.client.delete_object.call_args_list} == {
        case.row.staging_key,
        case.row.install_key,
    }
