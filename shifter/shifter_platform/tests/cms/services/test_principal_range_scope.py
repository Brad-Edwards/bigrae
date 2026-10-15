"""Scope moves authorize both persisted parents and fence all range projections."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cms.exceptions import CMSError
from shared.authorization import AuthorizationDecision, DecisionKind, port
from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["principal_range"]
pytestmark = [pytest.mark.django_db]


@pytest.fixture
def transferable_range(principal_range, monkeypatch):
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from workspaces.services import (
        add_account_member,
        create_account,
        create_account_organization,
        create_account_workspace,
    )

    fixture = principal_range
    admission = ServiceCredentialAdmission.objects.select_related("binding").get(
        uuid=fixture.credential.credential_uuid
    )
    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=admission.audience,
        scopes=[
            *admission.scopes,
            "authorization:account.manage_range_scope",
            "authorization:workspace.manage_range_scope",
        ],
    )
    fixture.credential = resolve_service_credential(
        issuer=binding.issuer, subject=binding.subject, audience=admission.audience
    )
    account = create_account(kind="team", name="Synthetic destination")
    organization = create_account_organization(account.uuid, "Synthetic organization")
    workspace = create_account_workspace(account.uuid, organization.uuid, "Synthetic workspace")
    add_account_member(account.uuid, fixture.owner)
    fixture.destination = SimpleNamespace(account=account, organization=organization, workspace=workspace)
    fixture.seen.clear()

    def check(request):
        fixture.seen.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    return fixture


def test_service_administrator_moves_all_projections_after_both_parent_decisions(transferable_range):
    from cms.services import RangeScopeAuditContext, list_range_scope_bindings, rebind_range_workspace
    from shared.models import AuditLog

    fixture = transferable_range
    result = rebind_range_workspace(
        fixture.credential,
        request_id=fixture.request.request_id,
        target_workspace_uuid=fixture.destination.workspace.uuid,
        audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
    )
    assert result.changed
    fixture.instance.refresh_from_db()
    fixture.request.refresh_from_db()
    fixture.realized.refresh_from_db()
    for row in (fixture.instance, fixture.request, fixture.realized):
        assert row.account_id == fixture.destination.account.id
        assert row.organization_id == fixture.destination.organization.id
        assert row.workspace_id == fixture.destination.workspace.id
        assert row.owner_principal_uuid == fixture.owner.uuid
    assert {request.scope.account_uuid for request in fixture.seen} == {
        fixture.account.uuid,
        fixture.destination.account.uuid,
    }
    assert AuditLog.objects.filter(actor_principal_uuid=fixture.credential.principal.uuid, action="update").exists()
    assert list(list_range_scope_bindings(fixture.credential, workspace_uuid=fixture.destination.workspace.uuid)) == [
        fixture.instance
    ]


def test_destination_denial_cannot_partially_move_source_range(transferable_range, monkeypatch):
    from cms.services import RangeScopeAuditContext, rebind_range_workspace

    fixture = transferable_range

    def check(request):
        return AuthorizationDecision(
            DecisionKind.ALLOWED if request.scope.account_uuid == fixture.account.uuid else DecisionKind.DENIED,
            "synthetic_policy",
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    with pytest.raises(CMSError):
        rebind_range_workspace(
            fixture.credential,
            request_id=fixture.request.request_id,
            target_workspace_uuid=fixture.destination.workspace.uuid,
            audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
        )
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.account_id == fixture.realized.account_id == fixture.account.id
    assert fixture.instance.workspace_id is None


def test_scope_move_cannot_bypass_destination_concurrent_range_quota(transferable_range):
    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from workspaces.models import WorkspaceQuotaDecision, WorkspaceQuotaPolicy

    fixture = transferable_range
    WorkspaceQuotaPolicy.objects.create(
        workspace_id=fixture.destination.workspace.id, resource="concurrent_ranges", limit=0, mode="enforcing"
    )
    with pytest.raises(CMSError):
        rebind_range_workspace(
            fixture.credential,
            request_id=fixture.request.request_id,
            target_workspace_uuid=fixture.destination.workspace.uuid,
            audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
        )
    fixture.instance.refresh_from_db()
    assert fixture.instance.account_id == fixture.account.id
    assert WorkspaceQuotaDecision.objects.get().actor_principal_uuid == fixture.credential.principal.uuid


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgres
def test_concurrent_launch_quota_admission_and_scope_move_use_account_before_workspace(transferable_range):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections, connection, connections, transaction

    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from workspaces.models import WorkspaceQuotaReservation
    from workspaces.services import (
        WorkspaceQuotaAuditContext,
        hierarchy_target_scope,
        reserve_workspace_concurrent_range,
        resolve_range_scope_binding,
    )

    fixture = transferable_range
    target = hierarchy_target_scope("workspace", fixture.destination.workspace.uuid)
    launch_has_account, move_needs_account, launch_can_reserve = Event(), Event(), Event()
    launch_id = uuid4()

    def launch_admission():
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '4s'")
                resolve_range_scope_binding(target, member=fixture.owner, for_update=True)
                launch_has_account.set()
                assert launch_can_reserve.wait(8)
                reserve_workspace_concurrent_range(
                    fixture.destination.workspace.id,
                    launch_id,
                    WorkspaceQuotaAuditContext(
                        actor_type="service", actor_id=None, actor_principal_uuid=fixture.owner.uuid
                    ),
                )
        finally:
            connections.close_all()

    def transfer():
        close_old_connections()

        def observe_account(execute, sql, params, many, context):
            if '"workspaces_account"' in sql and "FOR UPDATE" in sql:
                move_needs_account.set()
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(observe_account):
                return rebind_range_workspace(
                    fixture.credential,
                    request_id=fixture.request.request_id,
                    target_workspace_uuid=fixture.destination.workspace.uuid,
                    audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
                )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        launched = pool.submit(launch_admission)
        try:
            assert launch_has_account.wait(8)
            moved = pool.submit(transfer)
            assert move_needs_account.wait(8)
        finally:
            launch_can_reserve.set()
        launched.result(timeout=10)
        assert moved.result(timeout=10).changed
    assert (
        WorkspaceQuotaReservation.objects.filter(
            workspace_id=fixture.destination.workspace.id,
            released_at__isnull=True,
            correlation_key__in=[str(launch_id), str(fixture.request.request_id)],
        ).count()
        == 2
    )


def test_moving_back_to_a_previous_workspace_rechecks_quota(transferable_range):
    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from workspaces.models import WorkspaceQuotaPolicy
    from workspaces.services import create_account_workspace

    fixture = transferable_range
    second = create_account_workspace(
        fixture.destination.account.uuid, fixture.destination.organization.uuid, "Synthetic second workspace"
    )
    audit = RangeScopeAuditContext(actor_type="service", actor_id=None)
    for workspace in (fixture.destination.workspace, second):
        rebind_range_workspace(
            fixture.credential,
            request_id=fixture.request.request_id,
            target_workspace_uuid=workspace.uuid,
            audit=audit,
        )
    WorkspaceQuotaPolicy.objects.create(
        workspace_id=fixture.destination.workspace.id, resource="concurrent_ranges", limit=0, mode="enforcing"
    )
    with pytest.raises(CMSError):
        rebind_range_workspace(
            fixture.credential,
            request_id=fixture.request.request_id,
            target_workspace_uuid=fixture.destination.workspace.uuid,
            audit=audit,
        )
    fixture.instance.refresh_from_db()
    assert fixture.instance.workspace_id == second.id


def test_scope_move_audit_failure_rolls_back_all_projections(transferable_range, monkeypatch):
    from cms.services import RangeScopeAuditContext, rebind_range_workspace

    fixture = transferable_range

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("cms.services._scoped_range_scope.audit_log", unavailable)
    with pytest.raises(CMSError):
        rebind_range_workspace(
            fixture.credential,
            request_id=fixture.request.request_id,
            target_workspace_uuid=fixture.destination.workspace.uuid,
            audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
        )
    fixture.instance.refresh_from_db()
    fixture.request.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert all(row.account_id == fixture.account.id for row in (fixture.instance, fixture.request, fixture.realized))


def test_scope_move_cannot_strand_an_owner_outside_destination(transferable_range):
    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from workspaces.models import AccountMembership

    fixture = transferable_range
    AccountMembership.objects.filter(
        account_id=fixture.destination.account.id, principal_uuid=fixture.owner.uuid
    ).delete()
    with pytest.raises(CMSError):
        rebind_range_workspace(
            fixture.credential,
            request_id=fixture.request.request_id,
            target_workspace_uuid=fixture.destination.workspace.uuid,
            audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
        )
    fixture.instance.refresh_from_db()
    assert fixture.instance.account_id == fixture.account.id


def test_native_scope_admission_independently_requires_destination_owner_membership(transferable_range):
    from engine.services import prepare_range_scope_move
    from workspaces.models import AccountMembership
    from workspaces.services import hierarchy_target_scope

    fixture = transferable_range
    AccountMembership.objects.filter(
        account_id=fixture.destination.account.id, principal_uuid=fixture.owner.uuid
    ).delete()
    target = hierarchy_target_scope("workspace", fixture.destination.workspace.uuid)
    with pytest.raises(ValueError):
        prepare_range_scope_move(fixture.credential, fixture.request.request_id, target)


def test_service_administrator_reassigns_business_owner_without_fabricating_user(transferable_range):
    from cms.services import reassign_range_owner
    from management.services import create_service_principal
    from workspaces.services import add_account_member

    fixture = transferable_range
    new_owner = create_service_principal("Synthetic replacement owner")
    add_account_member(fixture.account.uuid, new_owner)
    reassign_range_owner(fixture.instance.pk, new_owner, credential=fixture.credential)
    fixture.instance.refresh_from_db()
    fixture.request.refresh_from_db()
    fixture.realized.refresh_from_db()
    fixture.realized.request.refresh_from_db()
    for row in (fixture.instance, fixture.request, fixture.realized, fixture.realized.request):
        assert row.owner_principal_uuid == new_owner.uuid
    assert fixture.instance.user_id is None
    assert fixture.realized.user_id is None
    assert fixture.instance.account_id == fixture.account.id


def test_ownership_audit_failure_rolls_back_all_projections(transferable_range, monkeypatch):
    from cms.services import reassign_range_owner
    from management.services import create_service_principal
    from workspaces.services import add_account_member

    fixture = transferable_range
    new_owner = create_service_principal("Synthetic replacement owner")
    add_account_member(fixture.account.uuid, new_owner)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("cms.services._scoped_range_owner.audit_log", unavailable)
    with pytest.raises(CMSError):
        reassign_range_owner(fixture.instance.pk, new_owner, credential=fixture.credential)
    fixture.instance.refresh_from_db()
    fixture.request.refresh_from_db()
    fixture.realized.refresh_from_db()
    fixture.realized.request.refresh_from_db()
    assert all(
        row.owner_principal_uuid == fixture.owner.uuid
        for row in (fixture.instance, fixture.request, fixture.realized, fixture.realized.request)
    )


def test_anonymous_native_owner_reassignment_cannot_mutate_principal_range(principal_range, django_user_model):
    from engine.services import reassign_range_owner_by_request

    user = django_user_model.objects.create_user(username="synthetic-unrelated-contact")
    with pytest.raises(PermissionError):
        reassign_range_owner_by_request(principal_range.request.request_id, user)
    principal_range.realized.refresh_from_db()
    assert principal_range.realized.user_id is None


def test_ambient_native_workspace_rebind_cannot_rehome_principal_range(transferable_range):
    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from engine.services import rebind_range_workspace_by_request
    from workspaces.services import create_account_workspace

    fixture = transferable_range
    rebind_range_workspace(
        fixture.credential,
        request_id=fixture.request.request_id,
        target_workspace_uuid=fixture.destination.workspace.uuid,
        audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
    )
    other = create_account_workspace(
        fixture.destination.account.uuid, fixture.destination.organization.uuid, "Synthetic second workspace"
    )

    with pytest.raises(PermissionError):
        rebind_range_workspace_by_request(
            fixture.request.request_id,
            expected_workspace_id=fixture.destination.workspace.id,
            new_workspace_id=other.id,
        )
    fixture.realized.refresh_from_db()
    assert fixture.realized.workspace_id == fixture.destination.workspace.id


def test_expiry_cleanup_retains_custody_after_authorized_scope_move(transferable_range, settings):
    from datetime import timedelta

    from django.utils import timezone

    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from cms.services._range_destroy import destroy_expired_range
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    settings.LOCAL_PROVISIONER = None
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    fixture = transferable_range
    rebind_range_workspace(
        fixture.credential,
        request_id=fixture.request.request_id,
        target_workspace_uuid=fixture.destination.workspace.uuid,
        audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
    )
    fixture.instance.refresh_from_db()
    fixture.instance.expires_at = timezone.now() - timedelta(seconds=1)
    fixture.instance.maximum_expires_at = fixture.instance.expires_at + timedelta(days=1)
    fixture.instance.extension_days = 1
    fixture.instance.save(update_fields=["expires_at", "maximum_expires_at", "extension_days"])
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    destroy_expired_range(fixture.instance)
    cleanup = ProvisionerLaunchIntent.objects.get()
    assert cleanup.authority_kind == "cleanup"
    assert cleanup.authority_account_uuid == fixture.destination.account.uuid
    authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)


@pytest.mark.postgres
def test_transfer_cleanup_proof_cannot_be_retargeted(transferable_range):
    from django.db import IntegrityError, transaction

    from cms.services import RangeScopeAuditContext, rebind_range_workspace
    from engine.models import RangeAuthorityChange

    fixture = transferable_range
    rebind_range_workspace(
        fixture.credential,
        request_id=fixture.request.request_id,
        target_workspace_uuid=fixture.destination.workspace.uuid,
        audit=RangeScopeAuditContext(actor_type="service", actor_id=None),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        RangeAuthorityChange.objects.update(authority_owner_uuid=uuid4())


def test_expiry_cleanup_retains_custody_after_authorized_owner_change(transferable_range, settings):
    from datetime import timedelta

    from django.utils import timezone

    from cms.services import reassign_range_owner
    from cms.services._range_destroy import destroy_expired_range
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission
    from management.services import create_service_principal
    from workspaces.services import add_account_member

    settings.LOCAL_PROVISIONER = None
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    fixture = transferable_range
    owner = create_service_principal("Synthetic replacement cleanup owner")
    add_account_member(fixture.account.uuid, owner)
    reassign_range_owner(fixture.instance.pk, owner, credential=fixture.credential)
    fixture.instance.refresh_from_db()
    fixture.instance.expires_at = timezone.now() - timedelta(seconds=1)
    fixture.instance.maximum_expires_at = fixture.instance.expires_at + timedelta(days=1)
    fixture.instance.extension_days = 1
    fixture.instance.save(update_fields=["expires_at", "maximum_expires_at", "extension_days"])
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    destroy_expired_range(fixture.instance)
    cleanup = ProvisionerLaunchIntent.objects.get()
    assert cleanup.authority_kind == "cleanup"
    assert cleanup.authority_owner_uuid == owner.uuid
    authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)
