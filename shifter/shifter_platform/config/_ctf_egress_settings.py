"""CTF egress proxy setting (#2090).

Deployed platform pods have no public egress. Organizer image copies and CTF
webhooks reach organizer-chosen HTTPS hosts through the credential-less
``content-egress`` CONNECT proxy. The deployment sets ``CTF_EGRESS_PROXY`` to its
``host:port``; an empty value (local development and tests) connects directly
to the validated, pinned address. Star-imported by ``config.settings``.
"""

from __future__ import annotations

import re

from django.core.exceptions import ImproperlyConfigured

from config._product_env import product_env

__all__ = ["CTF_EGRESS_PROXY"]

_HOST_PORT = re.compile(r"(?P<host>[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?):(?P<port>\d{1,5})")


def resolve_ctf_egress_proxy(raw: str) -> str:
    """Return the validated ``host:port``, or ``""``; fail loud on any other value."""
    value = raw.strip()
    if not value:
        return ""
    match = _HOST_PORT.fullmatch(value)
    if match is None or not 1 <= int(match["port"]) <= 65535:
        raise ImproperlyConfigured("CTF_EGRESS_PROXY must be empty or host:port")
    return value


CTF_EGRESS_PROXY = resolve_ctf_egress_proxy(product_env("CTF_EGRESS_PROXY", ""))
