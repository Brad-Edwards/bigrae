"""Bounded operational policy for the communication delivery worker (#2098, #1525).

The typed, relationally-validated owner of these values is
``config._ctf_communication_settings``; the defaults below keep the worker
importable and unit-testable without a full settings surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

_DEFAULTS = {
    "CTF_COMMUNICATION_WORKER_BATCH_SIZE": 100,
    "CTF_COMMUNICATION_WORKER_PER_EVENT_CAP": 25,
    "CTF_COMMUNICATION_WORKER_LEASE_SECONDS": 120,
    "CTF_COMMUNICATION_TRANSPORT_TIMEOUT_SECONDS": 10,
    "CTF_COMMUNICATION_MAX_ATTEMPTS": 6,
    "CTF_COMMUNICATION_MAX_ELAPSED_SECONDS": 86400,
    "CTF_COMMUNICATION_BACKOFF_BASE_SECONDS": 30,
    "CTF_COMMUNICATION_BACKOFF_CAP_SECONDS": 3600,
    "CTF_COMMUNICATION_BACKOFF_JITTER_FRACTION": 0.25,
}


def _setting(name: str) -> int | float:
    """Return a bounded worker setting, defaulting when the settings owner is absent."""
    return getattr(settings, name, _DEFAULTS[name])


@dataclass(frozen=True)
class WorkerConfig:
    """Immutable snapshot of the worker's bounded operational policy for one run."""

    batch_size: int
    per_event_cap: int
    lease_seconds: int
    timeout_seconds: float
    max_attempts: int
    max_elapsed_seconds: int
    backoff_base_seconds: int
    backoff_cap_seconds: int
    backoff_jitter_fraction: float

    @classmethod
    def from_settings(cls) -> WorkerConfig:
        """Build the config from Django settings (typed owner) or bounded defaults."""
        return cls(
            batch_size=int(_setting("CTF_COMMUNICATION_WORKER_BATCH_SIZE")),
            per_event_cap=int(_setting("CTF_COMMUNICATION_WORKER_PER_EVENT_CAP")),
            lease_seconds=int(_setting("CTF_COMMUNICATION_WORKER_LEASE_SECONDS")),
            timeout_seconds=float(_setting("CTF_COMMUNICATION_TRANSPORT_TIMEOUT_SECONDS")),
            max_attempts=int(_setting("CTF_COMMUNICATION_MAX_ATTEMPTS")),
            max_elapsed_seconds=int(_setting("CTF_COMMUNICATION_MAX_ELAPSED_SECONDS")),
            backoff_base_seconds=int(_setting("CTF_COMMUNICATION_BACKOFF_BASE_SECONDS")),
            backoff_cap_seconds=int(_setting("CTF_COMMUNICATION_BACKOFF_CAP_SECONDS")),
            backoff_jitter_fraction=float(_setting("CTF_COMMUNICATION_BACKOFF_JITTER_FRACTION")),
        )
