"""Facade for the in-box catalog registration used by installation setup (#2679)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.credentials import CredentialContext

    from ._content_ingestion import RegisteredPack


def register_inbox_catalog(*, actor: CredentialContext) -> list[RegisteredPack]:
    """Register, qualify, and publish the release's in-box packs as ``actor``."""
    # The scenario package imports this service package, so import it late.
    from cms.scenarios.inbox import register_inbox_packs

    return register_inbox_packs(actor=actor)
