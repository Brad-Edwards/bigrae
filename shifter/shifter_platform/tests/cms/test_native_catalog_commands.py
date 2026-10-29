"""Native catalog commands authenticate real proof and retain recoverable native rows."""

import pytest
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from cms.services import register_pack
from shared.api_tokens.cli import CredentialFileError, credential_from_file
from shared.authorization import AuthorizationDecision, DecisionKind
from shared.identity_scope import ResourceScope
from tests.cms._native_catalog import native_catalog as native_catalog
from tests.cms.test_content_ingestion import _request
from tests.cms.test_content_ingestion import repo_pack as repo_pack
from tests.cms.test_register_pack_command import _args

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("superuser", [False, True])
def test_staff_flags_cannot_register_unscoped_content(django_user_model, repo_pack, superuser):
    user = django_user_model.objects.create_user(username="legacy-catalog-actor", is_staff=True, is_superuser=superuser)
    with pytest.raises(CMSError):
        register_pack(user=user, request=_request(repo_pack), scope=ResourceScope("installation"))


@pytest.mark.parametrize("mode", ["real", "reference", "public", "symlink", "missing", "large"])
def test_private_file_uses_the_existing_bearer_authenticator(native_catalog, tmp_path, mode):
    path = native_catalog.path
    if mode == "reference":
        path.write_text(str(native_catalog.credential.credential_uuid))
    elif mode == "public":
        path.chmod(0o644)
    elif mode == "symlink":
        path = tmp_path / "linked-proof"
        path.symlink_to(native_catalog.path)
    elif mode == "missing":
        path = tmp_path / "absent-proof"
    elif mode == "large":
        path.write_text("private-proof-canary" * 2000)
    if mode == "real":
        assert credential_from_file(path) == native_catalog.credential
    else:
        with pytest.raises(CredentialFileError) as failure:
            credential_from_file(path)
        assert "private-proof-canary" not in str(failure.value)
        assert str(path) not in str(failure.value)


@pytest.mark.parametrize("failure", ["denied", "revoked", "publication_denied"])
def test_bootstrap_requires_current_native_authority(native_catalog, shipped_pack_root, monkeypatch, failure):
    if failure == "revoked":
        native_catalog.token.revoke()
    else:
        check = native_catalog.provider.check
        denied = "installation.publish_content" if failure == "publication_denied" else "installation.manage_content"
        monkeypatch.setattr(
            native_catalog.provider,
            "check",
            lambda request: (
                AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
                if request.action == denied
                else check(request)
            ),
        )
    with pytest.raises(CommandError):
        call_command("bootstrap_inbox_catalog", credential_file=str(native_catalog.path))
    assert not RaesPackageSource.objects.exists()


def test_bootstrap_never_evaluates_policy_inside_its_registration_transaction(
    native_catalog,
    shipped_pack_root,
    monkeypatch,
):
    check = native_catalog.provider.check

    def outside_sql(request):
        assert not connection.in_atomic_block
        return check(request)

    monkeypatch.setattr(native_catalog.provider, "check", outside_sql)
    call_command("bootstrap_inbox_catalog", credential_file=str(native_catalog.path))
    sources = list(RaesPackageSource.objects.all())
    assert len(sources) == 2
    assert all(source.scope_kind == "installation" and source.is_operator_published for source in sources)
    assert {source.owner_principal_uuid for source in sources} == {native_catalog.credential.principal.uuid}


def test_qualification_storage_failure_retains_pending_native_rows_for_retry(
    native_catalog,
    shipped_pack_root,
    monkeypatch,
):
    from django.db.backends.utils import CursorWrapper

    execute = CursorWrapper.execute

    def unavailable(cursor, sql, params=None):
        if f'UPDATE "{RaesPackageSource._meta.db_table}"' in sql and '"conformance_status"' in sql:
            raise DatabaseError("Synthetic qualification storage failure")
        return execute(cursor, sql, params)

    monkeypatch.setattr(CursorWrapper, "execute", unavailable)
    with pytest.raises(CommandError):
        call_command("bootstrap_inbox_catalog", credential_file=str(native_catalog.path))
    pending = list(RaesPackageSource.objects.all())
    identities = {row.pk for row in pending}
    assert len(pending) == 2
    assert all(row.conformance_status == "pending" and not row.is_operator_published for row in pending)
    monkeypatch.setattr(CursorWrapper, "execute", execute)
    call_command("bootstrap_inbox_catalog", credential_file=str(native_catalog.path))
    assert set(RaesPackageSource.objects.values_list("pk", flat=True)) == identities
    assert all(
        row.conformance_status == "passed" and row.is_operator_published for row in RaesPackageSource.objects.all()
    )


