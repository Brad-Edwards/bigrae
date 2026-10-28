"""Management-owned origin and password gates for temporary CTF accounts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib.auth import get_user_model

from management.models import UserProfile

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, User


def set_active_ctf_event(user: User, event_id: UUID | None) -> None:
    """Set or clear the active CTF event for a user.

    Args:
        user: The user to update.
        event_id: CTF event UUID PK to set, or None to clear.
    """
    from .services import get_user_profile

    profile = get_user_profile(user)
    profile.active_ctf_event_id = event_id
    profile.save(update_fields=["active_ctf_event_id"])


def configure_temporary_ctf_account(user: User, event_id: UUID) -> None:
    """Mark a freshly created local user as an isolated CTF account.

    The origin marker is intentionally one-way. Callers may update ordinary
    role state, but no service is provided to clear ``is_ctf_account``.
    """
    from .services import get_user_profile

    profile = get_user_profile(user)
    if profile.cognito_sub or profile.issuer:
        raise ValueError("A provider-bound user cannot become a CTF account")
    profile.is_ctf_account = True
    profile.must_change_password = True
    profile.user_type = "ctf_participant"
    profile.active_ctf_event_id = event_id
    profile.save(
        update_fields=[
            "is_ctf_account",
            "must_change_password",
            "user_type",
            "active_ctf_event_id",
        ]
    )
    # Keep an already-populated reverse one-to-one cache consistent
    # with the just-committed marker for callers in the same transaction.
    user.profile = profile


def is_temporary_ctf_account(user: User | AnonymousUser) -> bool:
    """Return the durable account-origin marker without exposing the model."""
    result = False
    if getattr(user, "is_authenticated", False) and isinstance(user, get_user_model()) and user.pk is not None:
        try:
            result = user.profile.is_ctf_account
        except UserProfile.DoesNotExist:
            result = False
    return result


def is_ctf_password_change_required(user: User | AnonymousUser) -> bool:
    """Return whether a marked account is still bootstrap-password gated."""
    result = False
    if getattr(user, "is_authenticated", False) and isinstance(user, get_user_model()) and user.pk is not None:
        try:
            profile = user.profile
            result = profile.is_ctf_account and profile.must_change_password
        except UserProfile.DoesNotExist:
            result = False
    return result


def set_ctf_password_change_required(user: User, required: bool) -> None:
    """Update the first-login password-change gate for a marked account."""
    from .services import get_user_profile

    profile = get_user_profile(user)
    if not profile.is_ctf_account:
        raise ValueError("Password-change state is only valid for CTF accounts")
    profile.must_change_password = required
    profile.save(update_fields=["must_change_password"])
