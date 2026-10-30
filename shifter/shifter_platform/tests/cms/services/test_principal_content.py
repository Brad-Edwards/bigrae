"""Private credential effects and projections work without a fabricated user."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db import connection

from cms.exceptions import CMSError
from shared.authorization import AuthorizationDecision, CredentialCeiling, DecisionKind, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def content_actor(settings, monkeypatch, django_user_model):
    from cms.models import CredentialType
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, principal_for_user, resolve_service_credential
    from shared.authorization import port
    from workspaces.services import create_account

    CredentialType.objects.get_or_create(
        slug="deployment_profile",
        defaults={"name": "Synthetic deployment profile", "spec_slug": "credential.deployment_profile"},
    )
    accounts = [
        create_account(
            kind="individual",
            name=f"Synthetic content {index}",
            owner=principal_for_user(django_user_model.objects.create_user(username=f"content-owner-{index}")),
        )
        for index in range(2)
    ]
    principal = create_service_principal("Content administrator")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid), issuer="https://accounts.google.com", subject=str(uuid4())
    )
    settings.GCP_SERVICE_TOKEN_AUDIENCE = settings.AWS_SERVICE_TOKEN_AUDIENCE = "https://example.test/shifter"
    ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["authorization:account.read_content", "authorization:account.manage_credentials"],
    )
    credential = resolve_service_credential(
        issuer=binding.issuer, subject=binding.subject, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE
    )
    seen = []

    def check(request):
        seen.append(request)
        allowed = request.scope.account_uuid == accounts[0].uuid
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED, "policy_allowed" if allowed else "policy_denied"
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    return SimpleNamespace(
        credential=credential,
        scope=ResourceScope("account", accounts[0].uuid),
        sibling=ResourceScope("account", accounts[1].uuid),
        seen=seen,
    )


@pytest.mark.postgres
def test_service_owned_credential_encrypts_secrets_and_returns_safe_projections(content_actor):
    from cms.models import Credential
    from cms.services import create_credential, delete_credential, get_credential, list_credentials

    fixture = content_actor
    created = create_credential(
        fixture.credential,
        "deployment_profile",
        scope=fixture.scope,
        name="Synthetic credential",
        authcode="SECRET_MARKER_2319",
    )
    row = Credential.objects.get(pk=created.credential_id)
    assert row.user_id is None
    assert row.owner_principal_uuid == fixture.credential.principal.uuid
    assert created.owner_principal_uuid == row.owner_principal_uuid
    with connection.cursor() as cursor:
        cursor.execute("SELECT data FROM cms_credential WHERE id = %s", [row.pk])
        assert "SECRET_MARKER_2319" not in str(cursor.fetchone())
    projection = get_credential(fixture.credential, row.pk)
    assert "SECRET_MARKER_2319" not in projection.model_dump_json()
    assert [item.credential_id for item in list_credentials(fixture.credential, scope=fixture.scope)] == [row.pk]
    assert delete_credential(fixture.credential, row.pk).is_deleted
    assert list_credentials(fixture.credential, scope=fixture.scope) == []


def test_credential_revoked_during_native_validation_cannot_persist_a_secret(content_actor, monkeypatch):
    from cms.models import Credential
    from cms.services import _scoped_credentials, create_credential
    from management.models import ServiceCredentialAdmission

    validate = _scoped_credentials.validate_credential_data

    def validate_and_revoke(*args, **kwargs):
        result = validate(*args, **kwargs)
        ServiceCredentialAdmission.objects.filter(uuid=content_actor.credential.credential_uuid).update(is_active=False)
        return result

    monkeypatch.setattr(_scoped_credentials, "validate_credential_data", validate_and_revoke)
    with pytest.raises(CMSError):
        create_credential(
            content_actor.credential,
            "deployment_profile",
            scope=content_actor.scope,
            name="Revoked before persistence",
            authcode="SYNTHETIC_SECRET_MARKER",
        )
    assert not Credential.objects.filter(name="Revoked before persistence").exists()


@pytest.mark.postgres
def test_service_credential_names_are_unique_in_their_customer_scope(content_actor):
    from django.db import IntegrityError, transaction

    from cms.models import Credential, CredentialType
    from cms.services import create_credential

    created = create_credential(
        content_actor.credential,
        "deployment_profile",
        scope=content_actor.scope,
        name="Scope credential",
        authcode="synthetic",
    )
    row = Credential.objects.get(pk=created.credential_id)
    fields = {
        "user": None,
        "name": row.name,
        "credential_type": CredentialType.objects.get(slug="deployment_profile"),
        "owner_principal_uuid": row.owner_principal_uuid,
        "scope_kind": row.scope_kind,
        "account_uuid": row.account_uuid,
        "organization_uuid": row.organization_uuid,
        "workspace_uuid": row.workspace_uuid,
        "data": {"authcode": "synthetic"},
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        Credential.objects.create(**fields)
    # The same service may use the same display name in a different customer.
    other = Credential.objects.create(**dict(fields, account_uuid=content_actor.sibling.account_uuid))
    assert other.pk != row.pk


def test_active_scope_administrator_can_remove_an_inactive_owners_credential(content_actor):
    from cms.models import Credential
    from cms.services import create_credential, delete_credential
    from management.models import Principal
    from management.services import create_service_principal

    created = create_credential(
        content_actor.credential,
        "deployment_profile",
        scope=content_actor.scope,
        name="Offboarding credential",
        authcode="synthetic",
    )
    owner = create_service_principal("Synthetic departing owner")
    Credential.objects.filter(pk=created.credential_id).update(owner_principal_uuid=owner.uuid)
    Principal.objects.filter(uuid=owner.uuid).update(is_active=False)
    assert delete_credential(content_actor.credential, created.credential_id).is_deleted


@pytest.mark.parametrize(
    "values",
    [
        {"authcode": "valid", "unexpected_secret": "UNKNOWN_SECRET_MARKER"},
        {},
        {"authcode": {"nested": "SECRET_MARKER"}},
    ],
)
def test_principal_credential_input_uses_the_closed_native_spec(content_actor, values):
    from cms.models import Credential
    from cms.services import create_credential

    with pytest.raises((CMSError, ValueError)):
        create_credential(
            content_actor.credential,
            "deployment_profile",
            scope=content_actor.scope,
            name="Invalid credential",
            **values,
        )
    assert not Credential.objects.filter(name="Invalid credential").exists()


def test_cross_account_and_exact_ceiling_deny_before_private_projection(content_actor):
    from cms.models import Credential, CredentialType
    from cms.services import create_credential, get_credential, list_credentials

    fixture = content_actor
    foreign = Credential.objects.create(
        user=None,
        owner_principal_uuid=fixture.credential.principal.uuid,
        scope_kind="account",
        account_uuid=fixture.sibling.account_uuid,
        name="Foreign private asset",
        credential_type=CredentialType.objects.get(slug="deployment_profile"),
        data={"authcode": "FOREIGN_SECRET_MARKER"},
    )
    with pytest.raises(CMSError):
        get_credential(fixture.credential, foreign.pk)
    with pytest.raises(CMSError):
        list_credentials(fixture.credential, scope=fixture.sibling)
    assert list_credentials(fixture.credential, scope=fixture.scope) == []
    exact = CredentialContext(
        fixture.credential.principal,
        fixture.credential.kind,
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"account.read_content"}), TargetRef("account", fixture.scope.account_uuid)),
        fixture.credential.scopes,
    )
    with pytest.raises(CMSError):
        create_credential(exact, "deployment_profile", scope=fixture.scope, name="No write", authcode="secret")
    assert not Credential.objects.filter(name="No write").exists()


@pytest.mark.parametrize(
    "values",
    [
        {"authcode": "valid", "unexpected_secret": "UNKNOWN_SECRET_MARKER"},
        {},
        {"authcode": {"nested": "SECRET_MARKER"}},
    ],
)
def test_human_credential_input_uses_the_same_closed_spec(
    content_actor, django_user_model, live_session_credential, values, caplog
):
    from cms.models import Credential
    from cms.services import create_credential

    user = django_user_model.objects.create_user(username="human-credential-validator")
    credential = live_session_credential(user)
    with pytest.raises(CMSError, match="Invalid credential input") as caught:
        create_credential(
            credential,
            "deployment_profile",
            scope=content_actor.scope,
            name="Invalid human credential",
            **values,
        )
    assert not Credential.objects.filter(name="Invalid human credential").exists()
    assert "SECRET_MARKER" not in str(caught.value)
    assert "SECRET_MARKER" not in caplog.text


def test_native_duplicate_credential_name_is_refused_without_an_error_log(content_actor, caplog):
    from django.core.exceptions import ValidationError

    from cms.models import Credential
    from cms.services import create_credential

    fixture = content_actor
    values = {"scope": fixture.scope, "name": "Duplicate native credential", "authcode": "valid"}
    create_credential(fixture.credential, "deployment_profile", **values)
    with pytest.raises(ValidationError):
        create_credential(fixture.credential, "deployment_profile", **values)
    assert Credential.objects.filter(name=values["name"]).count() == 1
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_catalog_filters_customer_rows_and_requires_explicit_operator_publication(content_actor, django_user_model):
    from cms.models import RaesPackageSource
    from cms.scenarios.registry import get_catalog_entry, list_all_scenarios
    from cms.services import get_scenario, list_launchable_scenarios, list_scenarios

    fixture = content_actor
    human = django_user_model.objects.create_user(username="historical-catalog-registrar")

    def source(identifier, **fields):
        return RaesPackageSource.objects.create(
            scenario_id=identifier,
            registered_by=human,
            source_kind="repo",
            contract_kind="raes",
            contract_profile="shifter",
            package_ref="packs/synthetic",
            package_version="1",
            package_digest="sha256:" + "1" * 64,
            conformance_status="passed",
            **fields,
        )

    source(
        "synthetic-own",
        scope_kind="account",
        account_uuid=fixture.scope.account_uuid,
        owner_principal_uuid=fixture.credential.principal.uuid,
    )
    source(
        "synthetic-foreign",
        scope_kind="account",
        account_uuid=fixture.sibling.account_uuid,
        owner_principal_uuid=fixture.credential.principal.uuid,
        is_operator_published=True,
    )
    source(
        "synthetic-shared",
        scope_kind="installation",
        owner_principal_uuid=fixture.credential.principal.uuid,
        is_operator_published=True,
    )
    source("synthetic-unpublished", scope_kind="installation", owner_principal_uuid=fixture.credential.principal.uuid)
    source("synthetic-legacy-unscoped")
    entries = list_all_scenarios(fixture.credential, scope=fixture.scope, include_unavailable=True)
    assert {entry["id"] for entry in entries} == {"synthetic-own", "synthetic-shared"}
    assert "packs/synthetic" not in repr(entries)
    assert get_catalog_entry("synthetic-foreign", user=fixture.credential, scope=fixture.scope) is None
    assert {entry["id"] for entry in list_scenarios(fixture.credential, scope=fixture.scope)} == {
        "synthetic-own",
        "synthetic-shared",
    }
    assert get_scenario("synthetic-own", credential=fixture.credential, scope=fixture.scope)["id"] == "synthetic-own"
    assert list_launchable_scenarios(fixture.credential, scope=fixture.scope) == [
        entry for entry in entries if entry["launchable"]
    ]
    with pytest.raises(CMSError):
        get_scenario("synthetic-foreign", credential=fixture.credential, scope=fixture.scope)
    with pytest.raises(CMSError):
        list_all_scenarios(fixture.credential, scope=fixture.sibling)


def test_credential_historical_user_does_not_bypass_private_scope(content_actor, django_user_model):
    from cms.models import Credential
    from cms.services import create_credential, get_credential, list_credentials

    created = create_credential(
        content_actor.credential,
        "deployment_profile",
        scope=content_actor.scope,
        name="Synthetic private credential",
        authcode="synthetic",
    )
    user = django_user_model.objects.create_user(username="synthetic-historical-credential-owner")
    Credential.objects.filter(pk=created.credential_id).update(user=user)
    with pytest.raises(CMSError, match="Content unavailable"):
        list_credentials(user)
    with pytest.raises(CMSError):
        get_credential(user, created.credential_id)
