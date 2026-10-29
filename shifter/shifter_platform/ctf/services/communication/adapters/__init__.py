"""Channel adapter seam for the scoped-communication delivery engine (#2098, #1525).

Re-exports the command/result contract and closed registry from :mod:`.contract`
and performs the explicit, closed registration of the in-app and email adapters
at import. The email adapter reports itself unavailable without a delivering
backend.
"""

from __future__ import annotations

from .contract import (
    ChannelAdapter,
    DeliveryCommand,
    DeliveryOutcome,
    OutcomeClass,
    get_adapter,
    register_adapter,
    registered_channels,
)
from .email import EmailAdapter
from .inapp import InAppAdapter

# Closed, explicit registration (not a dynamic plugin hook / AppConfig.ready).
register_adapter(InAppAdapter())
register_adapter(EmailAdapter())

__all__ = [
    "ChannelAdapter",
    "DeliveryCommand",
    "DeliveryOutcome",
    "EmailAdapter",
    "InAppAdapter",
    "OutcomeClass",
    "get_adapter",
    "register_adapter",
    "registered_channels",
]
