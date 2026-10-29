"""Native credential custody preserves encryption, projections and lifecycle behavior."""

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone

from cms import services
from cms.credential_encryption import ENCRYPTED_VALUE_PREFIX
from cms.exceptions import CMSError
from cms.models import Credential, CredentialType
from shared.authorization import AuthorizationDecision, DecisionKind, port
from shared.identity_scope import ResourceScope
from shared.schemas import CredentialRef, DeploymentProfileContext, SCMCredentialContext

pytestmark = pytest.mark.django_db

KINDS = ("scm", "deployment_profile")
DATA = {
    "scm": {
        "scm_folder_name": "TestFolder",
        "scm_pin_id": "pin123",
        "scm_pin_value": "synthetic-pin-secret",
        "sls_region": "americas",
    },
    "deployment_profile": {"authcode": "D76543210"},
}
SECRET_FIELDS = {"scm": "scm_pin_value", "deployment_profile": "authcode"}


@pytest.fixture
def actors(monkeypatch, live_session_credential):
    from management.services import principal_for_user
    from workspaces.services import create_account

    result = []
    for index in range(2):
        user = User.objects.create_user(username=f"credential-owner-{index}")
        principal = principal_for_user(user)
        account = create_account(kind="individual", name=f"Credential scope {index}", owner=principal)
        result.append(
            SimpleNamespace(
                user=user,
                credential=live_session_credential(user),
                scope=ResourceScope("account", account.uuid),
            )
        )
    grants = {actor.credential.principal.uuid: actor.scope for actor in result}

    def check(request):
        allowed = grants.get(request.principal.uuid) == request.scope and request.action in {
            "account.read_content",
            "account.manage_credentials",
        }
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
            "policy_allowed" if allowed else "policy_denied",
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    for kind in KINDS:
        CredentialType.objects.get_or_create(
            slug=kind,
            defaults={"name": kind, "spec_slug": f"credential.{kind}"},
        )
    return result


def create(actor, kind, **overrides):
    values = {**DATA[kind], "name": f"Synthetic {kind}", **overrides}
    return services.create_credential(actor.credential, kind, scope=actor.scope, **values)


def raw_data(identifier):
    with connection.cursor() as cursor:
        cursor.execute("SELECT data FROM cms_credential WHERE id = %s", [identifier])
        value = cursor.fetchone()[0]
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.parametrize("kind", KINDS)
def test_creation_retains_native_owner_scope_and_reference(actors, kind):
    actor = actors[0]
    result = create(actor, kind)
    row = Credential.objects.get(pk=result.credential_id)
    assert isinstance(result, CredentialRef)
    assert result.credential_id > 0 and not result.is_deleted
    assert result.user_id is None and row.user_id is None
    assert result.owner_principal_uuid == row.owner_principal_uuid == actor.credential.principal.uuid
    assert row.scope_kind == "account" and row.account_uuid == actor.scope.account_uuid
    assert row.organization_uuid is None and row.workspace_uuid is None
    assert row.name == f"Synthetic {kind}"
    assert row.data == DATA[kind]


@pytest.mark.parametrize("kind", KINDS)
def test_secret_is_encrypted_at_rest_and_excluded_from_projections(actors, kind):
    actor = actors[0]
    result = create(actor, kind)
    field = SECRET_FIELDS[kind]
    secret = DATA[kind][field]
    stored = raw_data(result.credential_id)
    assert stored[field] != secret
    assert stored[field].startswith(ENCRYPTED_VALUE_PREFIX)
    assert Credential.objects.get(pk=result.credential_id).data[field] == secret
    projection = services.get_credential(actor.credential, result.credential_id)
    assert field not in projection.model_dump()
    assert secret not in projection.model_dump_json()
    assert secret not in result.model_dump_json()


@pytest.mark.parametrize("kind", KINDS)
def test_expiry_is_preserved(actors, kind):
    expiry = timezone.now() + timezone.timedelta(days=30)
    result = create(actors[0], kind, expires_at=expiry)
    assert Credential.objects.get(pk=result.credential_id).expires_at == expiry
    assert services.get_credential(actors[0].credential, result.credential_id).expires_at == expiry


def test_display_projections_preserve_public_fields_and_mask_authcode(actors):
    actor = actors[0]
    refs = {kind: create(actor, kind) for kind in KINDS}
    rows = services.list_credentials(actor.credential, scope=actor.scope)
    assert len(rows) == 2
    scm = next(row for row in rows if isinstance(row, SCMCredentialContext))
    deployment = next(row for row in rows if isinstance(row, DeploymentProfileContext))
    assert scm.credential_id == refs["scm"].credential_id
    assert scm.name == "Synthetic scm" and scm.credential_type == "scm"
    assert scm.scm_folder_name == "TestFolder" and scm.scm_pin_id == "pin123"
    assert scm.sls_region == "americas"
    assert deployment.credential_id == refs["deployment_profile"].credential_id
    assert deployment.authcode_masked == "D7654***"
    assert all(row.user_id is None and row.owner_principal_uuid == actor.credential.principal.uuid for row in rows)


