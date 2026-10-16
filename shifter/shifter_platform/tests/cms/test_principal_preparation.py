"""Catalog preparation and supply assessment use the actual customer authority."""

import pytest

from cms.exceptions import CMSError
from shared.exceptions import ValidationError
from tests.cms.services.test_principal_content import content_actor
from tests.cms.test_artifact_preparation import registered_pack
from tests.engine.services.test_preparation_operations import administrator, grant, installed, operator
from tests.engine.services.test_principal_preparation import service_preparer

pytestmark = [pytest.mark.django_db]
__all__ = ["administrator", "content_actor", "grant", "installed", "operator", "registered_pack", "service_preparer"]


def test_service_can_prepare_registered_pack_under_its_actual_scope(service_preparer, installed, registered_pack):
    from cms.scenarios.preparation import prepare_registered_artifact
    from engine.models import PreparationOperation

    source, address = registered_pack
    source.owner_principal_uuid = service_preparer.principal.uuid
    source.scope_kind = "installation"
    source.save()
    result = prepare_registered_artifact(service_preparer, source.scenario_id, address, installed.id, "example-image")
    operation = PreparationOperation.objects.get(pk=result.id)
    assert operation.authority_kind
    assert operation.authority_owner_uuid == service_preparer.principal.uuid
    assert operation.authority_scope_kind == "installation"


def test_foreign_package_is_denied_before_private_staging(content_actor, registered_pack, monkeypatch):
    from uuid import uuid4

    from cms.scenarios.preparation import prepare_registered_artifact

    source, address = registered_pack
    source.owner_principal_uuid = content_actor.credential.principal.uuid
    source.scope_kind = "account"
    source.account_uuid = content_actor.sibling.account_uuid
    source.save()
    staged = []
    monkeypatch.setattr("cms.scenarios.preparation._trusted_scenario_path", lambda row: staged.append(row))
    with pytest.raises((CMSError, ValidationError)):
        prepare_registered_artifact(content_actor.credential, source.scenario_id, address, uuid4(), "example-image")
    assert not staged


@pytest.mark.parametrize("change", ["credential-revoked", "conformance-revoked"])
def test_preparation_rechecks_authority_and_package_after_staging(
    service_preparer, installed, registered_pack, monkeypatch, change
):
    from contextlib import contextmanager

    from cms.models import RaesPackageSource
    from cms.scenarios import preparation
    from engine.models import PreparationOperation
    from management.models import ServiceCredentialAdmission

    source, address = registered_pack
    source.owner_principal_uuid = service_preparer.principal.uuid
    source.scope_kind = "installation"
    source.save()
    trusted_path = preparation._trusted_scenario_path

    @contextmanager
    def changed_after_read(row):
        with trusted_path(row) as value:
            yield value
        if change == "credential-revoked":
            ServiceCredentialAdmission.objects.filter(uuid=service_preparer.credential_uuid).update(is_active=False)
        else:
            RaesPackageSource.objects.filter(pk=row.pk).update(conformance_status="failed")

    monkeypatch.setattr(preparation, "_trusted_scenario_path", changed_after_read)
    with pytest.raises(ValidationError):
        preparation.prepare_registered_artifact(
            service_preparer, source.scenario_id, address, installed.id, "example-image"
        )
    assert not PreparationOperation.objects.exists()


def test_scoped_realizability_uses_only_customer_inventory(content_actor, make_pack, tmp_path, settings):
    from cms.models import RaesPackageSource
    from cms.scenarios.pack_validation import pack_digest
    from cms.scenarios.realizability import get_scenario_realizability
    from engine.models import RaesImageMapping
    from tests.cms.conftest import IMAGELESS_PACK_SDL

    settings.CLOUD_PROVIDER = "gcp"
    settings.RAES_PACKAGE_ROOT = str(tmp_path)
    root = make_pack(tmp_path / "packs/synthetic", name="synthetic", sdl=IMAGELESS_PACK_SDL)
    source = RaesPackageSource.objects.create(
        scenario_id="synthetic-scoped-realizability",
        source_kind="repo",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="packs/synthetic",
        package_version="1",
        package_digest=pack_digest(root),
        conformance_status="passed",
        owner_principal_uuid=content_actor.credential.principal.uuid,
        scope_kind="account",
        account_uuid=content_actor.scope.account_uuid,
    )
    foreign = RaesImageMapping.objects.create(
        provider="gce",
        source_name="linux",
        image_ref="projects/synthetic/global/images/base",
        owner_principal_uuid=content_actor.credential.principal.uuid,
        scope_kind="account",
        account_uuid=content_actor.sibling.account_uuid,
    )
    result = get_scenario_realizability(
        source.scenario_id, credential=content_actor.credential, scope=content_actor.scope
    )
    assert result["outcome"] == "not_realizable"
    foreign.delete()
    RaesImageMapping.objects.create(
        provider="gce",
        source_name="linux",
        image_ref="projects/synthetic/global/images/base",
        owner_principal_uuid=content_actor.credential.principal.uuid,
        scope_kind="account",
        account_uuid=content_actor.scope.account_uuid,
    )
    result = get_scenario_realizability(
        source.scenario_id, credential=content_actor.credential, scope=content_actor.scope
    )
    assert result["outcome"] == "realizable", result
