"""Backfill the tenant pack archive inventory from trusted registration evidence (#2520).

Before #2520 an upload revision replaced ``package_ref`` and left the previous
object in storage with no pack-owned inventory. The current reference is on the
source row; every superseded reference is in the pack's strict revision audit
(``previous_state`` / ``new_state``). Only references that match the exact
server-generated tenant object pattern for the owning organization are owned.
Storage location is bound later by the removal request, never guessed here.
"""

import re

from django.db import migrations

_REF = re.compile(r"tenant-packs/([0-9a-f-]{36})/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tar\.gz")


def _owned(ref: object, organization_uuid: str) -> bool:
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    return match is not None and match.group(1) == organization_uuid


def backfill(apps, schema_editor):
    source_model = apps.get_model("cms", "RaesPackageSource")
    archive_model = apps.get_model("cms", "TenantPackArchive")
    audit_model = apps.get_model("shared", "AuditLog")
    for source in source_model.objects.filter(organization_uuid__isnull=False, source_kind="object").iterator():
        organization = str(source.organization_uuid)
        refs = {source.package_ref}
        for previous, new in audit_model.objects.filter(
            entity_type="scenario", entity_ref=source.scenario_id
        ).values_list("previous_state", "new_state"):
            for state in (previous, new):
                if isinstance(state, dict):
                    refs.add(state.get("package_ref"))
        archive_model.objects.bulk_create(
            [
                archive_model(
                    organization_uuid=source.organization_uuid, scenario_id=source.scenario_id, package_ref=ref
                )
                for ref in sorted(ref for ref in refs if _owned(ref, organization))
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0054_tenant_pack_removal"),
        ("shared", "0029_principal_audit_attribution"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
