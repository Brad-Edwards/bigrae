"""The legacy spare owner column survives an unsafe upgrade attempt."""

from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

BEFORE = [("ctf", "0070_ctfscheduledtask_authority_account_uuid_and_more")]
AFTER = [("ctf", "0071_remove_ctfsparerange_owner_user")]
pytestmark = pytest.mark.django_db(transaction=True)


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def legacy_spare_schema():
    apps = _migrate(BEFORE)
    try:
        yield apps
    finally:
        # Remove only test-owned legacy rows so the guard can restore the current
        # schema. Production migration never drains or deletes spare evidence.
        apps.get_model("ctf", "CTFSpareRange")._base_manager.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def _columns():
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, "ctf_ctfsparerange")}


@pytest.mark.parametrize("retained_status", ["ready", "consumed", "failed"])
def test_retained_legacy_spare_blocks_owner_drop_without_rewriting_custody(legacy_spare_schema, retained_status):
    from django.contrib.auth import get_user_model

    from cms.models import RangeInstance
    from cms.models import Request as CMSRequest
    from engine.models import Range
    from engine.models import Request as EngineRequest
    from management.services import principal_for_user
    from workspaces.models import Organization, Workspace

    user = get_user_model().objects.create_user(username="legacy-spare-holder")
    principal = principal_for_user(user)
    organization = Organization.objects.create(name="Legacy organization")
    workspace = Workspace.objects.create(organization=organization, name="Legacy workspace")
    event = legacy_spare_schema.get_model("ctf", "CTFEvent").objects.create(
        name="Legacy spare event",
        created_by_id=user.pk,
        creator_principal_uuid=principal.uuid,
        owner_principal_uuid=principal.uuid,
        workspace_id=workspace.pk,
        event_start=timezone.now(),
        event_end=timezone.now(),
        scenario_id="synthetic",
    )
    request_uuid = uuid4()
    cms_request = CMSRequest.objects.create(
        request_id=request_uuid, request_type="range", user=user, workspace_id=workspace.pk
    )
    cms_range = RangeInstance.objects.create(
        request=cms_request, user_id=user.pk, workspace_id=workspace.pk, status="ready", scenario_id="synthetic"
    )
    engine_request = EngineRequest.objects.create(request_id=request_uuid, request_type="range", user=user)
    runtime = Range.objects.create(request=engine_request, user=user, workspace_id=workspace.pk, status="ready")
    spare_model = legacy_spare_schema.get_model("ctf", "CTFSpareRange")
    spare = spare_model._base_manager.create(
        event=event,
        owner_user_id=user.pk,
        range_instance_id=cms_range.pk,
        request_id=request_uuid,
        status=retained_status,
        deleted_at=timezone.now() if retained_status == "failed" else None,
    )

    with pytest.raises(RuntimeError, match="drain and disposition"):
        _migrate(AFTER)

    assert "owner_user_id" in _columns()
    assert spare_model._base_manager.get(pk=spare.pk).owner_user_id == user.pk
    cms_request.refresh_from_db()
    cms_range.refresh_from_db()
    runtime.refresh_from_db()
    assert cms_request.user_id == cms_range.user_id == runtime.user_id == user.pk
    assert cms_request.owner_principal_uuid is cms_range.owner_principal_uuid is runtime.owner_principal_uuid is None
    assert cms_request.workspace_id == cms_range.workspace_id == runtime.workspace_id == workspace.pk


def test_empty_legacy_spare_pool_can_drop_owner_column(legacy_spare_schema):
    apps = _migrate(AFTER)
    assert "owner_user_id" not in _columns()
    assert "owner_user" not in {field.name for field in apps.get_model("ctf", "CTFSpareRange")._meta.get_fields()}
