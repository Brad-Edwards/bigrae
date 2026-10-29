"""Product-neutral CMS authorization boundary for OpenVPN profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cms.exceptions import CMSError
from shared.credentials import CredentialContext
from shared.enums import RangeSource, ResourceStatus
from workspaces.services import WorkspaceOperation

from ._range_workspace import authorize_range_workspace

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import User

    from cms.models import RangeInstance
    from shared.credentials import CredentialContext
    from shared.enums import RangeSource
    from shared.remote_access import OpenVpnProfile


_RANGE_NOT_FOUND = "Range not found"


class OpenVpnProfileNotFound(CMSError):
    """The caller has no matching product range (non-enumerating)."""


class OpenVpnProfileConflict(CMSError):
    """The owned range is not currently eligible for profile delivery."""


class OpenVpnProfileUnavailable(CMSError):
    """The profile binding or provider material cannot currently be resolved."""


class OpenVpnProfilePending(OpenVpnProfileConflict):
    """An individual profile is preparing through the existing provisioner."""


# Compatibility names retained for the CTF bridge and existing imports.
CtfOpenVpnProfileNotFound = OpenVpnProfileNotFound
CtfOpenVpnProfileConflict = OpenVpnProfileConflict
CtfOpenVpnProfileUnavailable = OpenVpnProfileUnavailable


def _load_owned_range(
    user: User | CredentialContext,
    range_source: RangeSource,
    range_instance_pk: int | None = None,
    *,
    request_id: UUID | None = None,
) -> RangeInstance:
    """Load one active, caller-owned product range without crossing sources."""
    from cms.models import RangeInstance

    if isinstance(user, CredentialContext):
        return _load_principal_range(user, range_source, range_instance_pk, request_id)

    if getattr(user, "id", None) is None:
        raise OpenVpnProfileNotFound(_RANGE_NOT_FOUND)
    query = RangeInstance.objects.select_related("request").filter(
        user_id=user.id,
        range_source=range_source.value,
    )
    if range_instance_pk is not None:
        query = query.filter(pk=range_instance_pk)
    instance = query.first()
    if instance is None:
        raise OpenVpnProfileNotFound(_RANGE_NOT_FOUND)
    try:
        authorize_range_workspace(user, instance.workspace_id, WorkspaceOperation.ACCESS_RANGE)
    except CMSError as exc:
        raise OpenVpnProfileNotFound(_RANGE_NOT_FOUND) from exc
    if instance.status != ResourceStatus.READY.value:
        raise OpenVpnProfileConflict("Range is not ready")
    if instance.request is None:
        raise OpenVpnProfileUnavailable("Range access is unavailable")
    return instance


def _resolve_profile(user: User | CredentialContext, instance: RangeInstance) -> OpenVpnProfile:
    """Resolve an Engine profile while preserving the CMS error vocabulary."""
    from cms import services as cms_services
    from engine.services import VpnProfileConflict, VpnProfileNotFound, VpnProfilePending, VpnProfileUnavailable

    request = instance.request
    if request is None:
        raise OpenVpnProfileUnavailable("Range access is unavailable")
    try:
        if isinstance(user, CredentialContext) and instance.range_source == RangeSource.CTF.value:
            return cms_services.engine_get_openvpn_profile(user, request.request_id, participation=True)
        return cms_services.engine_get_openvpn_profile(user, request.request_id)
    except VpnProfilePending as exc:
        raise OpenVpnProfilePending("VPN profile preparing") from exc
    except VpnProfileNotFound as exc:
        raise OpenVpnProfileNotFound(_RANGE_NOT_FOUND) from exc
    except VpnProfileConflict as exc:
        raise OpenVpnProfileConflict("Range VPN profile is not ready") from exc
    except VpnProfileUnavailable as exc:
        raise OpenVpnProfileUnavailable("Range VPN profile is unavailable") from exc


def _has_profile(user: User | CredentialContext, instance: RangeInstance) -> bool:
    """Return whether Engine has a profile for the owned range generation."""
    from cms import services as cms_services

    request = instance.request
    if request is None:
        return False
    return cms_services.engine_has_openvpn_profile(
        user,
        request.request_id,
        participation=isinstance(user, CredentialContext) and instance.range_source == RangeSource.CTF.value,
    )


def get_ctf_openvpn_profile(user: User | CredentialContext, range_instance_pk: int) -> OpenVpnProfile:
    """Return the profile for one caller-owned, ready CTF range."""
    return _resolve_profile(user, _load_owned_range(user, RangeSource.CTF, range_instance_pk))


def has_ctf_openvpn_profile(user: User | CredentialContext, range_instance_pk: int) -> bool:
    """Return whether one caller-owned CTF range has a ready profile."""
    try:
        instance = _load_owned_range(user, RangeSource.CTF, range_instance_pk)
    except CMSError:
        return False
    return _has_profile(user, instance)


def get_mission_control_openvpn_profile(
    user: User | CredentialContext, *, request_id: UUID | None = None
) -> tuple[OpenVpnProfile, int]:
    """Return the active Mission Control profile and range-instance ID."""
    instance = _load_owned_range(user, RangeSource.MISSION_CONTROL, request_id=request_id)
    return _resolve_profile(user, instance), instance.pk


def has_mission_control_openvpn_profile(user: User | CredentialContext, *, request_id: UUID | None = None) -> bool:
    """Return whether the caller's active Mission Control range has a profile."""
    try:
        instance = _load_owned_range(user, RangeSource.MISSION_CONTROL, request_id=request_id)
    except CMSError:
        return False
    return _has_profile(user, instance)


def _load_principal_range(
    user: CredentialContext, range_source: RangeSource, range_instance_pk: int | None, request_id: UUID | None
) -> RangeInstance:
    """Select and authorize the exact principal-owned VPN target before delivery."""
    from cms.models import RangeInstance

    from ._vpn_authority import admit_range_vpn_authority

    query = RangeInstance.objects.select_related("request").filter(range_source=range_source.value)
    if range_instance_pk is not None:
        query = query.filter(pk=range_instance_pk)
    elif request_id is not None:
        query = query.filter(request__request_id=request_id)
    else:
        query = query.filter(owner_principal_uuid=user.principal.uuid)
    instance = query.filter(status=ResourceStatus.READY.value).order_by("-created_at").first()
    if instance is None or instance.request is None:
        raise OpenVpnProfileNotFound(_RANGE_NOT_FOUND)
    try:
        admit_range_vpn_authority(user, instance.request.request_id, participation=range_source == RangeSource.CTF)
    except Exception:
        raise OpenVpnProfileNotFound(_RANGE_NOT_FOUND) from None
    return instance
