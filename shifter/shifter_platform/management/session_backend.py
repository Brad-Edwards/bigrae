"""Django's database session backend with a stable non-secret credential UUID."""

from __future__ import annotations

from uuid import UUID

from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore as DatabaseSessionStore
from django.utils import timezone

from management.models import PrincipalSession
from management.principals import principal_for_user

_UNAVAILABLE = "Credential unavailable"


class SessionStore(DatabaseSessionStore):
    """Preserve Django signing, expiry, save/cycle/delete and password checks."""

    @classmethod
    def get_model_class(cls):
        return PrincipalSession

    def create_model_instance(self, data):
        """Retain a reference on save; key cycling creates a distinct identity."""
        row = super().create_model_instance(data)
        prior = (
            self.model.objects.filter(session_key=self.session_key).values("credential_uuid", "principal_uuid").first()
        )
        if prior is not None:
            row.credential_uuid = prior["credential_uuid"]
        user_id = data.get(SESSION_KEY)
        if user_id is not None:
            user = get_user_model().objects.filter(pk=user_id).first()
            if user is None:
                raise ValueError(_UNAVAILABLE)
            row.principal_uuid = principal_for_user(user).uuid
        if prior is not None and prior["principal_uuid"] is not None and prior["principal_uuid"] != row.principal_uuid:
            raise ValueError(_UNAVAILABLE)
        return row

    @property
    def credential_uuid(self) -> UUID:
        """Expose only the non-secret reference, never the Django session key."""
        row = (
            self.model.objects.filter(
                session_key=self.session_key, expire_date__gt=timezone.now(), principal_uuid__isnull=False
            )
            .values_list("credential_uuid", flat=True)
            .first()
        )
        if row is None:
            raise ValueError(_UNAVAILABLE)
        return row

    @property
    def principal_uuid(self) -> UUID:
        """Return the immutable identity pinned to this authenticated session."""
        value = self.model.objects.filter(session_key=self.session_key).values_list("principal_uuid", flat=True).first()
        if value is None:
            raise ValueError(_UNAVAILABLE)
        return value


def session_for_credential(credential_uuid: UUID) -> SessionStore:
    """Resolve the current framework session through its owner; no new proof."""
    from shared.credentials import CredentialRevoked

    row = PrincipalSession.objects.filter(
        credential_uuid=credential_uuid, expire_date__gt=timezone.now(), principal_uuid__isnull=False
    ).first()
    if row is None:
        raise CredentialRevoked(_UNAVAILABLE)
    return SessionStore(session_key=row.session_key)


def save_session_assurance(session: DatabaseSessionStore) -> None:
    """Persist provider recheck metadata without renewing or recreating a session."""
    if not isinstance(session, DatabaseSessionStore) or not session.session_key:
        raise ValueError(_UNAVAILABLE)
    instance = session.create_model_instance(dict(session))
    updated = session.model.objects.filter(session_key=session.session_key, expire_date__gt=timezone.now()).update(
        session_data=instance.session_data
    )
    if updated != 1:
        raise ValueError(_UNAVAILABLE)
    session.modified = False
