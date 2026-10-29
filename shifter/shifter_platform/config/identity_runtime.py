"""Fail-closed admission for processes that can serve or produce effects."""

from __future__ import annotations

from django.db import DatabaseError

from config.openfga_authorization import OpenFgaRuntimeSettings, runtime_settings_from_django
from workspaces.services import IdentityCutoverStateView, get_identity_cutover_state

_IDENTITY_RUNTIME_IS_NOT_ACTIVATED = "Identity runtime is not activated"


class IdentityRuntimeNotReady(RuntimeError):
    """The coordinated identity cutover is not safe for application runtime."""


def require_identity_runtime_ready(*, settings: OpenFgaRuntimeSettings | None = None) -> None:
    """Require terminal cutover state bound to this process's exact policy model."""
    try:
        state = get_identity_cutover_state()
    except DatabaseError as exc:
        raise IdentityRuntimeNotReady(_IDENTITY_RUNTIME_IS_NOT_ACTIVATED) from exc
    if state is None:
        raise IdentityRuntimeNotReady(_IDENTITY_RUNTIME_IS_NOT_ACTIVATED)
    _require_activated_state(state)
    try:
        binding = settings or runtime_settings_from_django()
    except Exception as exc:
        raise IdentityRuntimeNotReady("Identity runtime binding is unavailable") from exc
    if state.provider_store_id != binding.store_id or state.model_id != binding.model_id:
        raise IdentityRuntimeNotReady("Identity runtime binding does not match the activated cutover")


def _require_activated_state(state: IdentityCutoverStateView) -> None:
    """Require every terminal bootstrap and credential-invalidation fact."""
    if (
        state.state != "activated"
        or state.bootstrap_principal_uuid is None
        or state.bootstrap_verified_at is None
        or state.admission_invalidated_at is None
    ):
        raise IdentityRuntimeNotReady(_IDENTITY_RUNTIME_IS_NOT_ACTIVATED)
