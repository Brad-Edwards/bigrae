"""Resolve source choices under the launch workspace's actual tenant authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, overload
from uuid import UUID

from shared.credentials import CredentialContext
from shared.model_access import EventModelDemand, ModelAccessCatalog
from shared.model_access.reservation import AuthorityRevision
from shared.model_access.sources import ModelSourceSponsorship

if TYPE_CHECKING:
    from cms.scenarios.model_needs import ScenarioModelNeedsProjection
    from engine.services import ModelSourceView
    from shared.identity_scope import ResourceScope
    from shared.model_access.core_models import EffectiveProfile, ModelAlias
    from shared.model_access.sources import ModelSourceConfiguration

from engine.services import compile_authorized_model_sources, project_authorized_model_sources
from shared.model_access import ContractError
from shared.model_access.sources import ModelSourceSelection, ModelSourceUseScope
from workspaces.services import AccountScopeError, hierarchy_target_scope, workspace_id_for_uuid, workspace_scope_for_id

SPONSOR_UNAVAILABLE = "source.sponsor_unavailable"


@overload
def resolve_launch_sources(
    actor: object, workspace_id: int | None, selection: object, *, catalog: ModelAccessCatalog
) -> tuple[ModelAccessCatalog, tuple[AuthorityRevision, ...]]:
    """Preserve the non-null catalog type when a deployment catalog is supplied."""
    ...


@overload
def resolve_launch_sources(
    actor: object, workspace_id: int | None, selection: object, *, catalog: None
) -> tuple[ModelAccessCatalog | None, tuple[AuthorityRevision, ...]]:
    """Preserve the optional catalog type when no deployment catalog is supplied."""
    ...


def resolve_launch_sources(
    actor: object, workspace_id: int | None, selection: object, *, catalog: ModelAccessCatalog | None
) -> tuple[ModelAccessCatalog | None, tuple[AuthorityRevision, ...]]:
    """Source selection conveys no permission to use another tenant's funds."""
    selected = ModelSourceSelection.model_validate(selection or {})
    if not selected.aliases:
        return catalog, ()
    if not isinstance(actor, CredentialContext) or workspace_id is None:
        raise ContractError("source.authority_unavailable")
    if catalog is None or not catalog.enabled:
        raise ContractError("source.policy_unavailable")
    organization_uuid, _workspace_uuid = _workspace_ancestry(workspace_id)
    return resolve_model_source_selection(actor, organization_uuid, selected, catalog=catalog)


def resolve_model_source_sponsorship(
    actor: CredentialContext, workspace_id: int, selection: object
) -> ModelSourceSponsorship:
    """Validate an organizer's sources before projecting event sponsorship."""
    from django.conf import settings

    from shared.model_access.sources import ModelSourceSponsorship

    organization_uuid, workspace_uuid = _workspace_ancestry(workspace_id)
    catalog = getattr(settings, "MODEL_ACCESS_CATALOG", None)
    if not getattr(settings, "MODEL_ACCESS_ENABLED", False) or catalog is None:
        raise ContractError("source.policy_unavailable")
    selected = ModelSourceSelection.model_validate(selection)
    resolve_model_source_selection(actor, organization_uuid, selected, catalog=catalog)
    from shared.model_access.sources import ModelSourceCredentialReference

    return ModelSourceSponsorship(
        credential_reference=ModelSourceCredentialReference.from_credential(actor),
        organization_uuid=organization_uuid,
        workspace_uuid=workspace_uuid,
        selection=selected,
    )


def resolve_sponsored_sources(
    sponsorship: ModelSourceSponsorship, *, catalog: ModelAccessCatalog
) -> tuple[ModelAccessCatalog, tuple[AuthorityRevision, ...]]:
    """Recheck persisted sponsor and tenant membership at participant admission."""
    try:
        from shared.credentials import resolve_current_credential

        actor = resolve_current_credential(sponsorship.credential_reference.credential())
        workspace_id = workspace_id_for_uuid(sponsorship.workspace_uuid)
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise ContractError(SPONSOR_UNAVAILABLE) from None
    current = resolve_model_source_sponsorship(actor, workspace_id, sponsorship.selection)
    if current != sponsorship:
        raise ContractError(SPONSOR_UNAVAILABLE)
    return resolve_model_source_selection(actor, sponsorship.organization_uuid, sponsorship.selection, catalog=catalog)


def _workspace_ancestry(workspace_id: int) -> tuple[UUID, UUID]:
    """Resolve one retained workspace through the SQL-owned hierarchy."""
    scope = workspace_scope_for_id(workspace_id)
    if scope.organization_uuid is None or scope.workspace_uuid is None:
        raise AccountScopeError(SPONSOR_UNAVAILABLE)
    return scope.organization_uuid, scope.workspace_uuid


def _source_use_scope(actor: CredentialContext, organization_uuid: UUID | None) -> ModelSourceUseScope:
    """Validate organization ancestry before Engine evaluates exact policy."""
    from workspaces.services import OrganizationAuthorizationError

    try:
        if organization_uuid is None:
            raise ValueError
        scope = hierarchy_target_scope("organization", organization_uuid)
        if scope.organization_uuid != organization_uuid:
            raise ValueError
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise OrganizationAuthorizationError("Organization access denied") from None
    return ModelSourceUseScope(credential=actor, organization_uuid=organization_uuid)


def list_usable_model_sources(actor: CredentialContext, organization_uuid: UUID | None) -> tuple[ModelSourceView, ...]:
    """Resolve current tenancy before requesting runtime source metadata."""
    return project_authorized_model_sources(_source_use_scope(actor, organization_uuid))


