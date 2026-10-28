"""Policy-aware account hierarchy commands prepared for the S8 cutover."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from shared.audit import AuditAction, AuditEntityType, AuditEvent, RequestAudit, audit_log
from shared.authorization import AuthorizationProvider, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef, ResourceScope
from shared.principal_port import resolve_principal
from workspaces.models import Account, AccountMembership

from ._account import (
    AccountScopeError,
    AccountView,
    OrganizationView,
    WorkspaceView,
    _account_view,
    _organization_view,
    _workspace_view,
    add_account_member,
    create_account,
    create_account_organization,
    create_account_workspace,
    hierarchy_target_scope,
)
from ._account_authority import _active_actor, _fence_actor, _require_action
from ._account_policy_queries import AccountAdminPage, AccountAdminView, _valid_account_page, admin_list_accounts
from ._egress import _set_egress_policy_unchecked
from ._hierarchy_publication import creation_for_receipt, publish_created_hierarchy
from ._lifecycle import WorkspaceAuditContext, WorkspaceProjection
from ._quota import WorkspaceQuotaAuditContext
from ._quota_admin import WorkspaceResourceUsage, _set_quota_policy_unchecked

__all__ = [
    "AccountAdminPage",
    "AccountAdminView",
    "AccountMemberPage",
    "AccountMemberView",
    "_valid_account_page",
    "admin_add_account_member",
    "admin_create_account",
    "admin_create_organization",
    "admin_create_workspace",
    "admin_individual_account",
    "admin_list_account_members",
    "admin_list_accounts",
    "admin_remove_account_member",
    "admin_set_workspace_egress_policy",
    "admin_set_workspace_quota_policy",
]

_DENIED = "Scope unavailable"
_LIST_BATCH_SIZE = 100
_MANAGE_ACCOUNTS = "installation.manage_accounts"
_READ_ACCOUNT = "account.read"
_MANAGE_MEMBERS = "account.manage_members"


@dataclass(frozen=True, slots=True)
class AccountMemberView:
    """One account membership fact without policy or contact information."""

    principal_uuid: UUID


@dataclass(frozen=True, slots=True)
class AccountMemberPage:
    """A bounded page of explicit account membership facts."""

    count: int
    results: tuple[AccountMemberView, ...]


def admin_create_account(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    *,
    kind: str,
    name: str,
    owner: PrincipalRef | None = None,
    audit: RequestAudit | None = None,
    publication_receipt: UUID | None = None,
) -> AccountView:
    """Create an account after installation authority and owner identity checks."""
    actor = _require_action(
        actor,
        provider,
        _MANAGE_ACCOUNTS,
        TargetRef("installation"),
        ResourceScope("installation"),
    )
    if owner is not None:
        try:
            resolve_principal(owner)
        except Exception as exc:
            raise AccountScopeError(_DENIED) from exc
    if publication_receipt is not None:
        existing = creation_for_receipt(publication_receipt, "account", None)
        if (existing.kind, existing.name, existing.individual_principal_uuid) != (
            kind,
            name,
            owner.uuid if owner else None,
        ):
            raise AccountScopeError(_DENIED)
        account = _account_view(existing)
    else:
        with transaction.atomic():
            _fence_actor(actor)
            account = create_account(
                kind=kind, name=name, owner=owner, audit_actor=actor.principal, request_audit=audit
            )
            _fence_actor(actor)
    publish_created_hierarchy("account", account.uuid, provider)
    return account


def admin_create_organization(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    account_uuid: UUID,
    *,
    name: str,
    audit: RequestAudit | None = None,
    publication_receipt: UUID | None = None,
) -> OrganizationView:
    """Create a real subdivision after exact account authority and ancestry checks."""
    try:
        scope = hierarchy_target_scope("account", account_uuid)
    except AccountScopeError as exc:
        raise AccountScopeError(_DENIED) from exc
    actor = _require_action(actor, provider, "account.manage_organizations", TargetRef("account", account_uuid), scope)
    if publication_receipt is not None:
        existing = creation_for_receipt(publication_receipt, "organization", account_uuid)
        if existing.name != name:
            raise AccountScopeError(_DENIED)
        organization = _organization_view(existing)
    else:
        with transaction.atomic():
            _fence_actor(actor)
            organization = create_account_organization(
                account_uuid, name, audit_actor=actor.principal, request_audit=audit
            )
            _fence_actor(actor)
    publish_created_hierarchy("organization", organization.uuid, provider)
    return organization


def admin_create_workspace(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    account_uuid: UUID,
    organization_uuid: UUID,
    *,
    name: str,
    audit: RequestAudit | None = None,
    publication_receipt: UUID | None = None,
) -> WorkspaceView:
    """Create a workspace only beneath the authorized parent organization."""
    try:
        scope = hierarchy_target_scope("organization", organization_uuid)
        if scope.account_uuid != account_uuid:
            raise AccountScopeError(_DENIED)
    except AccountScopeError as exc:
        raise AccountScopeError(_DENIED) from exc
    actor = _require_action(
        actor,
        provider,
        "organization.manage_workspaces",
        TargetRef("organization", organization_uuid),
        scope,
    )
    if publication_receipt is not None:
        existing = creation_for_receipt(publication_receipt, "workspace", organization_uuid)
        if existing.name != name:
            raise AccountScopeError(_DENIED)
        workspace = _workspace_view(existing)
    else:
        with transaction.atomic():
            _fence_actor(actor)
            workspace = create_account_workspace(
                account_uuid, organization_uuid, name, audit_actor=actor.principal, request_audit=audit
            )
            _fence_actor(actor)
    publish_created_hierarchy("workspace", workspace.uuid, provider)
    return workspace


def admin_individual_account(actor: CredentialContext, provider: AuthorizationProvider) -> AccountAdminView:
    """Resolve a human's individual account without an organization selector."""
    actor = _active_actor(actor)
    if actor.principal.kind != "human":
        raise AccountScopeError(_DENIED)
    account = Account.objects.filter(
        kind=Account.Kind.INDIVIDUAL, individual_principal_uuid=actor.principal.uuid
    ).first()
    if account is None:
        raise AccountScopeError(_DENIED)
    actor = _require_action(
        actor,
        provider,
        _READ_ACCOUNT,
        TargetRef("account", account.uuid),
        hierarchy_target_scope("account", account.uuid),
    )
    return AccountAdminView(account.uuid, account.name, account.kind)


