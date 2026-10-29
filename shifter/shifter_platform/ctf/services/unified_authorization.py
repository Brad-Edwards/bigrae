"""CTF-owned composition of live principal, event scope and shared policy."""

from __future__ import annotations

import logging
from uuid import UUID

from ctf.exceptions import CTFError, CTFPermissionError
from ctf.services.actors import current_event_actor_credential
from ctf.services.credential_scope import event_credential_scope, retained_event_delete_scope
from shared.authorization import (
    AuthorizationContractError,
    AuthorizationRequest,
    DecisionKind,
    TargetRef,
    configured_authorization_provider,
)
from shared.credentials import CredentialUnavailable
from shared.identity_scope import ResourceScope
from shared.principal_port import PrincipalResolutionError, resolve_principal
from workspaces.services import event_placement_scope

logger = logging.getLogger(__name__)

_DENIED = "CTF authority denied"
_UNAVAILABLE = "CTF authority unavailable"


def require_event_action(actor: object, event_uuid: UUID, action: str) -> None:
    """Require a live exact event decision; creator and CTF staff imply nothing."""
    try:
        credential = current_event_actor_credential(actor)
        if credential.kind == "temporary":
            raise CTFPermissionError(_DENIED)
        principal = resolve_principal(credential.principal)
        request = AuthorizationRequest(
            principal,
            action,
            TargetRef("event", event_uuid),
            event_credential_scope(event_uuid),
            credential.ceiling,
        )
    except CredentialUnavailable:
        raise CTFError(_UNAVAILABLE, code="CTF_AUTHORITY_UNAVAILABLE") from None
    except (ValueError, PrincipalResolutionError, AuthorizationContractError) as exc:
        raise CTFPermissionError(_DENIED) from exc
    _require_policy(request)


def require_retained_event_delete(actor: object, event_uuid: UUID) -> None:
    """Authorize only permanent deletion against retained tombstone ancestry."""
    try:
        credential = current_event_actor_credential(actor)
        if credential.kind == "temporary":
            raise CTFPermissionError(_DENIED)
        principal = resolve_principal(credential.principal)
        request = AuthorizationRequest(
            principal,
            "event.delete",
            TargetRef("event", event_uuid),
            retained_event_delete_scope(event_uuid),
            credential.ceiling,
        )
    except CredentialUnavailable:
        raise CTFError(_UNAVAILABLE, code="CTF_AUTHORITY_UNAVAILABLE") from None
    except (ValueError, PrincipalResolutionError, AuthorizationContractError) as exc:
        raise CTFPermissionError(_DENIED) from exc
    _require_policy(request)


def require_event_creation(actor: object, parent: TargetRef) -> ResourceScope:
    """Authorize creation at an existing customer parent, then return its SQL scope."""
    if parent.type not in {"account", "organization", "workspace"} or parent.uuid is None:
        raise CTFPermissionError(_DENIED)
    try:
        credential = current_event_actor_credential(actor)
        if credential.kind == "temporary":
            raise CTFPermissionError(_DENIED)
        principal = resolve_principal(credential.principal)
        scope = event_placement_scope(parent.type, parent.uuid)
        request = AuthorizationRequest(
            principal,
            f"{parent.type}.create_event",
            parent,
            scope,
            credential.ceiling,
        )
    except CredentialUnavailable:
        raise CTFError(_UNAVAILABLE, code="CTF_AUTHORITY_UNAVAILABLE") from None
    except (ValueError, PrincipalResolutionError, AuthorizationContractError) as exc:
        raise CTFPermissionError(_DENIED) from exc
    _require_policy(request)
    return scope


def _require_policy(request: AuthorizationRequest) -> None:
    """Keep evaluator failures distinct from an explicit policy denial."""
    try:
        decision = configured_authorization_provider().check(request)
    # provider errors may carry evaluator endpoint or credential details
    except Exception as exc:  # noqa: BLE001
        # Fail closed. ``from None`` keeps the provider cause out of later tracebacks.
        logger.error("CTF authority provider unavailable exc_type=%s", type(exc).__name__, exc_info=False)
        raise CTFError(_UNAVAILABLE, code="CTF_AUTHORITY_UNAVAILABLE") from None
    if decision.kind == DecisionKind.EVALUATOR_ERROR:
        raise CTFError(_UNAVAILABLE, code="CTF_AUTHORITY_UNAVAILABLE")
    if not decision.allowed:
        raise CTFPermissionError(_DENIED)