def resolve_model_source_selection(
    actor: CredentialContext, organization_uuid: UUID | None, selection: object, *, catalog: ModelAccessCatalog
) -> tuple[ModelAccessCatalog, tuple[AuthorityRevision, ...]]:
    """Compose tenancy authority with Engine-owned source grants and fences."""
    return compile_authorized_model_sources(_source_use_scope(actor, organization_uuid), selection, catalog=catalog)


def project_scenario_model_demands(scenario_id: str, *, expected_concurrency: int) -> tuple[EventModelDemand, ...]:
    """Use the verified authored envelope when an event has no demand override."""
    from cms.scenarios.model_needs import project_scenario_model_needs
    from shared.model_access import EventModelDemand

    projection = project_scenario_model_needs(scenario_id)
    if projection.resolution_failed or (projection.needs and not projection.digest_verified):
        raise ContractError("allocation.scenario_unavailable")
    return tuple(
        EventModelDemand(
            workload_role=need.workload_role,
            expected_concurrency=expected_concurrency,
            per_participant_requests=need.limits.max_requests_per_window,
            per_participant_input_tokens=need.limits.max_input_tokens,
            per_participant_output_tokens=need.limits.max_output_tokens,
            allowed_strategy=need.allowed_strategies[0],
        )
        for need in projection.needs.values()
    )


def model_source_alias_options(
    actor: CredentialContext, organization: UUID | None, scenario: str, *, scope: ResourceScope | None = None
) -> tuple[list[dict[str, object]], bool]:
    """Project compatible authorized choices for the scenario's verified needs."""

    from django.conf import settings

    from cms.scenarios.registry import check_scenario_access

    if scope is not None and scope.organization_uuid != organization:
        raise ValueError("Model source scope is unavailable")
    sources = list_usable_model_sources(actor, organization)
    catalog = getattr(settings, "MODEL_ACCESS_CATALOG", None)
    if not getattr(settings, "MODEL_ACCESS_ENABLED", False) or catalog is None:
        return [], False
    if not scenario:
        return [], True
    selected_scope = scope or _organization_scope(organization)
    check_scenario_access(scenario, actor, scope=selected_scope)
    projection = _verified_model_needs(scenario)
    profiles = {need.profile_id for need in projection.needs.values()}
    aliases = []
    for alias in catalog.aliases:
        if alias.profile_id not in profiles:
            continue
        aliases.append(_alias_options(alias, catalog, projection, sources))
    return aliases, True


def _source_fits_alias(
    source: ModelSourceView, alias: ModelAlias, catalog: ModelAccessCatalog, projection: ScenarioModelNeedsProjection
) -> bool:
    """Check one candidate against authored regions, capabilities and counting bounds."""
    from shared.model_access.policy import intersect_profile
    from shared.model_access.source_catalog import compile_source_catalog
    from shared.model_access.sources import ModelSourceConfiguration

    selected = ModelSourceSelection.model_validate(
        {
            "aliases": [
                {
                    "logical_alias": alias.logical_alias,
                    "sources": [{"source_id": str(source.id), "revision": source.revision}],
                }
            ]
        }
    )
    try:
        config = ModelSourceConfiguration.model_validate(source.configuration)
        compiled = compile_source_catalog(catalog, selected, {source.id: (source.revision, config)})
        profile = next(item for item in compiled.profiles if item.profile_id == alias.profile_id)
        effective = [
            intersect_profile(profile, need)
            for need in projection.needs.values()
            if need.profile_id == alias.profile_id
        ]
        return all(_effective_source_fit(config, item) for item in effective)
    except (ContractError, ValueError):
        return False


def _alias_options(
    alias: ModelAlias,
    catalog: ModelAccessCatalog,
    projection: ScenarioModelNeedsProjection,
    sources: tuple[ModelSourceView, ...],
) -> dict[str, object]:
    """Expose compatible choices and the alias's effective mixing policy."""
    from dataclasses import asdict

    candidates = [asdict(source) for source in sources if _source_fits_alias(source, alias, catalog, projection)]
    profile = next(item for item in catalog.profiles if item.profile_id == alias.profile_id)
    multiple = "weighted-rendezvous-v1" in profile.allowed_strategies and all(
        "weighted-rendezvous-v1" in need.allowed_strategies
        for need in projection.needs.values()
        if need.profile_id == alias.profile_id
    )
    return {"logical_alias": alias.logical_alias, "multiple_allowed": multiple, "sources": candidates}


def _effective_source_fit(config: ModelSourceConfiguration, profile: EffectiveProfile | None) -> bool:
    """Check geography and the full-context reservation required without free counting."""
    if profile is None or config.region not in profile.data_regions:
        return False
    return config.provider != "openrouter-v1" or profile.limits.max_input_tokens >= config.context_window_tokens


def _verified_model_needs(scenario: str) -> ScenarioModelNeedsProjection:
    """Require verified authored model needs before listing any compatible sources."""
    from cms.scenarios.model_needs import project_scenario_model_needs

    projection = project_scenario_model_needs(scenario)
    if projection.resolution_failed or (projection.needs and not projection.digest_verified):
        raise ContractError("source.scenario_unavailable")
    return projection


def _organization_scope(organization: UUID | None) -> ResourceScope | None:
    """Resolve only explicitly selected organization ancestry; absence selects no parent."""
    return hierarchy_target_scope("organization", organization) if organization is not None else None
