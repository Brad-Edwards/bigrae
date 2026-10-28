"""Resolve CMS-owned model launch facts before the atomic Engine outbox write."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from django.conf import settings
from django.db import transaction

from cms.models import RangeInstance
from cms.scenarios.model_needs import project_scenario_model_needs
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.model_access import ContractError, EventModelDemand, ModelAccessCatalog, OwnedReference
from shared.model_access.core_models import ScenarioNeed
from shared.model_access.reservation import (
    ModelLaunchScope,
    ModelWarmScope,
    SystemPreparationAuthority,
    WarmPreparationAuthority,
)
from shared.operation_authority import credential_reference_from_record

_SCENARIO_UNAVAILABLE = "allocation.scenario_unavailable"


_AUTHORITY_UNAVAILABLE = "allocation.authority_unavailable"


def needs_model_preparation(request_id: UUID | str) -> bool:
    """Route lifecycle through the durable snapshot, not a mutable overlay alone."""
    from engine.services import get_model_launch_preparation

    if get_model_launch_preparation(UUID(str(request_id))) is not None:
        return True
    instance = RangeInstance.objects.filter(request__request_id=request_id).first()
    if instance is None:
        return False
    projection = project_scenario_model_needs(instance.scenario_id, expected_digest=instance.model_package_digest)
    if projection.resolution_failed:
        raise ContractError(_SCENARIO_UNAVAILABLE)
    return bool(projection.needs)


def _snapshot_needs(instance: RangeInstance) -> tuple[ScenarioNeed, ...]:
    """One package-bound identity controls both lifecycle routing and preparation."""
    from engine.services import get_model_launch_preparation

    assert instance.request is not None
    prepared = get_model_launch_preparation(instance.request.request_id)
    if prepared is not None:
        if any(need.scenario_digest != instance.model_package_digest for need in prepared.needs):
            raise ContractError(_SCENARIO_UNAVAILABLE)
        return prepared.needs
    projection = project_scenario_model_needs(instance.scenario_id, expected_digest=instance.model_package_digest)
    if projection.resolution_failed or (projection.needs and not projection.digest_verified):
        raise ContractError(_SCENARIO_UNAVAILABLE)
    return tuple(projection.needs.values())


def _warm_scope(instance: RangeInstance, deployment_id: UUID) -> ModelWarmScope | None:
    """Resolve warm authority only for generations without a user lease."""
    if instance.expires_at is not None:
        return None
    from engine.services import get_model_warm_scope

    assert instance.request is not None
    with transaction.atomic():
        return get_model_warm_scope(instance.request.request_id, deployment_id=deployment_id)


def _standalone_scope(
    instance: RangeInstance,
    range_id: UUID,
    needs: tuple[ScenarioNeed, ...],
    deployment_id: UUID,
) -> ModelLaunchScope:
    """A non-event commitment uses the authored envelope and real lease window."""
    assert instance.request is not None
    warm = _warm_scope(instance, deployment_id)
    if instance.range_source == "ctf" or (instance.expires_at is None and warm is None):
        raise ContractError("allocation.scope_unavailable")
    if warm:
        kind: Literal["warm", "standalone"] = "warm"
        scope_id = warm.scope_id
        draw_key = warm.draw_key
        window_start = warm.window_start
        window_end = warm.window_end
        revisions = warm.authority_revisions
        system = WarmPreparationAuthority(
            owner_ref=_owner_reference(instance),
            generation_id=warm.generation_id,
        )
    else:
        assert instance.expires_at is not None
        kind = "standalone"
        scope_id = draw_key = instance.request.request_id
        window_start = instance.created_at
        window_end = instance.expires_at
        revisions = ()
        system = None
    return ModelLaunchScope(
        kind=kind,
        scope_id=scope_id,
        draw_key=draw_key,
        subject_ref=OwnedReference(owner="deployment", reference=f"range:{range_id}"),
        window_start=window_start,
        window_end=window_end,
        authority_revisions=revisions,
        system_preparation=system,
        demands=tuple(
            EventModelDemand(
                workload_role=need.workload_role,
                expected_concurrency=1,
                per_participant_requests=need.limits.max_requests_per_window,
                per_participant_input_tokens=need.limits.max_input_tokens,
                per_participant_output_tokens=need.limits.max_output_tokens,
                allowed_strategy=need.allowed_strategies[0],
            )
            for need in needs
        ),
    )


def _resolve_model_catalog(
    request_id: UUID, owner: OwnedReference, needs: tuple[ScenarioNeed, ...]
) -> ModelAccessCatalog | None:
    """Return enabled policy or persist the permitted optional absence."""
    catalog = cast(ModelAccessCatalog | None, getattr(settings, "MODEL_ACCESS_CATALOG", None))
    if getattr(settings, "MODEL_ACCESS_ENABLED", False) and catalog is not None and catalog.enabled:
        return catalog
    if any(need.required for need in needs):
        raise ContractError("allocation.policy_unavailable")
    from engine.services import disable_optional_model_preparation

    disable_optional_model_preparation(request_id=request_id, owner_ref=owner, needs=needs)
    return None


def _owner_reference(instance: RangeInstance) -> OwnedReference:
    """Use the durable principal identity; a contact user is never authority."""
    if instance.owner_principal_uuid is None:
        raise ContractError(_AUTHORITY_UNAVAILABLE)
    return OwnedReference(owner="management", reference=f"principal:{instance.owner_principal_uuid}")


def _authorize_preparation(
    instance: RangeInstance,
    owner: OwnedReference,
    system: SystemPreparationAuthority | WarmPreparationAuthority | None,
    *,
    credential: CredentialContext | None = None,
    renew: bool = False,
    authority_action: str = "range.manage",
    authority_target=None,
) -> CredentialContext:
    """Revalidate the exact initiating credential before delayed provider work."""
    assert instance.request is not None
    try:
        if renew:
            if credential is None:
                raise ContractError(_AUTHORITY_UNAVAILABLE)
            current = resolve_current_credential(credential)
            from ._unified_authorization import require_range_action

            require_range_action(current, instance.uuid, authority_action, authority_target=authority_target)
        else:
            current = resolve_current_credential(credential_reference_from_record(instance.request))
        if not renew and current.principal.uuid != instance.owner_principal_uuid:
            if instance.range_source != "ctf" or instance.event_uuid is None:
                raise ContractError(_AUTHORITY_UNAVAILABLE)
            from shared.authorization import (
                AuthorizationRequest,
                TargetRef,
                configured_authorization_provider,
            )
            from workspaces.services import resource_scope_from_ids

            scope = resource_scope_from_ids(
                kind=instance.scope_kind,
                account_id=instance.account_id,
                organization_id=instance.organization_id,
                workspace_id=instance.workspace_id,
            )
            decision = configured_authorization_provider().check(
                AuthorizationRequest(
                    current.principal,
                    "event.manage_ranges",
                    TargetRef("event", instance.event_uuid),
                    scope,
                    current.ceiling,
                )
            )
            if not decision.allowed:
                raise ContractError(_AUTHORITY_UNAVAILABLE)
        if system is not None:
            warm = (
                system.owner_ref == owner
                and system.kind == "warm_pool"
                and current.principal.kind == "service"
                and instance.user_id is None
                and instance.request.user_id is None
                and instance.scope_kind == "installation"
                and instance.request.scope_kind == "installation"
                and all(
                    value is None
                    for value in (
                        instance.account_id,
                        instance.organization_id,
                        instance.workspace_id,
                        instance.request.account_id,
                        instance.request.organization_id,
                        instance.request.workspace_id,
                    )
                )
            )
            ctf_spare = (
                system.owner_ref == owner
                and system.kind == "ctf_spare"
                and instance.range_source == "ctf"
                and instance.event_uuid is not None
                and instance.request.scope_kind == instance.scope_kind == "account"
            )
            if not (warm or ctf_spare):
                raise ContractError("allocation.system_owner_unavailable")
        return current
    except Exception:
        raise ContractError(_AUTHORITY_UNAVAILABLE) from None


def _source_policy_scope(instance: RangeInstance, scope: ModelLaunchScope) -> ModelLaunchScope:
    """Apply the range's current source-policy revision and optional sponsorship."""
    from shared.model_access.sources import ModelSourceSponsorship

    scope_updates: dict[str, object] = {"source_policy_revision": instance.model_source_policy_revision}
    if instance.model_source_sponsorship is not None:
        scope_updates["source_sponsorship"] = ModelSourceSponsorship.model_validate(instance.model_source_sponsorship)
    return scope.model_copy(update=scope_updates)


