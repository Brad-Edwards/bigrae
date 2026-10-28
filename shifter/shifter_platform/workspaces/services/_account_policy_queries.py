"""Policy-filtered account discovery with bounded SQL and provider batches."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from shared.authorization import AuthorizationProvider, AuthorizationRequest, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from workspaces.models import Account

from ._account import AccountScopeError, hierarchy_target_scope
from ._account_authority import _active_actor

_DENIED = "Scope unavailable"
_MANAGE_ACCOUNTS = "installation.manage_accounts"
_READ_ACCOUNT = "account.read"
_LIST_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class AccountAdminView:
    """Bounded public account identity for policy-authorized administration."""

    uuid: UUID
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class AccountAdminPage:
    """A page whose count includes only policy-visible accounts."""

    count: int
    results: tuple[AccountAdminView, ...]


def _read_batch(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    accounts: tuple[Account, ...],
) -> tuple[bool, ...]:
    """Check every SQL-owned target without trusting partial provider results."""
    try:
        requests = tuple(
            AuthorizationRequest(
                actor.principal,
                _READ_ACCOUNT,
                TargetRef("account", account.uuid),
                hierarchy_target_scope("account", account.uuid),
                actor.ceiling,
            )
            for account in accounts
        )
        decisions = provider.batch_check(requests)
        if len(decisions) != len(requests) or any(decision.kind == "evaluator_error" for decision in decisions):
            raise AccountScopeError(_DENIED)
        return tuple(decision.allowed for decision in decisions)
    except Exception as exc:
        raise AccountScopeError(_DENIED) from exc


def _valid_account_page(offset: int, limit: int) -> None:
    """Bound count and page work before reading customer records."""
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
    ):
        raise AccountScopeError(_DENIED)


def _installation_list_access(actor: CredentialContext, provider: AuthorizationProvider) -> bool:
    """Use a global account list only with live installation authority."""
    if not actor.ceiling.permits(_MANAGE_ACCOUNTS, TargetRef("installation")):
        return False
    try:
        request = AuthorizationRequest(
            actor.principal,
            _MANAGE_ACCOUNTS,
            TargetRef("installation"),
            ResourceScope("installation"),
            actor.ceiling,
        )
        decision = provider.check(request)
        if decision.kind == "evaluator_error":
            raise AccountScopeError(_DENIED)
        return decision.allowed
    except Exception as exc:
        raise AccountScopeError(_DENIED) from exc


def _account_batches(exact_uuid: UUID | None, search: str = "") -> Iterator[tuple[Account, ...]]:
    """Read bounded SQL batches while retaining a stable count cursor."""
    cursor = 0
    while True:
        query = Account.objects.filter(pk__gt=cursor)
        if search:
            query = query.filter(name__icontains=search)
        if exact_uuid is not None:
            query = query.filter(uuid=exact_uuid)
        batch = tuple(query.order_by("pk")[:_LIST_BATCH_SIZE])
        if not batch:
            return
        cursor = batch[-1].pk
        yield batch


def _page_account_rows(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    *,
    global_access: bool,
    exact_uuid: UUID | None,
    offset: int,
    limit: int,
    search: str = "",
) -> AccountAdminPage:
    """Count and slice only accounts allowed by the live batch decisions."""
    count = 0
    results: list[AccountAdminView] = []
    for batch in _account_batches(exact_uuid, search):
        visible = (True,) * len(batch) if global_access else _read_batch(actor, provider, batch)
        for account, allowed in zip(batch, visible, strict=True):
            if not allowed:
                continue
            if offset <= count < offset + limit:
                results.append(AccountAdminView(account.uuid, account.name, account.kind))
            count += 1
    return AccountAdminPage(count, tuple(results))


def admin_list_accounts(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    *,
    offset: int = 0,
    limit: int = 50,
    search: str = "",
) -> AccountAdminPage:
    """Filter account authority before counting or slicing a collection."""
    actor = _active_actor(actor)
    _valid_account_page(offset, limit)
    if not isinstance(search, str) or len(search) > 200:
        raise AccountScopeError(_DENIED)
    global_access = _installation_list_access(actor, provider)
    if not global_access and _READ_ACCOUNT not in actor.ceiling.actions:
        return AccountAdminPage(0, ())
    exact_target = actor.ceiling.target if not global_access else None
    if exact_target is not None and (exact_target.type != "account" or exact_target.uuid is None):
        return AccountAdminPage(0, ())
    exact_uuid = exact_target.uuid if exact_target is not None else None
    return _page_account_rows(
        actor, provider, global_access=global_access, exact_uuid=exact_uuid, offset=offset, limit=limit, search=search
    )
