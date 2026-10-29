"""Compose ongoing socket policy through the owning application services."""

import re

from django.contrib.auth.models import User

from shared.credentials import CredentialContext
from shared.remote_access import TERMINAL_TARGET_PATH_RE

_RANGE_STATUS = re.compile(r"/ws/range-status/(?P<request_id>[a-f0-9-]+)/")
_NGFW_STATUS = re.compile(r"/ws/ngfw-status/(?P<app_id>[a-f0-9-]+)/")


def ctf_account_socket_standing(user: User) -> bool:
    """Return whether ``user`` may keep a socket open under the participant boundary.

    Accounts other than temporary CTF accounts pass. A temporary CTF account
    needs a live participation (not banned, inside the event window) and no
    pending forced password change, as on the HTTP participant boundary. The
    check runs on every socket revalidation, so a ban or the event end closes an
    open terminal instead of only refusing the next connection (#2617).
    """
    from ctf.services.participant.accounts import live_participant_for_user
    from management.services import is_ctf_password_change_required, is_temporary_ctf_account

    if not is_temporary_ctf_account(user):
        return True
    return live_participant_for_user(user) is not None and not is_ctf_password_change_required(user)


def authorize_socket_resource(scope: dict[str, object]) -> bool:
    """Source callers must retain policy on their exact route resource."""
    credential = scope.get("credential_context")
    if credential is not None and not isinstance(credential, CredentialContext):
        return False
    browser = scope.get("user")
    if isinstance(browser, User) and not ctf_account_socket_standing(browser):
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
    status = _RANGE_STATUS.fullmatch(path)
    if status:
        from cms.services import get_range_by_request_id

        get_range_by_request_id(credential, status.group("request_id"), include_terminal=True)
        return True
    ngfw = _NGFW_STATUS.fullmatch(path)
    if ngfw:
        from cms.services import get_ngfw

        get_ngfw(credential, ngfw.group("app_id"))
        return True
    return False
