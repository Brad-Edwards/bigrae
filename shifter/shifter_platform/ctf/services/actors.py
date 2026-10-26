"""Principal-native actor conversion for CTF owning services.

HTTP callers pass :class:`CredentialContext` directly.  The ``User`` and
integer forms exist only for trusted, still-supported in-process callers while
they are converted to the same credential contract; they never restore the
legacy owner, group, staff, or superuser authorization rules.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import User

from shared.credentials import CredentialContext

type EventActor = CredentialContext | User | int


def event_actor_credential(actor: EventActor) -> CredentialContext:
    """Return the canonical credential for a native or compatibility actor."""
    if isinstance(actor, CredentialContext):
        return actor
    if isinstance(actor, int):
        user = get_user_model().objects.filter(pk=actor, is_active=True).first()
        if user is None:
            raise ValueError("CTF actor unavailable")
    else:
        user = actor
        if not getattr(user, "is_active", False) or getattr(user, "pk", None) is None:
            raise ValueError("CTF actor unavailable")
    from shared.api.principals import human_session_credential

    return human_session_credential(user)


def event_actor_user_id(actor: EventActor) -> int | None:
    """Return the compatibility user id for a human principal, else ``None``."""
    if isinstance(actor, int):
        return actor
    if not isinstance(actor, CredentialContext):
        return actor.pk
    if actor.principal.kind != "human":
        return None
    try:
        from management.services import human_principal_user_id

        return human_principal_user_id(actor.principal)
    except Exception:
        return None


def event_actor_user(actor: EventActor) -> User | None:
    """Resolve the optional human compatibility projection for domain code."""
    user_id = event_actor_user_id(actor)
    if user_id is None:
        return None
    return get_user_model().objects.filter(pk=user_id, is_active=True).first()
