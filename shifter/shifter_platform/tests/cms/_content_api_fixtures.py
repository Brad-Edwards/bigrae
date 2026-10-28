"""Explicit application grants for retained CMS behavior suites."""

import pytest


@pytest.fixture
def content_policy(monkeypatch):
    from types import SimpleNamespace

    from management.services import principal_for_user
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    grants = {}
    actions = {
        "installation.read_content",
        "installation.manage_content",
        "installation.prepare_content",
        "installation.use_personal_tokens",
    }

    def check(request):
        allowed = request.scope.kind == "installation" and request.action in grants.get(request.principal.uuid, set())
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
            "policy_allowed" if allowed else "policy_denied",
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))

    def grant(user):
        grants[principal_for_user(user).uuid] = set(actions)
        return user

    return grant
