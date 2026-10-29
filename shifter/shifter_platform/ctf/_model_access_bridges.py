"""CTF model-access integration through the public CMS service boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from shared.credentials import CredentialContext
    from shared.model_access import (
        AuthorityInvalidation,
        EventModelDemand,
        ModelAccessRangeInstanceView,
        ModelAccessRangeView,
        OwnedReference,
    )
    from shared.model_access.reservation import AuthorityRevision
    from shared.model_access.sources import ModelSourceSponsorship


def cms_project_model_launch_authority(
    *, deployment_id: UUID, authority_refs: tuple[OwnedReference, ...]
) -> tuple[AuthorityRevision, ...]:
    """Publish CTF facts checked under owner locks through the CMS/Engine bridge."""
    from cms.services import engine_project_model_launch_authority

    return engine_project_model_launch_authority(deployment_id=deployment_id, authority_refs=authority_refs)


def cms_resolve_model_access_range_instances(range_instance_ids: tuple[int, ...]) -> tuple[ModelAccessRangeView, ...]:
    """Resolve CTF-owned CMS instance PKs to canonical Engine range subjects."""
    import cms.services as cms_services

    return cms_services.resolve_model_access_range_instances(range_instance_ids)


def cms_resolve_model_access_range_reference(range_instance_id: int) -> OwnedReference:
    """Resolve one CTF-owned CMS instance PK to its range's sharing identity, in any state."""
    import cms.services as cms_services

    return cms_services.resolve_model_access_range_reference(range_instance_id)


def cms_find_model_access_selected_ranges(
    range_uuids: tuple[UUID, ...],
) -> tuple[ModelAccessRangeInstanceView, ...]:
    """Find the CMS instance correlations that exist for Engine range UUIDs."""
    import cms.services as cms_services

    return cms_services.find_model_access_selected_ranges(range_uuids)


def cms_resolve_model_access_selected_ranges(
    range_uuids: tuple[UUID, ...],
) -> tuple[ModelAccessRangeInstanceView, ...]:
    """Correlate explicit Engine range UUIDs to CMS instance identities."""
    import cms.services as cms_services

    return cms_services.resolve_model_access_selected_ranges(range_uuids)


def cms_invalidate_model_access_authority(command: AuthorityInvalidation) -> int:
    """Advance Engine's synchronous authority fence through the CMS boundary."""
    import cms.services as cms_services

    return cms_services.engine_invalidate_sharing_authority(command)


def cms_resolve_model_source_sponsorship(
    actor: CredentialContext, workspace_id: int, selection: object
) -> ModelSourceSponsorship:
    """Keep source authorization below the CTF ownership boundary."""
    from cms.services import resolve_model_source_sponsorship

    return resolve_model_source_sponsorship(actor, workspace_id, selection)


def cms_project_scenario_model_demands(scenario_id: str, *, expected_concurrency: int) -> tuple[EventModelDemand, ...]:
    """Resolve typed defaults from the scenario owner's verified envelope."""
    from cms.services import project_scenario_model_demands

    return project_scenario_model_demands(scenario_id, expected_concurrency=expected_concurrency)
