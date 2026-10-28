"""Use the incumbent removal journal to own uncertain upload bytes before storage I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from cms.models import TenantPackArchive, TenantPackRemoval
from shared.exceptions import ValidationError

from ._tenant_pack_authority import TenantPackAdmission, fence_tenant_pack

if TYPE_CHECKING:
    from ._pack_registration_types import PackRegistrationRequest

_UPLOAD_LEASE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class PackUploadJournal:
    """Retain exact upload cleanup custody and its bounded write lease."""

    removal: TenantPackRemoval
    archive: TenantPackArchive
    lease_id: UUID


def reserve_pack_upload(
    admission: TenantPackAdmission, request: PackRegistrationRequest, location: tuple[str, str], request_id: str
) -> PackUploadJournal:
    """Record exact cleanup custody before a write can be attempted or acknowledged."""
    lease_id = uuid4()
    expires_at = timezone.now() + timedelta(seconds=_UPLOAD_LEASE_SECONDS)
    with transaction.atomic():
        fence_tenant_pack(admission)
        removal = TenantPackRemoval.objects.create(
            organization_uuid=admission.organization_uuid,
            scenario_id=request.scenario_id,
            package_name=request.package_name,
            package_version=request.package_version,
            package_digest=request.package_digest,
            requested_by_principal_uuid=admission.credential.principal.uuid,
            request_id=request_id,
            upload_outcome="unknown",
            lease_id=lease_id,
            lease_expires_at=expires_at,
            next_attempt_at=expires_at,
        )
        archive = TenantPackArchive.objects.create(
            organization_uuid=admission.organization_uuid,
            scenario_id=request.scenario_id,
            package_ref=request.package_ref,
            bucket=location[0],
            object_key=location[1],
            location_verified=True,
            removal=removal,
        )
        return PackUploadJournal(removal, archive, lease_id)


def _locked_journal(journal: PackUploadJournal) -> tuple[TenantPackRemoval, TenantPackArchive]:
    """Lock and validate the exact immutable upload cleanup records."""
    removal = TenantPackRemoval.objects.select_for_update().get(pk=journal.removal.pk)
    archive = TenantPackArchive.objects.select_for_update().get(pk=journal.archive.pk, removal=removal)
    for current, expected, names in (
        (
            removal,
            journal.removal,
            (
                "organization_uuid",
                "scenario_id",
                "package_name",
                "package_version",
                "package_digest",
                "requested_by_principal_uuid",
            ),
        ),
        (
            archive,
            journal.archive,
            ("organization_uuid", "scenario_id", "package_ref", "bucket", "object_key", "location_verified"),
        ),
    ):
        if any(getattr(current, name) != getattr(expected, name) for name in names):
            raise ValidationError("Pack upload journal changed")
    if removal.state != "pending" or removal.upload_outcome not in {"unknown", "settled"}:
        raise ValidationError("Pack upload journal unavailable")
    return removal, archive


def settle_pack_upload(journal: PackUploadJournal) -> None:
    """Only a successful storage return proves this invocation finished writing.

    A lost lease never regains publication authority. It may still supply this
    positive completion evidence so the existing cleanup worker can finish.
    """
    with transaction.atomic():
        removal, _ = _locked_journal(journal)
        removal.upload_outcome = "settled"
        removal.save(update_fields=["upload_outcome"])


def claim_pack_upload(
    journal: PackUploadJournal, admission: TenantPackAdmission, request: PackRegistrationRequest
) -> None:
    """Consume exact unexpired uploader custody in the registration transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise ValidationError("Pack upload commit requires a transaction")
    removal, _ = _locked_journal(journal)
    if (
        removal.lease_id != journal.lease_id
        or removal.lease_expires_at is None
        or removal.lease_expires_at <= timezone.now()
        or removal.upload_outcome != "settled"
        or not _matches_upload_request(removal, journal.archive, admission, request)
    ):
        raise ValidationError("Pack upload lease unavailable")


def retain_installed_archive(journal: PackUploadJournal) -> None:
    """Move its exact archive into installed inventory in the same SQL commit."""
    removal, archive = _locked_journal(journal)
    archive.removal = None
    archive.save(update_fields=["removal"])
    removal.delete()


def release_pack_upload(journal: PackUploadJournal) -> None:
    """Hand failed/uncertain writes to cleanup without resolving the initiator."""
    from ._tenant_pack_removal import reconcile_tenant_pack_removal

    with transaction.atomic():
        removal, _ = _locked_journal(journal)
        if removal.lease_id in {None, journal.lease_id}:
            removal.lease_id = removal.lease_expires_at = None
            removal.next_attempt_at = timezone.now()
            removal.save(update_fields=["lease_id", "lease_expires_at", "next_attempt_at"])
    reconcile_tenant_pack_removal(journal.removal.pk)


def _matches_upload_request(
    removal: TenantPackRemoval,
    archive: TenantPackArchive,
    admission: TenantPackAdmission,
    request: PackRegistrationRequest,
) -> bool:
    """Compare every accepted uploader and immutable pack identity fact."""
    return (
        removal.organization_uuid,
        removal.requested_by_principal_uuid,
        removal.scenario_id,
        removal.package_name,
        removal.package_version,
        removal.package_digest,
        archive.package_ref,
    ) == (
        admission.organization_uuid,
        admission.credential.principal.uuid,
        request.scenario_id,
        request.package_name,
        request.package_version,
        request.package_digest,
        request.package_ref,
    )
