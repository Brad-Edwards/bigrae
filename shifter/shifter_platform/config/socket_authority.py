"""Compose ongoing socket policy through the owning application services."""

import re

from django.contrib.auth.models import User

from shared.credentials import CredentialContext
from shared.remote_access import TERMINAL_TARGET_PATH_RE

_RANGE_STATUS = re.compile(r"/ws/range-status/(?P<request_id>[a-f0-9-]+)/")
_NGFW_STATUS = re.compile(r"/ws/ngfw-status/(?P<app_id>[a-f0-9-]+)/")


def authorize_socket_resource(scope: dict[str, object]) -> bool:
    """Source callers must retain policy on their exact route resource."""
    credential = scope.get("credential_context")
    if credential is not None and not isinstance(credential, CredentialContext):
        return False
    path = str(scope.get("path", ""))
    terminal = TERMINAL_TARGET_PATH_RE.fullmatch(path)
    if terminal:
        allowed = _authorize_terminal_scope(scope, path, credential)
    elif credential is None:
        allowed = True
    else:
        allowed = _authorize_status_scope(credential, path)
    return allowed


def _authorize_terminal_scope(scope: dict[str, object], path: str, credential: CredentialContext | None) -> bool:
    """Authorize the actual terminal route through the owning CMS boundary."""
    from cms.services import authorize_range_terminal

    browser = scope.get("user")
    actor = browser if isinstance(browser, User) else credential
    if actor is None:
        return False
    authorize_range_terminal(
        actor,
        path.removeprefix("/ws/terminal/").removesuffix("/"),
        credential=credential,
    )
    return True


def _authorize_status_scope(credential: CredentialContext, path: str) -> bool:
    """Authorize exact status resources while refusing unregistered authenticated routes."""
    if path == "/ws/notifications/":
        # This admits transport only. The consumer rechecks the actual proof and
        # owning event policy on every subscription, replay and delivery.
        return credential.principal.kind == "human"
    status = _RANGE_STATUS.fullmatch(path)
    if status:
        from cms.services import get_range_by_request_id

        get_range_by_request_id(credential, status.group("request_id"), include_terminal=True)
        allowed = True
    elif ngfw := _NGFW_STATUS.fullmatch(path):
        from cms.services import get_ngfw

        get_ngfw(credential, ngfw.group("app_id"))
        allowed = True
    else:
        allowed = False
    return allowed
