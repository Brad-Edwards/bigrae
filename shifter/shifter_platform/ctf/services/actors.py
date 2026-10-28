"""Principal-native actor conversion for CTF owning services.

HTTP callers pass :class:`CredentialContext` directly.  The ``User`` and
integer forms exist only for trusted, still-supported in-process callers while
they are converted to the same credential contract; they never restore the
legacy owner, group, staff, or superuser authorization rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.contrib.auth import get_user_model
from django.contrib.auth.models import User

from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential


@dataclass(frozen=True, slots=True)
class EventActionAdmission:
    """Explicit, invocation-local policy admission for a locked event mutation."""

    credential: CredentialContext
    event_uuid: UUID
    actions: frozenset[str]
    binding: tuple[object, ...]
    compatibility_user_id: int | None = None

    @staticmethod
    def event_binding(event) -> tuple[object, ...]:
        return tuple(
            getattr(event, name)
            for name in (
                "owner_principal_uuid",
                "scope_kind",
                "account_id",
                "organization_id",
                "workspace_id",
                "deleted_at",
                "authorization_publication_state",
            )
        )

    def validate(self, event, action: str) -> None:
        if (
            event.pk != self.event_uuid
            or action not in self.actions
            or self.event_binding(event) != self.binding
            or not self.local_credential_valid()
        ):
            raise ValueError("Event admission changed")

    def local_credential_valid(self) -> bool:
        """Trusted User callers retain their live SQL identity; proofs retain their owner."""
        if self.compatibility_user_id is None:
            return resolve_local_credential(self.credential) == self.credential
        return _compatibility_credential_valid(self.compatibility_user_id, self.credential)


type EventActor = CredentialContext | EventActionAdmission | User | int


def _compatibility_credential_valid(user_id: int | None, credential: CredentialContext) -> bool:
    """Validate only the explicitly selected synchronous human compatibility path."""
    from shared.principal_port import principal_for_user

    if user_id is None:
        return False
    user = get_user_model().objects.select_related("profile").filter(pk=user_id, is_active=True).first()
    return (
        user is not None
        and not user.profile.is_ctf_account
        and user.profile.deleted_at is None
        and principal_for_user(user) == credential.principal
    )


def event_actor_credential(actor: object) -> CredentialContext:
    """Return the canonical credential for a native or compatibility actor."""
    if isinstance(actor, EventActionAdmission):
        return actor.credential
    if isinstance(actor, CredentialContext):
        return actor
    if isinstance(actor, bool):
        raise ValueError("CTF actor unavailable")
    if isinstance(actor, int):
        user = get_user_model().objects.filter(pk=actor, is_active=True).first()
        if user is None:
            raise ValueError("CTF actor unavailable")
    elif isinstance(actor, User):
        user = actor
        if not user.is_active or user.pk is None:
            raise ValueError("CTF actor unavailable")
    else:
        raise ValueError("CTF actor unavailable")
    from shared.api.principals import human_session_credential

    return human_session_credential(user)


def current_event_actor_credential(actor: object) -> CredentialContext:
    """Admit native proofs through their owner; keep explicit synchronous User callers."""
    if isinstance(actor, CredentialContext):
        return resolve_current_credential(actor)
    if isinstance(actor, (User, int)) and not isinstance(actor, bool):
        return event_actor_credential(actor)
    raise ValueError("CTF actor unavailable")


def fence_event_actor(actor: EventActor) -> None:
    """Fence native lifecycle or the explicit compatibility human before a write."""
    if isinstance(actor, EventActionAdmission):
        valid = actor.local_credential_valid()
    elif isinstance(actor, CredentialContext):
        valid = resolve_local_credential(actor) == actor
    elif isinstance(actor, (User, int)) and not isinstance(actor, bool):
        credential = event_actor_credential(actor)
        valid = _compatibility_credential_valid(event_actor_user_id(actor), credential)
    else:
        valid = False
    if not valid:
        raise ValueError("CTF actor unavailable")


def event_actor_user_id(actor: object) -> int | None:
    """Return the compatibility user id for a human principal, else ``None``."""
    if isinstance(actor, EventActionAdmission):
        actor = actor.credential
    if isinstance(actor, int):
        return actor
    if isinstance(actor, User):
        return actor.pk
    if not isinstance(actor, CredentialContext):
        raise ValueError("CTF actor unavailable")
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
