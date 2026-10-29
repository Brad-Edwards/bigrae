"""Mounted CMS HTTP routes preserve native credentials, scope and current policy."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from shared.authorization import AuthorizationDecision, CredentialCeiling, DecisionKind, port
from shared.credentials import CredentialContext

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["human", "service"])
def content_http_actor(request, django_user_model, live_session_credential, settings, allow_all_policy):
    from cms.models import RaesPackageSource
    from tests.cms._warm_pool_fixtures import service_credential

    user = django_user_model.objects.create_user(username=f"content-http-{uuid4()}")
    actions = ["installation.read_content", "installation.manage_content", "installation.prepare_content"]
    credential = (
        live_session_credential(user)
        if request.param == "human"
        else service_credential(settings, scopes=[f"authorization:{action}" for action in actions])
    )
    client = APIClient()
    client.force_authenticate(user=user if request.param == "human" else None, token=credential)
    source = RaesPackageSource.objects.create(
        scenario_id=f"synthetic-{uuid4().hex}",
        source_kind="object",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="synthetic/content.tar.gz",
        package_version="1",
        package_digest="sha256:" + "a" * 64,
        conformance_status="passed",
        scope_kind="installation",
        owner_principal_uuid=credential.principal.uuid,
        is_operator_published=True,
    )
    return SimpleNamespace(client=client, credential=credential, source=source, user=user)


def _registration():
    return {
        "scenario_id": f"synthetic-register-{uuid4().hex}",
        "source_kind": "object",
        "contract_kind": "raes",
        "contract_profile": "shifter",
        "package_ref": "synthetic/registered.tar.gz",
        "package_version": "1",
        "package_digest": "sha256:" + "b" * 64,
    }


def _image():
    return {"provider": "gce", "source_name": "synthetic-base", "image_ref": "projects/synthetic/global/images/base"}


def test_nonstaff_human_and_service_use_mounted_content_routes(content_http_actor):
    from cms.models import RaesPackageSource, ScenarioMetadata
    from engine.models import RaesImageMapping

    actor = content_http_actor
    client = actor.client
    assert not actor.user.is_staff and not actor.user.is_superuser
    response = client.get("/api/v1/cms/catalog/")
    assert response.status_code == 200, response.data
    assert [row["id"] for row in response.data] == [actor.source.scenario_id]
    for path in ("catalog", "scenarios"):
        assert client.get(f"/api/v1/cms/{path}/{actor.source.scenario_id}/").status_code == 200
    response = client.patch(
        f"/api/v1/cms/scenarios/{actor.source.scenario_id}/metadata/", {"enabled": False}, format="json"
    )
    assert response.status_code == 200, response.data
    assert not ScenarioMetadata.objects.get().enabled
    body = _registration()
    response = client.post("/api/v1/cms/catalog/packs/", body, format="json")
    assert response.status_code == 201, response.data
    registered = RaesPackageSource.objects.get(scenario_id=body["scenario_id"])
    assert registered.owner_principal_uuid == actor.credential.principal.uuid
    assert registered.scope_kind == "installation" and not registered.is_operator_published
    response = client.post("/api/v1/cms/raes-image-mappings/", _image(), format="json")
    assert response.status_code == 200, response.data
    mapping = RaesImageMapping.objects.get()
    assert mapping.scope_kind == "installation"
    assert mapping.owner_principal_uuid == actor.credential.principal.uuid
    assert client.get("/api/v1/cms/raes-image-mappings/").data[0]["id"] == mapping.pk
    response = client.post(
        "/api/v1/cms/raes-image-mappings/disable/", {"provider": "gce", "source_name": "synthetic-base"}, format="json"
    )
    assert response.status_code == 200, response.data
    mapping.refresh_from_db()
    assert not mapping.enabled


def test_legacy_roles_cannot_substitute_for_current_content_policy(content_http_actor, monkeypatch):
    from django.contrib.auth.models import Group

    from cms.models import ScenarioMetadata
    from engine.models import RaesImageMapping
    from shared.auth import THREAT_RESEARCH_GROUP

    actor = content_http_actor
    actor.user.is_staff = actor.user.is_superuser = True
    actor.user.save(update_fields=["is_staff", "is_superuser"])
    actor.user.groups.add(Group.objects.get_or_create(name=THREAT_RESEARCH_GROUP)[0])
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied")),
    )
    assert actor.client.get("/api/v1/cms/catalog/").status_code == 403
    assert (
        actor.client.patch(
            f"/api/v1/cms/scenarios/{actor.source.scenario_id}/metadata/", {"enabled": False}, format="json"
        ).status_code
        >= 400
    )
    assert actor.client.post("/api/v1/cms/catalog/packs/", _registration(), format="json").status_code >= 400
    assert actor.client.post("/api/v1/cms/raes-image-mappings/", _image(), format="json").status_code == 403
    assert not ScenarioMetadata.objects.exists()
    assert not RaesImageMapping.objects.exists()


def test_exact_read_ceiling_never_admits_content_or_image_writes(content_http_actor):
    actor = content_http_actor
    original = actor.credential
    narrow = CredentialContext(
        original.principal,
        original.kind,
        original.credential_uuid,
        CredentialCeiling(frozenset({"installation.read_content"})),
        original.scopes,
    )
    actor.client.force_authenticate(user=actor.user, token=narrow)
    assert actor.client.get("/api/v1/cms/catalog/").status_code == 200
    assert actor.client.post("/api/v1/cms/catalog/packs/", _registration(), format="json").status_code >= 400
    assert (
        actor.client.patch(
            f"/api/v1/cms/scenarios/{actor.source.scenario_id}/metadata/", {"enabled": False}, format="json"
        ).status_code
        >= 400
    )
    assert actor.client.post("/api/v1/cms/raes-image-mappings/", _image(), format="json").status_code == 403


def test_revoked_native_proof_is_denied_before_content_effects(content_http_actor):
    from cms.models import ScenarioMetadata
    from engine.models import RaesImageMapping
    from management.models import Principal

    actor = content_http_actor
    Principal.objects.filter(uuid=actor.credential.principal.uuid).update(is_active=False)
    assert actor.client.get("/api/v1/cms/catalog/").status_code in {401, 403}
    assert actor.client.post("/api/v1/cms/catalog/packs/", _registration(), format="json").status_code in {401, 403}
    assert actor.client.patch(
        f"/api/v1/cms/scenarios/{actor.source.scenario_id}/metadata/", {"enabled": False}, format="json"
    ).status_code in {401, 403}
    assert actor.client.post("/api/v1/cms/raes-image-mappings/", _image(), format="json").status_code in {401, 403}
    assert not ScenarioMetadata.objects.exists()
    assert not RaesImageMapping.objects.exists()


def test_staff_user_without_native_session_proof_cannot_author_content(django_user_model):
    client = APIClient()
    user = django_user_model.objects.create_superuser(username="unproven-content-staff", password="unused")
    # Bypass the suite's convenience wrapper that gives force-authenticated
    # users a real session reference; this case deliberately has none.
    client.handler._force_user = user
    client.handler._force_token = None
    assert client.get("/api/v1/cms/catalog/").status_code == 403
    assert client.post("/api/v1/cms/catalog/packs/", _registration(), format="json").status_code == 403


def test_image_registry_revocation_after_policy_prevents_write(content_http_actor, monkeypatch):
    from engine.models import RaesImageMapping
    from engine.services import _principal_image_registry
    from management.models import Principal

    original = _principal_image_registry.authorize_image_registry

    def admit_then_revoke(*args, **kwargs):
        current = original(*args, **kwargs)
        Principal.objects.filter(uuid=current.principal.uuid).update(is_active=False)
        return current

    monkeypatch.setattr(_principal_image_registry, "authorize_image_registry", admit_then_revoke)
    response = content_http_actor.client.post("/api/v1/cms/raes-image-mappings/", _image(), format="json")
    assert response.status_code == 403, response.data
    assert not RaesImageMapping.objects.exists()


def test_private_catalog_scope_is_sql_validated_and_not_inferred_from_roles(content_http_actor, monkeypatch, settings):
    from cms.models import RaesPackageSource
    from workspaces.services import create_account

    actor = content_http_actor
    own, sibling = [create_account(kind="team", name=f"Synthetic content scope {i}") for i in range(2)]
    for account in (own, sibling):
        RaesPackageSource.objects.create(
            scenario_id=f"synthetic-private-{account.id}",
            scope_kind="account",
            account_uuid=account.uuid,
            owner_principal_uuid=actor.credential.principal.uuid,
            source_kind="object",
            contract_kind="raes",
            contract_profile="shifter",
            package_ref="synthetic/private.tar.gz",
            package_version="1",
            package_digest="sha256:" + "c" * 64,
            conformance_status="passed",
        )
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(
            check=lambda request: AuthorizationDecision(
                DecisionKind.ALLOWED if request.scope.account_uuid == own.uuid else DecisionKind.DENIED,
                "policy_allowed" if request.scope.account_uuid == own.uuid else "policy_denied",
            )
        ),
    )
    # The service fixture intentionally carries only installation actions; give
    # both identities a real scoped action reference before testing account policy.
    if actor.credential.kind == "service":
        from tests.cms._warm_pool_fixtures import service_credential

        credential = service_credential(settings, scopes=["authorization:account.read_content"])
        actor.client.force_authenticate(user=None, token=credential)
    response = actor.client.get("/api/v1/cms/catalog/", {"scope_kind": "account", "account": own.uuid})
    assert response.status_code == 200, response.data
    by_id = {row["id"]: row for row in response.data}
    assert f"synthetic-private-{own.id}" in by_id
    assert f"synthetic-private-{sibling.id}" not in by_id
    assert by_id[actor.source.scenario_id]["raes"]["package_ref"] == ""
    assert actor.client.get(f"/api/v1/cms/catalog/synthetic-private-{sibling.id}/").status_code == 404
    assert (
        actor.client.get("/api/v1/cms/catalog/", {"scope_kind": "installation", "account": own.uuid}).status_code == 400
    )