def _source_authority_refs(
    instance: RangeInstance, scope: ModelLaunchScope, refs: tuple[OwnedReference, ...], has_sources: bool
) -> tuple[OwnedReference, ...]:
    """Include sponsor or launch-tenant authority in the admission fence vector."""
    if scope.source_sponsorship:
        sponsor = OwnedReference(
            owner="management",
            reference=f"principal:{scope.source_sponsorship.credential_reference.principal_uuid}",
        )
        sponsor_workspace = OwnedReference(
            owner="workspaces", reference=f"workspace:{scope.source_sponsorship.workspace_uuid}"
        )
        sponsor_organization = OwnedReference(
            owner="workspaces", reference=f"organization:{scope.source_sponsorship.organization_uuid}"
        )
        refs = tuple(dict.fromkeys((*refs, sponsor, sponsor_workspace, sponsor_organization)))
    elif has_sources:
        if instance.workspace_id is None:
            raise ContractError(_AUTHORITY_UNAVAILABLE)
        from cms.services._model_source_selection import _workspace_ancestry

        organization_uuid, _ = _workspace_ancestry(instance.workspace_id)
        refs += (OwnedReference(owner="workspaces", reference=f"organization:{organization_uuid}"),)
    return refs


_MODEL_SOURCE_FENCE_FIELDS = (
    "request_id",
    "range_id",
    "owner_principal_uuid",
    "scope_kind",
    "account_id",
    "organization_id",
    "workspace_id",
    "model_package_digest",
    "model_source_policy_revision",
    "model_launch_scope",
    "model_sources",
    "model_source_sponsorship",
)


