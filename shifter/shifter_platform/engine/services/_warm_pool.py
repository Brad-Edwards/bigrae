"""Engine warm-pool allocation services (#28).

Two responsibilities live here, both database-only and provider-free so they are
safe on the launch path:

1. **Pool-scoped capacity** reuses the Engine capacity ledger
   (:mod:`engine.services._capacity_admit`) with a deterministic pool scope key
   instead of a fabricated CTF event (preflight #28). A warm generation draws
   from its bucket's scope budget when the operator has declared one, and
   otherwise no-ops exactly like any un-assessed range. Capacity is held while
   the generation's resources exist, including after claim, and released only
   after reconciliation observes provider absence.

2. **Atomic claim** transitions a ready, unclaimed, exact-fingerprint generation
   to ``CLAIMED`` under ``select_for_update(skip_locked=True)`` with a deterministic
   lock order. ``skip_locked`` gives one-winner semantics: two launches racing for
   the same single ready generation cannot both claim it — the loser sees no row
   and cold-falls-back. The claim is a plain transition; the CMS launch path wraps
   it together with ownership/workspace projection and the activation outbox in
   one transaction. Workers execute only after that transaction commits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from django.db import transaction
from django.db.models import Q

from engine.services._capacity_admit import admit_range_capacity, release_range_capacity
from shared.capacity import CapacityAssessmentResult

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from engine.models import WarmRangeGeneration
    from shared.credentials import CredentialContext

    from ._principal_warm_claim import PreparedWarmRangeClaim

from ._warm_pool_retirement import (
    _finalize_warm_generation as _finalize_warm_generation,
)
from ._warm_pool_retirement import (
    _generation_is_stalled as _generation_is_stalled,
)
from ._warm_pool_retirement import (
    _request_warm_retirement as _request_warm_retirement,
)
from ._warm_pool_retirement import (
    abandon_warm_preparation as abandon_warm_preparation,
)
from ._warm_pool_retirement import (
    finalize_retiring_generations as finalize_retiring_generations,
)
from ._warm_pool_retirement import (
    recover_stalled_generations as recover_stalled_generations,
)
from ._warm_pool_retirement import (
    retire_generation as retire_generation,
)
from ._warm_pool_retirement import (
    retire_generations_for_request as retire_generations_for_request,
)
from ._warm_pool_retirement import (
    retire_removed_bucket_generations as retire_removed_bucket_generations,
)

logger = logging.getLogger(__name__)

# Stable namespace so a bucket's capacity scope key is byte-identical across
# processes and restarts (mirrors the CTF spare draw namespace pattern).
_WARM_SCOPE_NAMESPACE = UUID("2a7b5c9e-3d81-4f26-8b14-6c0d9e2f1a55")

__all__ = [
    "WarmGenerationDraft",
    "_finalize_warm_generation",
    "_generation_is_stalled",
    "_request_warm_retirement",
    "abandon_warm_preparation",
    "active_generation_count",
    "admit_warm_generation_capacity",
    "bucket_state_counts",
    "claim_ready_generation",
    "create_warm_generation",
    "enqueue_range_activation",
    "finalize_retiring_generations",
    "ready_generations",
    "recover_stalled_generations",
    "release_warm_generation_capacity",
    "retire_generation",
    "retire_generations_for_request",
    "retire_removed_bucket_generations",
    "total_active_generation_count",
    "warm_capacity_scope_ref",
]


def enqueue_range_activation(request_id: UUID | str, *, credential: CredentialContext | None = None) -> str:
    """Enqueue the ``raes-range activate`` operation for a claimed generation (#28).

    Called by the CMS claim path *after* the claim + ownership transfer commit, so
    a downstream conflict never leaves an activation queued against an unclaimed
    generation. Returns the durable launch-intent id. The provisioner realizes the
    claimant's fresh, sanitized access; the result applier transitions the range to
    READY for its new owner.
    """
    from engine.launch_intents import enqueue_provisioner_launch

    return enqueue_provisioner_launch(
        ["raes-range", "activate", "--request-id", str(request_id)],
        credential=credential,
    )


def warm_capacity_scope_ref(deployment_name: str, bucket_id: str) -> UUID:
    """Return the deterministic capacity scope key for one warm bucket.

    The scope is per (deployment, bucket) so each bucket's warm generations draw
    from a budget an operator can assess independently, without inventing a CTF
    event. Deterministic so a restart or a second reconciler derives the same key.
    """
    return uuid5(_WARM_SCOPE_NAMESPACE, f"{deployment_name}:{bucket_id}")


def admit_warm_generation_capacity(
    *, scope_ref: UUID, draw_key: UUID, now: datetime | None = None
) -> CapacityAssessmentResult:
    """Draw one warm generation's share from its bucket scope budget.

    Thin pass-through to :func:`admit_range_capacity`: the warm pool is a
    first-class capacity consumer, not a bypass. Returns the folded capacity
    outcome (``INDETERMINATE`` when the operator declared no budget for the scope,
    which does not block, exactly as for an un-assessed range).
    """
    return admit_range_capacity(scope_ref, draw_key=draw_key, now=now)


def release_warm_generation_capacity(draw_key: UUID, *, now: datetime | None = None) -> int:
    """Release a warm generation's capacity draw. Idempotent.

    Called only after reconciliation observes provider absence for the generation
    (ownership transfer does **not** release capacity — the resources still exist).
    """
    return release_range_capacity(draw_key, now=now)


def claim_ready_generation(
    *,
    candidates: list[tuple[str, str]],
    backend: str,
    range_source: str,
    claimant_request_id: UUID,
    now: datetime | None = None,
    expected_request_id: UUID | None = None,
) -> WarmRangeGeneration | None:
    """Atomically claim one ready generation from an authorized candidate set, or None.

    ``candidates`` is the launch's ``(bucket_id, compatibility_digest)`` pairs for the
    effective-policy buckets it is authorized to claim from -- so a launch can only
    receive a generation from a bucket the current server-side policy still permits,
    and only when the generation's realization/placement/posture digest matches
    (security: policy-to-allocation binding). The query is additionally bound to the
    invariant ``backend`` and ``range_source``.

    One-winner: ``select_for_update(skip_locked=True)`` plus a conditional
    ``READY -> CLAIMED`` transition means a generation is claimed by at most one launch
    (the partial-unique ``claimed_by_request_id`` constraint is the database backstop).
    A miss returns ``None`` and the caller cold-falls-back. Must run inside the caller's
    launch transaction so a downstream conflict rolls the claim back. An expired
    generation is never claimed; the reconciler retires it.
    """
    from django.utils import timezone

    from engine.models import WarmRangeGeneration

    moment = now or timezone.now()
    with transaction.atomic():
        candidate = (
            _ready_candidates(candidates, backend, range_source, moment)
            .select_for_update(skip_locked=True)
            .filter(Q(request_id=expected_request_id) if expected_request_id is not None else Q())
            .first()
        )
        if candidate is None:
            return None
        candidate.state = WarmRangeGeneration.State.CLAIMED
        candidate.claimed_by_request_id = claimant_request_id
        candidate.claimed_at = moment
        candidate.save(update_fields=["state", "claimed_by_request_id", "claimed_at"])
        return candidate


def _ready_candidates(
    candidates: list[tuple[str, str]], backend: str, range_source: str, moment: datetime
) -> QuerySet[WarmRangeGeneration]:
    """Build one exact-fingerprint, unclaimed and unexpired generation query."""
    from engine.models import WarmRangeGeneration

    authorized = Q(pk__in=[])
    for bucket_id, digest in candidates:
        authorized |= Q(bucket_id=bucket_id, compatibility_digest=digest)
    return (
        WarmRangeGeneration.objects.filter(
            authorized,
            state=WarmRangeGeneration.State.READY,
            backend=backend,
            range_source=range_source,
            claimed_by_request_id__isnull=True,
        )
        .filter(Q(idle_deadline__isnull=True) | Q(idle_deadline__gt=moment))
        .order_by("created_at", "pk")
    )


def preview_ready_generation(
    *, candidates: list[tuple[str, str]], backend: str, range_source: str
) -> WarmRangeGeneration | None:
    """Read one eligible generation for pre-lock admission; claiming still rechecks every predicate."""
    from django.utils import timezone

    return _ready_candidates(candidates, backend, range_source, timezone.now()).first()


def ready_generations(bucket_id: str) -> list[WarmRangeGeneration]:
    """Return the READY (unclaimed) generations for a bucket, oldest first."""
    from engine.models import WarmRangeGeneration

    return list(
        WarmRangeGeneration.objects.filter(bucket_id=bucket_id, state=WarmRangeGeneration.State.READY).order_by(
            "created_at", "pk"
        )
    )


def active_generation_count(bucket_id: str) -> int:
    """Return the nonterminal-unclaimed (provisioning + ready) count for a bucket."""
    from engine.models import WarmRangeGeneration

    return WarmRangeGeneration.objects.filter(
        bucket_id=bucket_id, state__in=WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES
    ).count()


def total_active_generation_count() -> int:
    """Return the nonterminal-unclaimed count across every bucket (ceiling accounting)."""
    from engine.models import WarmRangeGeneration

    return WarmRangeGeneration.objects.filter(state__in=WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES).count()


@dataclass(frozen=True)
class WarmGenerationDraft:
    """The fields for a new PROVISIONING warm-generation ledger row (#28).

    Groups the realization identity and capacity-draw references so the reconciler
    hands the Engine one immutable value object instead of a wide argument list.
    """

    bucket_id: str
    compatibility_digest: str
    effective_policy_fingerprint: str
    backend: str
    range_source: str
    capacity_partition: str
    capacity_scope_ref: UUID
    capacity_draw_key: UUID
    request_id: UUID
    idle_deadline: datetime


def create_warm_generation(draft: WarmGenerationDraft) -> WarmRangeGeneration:
    """Create a PROVISIONING warm-generation ledger row keyed by request_id.

    Created at reservation time, before dispatch, so the warm-prepare provision
    suppresses participant access (quarantine).
    """
    from engine.models import WarmRangeGeneration

    return WarmRangeGeneration.objects.create(
        bucket_id=draft.bucket_id,
        compatibility_digest=draft.compatibility_digest,
        effective_policy_fingerprint=draft.effective_policy_fingerprint,
        backend=draft.backend,
        range_source=draft.range_source,
        capacity_partition=draft.capacity_partition,
        capacity_scope_ref=draft.capacity_scope_ref,
        capacity_draw_key=draft.capacity_draw_key,
        request_id=draft.request_id,
        state=WarmRangeGeneration.State.PROVISIONING,
        idle_deadline=draft.idle_deadline,
    )


def bucket_state_counts(bucket_id: str) -> dict[str, int]:
    """Return per-state generation counts for a bucket (metrics gauges, #28).

    Keys are the private allocation-state values; a state with no rows is 0. Used
    by the reconciler to publish pool-depth gauges without exposing the model.
    """
    from django.db.models import Count

    from engine.models import WarmRangeGeneration

    counts = {state.value: 0 for state in WarmRangeGeneration.State}
    rows = WarmRangeGeneration.objects.filter(bucket_id=bucket_id).values_list("state").annotate(n=Count("state"))
    for state, n in rows:
        counts[str(state)] = n
    return counts


def enqueue_admitted_warm_activation(
    prepared: PreparedWarmRangeClaim, *, request_id: UUID, claimant_request_id: UUID
) -> str:
    """Commit activation with the exact already-admitted warm custody transition."""
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Range, RangeAuthorityChange, WarmRangeGeneration
    from shared.credentials import resolve_local_credential

    from ._launch_authority import range_runtime_binding_digest
    from ._principal_warm_claim import PreparedWarmRangeClaim

    if (
        not isinstance(prepared, PreparedWarmRangeClaim)
        or resolve_local_credential(prepared.credential) != prepared.credential
    ):
        raise ValueError("Warm activation authority changed")
    generation = WarmRangeGeneration.objects.select_for_update().get(
        request_id=request_id, claimed_by_request_id=claimant_request_id, state="claimed"
    )
    if generation.range_id is None:
        raise ValueError("Warm generation runtime unavailable")
    row = Range.objects.select_for_update().get(pk=generation.range_id)
    if row.resource_generation is None:
        raise ValueError("Warm generation identity unavailable")
    retained = (
        RangeAuthorityChange.objects.filter(
            range=row,
            resource_generation=row.resource_generation,
            authority_binding_digest=range_runtime_binding_digest(row),
        )
        .order_by("-accepted_at", "-id")
        .first()
    )
    if (
        retained is None
        or retained.credential_uuid != prepared.credential.credential_uuid
        or retained.authority_binding_digest != range_runtime_binding_digest(row)
        or row.owner_principal_uuid != prepared.credential.principal.uuid
    ):
        raise ValueError("Warm activation binding changed")
    from shared.operation_authority import credential_reference_fields

    names = [field.name for field in RangeAuthorityChange._meta.fields if field.name.startswith("authority_")]
    fields = credential_reference_fields(prepared.credential) | {name: getattr(retained, name) for name in names}
    payload = {"version": 1, "resource": "raes-range", "operation": "activate", "request_id": str(request_id)}
    return _enqueue_admitted_provisioner_launch(
        ["raes-range", "activate", "--request-id", str(request_id)], payload, fields
    )
