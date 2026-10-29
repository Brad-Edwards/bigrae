"""Native preparation setup shared by retained behavior tests."""

from types import SimpleNamespace

from shared.identity_scope import ResourceScope


def native_credential(user):
    """Create real session metadata for a test's explicitly selected human."""
    from shared.api.principals import human_session_credential
    from tests._credential_helpers import persisted_credential

    return persisted_credential(human_session_credential(user))


def preparation_policy(monkeypatch):
    """Translate the retained test permission matrix at the external policy port."""
    from management.models import Principal
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    previous = port._provider_factory

    def check(request):
        principal = Principal.objects.filter(uuid=request.principal.uuid).select_related("user").first()
        if principal is None or principal.user is None:
            return previous().check(request)
        permission = {
            "installation.manage_adapters": "engine.manage_preparation_adapters",
            "installation.prepare_content": "engine.prepare_artifacts",
            "installation.read_content": "engine.prepare_artifacts",
        }.get(request.action)
        allowed = permission is None or principal.user.has_perm(permission)
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
            "policy_allowed" if allowed else "policy_denied",
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))


def request_artifact_preparation(actor, adapter_id, package_input, **kwargs):
    """These synthetic fixtures explicitly target installation-owned content."""
    from engine.services import request_artifact_preparation as request

    return request(actor, adapter_id, package_input, scope=ResourceScope("installation"), **kwargs)