@dataclass(frozen=True, slots=True)
class PreparedClaimModelSources:
    """Provider decisions for the exact prospective warm claim, before aggregate locks."""

    credential: CredentialContext
    instance_pk: int
    facts: tuple[object, ...]
    catalog: ModelAccessCatalog
    source_revisions: tuple


def prepare_claim_model_sources(
    instance: RangeInstance, credential: CredentialContext
) -> PreparedClaimModelSources | None:
    """Resolve claimant-funded sources and refresh policy before changing warm custody."""
    from shared.model_access.projection_port import refresh_launch_projections

    from ._model_source_selection import resolve_launch_sources

    needs = _snapshot_needs(instance)
    if not needs:
        return None
    if instance.request is None:
        raise ContractError("allocation.authority_unavailable")
    catalog = _resolve_model_catalog(instance.request.request_id, _owner_reference(instance), needs)
    if catalog is None:
        return None
    catalog, revisions = resolve_launch_sources(
        credential, instance.workspace_id, instance.model_sources, catalog=catalog
    )
    refresh_launch_projections(catalog.deployment_id)
    return PreparedClaimModelSources(
        credential,
        instance.pk,
        deepcopy(tuple(getattr(instance, field) for field in _MODEL_SOURCE_FENCE_FIELDS)),
        catalog,
        revisions,
    )


