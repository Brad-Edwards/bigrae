"""Normalize public product environment aliases at the settings ingress."""

from __future__ import annotations

import logging
import os
from typing import overload

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


@overload
def product_env(name: str, default: str) -> str:
    """Return a string when a string default is supplied."""
    ...


@overload
def product_env(name: str, default: None = None) -> str | None:
    """Allow an absent value when no string default is supplied."""
    ...


def product_env(name: str, default: str | None = None) -> str | None:
    """Resolve one setting without ever including its value in diagnostics.

    Existing parsers continue to own type, range and cross-field validation.
    Equal aliases are accepted; differing values fail before either is used.
    """
    canonical = "BIGRAE_" + name.removeprefix("SHIFTER_") if name.startswith("SHIFTER_") else name
    current = os.environ.get(canonical)
    legacy = os.environ.get(name)
    if canonical != name and legacy is not None:
        if current is not None and current != legacy:
            raise ImproperlyConfigured(f"conflicting environment aliases: {canonical} and {name}")
        logger.warning("Deprecated environment key %s; use %s", name, canonical)
    if current is not None:
        return current
    if legacy is not None:
        return legacy
    return default
