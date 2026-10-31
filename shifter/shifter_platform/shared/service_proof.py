"""Provider-neutral port for the configured native service-proof profile.

The composition root binds one factory. Owning domains read the issuer,
audience and subject grammar through it, so management admission does not
hardcode one cloud provider's identity literals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceProofProfile:
    """The configured provider issuer, audience and subject grammar for service proof."""

    issuer: str
    audience: str
    subject_valid: Callable[[str], bool]


_profile_factory: Callable[[], ServiceProofProfile] | None = None


def bind_service_proof_profile(factory: Callable[[], ServiceProofProfile]) -> None:
    """Bind the sole config-owned profile factory during composition."""
    global _profile_factory
    if not callable(factory) or (_profile_factory is not None and _profile_factory is not factory):
        raise ValueError("A service proof profile is already bound")
    _profile_factory = factory


def service_proof_profile() -> ServiceProofProfile:
    """Return the current provider profile; settings are read at call time."""
    if _profile_factory is None:
        raise ValueError("Service credential verification unavailable")
    profile = _profile_factory()
    if not isinstance(profile, ServiceProofProfile):
        raise ValueError("Service credential verification unavailable")
    return profile
