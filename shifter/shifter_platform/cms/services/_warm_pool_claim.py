"""CMS warm-pool claim path for initial launches (#28).

Inserted into the canonical RAES launch orchestration *after* every product,
tenancy, backend, and capacity gate and the reauthorized workspace lock, and
*before* the cold create branch. A miss, race, disabled policy, or unsupported
backend cold-falls-back with the inputs already validated for that launch -- no
divergent revalidation (preflight #28).

On a hit the claim is atomic (``engine.services.claim_ready_generation``), and the
installation-owned generation's principal and explicit account scope are moved
together through the native warm-claim command. Only then is the durable
``activate`` operation enqueued.
The provisioner's activation rotates/scrubs every pre-claim credential and access
surface and creates the claimant's fresh access; nothing user-specific is reused.

Compatibility is decided by the canonical digest (:mod:`shared.warm_pool.compatibility`):
the launch computes the digest of its immutable inputs and claims a ready
generation whose digest is equal. The digest excludes ``user_id`` -- warm
eligibility is the ownership-neutral RAES realization identity (registered
``package_digest`` + ``lock_digest``, #1607).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from shared.enums import RangeSource
from shared.range_instantiation_policy import backend_supports_warm_activation
from shared.warm_pool.compatibility import CompatibilityKey, compatibility_digest
from shared.warm_pool.policy import (
    WarmPoolBucketPolicy,
    WarmPoolOverride,
    WarmPoolRuntimePolicy,
    resolve_effective_policy,
)

from ._warm_claim_custody import _commit_cms_warm_claim as _commit_cms_warm_claim
from ._warm_claim_custody import _WarmClaimRollback
from ._warm_claim_types import WarmClaimRequest as WarmClaimRequest
from ._warm_claim_types import _ClaimOutcome, _WarmGeneration

if TYPE_CHECKING:
    from cms.models import RangeInstance
    from cms.services._model_allocation import PreparedClaimModelSources
    from cms.services._range_lease import RangeLease
    from engine.services import PreparedWarmRangeClaim
    from workspaces.services import WorkspaceQuotaAuditContext

logger = logging.getLogger(__name__)


# Warm preparation is customer-neutral. A claim supplies an exact account scope,
# including an optional organization/workspace ancestry, during the atomic move.
ISOLATION_ACCOUNT = "account"


def _eligible_buckets(policy: WarmPoolRuntimePolicy, *, backend: str, scenario: str) -> list[WarmPoolBucketPolicy]:
    """Return the effective-policy buckets that warm this backend+scenario.

    These are the only buckets a launch may claim from; an override that disables or
    narrows the bucket set removes candidates here, and the claim is filtered to
    exactly these bucket ids so a launch can never receive a generation from a
    bucket the current server-side (deployment + override) policy no longer
    authorizes (security: policy-to-allocation binding).
    """
    return [b for b in policy.buckets if b.backend == backend and b.scenario == scenario]


def _resolve_claim_candidates(request: WarmClaimRequest, override: WarmPoolOverride | None) -> list[tuple[str, str]]:
    """Return ``(bucket_id, digest)`` candidate pairs, or ``[]`` when warm is unavailable.

    Empty covers every not-attemptable branch (disabled/narrowed policy, a backend
    without a warm-activation adapter, or no eligible bucket) so the caller cold-
    falls-back on a single ``[]`` check.
    """
    from django.conf import settings

    policy: WarmPoolRuntimePolicy = resolve_effective_policy(settings.WARM_POOL_POLICY, override)
    if not policy.is_active() or not backend_supports_warm_activation(request.backend):
        # A bucket may target a backend without a warm-activation adapter; such a
        # generation can never be safely handed over, so never claim -- cold path.
        return []
    eligible = _eligible_buckets(policy, backend=request.backend, scenario=request.scenario)
    if not eligible or request.scope.kind != "account":
        return []
    # The digest is the launch's independently-resolved realization identity (same
    # for every eligible bucket of this scenario/backend). Authorization to a
    # specific bucket is the *separate* eligibility filter: candidates pair each
    # effective-policy bucket id with that digest, so the claim can only take a
    # generation whose realization matches AND whose bucket the current policy still
    # authorizes for this launch. No bucket-declared routing is echoed into the
    # digest; the bucket-set filter enforces policy-to-allocation binding.
    digest = compatibility_digest(
        CompatibilityKey(
            backend=request.backend,
            instantiation_purpose=request.instantiation_purpose.value,
            range_source=request.range_source.value,
            workspace_isolation_class=ISOLATION_ACCOUNT,
            egress_mode=request.egress_mode,
            scenario=request.scenario,
            package_digest=request.package_digest,
            lock_digest=request.lock_digest,
        )
    )
    return [(bucket.id, digest) for bucket in eligible]


def _run_atomic_claim(request: WarmClaimRequest, candidates: list[tuple[str, str]]) -> _ClaimOutcome | None:
    """Claim + move one ready generation in a single transaction.

    Returns the committed outcome, or ``None`` on a genuine miss or a rollback of an
    inconsistent ledger row (both cold-fall-back). The claim and ownership transfer
    share one transaction so a transfer failure returns the generation to READY.
    """
    from engine.services import (
        claim_ready_generation,
        prepare_warm_range_claim,
    )
    from shared.audit import principal_actor_fields
    from shared.warm_pool.metrics import CLAIM_FALLBACK, emit_claim_outcome
    from workspaces.services import (
        WorkspaceQuotaAuditContext,
        WorkspaceQuotaRejected,
        record_workspace_quota_rejection,
        resolve_range_scope_binding,
    )

    source_binding, source_metadata = _prepare_claim_source(request)
    prepared = prepare_warm_range_claim(request.credential, request.scope)
    quota_audit = WorkspaceQuotaAuditContext(**principal_actor_fields(prepared.credential.principal))

    prospective = _prepare_candidate_sources(request, candidates, prepared)
    if prospective is None:
        return None
    candidate, claim_sources = prospective
    try:
        with transaction.atomic():
            resolve_range_scope_binding(request.scope, member=prepared.credential.principal, for_update=True)
            from ._range_workspace import resolve_effective_egress_mode_locked

            workspace_id = prepared.scope_ids[3]
            egress = resolve_effective_egress_mode_locked(workspace_id) if workspace_id is not None else "none"
            if egress != request.egress_mode:
                # The ready generation's digest pins its original network mode.
                # Cold reservation re-resolves the changed policy under this lock.
                raise _WarmClaimRollback
            generation = claim_ready_generation(
                candidates=candidates,
                backend=request.backend,
                range_source=request.range_source.value,
                claimant_request_id=request.request_id,
                expected_request_id=candidate.request_id,
            )
            if generation is None:
                # Warm is enabled for this class but no compatible ready generation
                # in an authorized bucket was available: a genuine miss -> cold path.
                emit_claim_outcome(bucket_id="", backend=request.backend, outcome=CLAIM_FALLBACK)
                outcome = None
            else:
                outcome = _commit_claimed_generation(
                    request, generation, prepared, quota_audit, (source_binding, source_metadata), claim_sources
                )
    except WorkspaceQuotaRejected as rejected:
        record_workspace_quota_rejection(
            rejected.workspace_id,
            rejected.verdict,
            quota_audit,
            correlation_key=str(request.request_id),
        )
        from cms.exceptions import WorkspaceLaunchQuotaExceeded
        from cms.services._range_launch_common import _LAUNCH_QUOTA_MESSAGE

        raise WorkspaceLaunchQuotaExceeded(_LAUNCH_QUOTA_MESSAGE) from None
    except _WarmClaimRollback:
        return None
    return outcome


def _prepare_claim_source(request: WarmClaimRequest) -> tuple[dict[str, object], tuple[bool, bool]]:
    """Revalidate current source eligibility before provider reads and retain its exact facts."""
    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource
    from cms.services._scoped_range_launch import (
        revalidate_scoped_launch,
        source_launch_binding,
        source_launch_metadata,
    )

    source = RaesPackageSource.objects.filter(scenario_id=request.scenario).first()
    if source is None:
        raise CMSError("Range source unavailable")
    binding = source_launch_binding(source)
    metadata = source_launch_metadata(request.scenario)
    if (
        source.package_digest != request.package_digest
        or source.lock_digest != request.lock_digest
        or (request.source_binding is not None and dict(request.source_binding) != binding)
        or (request.source_metadata is not None and request.source_metadata != metadata)
        or not metadata[0]
    ):
        raise CMSError("Range source changed")
    revalidate_scoped_launch(request.credential, request.scope, source)
    return binding, metadata


def _can_claim_base_range(request: WarmClaimRequest) -> bool:
    """Require current parent authority and cold admission for any adapter binding."""
    # Warm compatibility currently describes the base realization only. A pack
    # with any plugin selection (including disabled/retired) must go through cold
    # admission; otherwise a ready base image could silently skip its adapter.
    from cms.services._content_authority import require_content_scope
    from engine.services import has_runtime_plugin_binding

    current = require_content_scope(request.credential, request.scope, "launch_range")
    if current.principal != request.credential.principal:
        return False
    organization_uuid = request.scope.organization_uuid
    return organization_uuid is None or not has_runtime_plugin_binding(organization_uuid, request.scenario)


def attempt_warm_claim(request: WarmClaimRequest, override: WarmPoolOverride | None = None) -> UUID | None:
    """Attempt to claim a compatible ready warm generation for this launch.

    Returns the claimed generation's Engine ``request_id`` on a hit (the launch
    binds its result to that realized generation), or ``None`` to cold-fall-back.
    Never raises for a miss; a miss is a normal branch.
    """
    from shared.warm_pool.metrics import CLAIM_HIT, emit_claim_outcome

    # Existing warm generations are not bound to an event's policy workspace.
    # An event-policy launch must take the cold reservation path, which pins that
    # policy under the workspace lock before dispatch.
    if (
        request.policy_workspace_id is not None
        or not (candidates := _resolve_claim_candidates(request, override))
        or not _can_claim_base_range(request)
    ):
        return None
    outcome = _run_atomic_claim(request, candidates)
    if outcome is None:
        return None
    emit_claim_outcome(bucket_id=outcome.bucket_id, backend=request.backend, outcome=CLAIM_HIT)
    logger.info(
        "warm claim: hit bucket=%s generation=%s principal=%s",
        outcome.bucket_id,
        outcome.generation_uuid,
        request.credential.principal.uuid,
    )
    return outcome.request_id


def _grant_claim_remote_access(request: WarmClaimRequest, generation: _WarmGeneration, lease: RangeLease) -> None:
    """Grant the claimant's OpenVPN authority before activation is composed (#2030).

    A warm generation is prepared without participant VPN; the claimant's
    capability is minted here, bounded by the claimant's lease ceiling, so the
    activation realizes a gateway owned by the claimant (ADR-039-R10).
    """
    from cms.services._range_remote_access import openvpn_deadline
    from engine.services import grant_raes_remote_access

    grant_raes_remote_access(generation.request_id, openvpn_deadline(request.backend, lease.maximum_expires_at))


def _admit_claim_models(
    request: WarmClaimRequest,
    generation: _WarmGeneration,
    instance: RangeInstance,
    claim_sources: PreparedClaimModelSources | None,
) -> bool:
    """Commit model grant/allocation and activation with the claim, or roll it all back."""
    from cms.services._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch
    from engine.services import resolve_model_access_range_views

    if not needs_model_preparation(generation.request_id):
        return False
    instance.refresh_from_db()
    instance.model_launch_scope = (
        request.model_launch_scope.model_dump(mode="json") if request.model_launch_scope else None
    )
    instance.model_sources = request.model_sources or {}
    instance.save(update_fields=["model_launch_scope", "model_sources"])
    (view,) = resolve_model_access_range_views(request_uuids=(generation.request_id,))
    prepare_model_access_for_dispatch(
        generation.request_id,
        range_id=view.range_uuid,
        renew=True,
        credential=request.credential,
        _claim_sources=claim_sources,
    )
    return True


def _system_range_instance_for(request_id: UUID) -> RangeInstance | None:
    """Return the system-owned CMS RangeInstance correlated to ``request_id``."""
    from cms.models import RangeInstance

    return (
        RangeInstance.objects.select_for_update()
        .filter(request__request_id=request_id)
        .select_related("request")
        .first()
    )


def _assign_initial_user_lease(range_instance: RangeInstance, lease: RangeLease) -> None:
    """First-assign the claimant's user lease onto an unleased warm generation (#27).

    A warm-prepared generation is system-owned and unleased, so the user lifetime
    starts at this claim -- persisting the deadline (the sole automatic-cleanup
    authority) and the generation's extension increment inside the same claim/ownership
    transaction, before activation. Guard on an unleased row (``maximum_expires_at is
    None``) so rehoming never resets an already-leased generation's ceiling (e.g. a CTF
    spare keeps its event deadline). The separate warm idle ledger continues to own
    pre-claim pool cost and retirement.
    """
    if range_instance.maximum_expires_at is not None:
        return
    range_instance.expires_at = lease.expires_at
    range_instance.maximum_expires_at = lease.maximum_expires_at
    range_instance.extension_days = lease.extension_days
    range_instance.lease_initial_days = lease.initial_days
    range_instance.lease_maximum_days = lease.maximum_days
    range_instance.lease_policy_source = lease.policy_source
    range_instance.lease_policy_tenant_revision = lease.tenant_revision
    range_instance.lease_policy_group_revisions = [
        {"group_id": group_id, "revision": revision} for group_id, revision in lease.group_revisions
    ]
    range_instance.save(
        update_fields=[
            "expires_at",
            "maximum_expires_at",
            "extension_days",
            "lease_initial_days",
            "lease_maximum_days",
            "lease_policy_source",
            "lease_policy_tenant_revision",
            "lease_policy_group_revisions",
            "updated_at",
        ]
    )


def _finish_claim(
    request: WarmClaimRequest,
    generation: _WarmGeneration,
    range_instance: RangeInstance,
    prepared: PreparedWarmRangeClaim,
    claim_sources: PreparedClaimModelSources | None,
) -> _ClaimOutcome:
    """Commit native custody, lease, access, model allocation and queue in the claim transaction."""
    from cms.services import AuditEvent, audit_log
    from engine.services import commit_warm_range_claim, enqueue_admitted_warm_activation
    from management.services import human_principal_user_id
    from shared.audit import principal_actor_fields
    from shared.audit.vocabulary import AuditAction, AuditEntityType

    user_id = (
        human_principal_user_id(prepared.credential.principal)
        if prepared.credential.principal.kind == "human"
        else None
    )
    _commit_cms_warm_claim(range_instance, prepared, user_id)
    commit_warm_range_claim(
        prepared,
        request_id=generation.request_id,
        claimant_request_id=request.request_id,
        cms_range_uuid=range_instance.uuid,
        user_id=user_id,
    )
    from cms.services._range_lease import build_range_lease, build_resolved_mission_control_range_lease

    lease = (
        build_resolved_mission_control_range_lease(prepared.credential.principal, for_update=True)
        if request.range_source is RangeSource.MISSION_CONTROL
        else build_range_lease(request.range_source, enforced_deadline=request.enforced_deadline)
    )
    _assign_initial_user_lease(range_instance, lease)
    _grant_claim_remote_access(request, generation, lease)
    _admit_claim_models(request, generation, range_instance, claim_sources)
    enqueue_admitted_warm_activation(prepared, request_id=generation.request_id, claimant_request_id=request.request_id)
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.RANGE.value,
            entity_id=range_instance.pk,
            entity_ref=str(range_instance.uuid),
            action=AuditAction.WARM_CLAIM.value,
            **principal_actor_fields(prepared.credential.principal),
            new_state={
                "bucket": generation.bucket_id,
                "generation": str(generation.uuid),
                "claimed_request_id": str(generation.request_id),
                "range_source": request.range_source.value,
            },
            request_id=str(generation.request_id),
        )
    )
    return _ClaimOutcome(
        request_id=generation.request_id,
        bucket_id=generation.bucket_id,
        generation_uuid=generation.uuid,
    )


def _prepare_candidate_sources(
    request: WarmClaimRequest, candidates: list[tuple[str, str]], prepared: PreparedWarmRangeClaim
) -> tuple[_WarmGeneration, PreparedClaimModelSources | None] | None:
    """Resolve model policy for the exact prospective warm generation before SQL locks."""
    from copy import copy

    from cms.models import RangeInstance
    from engine.services import preview_ready_generation

    from ._model_allocation import prepare_claim_model_sources

    candidate = preview_ready_generation(
        candidates=candidates, backend=request.backend, range_source=request.range_source.value
    )
    if candidate is None:
        return None
    prospective = (
        RangeInstance.objects.select_related("request").filter(request__request_id=candidate.request_id).first()
    )
    if prospective is None:
        return None
    prospective = copy(prospective)
    prospective.owner_principal_uuid = prepared.credential.principal.uuid
    prospective.scope_kind, prospective.account_id, prospective.organization_id, prospective.workspace_id = (
        prepared.scope_ids
    )
    prospective.model_launch_scope = (
        request.model_launch_scope.model_dump(mode="json") if request.model_launch_scope else None
    )
    prospective.model_sources = request.model_sources or {}
    return candidate, prepare_claim_model_sources(prospective, prepared.credential)


def _commit_claimed_generation(
    request: WarmClaimRequest,
    generation: _WarmGeneration,
    prepared: PreparedWarmRangeClaim,
    quota_audit: WorkspaceQuotaAuditContext,
    source_snapshot: tuple[dict[str, object], tuple[bool, bool]],
    claim_sources: PreparedClaimModelSources | None,
) -> _ClaimOutcome:
    """Fence source and CMS custody under the accepted ledger claim before atomic effects."""
    from cms.services._pack_fence import fence_pack_reference
    from workspaces.services import reserve_workspace_concurrent_range

    source_binding, source_metadata = source_snapshot
    workspace_id = prepared.scope_ids[3]
    range_instance = _system_range_instance_for(generation.request_id)
    if range_instance is None:
        # A claimed generation with no CMS range instance is an inconsistent
        # ledger row; abort the claim (rolls back) and cold-fall-back rather
        # than hand over a half-modeled range.
        logger.error("warm claim: generation %s has no CMS range instance; rolling back", generation.uuid)
        raise _WarmClaimRollback
    if workspace_id is not None:
        reserve_workspace_concurrent_range(workspace_id, generation.request_id, quota_audit)
    # Admission fence (ADR-034-R13): the claimed generation's pack revision
    # must still be registered when ownership transfers.
    source = fence_pack_reference(request.scenario, expected_digest=request.package_digest)
    from cms.services._scoped_range_launch import source_launch_binding, source_launch_metadata

    if (
        source is None
        or source_launch_binding(source) != source_binding
        or source_launch_metadata(request.scenario) != source_metadata
    ):
        from cms.exceptions import CMSError

        raise CMSError("Range source changed")
    return _finish_claim(request, generation, range_instance, prepared, claim_sources)