def admin_add_account_member(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    account_uuid: UUID,
    principal: PrincipalRef,
    *,
    audit: RequestAudit | None = None,
) -> None:
    """Add an active principal as a fact, without issuing an authority grant."""
    try:
        scope = hierarchy_target_scope("account", account_uuid)
    except AccountScopeError as exc:
        raise AccountScopeError(_DENIED) from exc
    actor = _require_action(actor, provider, _MANAGE_MEMBERS, TargetRef("account", account_uuid), scope)
    try:
        resolve_principal(principal)
    except Exception as exc:
        raise AccountScopeError(_DENIED) from exc
    with transaction.atomic():
        _fence_actor(actor)
        add_account_member(account_uuid, principal, audit_actor=actor.principal, request_audit=audit)
        _fence_actor(actor)


def admin_list_account_members(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    account_uuid: UUID,
    *,
    offset: int = 0,
    limit: int = 50,
) -> AccountMemberPage:
    """Page facts only after authority over the exact account is established."""
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
    ):
        raise AccountScopeError(_DENIED)
    scope = hierarchy_target_scope("account", account_uuid)
    actor = _require_action(actor, provider, _MANAGE_MEMBERS, TargetRef("account", account_uuid), scope)
    queryset = AccountMembership.objects.filter(account__uuid=account_uuid).order_by("pk")
    count = queryset.count()
    rows = queryset[offset : offset + limit]
    return AccountMemberPage(count, tuple(AccountMemberView(row.principal_uuid) for row in rows))


def admin_remove_account_member(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    account_uuid: UUID,
    principal_uuid: UUID,
    *,
    audit: RequestAudit | None = None,
) -> None:
    """Remove only the selected account's membership fact with strict evidence."""
    scope = hierarchy_target_scope("account", account_uuid)
    actor = _require_action(actor, provider, _MANAGE_MEMBERS, TargetRef("account", account_uuid), scope)
    request_audit = audit or RequestAudit()
    with transaction.atomic():
        account = Account.objects.select_for_update().filter(uuid=account_uuid).first()
        _fence_actor(actor)
        if account is None:
            raise AccountScopeError(_DENIED)
        membership = (
            AccountMembership.objects.select_for_update().filter(account=account, principal_uuid=principal_uuid).first()
        )
        if membership is None:
            raise AccountScopeError(_DENIED)
        membership_id = membership.pk
        membership.delete()
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.ACCOUNT_MEMBERSHIP,
                entity_id=membership_id,
                action=AuditAction.DELETE,
                previous_state={"account_id": account.pk},
                context="account_hierarchy",
                actor_type="principal",
                actor_principal_uuid=actor.principal.uuid,
                source_ip=request_audit.source_ip,
                user_agent=request_audit.user_agent[:500],
                request_id=request_audit.request_id[:64],
            ),
            strict=True,
        )


def admin_set_workspace_quota_policy(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    workspace_uuid: UUID,
    resource: str,
    limit: int,
    mode: str,
    *,
    audit: RequestAudit | None = None,
) -> WorkspaceResourceUsage:
    """Authorize quota authoring for a human or service application admin."""
    try:
        scope = hierarchy_target_scope("workspace", workspace_uuid)
    except AccountScopeError as exc:
        raise AccountScopeError(_DENIED) from exc
    actor = _require_action(actor, provider, "workspace.manage_quota", TargetRef("workspace", workspace_uuid), scope)
    request_audit = audit or RequestAudit()
    with transaction.atomic():
        _fence_actor(actor)
        result = _set_quota_policy_unchecked(
            workspace_uuid,
            resource,
            limit,
            mode,
            audit=WorkspaceQuotaAuditContext(
                actor_type="principal",
                actor_id=None,
                actor_principal_uuid=actor.principal.uuid,
                source_ip=request_audit.source_ip,
                user_agent=request_audit.user_agent,
                request_id=request_audit.request_id,
            ),
            actor_id=None,
        )
        _fence_actor(actor)
        return result


def admin_set_workspace_egress_policy(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    workspace_uuid: UUID,
    egress_policy: str,
    *,
    audit: RequestAudit | None = None,
) -> WorkspaceProjection:
    """Set a workspace egress selector after exact live policy and SQL scope checks."""
    try:
        scope = hierarchy_target_scope("workspace", workspace_uuid)
    except AccountScopeError as exc:
        raise AccountScopeError(_DENIED) from exc
    actor = _require_action(actor, provider, "workspace.manage_egress", TargetRef("workspace", workspace_uuid), scope)
    request_audit = audit or RequestAudit()
    with transaction.atomic():
        _fence_actor(actor)
        result = _set_egress_policy_unchecked(
            workspace_uuid,
            egress_policy,
            audit=WorkspaceAuditContext(
                actor_type="principal",
                actor_id=None,
                actor_principal_uuid=actor.principal.uuid,
                source_ip=request_audit.source_ip,
                user_agent=request_audit.user_agent,
                request_id=request_audit.request_id,
            ),
        )
        _fence_actor(actor)
        return result
