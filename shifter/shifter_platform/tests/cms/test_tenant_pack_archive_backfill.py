"""Historical-schema proof for the tenant pack archive inventory backfill (#2520)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("cms", "0054_tenant_pack_removal"), ("shared", "0029_principal_audit_attribution")]
AFTER = [("cms", "0055_backfill_tenant_pack_archives")]

pytestmark = [pytest.mark.django_db(transaction=True)]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def historical_schema():
    # Remove both post-fork CMS branches before constructing the historical
    # model state; an unmentioned sibling branch can otherwise leave columns behind.
    _migrate([("cms", "0053_rangeinstance_uuid"), ("shared", "0029_principal_audit_attribution")])
    apps = _migrate(BEFORE)
    try:
        yield apps
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_backfill_attributes_current_and_superseded_owned_archives_only(historical_schema):
    user = historical_schema.get_model("auth", "User").objects.create(username="legacy-pack-admin")
    sources = historical_schema.get_model("cms", "RaesPackageSource")
    audit = historical_schema.get_model("shared", "AuditLog")
    organization, other = uuid4(), uuid4()
    current = f"tenant-packs/{organization}/{uuid4()}.tar.gz"
    superseded = f"tenant-packs/{organization}/{uuid4()}.tar.gz"
    foreign = f"tenant-packs/{other}/{uuid4()}.tar.gz"
    pack = sources.objects.create(
        scenario_id="pack-legacy",
        organization_uuid=organization,
        package_name="legacy",
        source_kind="object",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref=current,
        package_version="2",
        package_digest="sha256:" + "a" * 64,
        registered_by=user,
    )
    sources.objects.create(
        scenario_id="platform-pack",
        source_kind="repo",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="packs/platform",
        package_version="1",
        package_digest="sha256:" + "b" * 64,
        registered_by=user,
    )
    for sequence, (previous, new) in enumerate(
        [({"package_ref": superseded}, {"package_ref": current}), ({"package_ref": foreign}, {"package_ref": "../x"})],
        start=1,
    ):
        audit.objects.create(
            deployment_scope="test",
            sequence=sequence,
            record_digest=f"{sequence:064x}",
            entity_type="scenario",
            entity_id=0,
            entity_ref=pack.scenario_id,
            action="update",
            actor_type="user",
            previous_state=previous,
            new_state=new,
        )

    after = _migrate(AFTER)

    archives = after.get_model("cms", "TenantPackArchive").objects.order_by("package_ref")
    assert sorted(archives.values_list("package_ref", flat=True)) == sorted([current, superseded])
    assert {row.scenario_id for row in archives} == {"pack-legacy"}
    assert all(row.organization_uuid == organization and not row.object_key for row in archives)
