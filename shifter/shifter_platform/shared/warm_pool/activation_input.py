"""Closed warm-pool ``activate`` operation-input contract (#28, ADR-039-R11).

``shared.operation_envelope`` owns the transport shape; this module owns the
bounded ``payload`` the Engine materializes into an ``OperationInput`` for a
warm-pool ``activate`` generation, and the fail-closed parser the provisioner runs
before it rotates or scrubs anything on a claimed generation.

Warm-prepare reuses the ordinary RAES provision input (:mod:`shared.raes.operation_input`),
which is deliberately ownership-neutral. Activation is the *only* warm operation
that carries a claimant identity, and it carries exactly the trusted
owner/workspace/product projection needed to create fresh claimant-specific access
and prove isolation -- nothing more. The claimant identity is loaded from this
immutable operation input, never from HTTP, environment, or process arguments
(preflight #28 security gate 6).

Deliberately absent: any secret or credential material, provider inventory,
policy JSON, registry rows, or command fragments. The provisioner resolves secret
stores itself at the current-owner boundary; this input carries references and
bounded identity metadata only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from shared.identity_scope import PrincipalRef, ResourceScope
from shared.raes.operation_input import RaesOperationInput, RaesOperationInputError, parse_raes_operation_input

__all__ = [
    "ActivationClaimant",
    "ActivationGeneration",
    "ActivationInput",
    "ActivationInputError",
    "build_activation_input",
    "parse_activation_input",
]

#: Schema tag so a future change to the activation projection cannot be read under
#: an older parser. Bump when a field's meaning changes or a field is added/removed.
ACTIVATION_SCHEMA = "range-warm-activation/v2"

_INPUT_KEYS = frozenset(
    {
        "schema",
        "claimant_principal_uuid",
        "claimant_principal_kind",
        "scope_kind",
        "account_uuid",
        "organization_uuid",
        "workspace_uuid",
        "range_source",
        "instantiation_purpose",
        "range_backend",
        "legacy_range_id",
        "compatibility_digest",
        "prepared_generation_fence",
        # The full ownership-neutral RAES realization projection for the claimed
        # generation (plan + participant-access bindings + image candidates). The
        # provisioner uses it to locate the realized hosts/accounts and realize the
        # claimant's fresh access after scrubbing the pre-claim state.
        "raes_input",
    }
)


class ActivationInputError(Exception):
    """The activation operation input is not a valid, bounded projection."""


def _require(condition: bool, message: str) -> None:
    """Raise :class:`ActivationInputError` with ``message`` unless ``condition`` holds."""
    if not condition:
        raise ActivationInputError(message)


def _require_positive_int(value: object, field: str) -> int:
    """Return ``value`` as a positive int, failing closed otherwise."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ActivationInputError(f"activation input {field} must be a positive int")
    return value