def test_revocation_after_actual_compilation_cannot_qualify_or_publish(native_catalog, shipped_pack_root, monkeypatch):
    from cms.services import _pack_conformance

    validate = _pack_conformance.validate_pack_contract

    def revoked(path, report):
        validate(path, report)
        native_catalog.token.revoke()

    monkeypatch.setattr(_pack_conformance, "validate_pack_contract", revoked)
    with pytest.raises(CommandError):
        call_command("bootstrap_inbox_catalog", credential_file=str(native_catalog.path))
    assert RaesPackageSource.objects.count() == 2
    assert not RaesPackageSource.objects.exclude(conformance_status="pending", is_operator_published=False).exists()


def test_catalog_publication_cannot_publish_a_replaced_digest(native_catalog, repo_pack, monkeypatch):
    from cms.services import set_catalog_publication

    request = _request(repo_pack)
    register_pack(user=native_catalog.credential, request=request, scope=native_catalog.scope)
    check = native_catalog.provider.check

    def replace_during_policy(decision_request):
        decision = check(decision_request)
        if decision_request.action == "installation.publish_content":
            RaesPackageSource.objects.filter(scenario_id=request.scenario_id).update(
                package_digest="sha256:" + "b" * 64
            )
        return decision

    monkeypatch.setattr(native_catalog.provider, "check", replace_during_policy)
    with pytest.raises(CMSError):
        set_catalog_publication(
            native_catalog.credential,
            request.scenario_id,
            published=True,
            expected_package_digest=request.package_digest,
        )
    assert not RaesPackageSource.objects.get().is_operator_published


@pytest.mark.parametrize("kind", ["installation", "account", "organization", "workspace"])
def test_register_command_resolves_every_explicit_native_parent(native_catalog, repo_pack, kind):
    from shared.api_tokens.models import ApiToken
    from workspaces.models import Account, Organization, Workspace

    account = Account.objects.create(kind="team", name="Synthetic content customer")
    organization = Organization.objects.create(account=account, name="Synthetic content organization")
    workspace = Workspace.objects.create(organization=organization, name="Synthetic content workspace")
    scoped_action = "installation" if kind == "installation" else kind
    token, raw = ApiToken.create_token(
        name="Synthetic scoped import",
        created_by=native_catalog.user,
        scopes=["authorization:installation.manage_content", f"authorization:{scoped_action}.manage_content"],
    )
    native_catalog.path.write_text(raw)
    arguments = _args(
        repo_pack, native_catalog.path, **{"--scope-kind": kind, "--scenario-id": _request(repo_pack).scenario_id}
    )
    parents = {"account": account, "organization": organization, "workspace": workspace}
    if kind in parents:
        arguments.extend([f"--{kind}", str(parents[kind].uuid)])
    call_command("register_pack", *arguments)
    source = RaesPackageSource.objects.get()
    assert source.owner_principal_uuid == token.principal_uuid
    assert source.scope_kind == ("installation" if kind == "installation" else "account")
    assert source.account_uuid == (None if kind == "installation" else account.uuid)
    assert source.organization_uuid == (organization.uuid if kind in {"organization", "workspace"} else None)
    assert source.workspace_uuid == (workspace.uuid if kind == "workspace" else None)


@pytest.mark.parametrize(
    "kind,parents",
    [
        ("installation", ["--account", "00000000-0000-4000-8000-000000000001"]),
        ("account", []),
        ("organization", ["--workspace", "00000000-0000-4000-8000-000000000001"]),
    ],
)
def test_register_command_rejects_missing_or_ambiguous_parent(native_catalog, repo_pack, kind, parents):
    arguments = _args(repo_pack, native_catalog.path, **{"--scope-kind": kind})
    with pytest.raises(CommandError):
        call_command("register_pack", *(arguments + parents))
    assert not RaesPackageSource.objects.exists()


def test_conformance_command_uses_the_same_native_proof_adapter(native_catalog, repo_pack):
    request = _request(repo_pack)
    register_pack(user=native_catalog.credential, request=request, scope=native_catalog.scope)
    call_command(
        "validate_pack_conformance",
        credential_file=str(native_catalog.path),
        scenario_id=request.scenario_id,
        expected_package_digest=request.package_digest,
    )
    assert RaesPackageSource.objects.get().conformance_status == "passed"
    native_catalog.token.revoke()
    with pytest.raises(CommandError):
        call_command(
            "validate_pack_conformance",
            credential_file=str(native_catalog.path),
            scenario_id=request.scenario_id,
            expected_package_digest=request.package_digest,
        )
