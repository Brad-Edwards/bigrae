"""Channel adapter command/result contract for the delivery engine (ADR-051-R12, #2098).

A channel adapter is the ONLY seam between the durable ledger and a transport. The
worker hands an adapter a bounded, immutable :class:`DeliveryCommand` (identifiers
and non-secret projections only), the adapter attempts exactly one delivery within
a bounded timeout, and returns a closed :class:`DeliveryOutcome`. The adapter never
decides authorization, audience, retry policy, or scheduling; those stay owned by
the ledger and the worker.

Registration is a plain, closed module-level mapping populated at import, never an
``AppConfig.ready()`` workflow hook or a dynamic plugin registry (preflight
non-goal). A registered adapter also reports whether its transport is available
in this deployment; an unavailable channel is invisible to release and to the
worker (no silent downgrade, no invented acceptance; ADR-071).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class OutcomeClass(StrEnum):
    """Closed classification of a single delivery attempt's observed result.

    ``ACCEPTED`` means the transport backend (or socket) accepted the message; it
    never means read or acknowledged. ``RETRIABLE`` is a transient failure the
    worker may retry within its bounded budget. ``TERMINAL`` is a permanent failure
    that must not be retried. ``SUPPRESSED`` means the adapter declined because a
    cancellation/lifecycle/generation fence applied before its irreversible boundary.
    """

    ACCEPTED = "accepted"
    RETRIABLE = "retriable"
    TERMINAL = "terminal"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class DeliveryCommand:
    """Bounded, immutable input for one delivery attempt (reference-only).

    Carries stable identities and non-secret projections. The in-app channel never
    receives the email coordinate or decrypted body; the email adapter resolves
    its own coordinate from the snapshot under its own boundary.
    ``idempotency_key`` is the row's stable per (intent, snapshot, channel) key,
    identical across claims and retries of the same command.
    """

    attempt_id: UUID
    intent_id: UUID
    snapshot_id: UUID
    channel: str
    event_id: UUID
    participant_public_id: UUID
    recipient_user_id: int | None
    occurrence_key: str
    idempotency_key: str


@dataclass(frozen=True)
class DeliveryOutcome:
    """The closed result of one delivery attempt.

    ``reason`` is a bounded, closed reason class for metrics/audit, never raw
    provider/model text. ``provider_receipt`` is an optional stable backend message
    identity used to mitigate at-least-once duplicates where supported.
    """

    outcome: OutcomeClass
    reason: str = ""
    provider_receipt: str = ""


@runtime_checkable
class ChannelAdapter(Protocol):
    """The bounded transport seam every channel implements."""

    channel: str

    def available(self) -> bool:
        """Return True when this deployment can deliver on the channel."""
        ...

    def deliver(self, command: DeliveryCommand, *, timeout: float) -> DeliveryOutcome:
        """Attempt exactly one delivery for ``command`` within ``timeout`` seconds."""
        ...


_ADAPTERS: dict[str, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    """Register (or replace) the adapter for its channel in the closed mapping."""
    _ADAPTERS[adapter.channel] = adapter


def get_adapter(channel: str) -> ChannelAdapter | None:
    """Return the registered, available adapter for ``channel``, or ``None``."""
    adapter = _ADAPTERS.get(channel)
    return adapter if adapter is not None and adapter.available() else None


def registered_channels() -> frozenset[str]:
    """Return the channels whose registered adapter is available in this deployment.

    Release admits and the worker claims only commands whose channel is in this
    set, so an unavailable channel (for example ``email`` with a console backend)
    is never claimed, downgraded, or falsely marked accepted.
    """
    return frozenset(channel for channel, adapter in _ADAPTERS.items() if adapter.available())
