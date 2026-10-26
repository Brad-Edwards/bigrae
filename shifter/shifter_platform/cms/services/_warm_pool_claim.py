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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from django.db import transaction

from shared.credentials import CredentialContext
from shared.enums import RangeSource
from shared.identity_scope import ResourceScope
from shared.range_instantiation_policy import InstantiationPurpose, backend_supports_warm_activation
from shared.warm_pool.compatibility import CompatibilityKey, compatibility_digest
from shared.warm_pool.policy import (
    WarmPoolBucketPolicy,
    WarmPoolOverride,
    WarmPoolRuntimePolicy,
    resolve_effective_policy,
)

if TYPE_CHECKING:
    from cms.models import RangeInstance
    from cms.services._range_lease import RangeLease
    from shared.model_access.reservation import ModelLaunchScope

logger = logging.getLogger(__name__)


class _WarmGeneration(Protocol):
    """Narrow structural view received from the Engine claim service."""

    request_id: UUID


# Warm preparation is customer-neutral. A claim supplies an exact account scope,
# including an optional organization/workspace ancestry, during the atomic move.
ISOLATION_ACCOUNT = "account"


@dataclass(frozen=True)
class WarmClaimRequest:
    """Immutable launch inputs for one warm-pool claim attempt (#28).

    Groups the realization identity and launch context so the claim path derives
    the same compatibility digest the warm reconciler stamped, resolved
    independently on each side.
    """

    credential: CredentialContext
    scope: ResourceScope
    scenario: str
    package_digest: str
    lock_digest: str
    backend: str
    instantiation_purpose: InstantiationPurpose
    range_source: RangeSource
    egress_mode: str
    request_id: UUID
    #: Trusted event deadline for a CTF-owned generation. Mission Control policy is
    #: resolved only after a warm generation is claimed inside this transaction.
    enforced_deadline: datetime | None = None
    model_launch_scope: ModelLaunchScope | None = None
    model_sources: dict | None = None
    policy_workspace_id: int | None = None


@dataclass(frozen=True)
class _ClaimOutcome:
    """The result of a committed atomic claim, carried to the post-commit steps."""

    request_id: UUID
    bucket_id: str
    generation_uuid: UUID
    activation_enqueued: bool = False


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
    if not eligible:
        return []

    if request.scope.kind != "account":
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
    from cms.services import AuditEvent, audit_log
    from cms.services._pack_fence import fence_pack_reference
    from engine.services import claim_ready_generation, commit_warm_range_claim, prepare_warm_range_claim
    from management.services import human_principal_user_id
    from shared.audit import principal_actor_fields
    from shared.audit.vocabulary import AuditAction, AuditEntityType
    from shared.warm_pool.metrics import CLAIM_FALLBACK, emit_claim_outcome
    from workspaces.services import (
        WorkspaceQuotaAuditContext,
        WorkspaceQuotaRejected,
        record_workspace_quota_rejection,
        reserve_workspace_concurrent_range,
    )

    prepared = prepare_warm_range_claim(request.credential, request.scope)
    quota_audit = WorkspaceQuotaAuditContext(**principal_actor_fields(prepared.credential.principal))
    try:
        with transaction.atomic():
            generation = claim_ready_generation(
                candidates=candidates,
                backend=request.backend,
                range_source=request.range_source.value,
                claimant_request_id=request.request_id,
            )
            if generation is None:
                # Warm is enabled for this class but no compatible ready generation
                # in an authorized bucket was available: a genuine miss -> cold path.
                emit_claim_outcome(bucket_id="", backend=request.backend, outcome=CLAIM_FALLBACK)
                return None
            range_instance = _system_range_instance_for(generation.request_id)
            if range_instance is None:
                # A claimed generation with no CMS range instance is an inconsistent
                # ledger row; abort the claim (rolls back) and cold-fall-back rather
                # than hand over a half-modeled range.
                logger.error("warm claim: generation %s has no CMS range instance; rolling back", generation.uuid)
                raise _WarmClaimRollback
            workspace_id = prepared.scope_ids[3]
            if workspace_id is not None:
                reserve_workspace_concurrent_range(workspace_id, generation.request_id, quota_audit)
            # Admission fence (ADR-034-R13): the claimed generation's pack revision
            # must still be registered when ownership transfers.
            fence_pack_reference(request.scenario, expected_digest=request.package_digest)
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
            activation_enqueued = _admit_claim_models(request, generation, range_instance)
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
            outcome = _ClaimOutcome(
                request_id=generation.request_id,
                bucket_id=generation.bucket_id,
                generation_uuid=generation.uuid,
                activation_enqueued=activation_enqueued,
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
    from engine.services import enqueue_range_activation
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
    if not outcome.activation_enqueued:
        enqueue_range_activation(outcome.request_id, credential=request.credential)
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
) -> bool:
    """Commit model grant/allocation and activation with the claim, or roll it all back."""
    from cms.services._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch
    from engine.services import enqueue_range_activation, resolve_model_access_range_views

    if not needs_model_preparation(generation.request_id):
        return False
    instance.refresh_from_db()
    instance.model_launch_scope = (
        request.model_launch_scope.model_dump(mode="json") if request.model_launch_scope else None
    )
    instance.model_sources = request.model_sources or {}
    instance.save(update_fields=["model_launch_scope", "model_sources"])
    (view,) = resolve_model_access_range_views(request_uuids=(generation.request_id,))
    prepare_model_access_for_dispatch(generation.request_id, range_id=view.range_uuid, renew=True)
    enqueue_range_activation(generation.request_id, credential=request.credential)
    return True


class _WarmClaimRollback(Exception):
    """Internal sentinel to roll back an inconsistent claim to cold fallback."""


def _commit_cms_warm_claim(range_instance, prepared, user_id: int | None) -> None:
    """Move CMS intent and projection from installation custody to the claimant."""
    from cms.models import Request
    from shared.credentials import resolve_local_credential
    from shared.operation_authority import credential_reference_fields, credential_reference_from_record

    request = range_instance.request
    if request is None:
        raise _WarmClaimRollback
    original = resolve_local_credential(credential_reference_from_record(request))
    if (
        original.principal.kind != "service"
        or original.principal.uuid != range_instance.owner_principal_uuid
        or request.owner_principal_uuid != range_instance.owner_principal_uuid
        or range_instance.scope_kind != "installation"
        or request.scope_kind != "installation"
        or any(
            value is not None
            for value in (
                range_instance.account_id,
                range_instance.organization_id,
                range_instance.workspace_id,
                request.account_id,
                request.organization_id,
                request.workspace_id,
            )
        )
        or range_instance.status != "provisioning"
    ):
        raise _WarmClaimRollback
    kind, account_id, organization_id, workspace_id = prepared.scope_ids
    fields = {
        "user_id": user_id,
        "owner_principal_uuid": prepared.credential.principal.uuid,
        "scope_kind": kind,
        "account_id": account_id,
        "organization_id": organization_id,
        "workspace_id": workspace_id,
    }
    locked_request = Request.objects.select_for_update().get(pk=request.pk)
    for name, value in fields.items():
        setattr(range_instance, name, value)
        setattr(locked_request, "user_id" if name == "user_id" else name, value)
    authority = credential_reference_fields(prepared.credential)
    for name, value in authority.items():
        setattr(locked_request, name, value)
    range_instance.save(update_fields=[*fields, "updated_at"])
    locked_request.save(update_fields=[*fields, *authority])


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
