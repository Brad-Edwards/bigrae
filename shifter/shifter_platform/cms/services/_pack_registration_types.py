"""Immutable input, result and invocation-local admission for uniform ingestion."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope

from ._pack_registration_custody import RegistrationReferenceAdmission


@dataclass(frozen=True)
class PackRegistrationRequest:
    """Source-agnostic, entitlement-blind input to :func:`register_pack`.

    The shape does not vary with provenance: an in-box, public, private, or
    self-authored pack is described by the same fields. ``provenance`` is bounded
    reference metadata only (validated by the model), never a body or credential.

    Conformance status is intentionally absent: a caller cannot assert that a
    pack has passed conformance (see :data:`_REGISTRATION_CONFORMANCE_STATUS`).
    """

    scenario_id: str
    source_kind: str
    contract_kind: str
    contract_profile: str
    package_ref: str
    package_version: str
    package_digest: str
    lock_ref: str = ""
    lock_digest: str = ""
    provenance: dict[str, object] | None = None
    # Explicit optimistic concurrency precondition for an intentional revision.
    # This is registration control, never part of the portable pack identity.
    expected_package_digest: str = ""
    package_name: str = ""


@dataclass(frozen=True)
class RegisteredPack:
    """Bounded result of a successful registration (no reference payload)."""

    scenario_id: str
    source_kind: str
    contract_kind: str
    contract_profile: str
    conformance_status: str
    created: bool


@dataclass(frozen=True, slots=True)
class PackRegistrationAdmission:
    """Freeze native authority and reference custody for one exact registration."""

    credential: CredentialContext
    scope: ResourceScope
    request_digest: str
    reference: RegistrationReferenceAdmission


def _registration_digest(request: PackRegistrationRequest) -> str:
    """Bind preflight admission to every immutable registration input."""
    from shared.operation_envelope import canonical_payload_digest

    return canonical_payload_digest(asdict(request))
