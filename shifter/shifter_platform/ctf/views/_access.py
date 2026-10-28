"""Credential delivery rate limit shared by the CTF organizer API."""

from __future__ import annotations

from uuid import UUID


def _check_credential_delivery_rate_limit(user_id: int | UUID, limit: int = 50, window: int = 3600) -> bool:
    """Return True if credential reset/delivery remains within its budget.

    Uses Django's cache with a fixed-window counter. Default: 50 per hour.
    Note: with the default LocMemCache, limits are per-process. For cross-worker
    enforcement, configure a shared CACHES backend (e.g. Redis, Memcached).
    """
    from shared.credential_delivery import credential_delivery_allowed

    return credential_delivery_allowed(user_id, limit=limit, window=window)
