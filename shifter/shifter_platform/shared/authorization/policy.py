"""Fail-closed checks shared by owning domain services, never HTTP permissions."""

from collections.abc import Sequence

from shared.identity_scope import ResourceScope

from .contracts import AuthorizationRequest, DecisionKind, TargetRef
from .port import AuthorizationProviderBindingError, configured_authorization_provider


def scope_parent(scope: ResourceScope) -> TargetRef:
    """Select the deepest already-resolved parent; missing scope is never root."""
    if scope.kind == "installation":
        return TargetRef("installation")
    if scope.workspace_uuid is not None:
        return TargetRef("workspace", scope.workspace_uuid)
    if scope.organization_uuid is not None:
        return TargetRef("organization", scope.organization_uuid)
    return TargetRef("account", scope.account_uuid)


def require_authorization(request: AuthorizationRequest) -> None:
    """Require the sole provider decision while keeping outages fail-closed."""
    try:
        decision = configured_authorization_provider().check(request)
    except Exception:
        raise AuthorizationProviderBindingError("Application authority unavailable") from None
    if decision.kind == DecisionKind.EVALUATOR_ERROR:
        raise AuthorizationProviderBindingError("Application authority unavailable")
    if not decision.allowed:
        raise PermissionError("Application authority denied")


def authorized_requests(requests: Sequence[AuthorizationRequest]) -> tuple[bool, ...]:
    """Filter trusted candidates; incomplete/outage responses are not empty data."""
    if not requests:
        return ()
    try:
        decisions = configured_authorization_provider().batch_check(requests)
        if len(decisions) != len(requests) or any(result.kind == DecisionKind.EVALUATOR_ERROR for result in decisions):
            raise ValueError("Incomplete authorization result")
        return tuple(result.allowed for result in decisions)
    except Exception:
        raise AuthorizationProviderBindingError("Application authority unavailable") from None
