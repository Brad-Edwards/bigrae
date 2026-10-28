"""A deferred native generation retains its first exact proof before the outbox exists."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.cms.test_runtime_plugins import tenant
from tests.engine.services.test_raes_range import make_compiled_plan

__all__ = ["tenant"]
pytestmark = pytest.mark.django_db


@pytest.fixture(params=["human", "service"])
def deferred_creation(tenant, live_session_credential, settings, request):
    from cms.models import RangeInstance
    from cms.models import Request as CmsRequest
    from engine.models import Range
    from engine.services import RangeBindings, create_raes_range
    from shared.operation_authority import credential_reference_fields
    from tests.cms._warm_pool_fixtures import service_credential
    from workspaces.services import add_account_member, hierarchy_target_scope, resolve_resource_scope

    actor, organization = tenant
    credential = actor.credential
    if request.param == "service":
        credential = service_credential(settings, scopes=["authorization:organization.launch_range"])
        actor.policy_principals.add(credential.principal.uuid)
    add_account_member(organization.account.uuid, credential.principal)
    settings.CLOUD_PROVIDER = "aws"
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    scope = hierarchy_target_scope("organization", organization.uuid)
    resolved = resolve_resource_scope(scope)
    request_uuid = uuid4()
    cms_request = CmsRequest.objects.create(
        request_id=request_uuid,
        request_type="range",
        user=actor if request.param == "human" else None,
        owner_principal_uuid=credential.principal.uuid,
        scope_kind="account",
        account_id=resolved.account_id,
        organization_id=resolved.organization_id,
        **credential_reference_fields(credential),
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        user_id=cms_request.user_id,
        scenario_id="synthetic-deferred",
        status="provisioning",
        owner_principal_uuid=credential.principal.uuid,
        scope_kind="account",
        account_id=resolved.account_id,
        organization_id=resolved.organization_id,
    )
    plan = make_compiled_plan()
    bindings = RangeBindings(
        credential=credential, resource_scope=scope, cms_range_uuid=instance.uuid, defer_dispatch=True
    )
    create_raes_range(
        request_id=request_uuid,
        user_id=cms_request.user_id,
        compiled_plan=plan,
        workspace_id=None,
        egress_mode="none",
        bindings=bindings,
    )
    runtime = Range.objects.get(request__request_id=request_uuid)
    instance.range_id = runtime.pk
    instance.save(update_fields=["range_id"])
    if request.param == "human":
        alternate = live_session_credential(actor)
    else:
        from tests._credential_helpers import persisted_credential

        alternate = persisted_credential(replace(credential, credential_uuid=uuid4()))
    assert alternate.principal == credential.principal and alternate.credential_uuid != credential.credential_uuid
    return SimpleNamespace(
        credential=credential,
        alternate=alternate,
        request_uuid=request_uuid,
        instance=instance,
        runtime=runtime,
        plan=plan,
        bindings=bindings,
    )


def test_deferred_native_replay_rejects_a_second_same_principal_proof(deferred_creation):
    from engine.models import ProvisionerLaunchIntent, Range
    from engine.services import create_raes_range

    case = deferred_creation
    with pytest.raises((PermissionError, ValueError), match=r"authority|proof|origin"):
        create_raes_range(
            request_id=case.request_uuid,
            user_id=case.instance.user_id,
            compiled_plan=case.plan,
            workspace_id=None,
            egress_mode="none",
            bindings=replace(case.bindings, credential=case.alternate),
        )
    assert Range.objects.count() == 1 and not ProvisionerLaunchIntent.objects.exists()


def test_first_dispatch_rejects_a_second_same_principal_proof(deferred_creation):
    from engine.models import ProvisionerLaunchIntent
    from engine.services import dispatch_created_raes_range

    case = deferred_creation
    with pytest.raises((PermissionError, ValueError), match=r"authority|proof|origin"):
        dispatch_created_raes_range(case.request_uuid, credential=case.alternate)
    assert not ProvisionerLaunchIntent.objects.exists()
    assert dispatch_created_raes_range(case.request_uuid, credential=case.credential).accepted
    intent = ProvisionerLaunchIntent.objects.get()
    assert intent.credential_uuid == case.credential.credential_uuid


def test_creation_receipt_from_another_generation_cannot_admit_replay_or_dispatch(deferred_creation):
    from engine.models import ProvisionerLaunchIntent, Range
    from engine.services import create_raes_range, dispatch_created_raes_range

    case = deferred_creation
    # Native receipts are append-only on PostgreSQL. A subsequent runtime
    # generation must never borrow the retained original generation's receipt.
    Range.objects.filter(pk=case.runtime.pk).update(resource_generation=uuid4())
    with pytest.raises((PermissionError, ValueError), match=r"authority|proof|origin"):
        create_raes_range(
            request_id=case.request_uuid,
            user_id=case.instance.user_id,
            compiled_plan=case.plan,
            workspace_id=None,
            egress_mode="none",
            bindings=case.bindings,
        )
    with pytest.raises((PermissionError, ValueError), match=r"authority|proof|origin"):
        dispatch_created_raes_range(case.request_uuid, credential=case.credential)
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("deferred_creation", ["human"], indirect=True)
def test_prepared_dispatch_locks_ancestry_only_inside_its_real_transaction(deferred_creation):
    from django.db import connection

    from engine.models import ProvisionerLaunchIntent
    from engine.services import dispatch_created_raes_range, prepare_raes_range_creation

    case = deferred_creation
    assert not connection.in_atomic_block
    prepared = prepare_raes_range_creation(case.bindings, None, case.instance.user_id, case.plan)
    assert not connection.in_atomic_block
    assert dispatch_created_raes_range(
        case.request_uuid, credential=case.credential, prepared_creation=prepared
    ).accepted
    assert ProvisionerLaunchIntent.objects.get().credential_uuid == case.credential.credential_uuid


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("deferred_creation", ["human"], indirect=True)
@pytest.mark.parametrize("replay", [False, True])
def test_prepared_dispatch_locks_actual_ancestry_before_native_range(deferred_creation, replay):
    """Observe public dispatch SQL directly, without a fixture prelocking ancestry."""
    from django.db import connection

    from engine.models import ProvisionerLaunchIntent, Range
    from engine.services import dispatch_created_raes_range, prepare_raes_range_creation

    case = deferred_creation
    prepared = prepare_raes_range_creation(case.bindings, None, case.instance.user_id, case.plan)
    if replay:
        dispatch_created_raes_range(case.request_uuid, credential=case.credential, prepared_creation=prepared)
    assert not connection.in_atomic_block
    locks = []

    def observe(execute, sql, params, many, context):
        if "FOR UPDATE" in sql.upper():
            if 'FROM "workspaces_account"' in sql:
                locks.append("account")
            if f'FROM "{Range._meta.db_table}"' in sql:
                locks.append("range")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe):
        assert dispatch_created_raes_range(
            case.request_uuid, credential=case.credential, prepared_creation=prepared
        ).accepted
    assert "account" in locks and "range" in locks, locks
    assert locks.index("account") < locks.index("range"), locks
    assert ProvisionerLaunchIntent.objects.count() == 1


def test_first_dispatch_cannot_commit_a_proof_revoked_after_live_admission(deferred_creation, monkeypatch):
    from engine.models import ProvisionerLaunchIntent
    from engine.services import _launch_authority, dispatch_created_raes_range
    from management.models import PrincipalSession, ServiceCredentialAdmission

    case = deferred_creation
    original = _launch_authority.prepare_launch_authority

    def admit_then_revoke(*args, **kwargs):
        authority = original(*args, **kwargs)
        if case.credential.kind == "session":
            PrincipalSession.objects.filter(credential_uuid=case.credential.credential_uuid).delete()
        else:
            ServiceCredentialAdmission.objects.filter(uuid=case.credential.credential_uuid).update(is_active=False)
        return authority

    monkeypatch.setattr(_launch_authority, "prepare_launch_authority", admit_then_revoke)
    with pytest.raises((ValueError, PermissionError)):
        dispatch_created_raes_range(case.request_uuid, credential=case.credential)
    assert not ProvisionerLaunchIntent.objects.exists()
