"""Action-specific content discovery without a parent directory-read requirement."""

from dataclasses import dataclass
from typing import Literal

from django.db.models import QuerySet

from shared.authorization import AuthorizationProvider, AuthorizationRequest, TargetRef
from shared.credentials import CredentialContext, resolve_current_credential
from shared.identity_scope import ResourceScope
from workspaces.models import Account, Organization, Workspace

from ._account import AccountScopeError, hierarchy_target_scope
from ._account_policy_admin import _active_actor
from ._account_policy_reads import _collection_ceiling, _valid_page, _visible_page


@dataclass(frozen=True, slots=True)
class ContentScopeOption:
    """One policy-admitted content scope with its persisted ancestry."""

    kind: str
    name: str
    scope: ResourceScope


@dataclass(frozen=True, slots=True)
class ContentScopePage:
    """Bounded scope options and continuation metadata for discovery."""

    count: int
    results: tuple[ContentScopeOption, ...]


def list_content_scope_options(
    actor: CredentialContext, provider: AuthorizationProvider, *, offset: int = 0, limit: int = 50
) -> ContentScopePage:
    """Return only independently admitted leaf names and their routing ancestry."""
    actor = _active_actor(actor)
    _valid_page(offset, limit)
    count = 0
    selected = []
    if "installation.read_content" in actor.ceiling.actions and actor.ceiling.target in {
        None,
        TargetRef("installation"),
    }:
        request = AuthorizationRequest(
            actor.principal,
            "installation.read_content",
            TargetRef("installation"),
            ResourceScope("installation"),
            actor.ceiling,
        )
        decision = provider.check(request)
        if decision.kind == "evaluator_error":
            raise AccountScopeError("Content scopes unavailable")
        if decision.allowed:
            if offset == 0:
                selected.append(ContentScopeOption("installation", "Shared catalog", request.scope))
            count = 1
    collections: tuple[
        tuple[Literal["account", "organization", "workspace"], QuerySet[Account | Organization | Workspace]], ...
    ] = (
        ("account", Account.objects.all()),
        ("organization", Organization.objects.filter(account__isnull=False)),
        ("workspace", Workspace.objects.filter(archived_at__isnull=True, organization__account__isnull=False)),
    )
    for kind, query in collections:
        action = f"{kind}.read_content"
        permitted, exact_uuid = _collection_ceiling(actor, action, kind)
        if not permitted:
            continue
        if exact_uuid is not None:
            query = query.filter(uuid=exact_uuid)
        visible, rows = _visible_page(
            query,
            actor,
            provider,
            action,
            kind,
            lambda row: row.uuid,
            window=(max(0, offset - count), max(0, limit - len(selected))),
        )
        selected.extend(ContentScopeOption(kind, row.name, hierarchy_target_scope(kind, row.uuid)) for row in rows)
        count += visible
    if resolve_current_credential(actor) != actor:
        raise AccountScopeError("Content scopes unavailable")
    return ContentScopePage(count, tuple(selected))
