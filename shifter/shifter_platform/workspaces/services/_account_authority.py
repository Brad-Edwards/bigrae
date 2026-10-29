"""Live admission and local credential fencing for account commands."""

from __future__ import annotations

from shared.authorization import AuthorizationProvider, AuthorizationRequest, TargetRef
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope

from ._account import AccountScopeError

_DENIED = "Scope unavailable"


def _active_actor(actor: CredentialContext) -> CredentialContext:
    """Require a currently active non-participant principal."""
    try:
        if actor.kind == "temporary":
            raise AccountScopeError(_DENIED)
        return resolve_current_credential(actor)
    except Exception as exc:
        raise AccountScopeError(_DENIED) from exc


def _require_action(
    actor: CredentialContext,
    provider: AuthorizationProvider,
    action: str,
    target: TargetRef,
    scope: ResourceScope,
) -> CredentialContext:
    """Resolve durable identity and policy for each direct service invocation."""
    try:
        actor = _active_actor(actor)
        request = AuthorizationRequest(actor.principal, action, target, scope, actor.ceiling)
        if not provider.check(request).allowed:
            raise AccountScopeError(_DENIED)
        return actor
    except Exception as exc:
        raise AccountScopeError(_DENIED) from exc


def _fence_actor(actor: CredentialContext) -> None:
    """Fence the admitted native proof again in the owning SQL transaction."""
    try:
        if resolve_local_credential(actor) != actor:
            raise ValueError("Credential changed")
    except Exception as exc:
        raise AccountScopeError(_DENIED) from exc