def prepare_model_access_for_dispatch(
    request_id: UUID,
    *,
    range_id: UUID,
    renew: bool = False,
    credential: CredentialContext | None = None,
    authority_action: str = "range.manage",
    authority_target=None,
    _claim_sources: PreparedClaimModelSources | None = None,
) -> None:
    """Authenticate current persisted ownership, then hand closed facts downward.

    Called after Range creation invalidates selectors and before enqueue. The
    caller does not hold range-row locks during policy-provider work. Missing
    policy, scope, or authority denies required use.
    """
    from engine.services import get_model_launch_preparation, prepare_model_launch, project_model_launch_authority

    instance = RangeInstance.objects.select_related("request__user").get(request__request_id=request_id)
    prepared = get_model_launch_preparation(request_id)
    needs = _snapshot_needs(instance)
    if not needs:
        return
    if prepared is not None and prepared.unavailable_reason and not renew:
        # The first operation's optional absence is immutable. A later
        # lifecycle generation may explicitly renew against current policy.
        return
    owner = _owner_reference(instance)
    catalog = _resolve_model_catalog(request_id, owner, needs)
    if catalog is None:
        return
    scope = (
        ModelLaunchScope.model_validate(instance.model_launch_scope)
        if instance.model_launch_scope
        else _standalone_scope(instance, range_id, needs, catalog.deployment_id)
    )

    scope = _source_policy_scope(instance, scope)
    system = scope.system_preparation
    if _claim_sources is not None:
        if (
            not renew
            or credential != _claim_sources.credential
            or instance.pk != _claim_sources.instance_pk
            or tuple(getattr(instance, field) for field in _MODEL_SOURCE_FENCE_FIELDS) != _claim_sources.facts
        ):
            raise ContractError(_AUTHORITY_UNAVAILABLE)
        catalog = _claim_sources.catalog
        source_revisions = _claim_sources.source_revisions
    else:
        credential = _authorize_preparation(
            instance,
            owner,
            system,
            credential=credential,
            renew=renew,
            authority_action=authority_action,
            authority_target=authority_target,
        )
        from cms.services._model_source_selection import resolve_launch_sources, resolve_sponsored_sources

        if scope.source_sponsorship:
            catalog, source_revisions = resolve_sponsored_sources(scope.source_sponsorship, catalog=catalog)
        else:
            catalog, source_revisions = resolve_launch_sources(
                credential,
                instance.workspace_id,
                instance.model_sources,
                catalog=catalog,
            )
    refs = (() if system else (owner,)) + (OwnedReference(owner="engine", reference=f"model-launch:{range_id}"),)
    if instance.workspace_id is not None:
        refs += (OwnedReference(owner="workspaces", reference=f"workspace-id:{instance.workspace_id}"),)
    refs = _source_authority_refs(instance, scope, refs, bool(source_revisions))
    from shared.model_access.projection_port import refresh_launch_projections

    if _claim_sources is None:
        refresh_launch_projections(catalog.deployment_id)
    revisions = project_model_launch_authority(deployment_id=catalog.deployment_id, authority_refs=refs)
    revisions += scope.authority_revisions + source_revisions
    fence_fields = (
        "request_id",
        "range_id",
        "owner_principal_uuid",
        "scope_kind",
        "account_id",
        "organization_id",
        "workspace_id",
        "model_package_digest",
        "model_source_policy_revision",
        "model_launch_scope",
        "model_sources",
        "model_source_sponsorship",
        "expires_at",
    )
    fence = {name: getattr(instance, name) for name in fence_fields}
    with transaction.atomic():
        current_instance = RangeInstance.objects.select_for_update().get(pk=instance.pk)
        if resolve_local_credential(credential) != credential or any(
            getattr(current_instance, name) != value for name, value in fence.items()
        ):
            raise ContractError(_AUTHORITY_UNAVAILABLE)
        prepare_model_launch(
            request_id=request_id,
            owner_ref=owner,
            needs=needs,
            scope=scope,
            authority_revisions=revisions,
            catalog=catalog,
            replace_revoked=renew,
        )


def resume_model_range(request_id: UUID) -> bool:
    """Join status transition, owner reauthorization and resumed launch atomically."""
    from engine.services import (
        dispatch_prepared_range_resume,
        get_authoritative_range_status,
        resolve_model_access_range_views,
        resume_range,
    )

    with transaction.atomic():
        if not resume_range(request_id, defer_dispatch=True):
            return False
        if get_authoritative_range_status(request_id=request_id) == "ready":
            return True
        (view,) = resolve_model_access_range_views(request_uuids=(request_id,))
        prepare_model_access_for_dispatch(request_id, range_id=view.range_uuid, renew=True)
        if not dispatch_prepared_range_resume(request_id):
            raise ContractError("allocation.dispatch_unavailable")
        return True