@pytest.mark.parametrize("kind", KINDS)
def test_complete_lifecycle_soft_deletes_without_disclosing_retired_secret(actors, kind):
    actor = actors[0]
    assert services.list_credentials(actor.credential, scope=actor.scope) == []
    result = create(actor, kind)
    assert [row.credential_id for row in services.list_credentials(actor.credential, scope=actor.scope)] == [
        result.credential_id
    ]
    assert services.get_credential(actor.credential, result.credential_id).credential_id == result.credential_id
    retired = services.delete_credential(actor.credential, result.credential_id)
    assert retired.credential_id == result.credential_id and retired.is_deleted
    assert retired.owner_principal_uuid == actor.credential.principal.uuid
    assert Credential.all_objects.get(pk=result.credential_id).deleted_at is not None
    assert services.list_credentials(actor.credential, scope=actor.scope) == []
    for operation in (services.get_credential, services.delete_credential):
        with pytest.raises(CMSError, match="Content unavailable"):
            operation(actor.credential, result.credential_id)


@pytest.mark.parametrize("operation", ["get_credential", "delete_credential"])
def test_other_account_cannot_read_or_retire_credentials(actors, operation):
    result = create(actors[0], "scm")
    with pytest.raises(CMSError, match="Content unavailable"):
        getattr(services, operation)(actors[1].credential, result.credential_id)
    assert Credential.objects.get(pk=result.credential_id).deleted_at is None


def test_list_filters_exact_scope_and_excludes_deleted_rows(actors):
    first, second = actors
    kept = create(first, "scm")
    removed = create(first, "deployment_profile")
    create(second, "deployment_profile")
    services.delete_credential(first.credential, removed.credential_id)
    assert [row.credential_id for row in services.list_credentials(first.credential, scope=first.scope)] == [
        kept.credential_id
    ]
    with pytest.raises(CMSError, match="Content unavailable"):
        services.list_credentials(second.credential, scope=first.scope)


@pytest.mark.parametrize("operation", ["create", "list", "get", "delete"])
@pytest.mark.parametrize("proof", ["none", "string", "unsaved", "staff"])
def test_legacy_user_shapes_never_substitute_for_native_proof(actors, operation, proof):
    target = create(actors[0], "scm")
    values = {"none": None, "string": "not-a-proof", "unsaved": User(username="unsaved"), "staff": actors[0].user}
    actors[0].user.is_staff = True
    actors[0].user.is_superuser = True
    actors[0].user.save(update_fields=["is_staff", "is_superuser"])
    actor = values[proof]
    with pytest.raises(CMSError, match="Content unavailable"):
        if operation == "create":
            services.create_credential(actor, "scm", scope=actors[0].scope, name="Denied", **DATA["scm"])
        elif operation == "list":
            services.list_credentials(actor, scope=actors[0].scope)
        else:
            getattr(services, f"{operation}_credential")(actor, target.credential_id)
    assert Credential.objects.count() == 1
    assert Credential.objects.get(pk=target.credential_id).deleted_at is None


@pytest.mark.parametrize("kind", [None, "unknown_type"])
def test_unknown_credential_type_is_unavailable(actors, kind):
    actor = actors[0]
    with pytest.raises(CMSError, match="Content unavailable"):
        services.create_credential(actor.credential, kind, scope=actor.scope, name="Invalid")
    assert not Credential.objects.exists()


@pytest.mark.parametrize("name", [None, "", "  "])
def test_missing_name_is_rejected_without_persistence(actors, name):
    with pytest.raises(ValueError, match="name is required"):
        create(actors[0], "scm", name=name)
    assert not Credential.objects.exists()


@pytest.mark.parametrize("operation", ["get_credential", "delete_credential"])
@pytest.mark.parametrize("identifier", [None, -1, 999999])
def test_absent_credential_identity_is_opaque(actors, operation, identifier):
    with pytest.raises(CMSError, match="Content unavailable"):
        getattr(services, operation)(actors[0].credential, identifier)


@pytest.mark.parametrize("operation", ["get_credential", "delete_credential"])
def test_noninteger_identity_is_rejected(actors, operation):
    with pytest.raises(ValueError):
        getattr(services, operation)(actors[0].credential, "not_an_integer")
    assert not Credential.objects.exists()
