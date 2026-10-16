"""Private image inventory is selected under its actual customer authority."""

from uuid import uuid4

import pytest

from tests.cms.services.test_principal_content import content_actor
from tests.cms.services.test_principal_range_authority import principal_range

pytestmark = [pytest.mark.django_db, pytest.mark.postgres]
__all__ = ["content_actor", "principal_range"]


def test_private_artifact_inventory_does_not_cross_accounts_or_enter_legacy_catalog(content_actor):
    from engine.models import RaesImageMapping
    from engine.services import list_backend_artifacts

    fixture = content_actor
    for index, scope in enumerate((fixture.scope, fixture.sibling)):
        RaesImageMapping.objects.create(
            provider="gce",
            source_name=f"synthetic-image-{index}",
            image_ref=f"projects/synthetic/global/images/image-{index}",
            owner_principal_uuid=fixture.credential.principal.uuid,
            scope_kind=scope.kind,
            account_uuid=scope.account_uuid,
            artifact_id=f"synthetic-artifact-{index}",
            artifact_version="1",
            artifact_digest="sha256:" + "ab" * 32,
            media_type="application/vnd.raes.image",
            integrity_ref="synthetic-integrity",
            provenance_ref="synthetic-provenance",
        )
    rows = list_backend_artifacts(provider="gce", credential=fixture.credential, scope=fixture.scope)
    assert [row.artifact_id for row in rows] == ["synthetic-artifact-0"]
    assert list_backend_artifacts(provider="gce") == []
    with pytest.raises(PermissionError):
        list_backend_artifacts(provider="gce", credential=fixture.credential, scope=fixture.sibling)


def test_scoped_inventory_custody_cannot_be_rewritten_by_bulk_update(content_actor):
    from django.db import IntegrityError, transaction

    from engine.models import RaesImageMapping

    row = RaesImageMapping.objects.create(
        provider="gce",
        source_name="synthetic-private-image",
        image_ref="projects/synthetic/global/images/image",
        owner_principal_uuid=content_actor.credential.principal.uuid,
        scope_kind="account",
        account_uuid=content_actor.scope.account_uuid,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        RaesImageMapping.objects.filter(pk=row.pk).update(account_uuid=uuid4())


def test_range_input_candidates_exclude_other_customers_and_legacy_inventory(principal_range):
    from engine.models import RaesImageMapping
    from engine.operation_inputs import _raes_image_candidates
    from tests.engine.test_operation_input_raes import _plan

    fields = {"provider": "gce", "source_name": "kali", "image_ref": "projects/synthetic/global/images/base"}
    RaesImageMapping.objects.create(**fields, source_version="legacy")
    RaesImageMapping.objects.create(
        **fields,
        source_version="foreign",
        scope_kind="account",
        account_uuid=uuid4(),
        owner_principal_uuid=principal_range.owner.uuid,
    )
    RaesImageMapping.objects.create(
        **fields,
        source_version="own",
        scope_kind="account",
        account_uuid=principal_range.account.uuid,
        owner_principal_uuid=principal_range.owner.uuid,
    )
    candidates = _raes_image_candidates(_plan(), principal_range.realized)
    assert [row["source_version"] for rows in candidates.values() for row in rows] == ["own"]


def test_customer_inventory_can_share_source_identity_without_collision(content_actor):
    from engine.models import RaesImageMapping

    for scope in (content_actor.scope, content_actor.sibling):
        RaesImageMapping.objects.create(
            provider="gce",
            source_name="synthetic-base",
            image_ref="projects/synthetic/global/images/base",
            scope_kind="account",
            account_uuid=scope.account_uuid,
            owner_principal_uuid=content_actor.credential.principal.uuid,
        )
    assert RaesImageMapping.objects.filter(source_name="synthetic-base").count() == 2
