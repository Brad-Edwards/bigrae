"""Typed RAES sidecars, immutable replay checks and remote-access capability bindings."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from shared.raes.artifact_binding import ArtifactBinding
from shared.raes.content_delivery import DeliveryBinding
from shared.raes.participant_access import ParticipantAccessBinding
from shared.remote_access import build_openvpn_capability, raes_member_target_ref

from ._runtime_plugin_bindings import retained_runtime_plugin_pin

if TYPE_CHECKING:
    from engine.models import Range
    from shared.authorization import TargetRef
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.runtime_plugin_binding import RuntimePluginScope

    from ._principal_range_create import PreparedRaesRangeCreation


@dataclass(frozen=True)
class RangeBindings:
    """The byte-free sidecar bindings persisted beside a Range, in one argument.

    Groups the three binding collections ``create_raes_range`` persists in the
    same transaction as the Range -- content delivery (#1564, ``delivery``),
    participant access (#1710, ``participant_access``), and generation-fenced
    artifacts (#1580, ``artifact``) -- so the create seam takes one cohesive
    argument. All three default empty; the common create passes none.
    """

    delivery: tuple[DeliveryBinding, ...] = ()
    participant_access: tuple[ParticipantAccessBinding, ...] = ()
    artifact: tuple[ArtifactBinding, ...] = ()
    runtime_plugin_scope: RuntimePluginScope | None = None
    # CMS may finish downward authorization after Range creation signals, then
    # dispatch in the same enclosing transaction. No worker sees a partial launch.
    defer_dispatch: bool = False
    # The CMS-admitted OpenVPN credential deadline (the lease ceiling), or None
    # when the launch requests no VPN (ADR-039-R10, #2030). Engine mints the
    # capability against the persisted participant access and reserves a slot.
    openvpn_deadline: datetime | None = None
    credential: CredentialContext | None = None
    resource_scope: ResourceScope | None = None
    cms_range_uuid: UUID | None = None
    # Event launches deliberately separate the credential that requested the
    # work from the principal that owns the resulting range.  For ordinary
    # launches these remain unset and the credential principal is the owner.
    owner_principal: PrincipalRef | None = None
    authority_action: str | None = None
    authority_target: TargetRef | None = None
    prepared_creation: PreparedRaesRangeCreation | None = None


def mint_remote_access_capability(
    participant_access: Sequence[ParticipantAccessBinding],
    deadline: datetime | None,
    range_backend: str,
) -> dict[str, object] | None:
    """Mint OpenVPN authority for the sole participant-access target, when requested.

    ``deadline`` is the CMS-admitted credential deadline (the lease ceiling,
    ADR-039-R10). A scenario that does not declare exactly one participant-access
    target launches without VPN. The GCE range-cell backend is the only RAES
    adapter that realizes the gateway (EC2 is #2443), so a requested deadline on
    any other backend fails closed before the Range or its pool slot exists.
    """
    if deadline is None:
        return None
    if range_backend != "gce":
        raise ValueError("OpenVPN access is not realized by the selected range backend")
    targets = {binding.target_address for binding in participant_access}
    if len(targets) != 1:
        return None
    return build_openvpn_capability(raes_member_target_ref(next(iter(targets))), deadline)


def _verify_existing_remote_access(existing: Range, deadline: datetime | None) -> None:
    """Reject an idempotent replay that requests different OpenVPN authority."""
    from engine.models import RaesParticipantAccessBinding

    access = [
        ParticipantAccessBinding(row.target_address, row.channel, row.account_address)
        for row in RaesParticipantAccessBinding.objects.filter(range=existing)
    ]
    expected = mint_remote_access_capability(access, deadline, str(existing.range_backend or ""))
    if existing.remote_access_capability != expected:
        raise ValueError("RAES range replay requests different OpenVPN authority than the persisted capability")


def grant_raes_remote_access(request_id: UUID, deadline: datetime | None) -> bool:
    """Mint the claimant's OpenVPN authority on a just-claimed warm generation (#28, #2030).

    Runs inside the CMS claim transaction after the ownership rehome, so the
    activation input composed afterwards carries the capability and the profile
    is minted for the claimant. A warm generation is prepared without VPN, so a
    persisted capability or binding here is a contract violation. Returns whether
    a capability was granted.
    """
    from engine.models import RaesParticipantAccessBinding, Range

    if deadline is None:
        return False
    range_obj = Range.objects.get(request__request_id=request_id)
    if range_obj.remote_access_capability is not None or range_obj.vpn_access_binding is not None:
        raise ValueError("A warm generation must not hold OpenVPN authority before its claim")
    access = [
        ParticipantAccessBinding(row.target_address, row.channel, row.account_address)
        for row in RaesParticipantAccessBinding.objects.filter(range=range_obj)
    ]
    capability = mint_remote_access_capability(access, deadline, str(range_obj.range_backend or ""))
    if capability is None:
        return False
    with transaction.atomic():
        updated = Range.objects.filter(pk=range_obj.pk, remote_access_capability__isnull=True).update(
            remote_access_capability=capability,
            updated_at=timezone.now(),
        )
    if updated != 1:
        raise ValueError("A warm generation's OpenVPN authority changed during its claim")
    return True


def _verify_existing_plugin_scope(existing: Range, scope: RuntimePluginScope | None) -> None:
    """Reject replay that changes the administrator-selected tenant or pack revision."""
    pin = retained_runtime_plugin_pin(existing)
    if pin is not None and (
        scope is None
        or pin.organization_uuid != scope.organization_uuid
        or pin.pack_digest != scope.pack_digest
        or pin.pack_id != scope.pack_id
    ):
        raise ValueError("Range replay cannot change its runtime plugin pack identity")


def _persist_range_bindings(range_obj: Range, bindings: RangeBindings) -> None:
    """Persist every byte-free RAES sidecar binding in the range transaction."""
    from engine.models import (
        RaesArtifactSatisfactionBinding,
        RaesContentDeliveryBinding,
        RaesParticipantAccessBinding,
    )

    RaesContentDeliveryBinding.objects.bulk_create(
        RaesContentDeliveryBinding(
            range=range_obj,
            content_address=binding.content_address or "",
            resource_type=binding.resource_type or "",
            resource_address=binding.resource_address or "",
            payload_kind=binding.payload_kind or "",
            install_policy=binding.install_policy or "",
            sha256=binding.sha256,
            storage_key=binding.storage_key,
            byte_count=binding.byte_count,
            binding_version=binding.binding_version,
        )
        for binding in bindings.delivery
    )
    RaesParticipantAccessBinding.objects.bulk_create(
        RaesParticipantAccessBinding(
            range=range_obj,
            target_address=binding.target_address,
            channel=binding.channel,
            account_address=binding.account_address,
            binding_version=binding.binding_version,
        )
        for binding in bindings.participant_access
    )
    RaesArtifactSatisfactionBinding.objects.bulk_create(
        RaesArtifactSatisfactionBinding(
            range=range_obj,
            target_address=binding.target,
            requirement_id=binding.requirement_id,
            artifact_id=binding.artifact_id,
            artifact_version=binding.version,
            digest=binding.digest,
            media_type=binding.media_type,
            mechanism=binding.mechanism,
            acquisition=binding.acquisition,
            timing=binding.timing,
            image_ref=binding.image_ref,
            image_id=binding.image_id,
            machine_type=binding.machine_type,
            disk_size_gb=binding.disk_size_gb,
            disk_type=binding.disk_type,
            management_ssh_port=binding.management_ssh_port,
            management_ssh_username=binding.management_ssh_username,
            binding_version=1,
        )
        for binding in bindings.artifact
    )


def _verify_existing_participant_access(
    existing: Range,
    participant_access: tuple[ParticipantAccessBinding, ...],
) -> None:
    """Reject an idempotent replay that carries different access intent (#1710).

    The persisted rows are the immutable declaration the realized access binding
    is later compared against. Silently reusing the first declaration for a
    replay that now declares different access would let a second launch of the
    same ``request_id`` realize access the caller did not ask for, so a mismatch
    fails closed instead.
    """
    from engine.models import RaesParticipantAccessBinding

    persisted = {
        (row.target_address, row.channel, row.account_address)
        for row in RaesParticipantAccessBinding.objects.filter(range=existing)
    }
    requested = {(binding.target_address, binding.channel, binding.account_address) for binding in participant_access}
    if persisted != requested:
        raise ValueError("RAES range replay carries different participant access intent than the persisted binding")
