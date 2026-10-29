"""Bridge module for cross-domain integrations.

All external service calls from the CTF app go through this module.
This isolates domain boundaries -- if external APIs change, only
this file needs updating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._capacity_bridges import (
    cms_admit_range_capacity,
    cms_assess_event_capacity,
    cms_declare_event_capacity,
    cms_release_range_capacity,
)
from ._model_access_bridges import (
    cms_find_model_access_selected_ranges,
    cms_invalidate_model_access_authority,
    cms_project_model_launch_authority,
    cms_project_scenario_model_demands,
    cms_resolve_model_access_range_instances,
    cms_resolve_model_access_range_reference,
    cms_resolve_model_access_selected_ranges,
    cms_resolve_model_source_sponsorship,
)

__all__ = [
    "CTFRangeLaunchOptions",
    "RangeProvisionResult",
    "UserRole",
    "cms_admit_range_capacity",
    "cms_assess_event_capacity",
    "cms_confirm_ctf_receipt_binding",
    "cms_create_range",
    "cms_declare_event_capacity",
    "cms_destroy_event_range",
    "cms_destroy_range",
    "cms_destroy_recovery_range",
    "cms_dispatch_bound_event_range",
    "cms_fence_pack_reference",
    "cms_find_model_access_selected_ranges",
    "cms_find_range_instance_id",
    "cms_get_openvpn_profile",
    "cms_get_range_spec",
    "cms_get_range_status",
    "cms_get_range_target_instances",
    "cms_has_openvpn_profile",
    "cms_invalidate_model_access_authority",
    "cms_is_event_range_cleanup_verified_absent",
    "cms_list_scenarios",
    "cms_project_ctf_receipt_binding",
    "cms_project_model_launch_authority",
    "cms_project_scenario_images",
    "cms_project_scenario_model_demands",
    "cms_range_egress_compatible_with_event",
    "cms_range_owner_reassignment_available",
    "cms_reassign_range_owner",
    "cms_reconcile_ctf_range_leases",
    "cms_release_range_capacity",
    "cms_resolve_model_access_range_instances",
    "cms_resolve_model_access_range_reference",
    "cms_resolve_model_access_selected_ranges",
    "cms_resolve_model_source_sponsorship",
    "cms_start_range",
    "cms_stop_range",
    "get_user_role",
]

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from django.contrib.auth.models import User

    from ctf.models import CTFEvent
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.model_access import (
        OwnedReference,
    )
    from shared.model_access.reservation import ModelLaunchScope
    from shared.receipt_validation import ReceiptVerifierBinding
    from shared.remote_access import OpenVpnProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserRole:
    """CTF role information for a user."""

    is_ctf_organizer: bool
    is_ctf_participant: bool
    active_ctf_event: CTFEvent | None


def get_user_role(user: User) -> UserRole:
    """Get CTF role info for a user via Django Groups."""
    from management.services import get_user_profile
    from shared.auth import CTF_ORGANIZER_GROUP, CTF_PARTICIPANT_GROUP, get_user_group_names

    group_names = get_user_group_names(user)
    is_organizer = CTF_ORGANIZER_GROUP in group_names
    is_participant = CTF_PARTICIPANT_GROUP in group_names

    active_event = None
    if is_participant:
        profile = get_user_profile(user)
        event_id = profile.active_ctf_event_id
        if event_id:
            from ctf.models import CTFEvent

            active_event = CTFEvent.objects.filter(pk=event_id).first()

    return UserRole(
        is_ctf_organizer=is_organizer,
        is_ctf_participant=is_participant,
        active_ctf_event=active_event,
    )


@dataclass(frozen=True)
class RangeProvisionResult:
    """Result of a range provisioning request."""

    request_id: UUID


@dataclass(frozen=True)
class CTFRangeLaunchOptions:
    """Optional CTF launch facts forwarded to the CMS range boundary."""

    model_admission_subject: OwnedReference | None = None
    model_launch_scope: ModelLaunchScope | None = None
    credential: CredentialContext | None = None
    scope: ResourceScope | None = None
    owner_principal: PrincipalRef | None = None
    event_uuid: UUID | None = None
    request_uuid: UUID | None = None


def cms_project_scenario_images(scenario_id: str) -> dict[str, Any]:
    """Resolve a scenario's per-range image shape for capacity planning (PLAT-201).

    CMS owns scenario hydration, so the projection is resolved there and reaches
    CTF through the public service facade rather than a direct module import.
    """
    import cms.services as cms_services

    return cms_services.project_scenario_images(scenario_id).as_hint()


def cms_create_range(
    scenario: str,
    agents_by_os: dict[str, int],
    ngfw_enabled: bool,
    remote_access_teardown_at: datetime | None,
    launch_options: CTFRangeLaunchOptions | None = None,
) -> RangeProvisionResult:
    """Create a CTF range via CMS.

    Passes range_source=RangeSource.CTF so the CMS admission check is scoped
    to CTF ranges, allowing the user to hold both a Mission Control range and a
    CTF range simultaneously (#450). The source is server-derived here and is
    never caller-supplied.

    ``model_admission_subject`` is the CTF draw reference (PLAT-202) used to
    resolve the sharing overlap for required-model admission against the real
    launch subject rather than the launcher identity.
    """
    import cms.services as cms_services
    from shared.enums import RangeSource

    # RAES packages own topology; agents_by_os is accepted for caller back-compat
    # but does not shape the plan.
    del agents_by_os
    options = launch_options or CTFRangeLaunchOptions()
    if (
        options.credential is None
        or options.scope is None
        or options.owner_principal is None
        or options.event_uuid is None
    ):
        raise ValueError("CTF range authority unavailable")
    from shared.authorization import TargetRef

    result = cms_services.create_range_dispatch(
        user=options.credential,
        scenario=scenario,
        ngfw_enabled=ngfw_enabled,
        range_source=RangeSource.CTF,
        remote_access_teardown_at=remote_access_teardown_at,
        model_admission_subject=options.model_admission_subject,
        model_launch_scope=options.model_launch_scope,
        scope=options.scope,
        owner_principal=options.owner_principal,
        authority_action="event.manage_ranges",
        authority_target=TargetRef("event", options.event_uuid),
        request_uuid=options.request_uuid,
    )
    return RangeProvisionResult(request_id=result.request_id)


def cms_destroy_range(credential: CredentialContext, event_uuid: UUID, range_instance_id: int) -> None:
    """Destroy an event range via CMS under the event's exact authority."""
    import cms.services as cms_services

    cms_services.run_event_range_lifecycle(credential, event_uuid, range_instance_id, "destroy")


def cms_destroy_event_range(
    credential: CredentialContext, event_uuid: UUID, range_instance_id: int | None, *, request_id: UUID | None = None
) -> None:
    """Retain native cleanup ownership before the event aggregate is removed."""
    import cms.services as cms_services

    cms_services.destroy_event_range_for_deletion(credential, event_uuid, range_instance_id, request_id=request_id)


def cms_stop_range(credential: CredentialContext, event_uuid: UUID, range_instance_id: int) -> None:
    """Stop (pause) an event range via CMS under the event's exact authority."""
    import cms.services as cms_services

    cms_services.run_event_range_lifecycle(credential, event_uuid, range_instance_id, "pause")


def cms_start_range(credential: CredentialContext, event_uuid: UUID, range_instance_id: int) -> None:
    """Start (resume) an event range via CMS under the event's exact authority."""
    import cms.services as cms_services

    cms_services.run_event_range_lifecycle(credential, event_uuid, range_instance_id, "resume")


def cms_find_range_instance_id(request_id: str | UUID) -> int | None:
    """Find RangeInstance PK by provisioning request ID."""
    import cms.services as cms_services

    return cms_services.find_range_instance_id_by_request(request_id)


def cms_get_range_status(range_instance_id: int) -> str:
    """Get fresh range status from CMS."""
    import cms.services as cms_services

    return cms_services.get_range_status_by_id(range_instance_id)


def cms_project_ctf_receipt_binding(
    range_instance_id: int,
    *,
    owner_user_id: int,
    event_id: UUID,
    participant_id: UUID,
    profile_id: str,
    objective_id: str,
) -> ReceiptVerifierBinding:
    """Resolve the exact receipt registration through the public CMS boundary."""
    import cms.services as cms_services

    return cms_services.project_ctf_receipt_binding(
        range_instance_id,
        owner_user_id=owner_user_id,
        event_id=event_id,
        participant_id=participant_id,
        profile_id=profile_id,
        objective_id=objective_id,
    )


def cms_confirm_ctf_receipt_binding(
    range_instance_id: int,
    *,
    owner_user_id: int,
    objective_id: str,
    expected: ReceiptVerifierBinding,
) -> None:
    """Confirm an exact binding under CMS/Engine locks before scoring."""
    import cms.services as cms_services

    cms_services.confirm_ctf_receipt_binding(
        range_instance_id,
        owner_user_id=owner_user_id,
        objective_id=objective_id,
        expected=expected,
    )


def cms_get_range_target_instances(credential: CredentialContext, range_instance_id: int) -> list[dict[str, str]]:
    """Return the participant-safe target projection for one assigned range.

    Wraps ``cms.services.get_range_target_instances`` and projects each instance
    down to the participant-safe field allowlist (``uuid``, ``name``,
    ``private_ip``, ``os_type``). The projection is enforced here at the CTF/CMS
    boundary — not in a DRF serializer, which documents but does not filter a
    runtime ``Response`` dict — so range-internal metadata (roles, provider
    details, channel bindings, secret references) never crosses into the CTF
    participant surface (#1740). ``uuid`` is the identifier the Guacamole flow
    needs; the CMS range PK (``range_instance_id``) must never be sent instead.
    """
    import cms.services as cms_services

    return [
        {
            "uuid": str(instance.get("uuid") or ""),
            "name": str(instance.get("name") or ""),
            "private_ip": str(instance.get("private_ip") or ""),
            "os_type": str(instance.get("os_type") or ""),
        }
        for instance in cms_services.get_principal_range_target_instances(credential, range_instance_id)
    ]


def cms_is_event_range_cleanup_verified_absent(event_uuid: UUID, range_instance_id: int, request_id: UUID) -> bool:
    """Require owning CMS/native evidence for the exact recovery generation."""
    import cms.services as cms_services

    return cms_services.is_event_range_cleanup_verified_absent(event_uuid, range_instance_id, request_id)


def cms_get_range_spec(range_instance_id: int) -> dict | None:
    """Get range_spec dict from CMS RangeInstance."""
    import cms.services as cms_services

    return cms_services.get_range_spec_by_id(range_instance_id)


def cms_get_openvpn_profile(user: User | CredentialContext, range_instance_id: int) -> OpenVpnProfile:
    """Resolve a participant profile through the public CMS service boundary."""
    import cms.services as cms_services
    from ctf.exceptions import CTFNotFoundError, CTFRangeError, CTFStateError

    try:
        return cms_services.get_ctf_openvpn_profile(user, range_instance_id)
    except cms_services.OpenVpnProfilePending as exc:
        from ctf.exceptions import CTFVpnProfilePending

        raise CTFVpnProfilePending("VPN profile preparing") from exc
    except cms_services.CtfOpenVpnProfileNotFound as exc:
        raise CTFNotFoundError("VPN profile is not available") from exc
    except cms_services.CtfOpenVpnProfileConflict as exc:
        raise CTFStateError("VPN profile is not ready") from exc
    except cms_services.CtfOpenVpnProfileUnavailable as exc:
        raise CTFRangeError("VPN profile is unavailable") from exc


def cms_has_openvpn_profile(user: User | CredentialContext, range_instance_id: int) -> bool:
    """Project only safe OpenVPN readiness through CMS."""
    import cms.services as cms_services

    return cms_services.has_ctf_openvpn_profile(user, range_instance_id)


def cms_reconcile_ctf_range_leases(range_instance_ids: list[int], enforced_deadline: datetime) -> int:
    """Reconcile event-derived cleanup leases through the CMS boundary."""
    import cms.services as cms_services
    from ctf.exceptions import CTFValidationError

    try:
        return cms_services.reconcile_ctf_range_leases(range_instance_ids, enforced_deadline)
    except cms_services.RangeLeaseConflict as exc:
        raise CTFValidationError(
            "The requested cleanup time exceeds an existing range generation lifetime",
            code="CTF_RANGE_LEASE_CEILING",
        ) from exc


def cms_reassign_range_owner(
    credential: CredentialContext,
    range_instance_id: int,
    new_owner: PrincipalRef,
    event_uuid: UUID,
) -> None:
    """Reassign an event spare through the credential-native CMS boundary."""
    import cms.services as cms_services

    cms_services.reassign_event_range_owner(credential, range_instance_id, new_owner, event_uuid)


def cms_range_owner_reassignment_available(range_instance_id: int) -> bool:
    """Check spare-transfer safety through the public CMS boundary."""
    import cms.services as cms_services

    return cms_services.range_owner_reassignment_available(range_instance_id)


def cms_range_egress_compatible_with_event(range_instance_id: int, scope: ResourceScope) -> bool:
    """Check a spare's pinned posture against its event policy before reservation."""
    import cms.services as cms_services

    return cms_services.range_egress_compatible_with_scope(range_instance_id, scope)


def cms_fence_pack_reference(scenario_id: str) -> None:
    """Hold the CMS pack admission fence while an event or schedule references a pack.

    Call inside the transaction that commits the reference (ADR-034-R13). A
    scenario that was never registered stays allowed, as before; a pack that
    was removed is refused.

    Raises:
        CTFValidationError: the pack was removed or changed during admission.
    """
    import cms.services as cms_services
    from ctf.exceptions import CTFValidationError
    from shared.exceptions import CMSError

    try:
        cms_services.fence_pack_reference(scenario_id, required=False)
    except CMSError:
        raise CTFValidationError(
            "Scenario is not available for CTF event creation.", code="CTF_SCENARIO_NOT_AVAILABLE"
        ) from None


def cms_list_scenarios(user: User | CredentialContext, *, scope: ResourceScope | None = None) -> list[tuple[str, str]]:
    """List CTF-event-selectable scenarios as (id, name) tuples for form choices.

    CTF event creation is a launch workflow, so this returns only scenarios that
    are launchable for the ``ctf_event`` workflow (legacy YAML/DB scenarios plus
    any launchable RAES package entries); non-launchable RAES review entries are
    excluded. Staff review of non-launchable entries lives in the CMS scenario
    editor, not in CTF event selection.

    Args:
        user: Requesting user (used for access filtering).

    Returns:
        List of (scenario_id, name) tuples sorted by name.
    """
    import cms.services as cms_services

    scenarios = cms_services.list_launchable_scenarios(user, "ctf_event", scope=scope)
    return [(s["id"], s["name"]) for s in scenarios]


def cms_dispatch_bound_event_range(credential: CredentialContext, event_uuid: UUID, range_instance_id: int) -> None:
    """Activate one persisted launch after CTF has committed its exact association."""
    from cms.services import dispatch_bound_event_range

    dispatch_bound_event_range(credential, event_uuid, range_instance_id)


def cms_destroy_recovery_range(
    credential: CredentialContext, event_uuid: UUID, range_instance_id: int | None, *, request_id: UUID | None = None
) -> bool:
    """Retain generation-bound cleanup before a recovery changes its association."""
    from cms.services import destroy_event_range_for_recovery

    return destroy_event_range_for_recovery(credential, event_uuid, range_instance_id, request_id=request_id)
