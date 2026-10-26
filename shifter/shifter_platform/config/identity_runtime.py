"""Fail-closed admission for processes that can serve or produce effects."""

from __future__ import annotations

from django.db import DatabaseError

from config.openfga_authorization import OpenFgaRuntimeSettings, runtime_settings_from_django
from workspaces.services import get_identity_cutover_state


class IdentityRuntimeNotReady(RuntimeError):
    """The coordinated identity cutover is not safe for application runtime."""


def require_identity_runtime_ready(*, settings: OpenFgaRuntimeSettings | None = None) -> None:
    """Require terminal cutover state bound to this process's exact policy model."""
    try:
        state = get_identity_cutover_state()
    except DatabaseError as exc:
        raise IdentityRuntimeNotReady("Identity runtime is not activated") from exc
    if state is None:
        raise IdentityRuntimeNotReady("Identity runtime is not activated")
    if (
        state.state != "activated"
        or state.bootstrap_principal_uuid is None
        or state.bootstrap_verified_at is None
        or state.admission_invalidated_at is None
    ):
        raise IdentityRuntimeNotReady("Identity runtime is not activated")
    try:
        binding = settings or runtime_settings_from_django()
    except Exception as exc:
        raise IdentityRuntimeNotReady("Identity runtime binding is unavailable") from exc
    if state.provider_store_id != binding.store_id or state.model_id != binding.model_id:
        raise IdentityRuntimeNotReady("Identity runtime binding does not match the activated cutover")