def _require_nonempty_str(value: object, field: str, *, max_len: int) -> str:
    """Return ``value`` as a bounded non-empty string, failing closed otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise ActivationInputError(f"activation input {field} must be a non-empty string")
    if len(value) > max_len:
        raise ActivationInputError(f"activation input {field} exceeds {max_len} characters")
    return value


def _uuid(value: object, field: str, *, optional: bool = False) -> UUID | None:
    """Parse one canonical non-nil UUID identity, allowing explicit null only when requested."""
    if optional and value is None:
        return None
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ActivationInputError(f"activation input {field} must be a UUID") from None
    if parsed.int == 0 or str(value) != str(parsed):
        raise ActivationInputError(f"activation input {field} must be a canonical non-nil UUID")
    return parsed


@dataclass(frozen=True)
class ActivationInput:
    """The parsed, validated claimant projection for one warm activation."""

    claimant: PrincipalRef
    scope: ResourceScope
    range_source: str
    instantiation_purpose: str
    range_backend: str
    #: The realized warm generation's opaque naming key (its ``Range.id``), used to
    #: reconstruct deterministic cloud-resource and secret names for the generation
    #: being activated. Never used to select or authorize an Engine row.
    legacy_range_id: int
    #: The expected compatibility digest of the claimed generation (fence): the
    #: provisioner refuses to activate a generation whose realized digest differs.
    compatibility_digest: str
    #: The expected warm-prepare adapter-state generation (the prepare operation_id):
    #: a stale prepare result cannot be activated (generation fencing, ADR-043).
    prepared_generation_fence: str
    #: The parsed ownership-neutral RAES realization projection for the generation.
    raes_input: RaesOperationInput


def parse_activation_input(payload: object) -> ActivationInput:
    """Validate an activation operation-input payload and return the projection.

    Fails closed on an unexpected or missing key, a non-mapping payload, a wrong
    schema tag, a non-positive identity, an over-long username, or a blank fence.
    """
    if not isinstance(payload, Mapping):
        raise ActivationInputError("activation input must be an object")
    obj = dict(payload)
    unexpected = sorted(set(obj) - _INPUT_KEYS)
    _require(not unexpected, f"activation input has unexpected field(s): {', '.join(unexpected)}")
    missing = sorted(_INPUT_KEYS - set(obj))
    _require(not missing, f"activation input is missing field(s): {', '.join(missing)}")
    _require(obj["schema"] == ACTIVATION_SCHEMA, f"activation input schema must be {ACTIVATION_SCHEMA!r}")
    try:
        raes_input = parse_raes_operation_input(obj["raes_input"])
    except RaesOperationInputError as exc:
        raise ActivationInputError(f"activation input raes_input is invalid: {exc}") from None
    try:
        principal = PrincipalRef(
            cast(UUID, _uuid(obj["claimant_principal_uuid"], "claimant_principal_uuid")),
            cast(Literal["human", "service"], obj["claimant_principal_kind"]),
        )
        scope = ResourceScope(
            cast(Literal["installation", "account"], obj["scope_kind"]),
            _uuid(obj["account_uuid"], "account_uuid", optional=True),
            _uuid(obj["organization_uuid"], "organization_uuid", optional=True),
            _uuid(obj["workspace_uuid"], "workspace_uuid", optional=True),
        )
        return ActivationInput(
            claimant=principal,
            scope=scope,
            range_source=_require_nonempty_str(obj["range_source"], "range_source", max_len=64),
            instantiation_purpose=_require_nonempty_str(
                obj["instantiation_purpose"], "instantiation_purpose", max_len=64
            ),
            range_backend=_require_nonempty_str(obj["range_backend"], "range_backend", max_len=8),
            legacy_range_id=_require_positive_int(obj["legacy_range_id"], "legacy_range_id"),
            compatibility_digest=_require_nonempty_str(obj["compatibility_digest"], "compatibility_digest", max_len=80),
            prepared_generation_fence=_require_nonempty_str(
                obj["prepared_generation_fence"], "prepared_generation_fence", max_len=64
            ),
            raes_input=raes_input,
        )
    except ActivationInputError:
        raise
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise ActivationInputError("activation input claimant or scope is invalid") from None


@dataclass(frozen=True)
class ActivationClaimant:
    """The trusted claimant identity projection for one warm activation."""

    principal: PrincipalRef
    scope: ResourceScope


@dataclass(frozen=True)
class ActivationGeneration:
    """The claimed warm generation's realization-identity projection."""

    range_source: str
    instantiation_purpose: str
    range_backend: str
    legacy_range_id: int
    compatibility_digest: str
    prepared_generation_fence: str


def build_activation_input(
    *,
    claimant: ActivationClaimant,
    generation: ActivationGeneration,
    raes_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose and validate the activation operation-input payload in one step.

    Returns the JSON-serialisable payload for ``build_operation_envelope``.
    ``raes_input`` is the ownership-neutral RAES realization projection built by
    ``engine.operation_inputs`` (already validated by ``build_raes_operation_input``).
    """
    payload = {
        "schema": ACTIVATION_SCHEMA,
        "claimant_principal_uuid": str(claimant.principal.uuid),
        "claimant_principal_kind": claimant.principal.kind,
        "scope_kind": claimant.scope.kind,
        "account_uuid": str(claimant.scope.account_uuid) if claimant.scope.account_uuid else None,
        "organization_uuid": str(claimant.scope.organization_uuid) if claimant.scope.organization_uuid else None,
        "workspace_uuid": str(claimant.scope.workspace_uuid) if claimant.scope.workspace_uuid else None,
        "range_source": generation.range_source,
        "instantiation_purpose": generation.instantiation_purpose,
        "range_backend": generation.range_backend,
        "legacy_range_id": generation.legacy_range_id,
        "compatibility_digest": generation.compatibility_digest,
        "prepared_generation_fence": generation.prepared_generation_fence,
        "raes_input": dict(raes_input),
    }
    parse_activation_input(payload)
    return payload
