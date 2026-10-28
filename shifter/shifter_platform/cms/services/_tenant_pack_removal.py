"""Tenant pack removal with a durable, resumable storage cleanup (ADR-034-R13, #2520).

One transaction authorizes the organization administrator, fences the pack row,
refuses active references, captures every owned archive object, removes the
current adapter selection and catalog rows, and writes the strict removal audit.
Only then does destructive storage I/O start. A failed or interrupted cleanup
stays ``pending`` and is retried from the captured exact keys by the removal
reconciler and by repeated authorized requests.

Organization-less packs remain operator-managed. Acquired feature artifacts are
shared supply and are retained.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID, uuid4

from django.db import OperationalError, transaction
from django.utils import timezone

from cms.models import (
    RaesPackageSource,
    ScenarioMetadata,
    ScenarioModelNeeds,
    TenantPackArchive,
    TenantPackRemoval,
)
from engine.services import RuntimePluginBindingRemovalAdmission
from shared.audit import (
    AuditAction,
    AuditActorType,
    AuditEntityType,
    AuditEvent,
    RequestAudit,
    audit_log,
    principal_actor_fields,
)
from shared.credentials import CredentialContext
from shared.exceptions import ValidationError
from shared.log_sanitize import safe_log_value
from shared.pack_references import PackReferenceUnavailable

from ._pack_references import PackBlocker, find_pack_blockers
from ._tenant_pack_authority import TenantPackAdmission, admit_tenant_pack, fence_tenant_pack

logger = logging.getLogger(__name__)

_UNAVAILABLE = "The pack is unavailable"
# PostgreSQL lock_not_available: a reference writer holds the admission fence.
_LOCK_NOT_AVAILABLE = "55P03"
_STORAGE_DELETE_FAILED = "storage_delete_failed"
_LEGACY_LOCATION_UNVERIFIED = "legacy_location_unverified"
_LEASE_SECONDS = 300
_MAX_BACKOFF_SECONDS = 3600
_PACK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class _LocationUnverified(Exception):
    """A backfilled archive was not observed at its candidate location."""


class PackRemovalConflict(Exception):
    """The removal cannot proceed now; ``code`` is a stable API error code."""

    def __init__(self, code: str, message: str, blockers: tuple[PackBlocker, ...] = (), total: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.blockers = blockers
        self.total = total


@dataclass(frozen=True, slots=True)
class PackRemovalView:
    """Bounded removal outcome returned to the administrator."""

    removal_id: UUID
    pack_id: str
    state: str
    remaining_archives: int


def request_tenant_pack_removal(
    user: CredentialContext,
    organization_uuid: UUID,
    pack_id: str,
    expected_digest: str,
    *,
    audit: RequestAudit | None = None,
) -> PackRemovalView:
    """Remove an organization-owned pack, or resume its earlier removal.

    Raises:
        workspaces.services.OrganizationAuthorizationError: the actor does not
            administer the organization.
        ValidationError: the pack does not exist in this organization (opaque).
        PackRemovalConflict: stale digest, busy pack, or active references.
    """
    if not _PACK_ID.fullmatch(pack_id) or not _DIGEST.fullmatch(expected_digest):
        raise ValidationError(_UNAVAILABLE)
    attribution = audit or RequestAudit()
    admitted = admit_tenant_pack(user, organization_uuid)
    from engine.services import prepare_runtime_plugin_pack_binding_removal
    from shared.runtime_plugin_binding import RuntimePluginScope

    binding_admission = prepare_runtime_plugin_pack_binding_removal(
        admitted.credential,
        RuntimePluginScope(organization_uuid=organization_uuid, pack_id=pack_id, pack_digest=expected_digest),
    )
    removal_id = _commit_removal(admitted, pack_id, expected_digest, attribution, binding_admission)
    reconcile_tenant_pack_removal(removal_id)
    return _view(TenantPackRemoval.objects.get(pk=removal_id))


def _commit_removal(
    admitted: TenantPackAdmission,
    pack_id: str,
    expected_digest: str,
    attribution: RequestAudit,
    binding_admission: RuntimePluginBindingRemovalAdmission,
) -> UUID:
    """Fence, guard, capture and delete catalog state; return the removal id."""
    from engine.services import remove_runtime_plugin_pack_binding

    from ._content_authority import content_scope

    with transaction.atomic():
        fence_tenant_pack(admitted)
        organization_uuid = admitted.organization_uuid
        user = admitted.credential
        try:
            source = RaesPackageSource.objects.select_for_update(nowait=True).filter(scenario_id=pack_id).first()
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            raise PackRemovalConflict(
                "pack_busy", "The pack is being launched or updated; retry the removal shortly"
            ) from None
        if source is None:
            return _resumable(organization_uuid, pack_id, expected_digest)
        if source.organization_uuid != organization_uuid or content_scope(source) != admitted.scope:
            raise ValidationError(_UNAVAILABLE)
        if source.package_digest != expected_digest:
            raise PackRemovalConflict("pack_changed", "The pack changed; reload its current version before removing")
        _refuse_references(pack_id, user)
        removal = TenantPackRemoval.objects.create(
            organization_uuid=organization_uuid,
            scenario_id=pack_id,
            package_name=source.package_identity,
            package_version=source.package_version,
            package_digest=source.package_digest,
            requested_by=None,
            requested_by_principal_uuid=user.principal.uuid,
            request_id=attribution.request_id or "",
            next_attempt_at=timezone.now(),
        )
        archives = _capture_archives(source, removal)
        remove_runtime_plugin_pack_binding(binding_admission, organization_uuid, pack_id, audit=attribution)
        ScenarioModelNeeds.objects.filter(scenario_id=pack_id).delete()
        ScenarioMetadata.objects.filter(scenario_id=pack_id).delete()
        source.delete()
        _audit(
            attribution,
            user,
            AuditEvent(
                entity_type=AuditEntityType.SCENARIO,
                entity_id=0,
                entity_ref=pack_id,
                action=AuditAction.DELETE,
                previous_state={
                    "organization_uuid": str(organization_uuid),
                    "scenario_id": pack_id,
                    "package_name": removal.package_name,
                    "package_version": removal.package_version,
                    "package_digest": removal.package_digest,
                },
                new_state={"result": "removal_requested", "removal_id": str(removal.id), "archives": archives},
            ),
        )
        return removal.id


def _resumable(organization_uuid: UUID, pack_id: str, expected_digest: str) -> UUID:
    """Return this organization's latest removal of the same revision, else refuse."""
    removal = (
        TenantPackRemoval.objects.filter(
            organization_uuid=organization_uuid, scenario_id=pack_id, package_digest=expected_digest
        )
        .order_by("-created_at")
        .first()
    )
    if removal is None:
        raise ValidationError(_UNAVAILABLE)
    return removal.id


def _refuse_references(pack_id: str, user: CredentialContext) -> None:
    """Raise a conflict naming the references the actor may see that still use the pack."""
    try:
        blockers = find_pack_blockers(pack_id, user)
    except PackReferenceUnavailable:
        logger.exception("tenant pack removal: references unavailable pack_id=%s", safe_log_value(pack_id))
        raise PackRemovalConflict(
            "pack_references_unavailable", "Pack references could not be checked; retry the removal later"
        ) from None
    if not blockers.total:
        return
    named = [row.label for row in blockers.items if row.label]
    message = f"The pack is still in use by {blockers.total} active reference(s)"
    if named:
        message += ": " + ", ".join(named[:5])
    raise PackRemovalConflict("pack_in_use", message, blockers.items, blockers.total)


def _capture_archives(source: RaesPackageSource, removal: TenantPackRemoval) -> int:
    """Bind every owned archive of the pack to the removal with an exact location."""
    from django.conf import settings

    from ._raes_dispatch import _object_package_key

    bucket = settings.RAES_PACKAGE_BUCKET
    owned = TenantPackArchive.objects.select_for_update().filter(scenario_id=source.scenario_id, removal__isnull=True)
    if source.source_kind == "object" and not owned.filter(package_ref=source.package_ref).exists():
        # A pre-inventory revision with no backfilled evidence is still owned.
        TenantPackArchive.objects.create(
            organization_uuid=removal.organization_uuid, scenario_id=source.scenario_id, package_ref=source.package_ref
        )
    count = 0
    for archive in owned:
        if not archive.object_key:
            if not bucket:
                raise PackRemovalConflict("pack_storage_unavailable", "Pack storage is unavailable")
            archive.bucket = bucket
            archive.object_key = _object_package_key(archive.package_ref)
        archive.removal = removal
        archive.save(update_fields=["bucket", "object_key", "removal"])
        count += 1
    return count


def reconcile_tenant_pack_removals(*, limit: int = 20) -> int:
    """Run due pending removals; return how many completed in this pass."""
    due = list(
        TenantPackRemoval.objects.filter(state=TenantPackRemoval.State.PENDING, next_attempt_at__lte=timezone.now())
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )
    return sum(reconcile_tenant_pack_removal(removal_id) for removal_id in due)


def reconcile_tenant_pack_removal(removal_id: UUID) -> bool:
    """Delete one removal's remaining objects under a lease; return completion."""
    lease = _lease(removal_id)
    if lease is None:
        return False
    removal, lease_id = lease
    failure = ""
    for archive in removal.archives.order_by("pk"):
        try:
            _delete_owned_object(removal, archive)
        except _LocationUnverified:
            failure = _LEGACY_LOCATION_UNVERIFIED
            logger.warning(
                "tenant pack removal: legacy archive not found at its candidate location removal_id=%s archive_id=%s",
                removal.id,
                archive.pk,
            )
            continue
        except Exception:
            failure = failure or _STORAGE_DELETE_FAILED
            logger.warning(
                "tenant pack removal: archive delete failed removal_id=%s archive_id=%s",
                removal.id,
                archive.pk,
            )
            continue
        # One absent observation cannot close a still-unknown upload. A late
        # server write must remain owned by future exact-key cleanup attempts.
        if removal.upload_outcome != "unknown":
            TenantPackArchive.objects.filter(pk=archive.pk, removal=removal).delete()
    if removal.upload_outcome == "unknown":
        failure = failure or "upload_outcome_unknown"
    return _settle(removal.id, lease_id, failure)


def _lease(removal_id: UUID) -> tuple[TenantPackRemoval, UUID] | None:
    """Claim an exclusive, expiring attempt lease on a due pending removal."""
    now = timezone.now()
    with transaction.atomic():
        removal = TenantPackRemoval.objects.select_for_update(skip_locked=True).filter(pk=removal_id).first()
        if (
            removal is None
            or removal.state != TenantPackRemoval.State.PENDING
            or removal.next_attempt_at > now
            or (removal.lease_expires_at is not None and removal.lease_expires_at > now)
        ):
            return None
        removal.lease_id = uuid4()
        removal.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
        removal.save(update_fields=["lease_id", "lease_expires_at"])
        return removal, removal.lease_id


def _delete_owned_object(removal: TenantPackRemoval, archive: TenantPackArchive) -> None:
    """Delete one captured key after proving it is this organization's pack object.

    An unverified (backfilled) location must first show the object; otherwise an
    earlier bucket or prefix could hold it while absence here looks like success.
    """
    from shared.cloud import get_object_storage

    _require_owned_location(removal.organization_uuid, archive, archive.bucket, archive.object_key)
    storage = get_object_storage()
    if not archive.location_verified:
        if not storage.object_exists(archive.bucket, archive.object_key):
            raise _LocationUnverified
        # Record the observation before deleting so a retry after deletion is idempotent.
        TenantPackArchive.objects.filter(pk=archive.pk).update(location_verified=True)
    storage.delete_object(archive.bucket, archive.object_key)


def _require_owned_location(organization_uuid: UUID, archive: TenantPackArchive, bucket: str, key: str) -> None:
    """Reject a target that is not exactly this organization's server-generated object."""
    ref = archive.package_ref
    owned = re.fullmatch(rf"tenant-packs/{organization_uuid}/{_UUID}\.tar\.gz", ref)
    contained = key == ref or (key.endswith("/" + ref) and ".." not in key.split("/") and not key.startswith("/"))
    if not owned or not contained or not bucket or archive.organization_uuid != organization_uuid:
        raise ValueError("Archive target is not an owned tenant pack object")


def relocate_tenant_pack_archive(archive_id: int, bucket: str, object_key: str) -> None:
    """Operator reconciliation: point a pending legacy archive at its real location.

    The new location is still unverified; cleanup deletes only after it observes
    the object there.
    """
    with transaction.atomic():
        archive, removal_id = _pending_archive(archive_id)
        _require_owned_location(archive.organization_uuid, archive, bucket, object_key)
        previous = {"bucket": archive.bucket, "object_key": archive.object_key}
        archive.bucket, archive.object_key, archive.location_verified = bucket, object_key, False
        archive.save(update_fields=["bucket", "object_key", "location_verified"])
        TenantPackRemoval.objects.filter(pk=removal_id).update(next_attempt_at=timezone.now())
        _operator_audit(
            archive_id, removal_id, "archive_relocated", previous, {"bucket": bucket, "object_key": object_key}
        )


def confirm_tenant_pack_archive_absent(archive_id: int) -> None:
    """Operator reconciliation: record that a legacy archive is verified absent everywhere."""
    with transaction.atomic():
        archive, removal_id = _pending_archive(archive_id)
        if archive.location_verified:
            raise ValueError("A verified archive location is cleaned up by the reconciler")
        previous = {"package_ref": archive.package_ref, "bucket": archive.bucket, "object_key": archive.object_key}
        archive.delete()
        TenantPackRemoval.objects.filter(pk=removal_id).update(next_attempt_at=timezone.now())
        _operator_audit(archive_id, removal_id, "archive_confirmed_absent", previous, None)


def _pending_archive(archive_id: int) -> tuple[TenantPackArchive, UUID]:
    """Lock an archive captured by a pending removal and return it with the removal id."""
    archive = (
        TenantPackArchive.objects.select_for_update()
        .filter(pk=archive_id, removal__state=TenantPackRemoval.State.PENDING)
        .first()
    )
    removal_id = archive.removal_id if archive is not None else None
    if archive is None or removal_id is None:
        raise ValueError("No pending tenant pack removal holds this archive")
    return archive, removal_id


def _operator_audit(
    archive_id: int, removal_id: UUID, result: str, previous: dict[str, str], new: dict[str, str] | None
) -> None:
    """Write the strict audit record of an operator reconciliation step."""
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.SCENARIO,
            entity_id=0,
            entity_ref=str(removal_id),
            action=AuditAction.UPDATE,
            actor_type=AuditActorType.SYSTEM,
            previous_state=previous,
            new_state={"result": result, "archive_id": archive_id, **(new or {})},
            context="operator reconciliation",
        ),
        strict=True,
    )


def _settle(removal_id: UUID, lease_id: UUID, failure: str) -> bool:
    """Record completion with its strict audit, or schedule a bounded retry."""
    now = timezone.now()
    with transaction.atomic():
        removal = TenantPackRemoval.objects.select_for_update().get(pk=removal_id)
        if removal.lease_id != lease_id:
            return False
        removal.lease_id = None
        removal.lease_expires_at = None
        remaining = removal.archives.count()
        if failure or remaining or removal.upload_outcome == "unknown":
            removal.attempts += 1
            removal.last_failure_code = failure or _STORAGE_DELETE_FAILED
            delay = min(_MAX_BACKOFF_SECONDS, 30 * 2 ** min(removal.attempts, 10))
            removal.next_attempt_at = now + timedelta(seconds=delay)
            removal.save(
                update_fields=["lease_id", "lease_expires_at", "attempts", "last_failure_code", "next_attempt_at"]
            )
            return False
        removal.state = TenantPackRemoval.State.COMPLETED
        removal.completed_at = now
        removal.last_failure_code = ""
        removal.save(update_fields=["lease_id", "lease_expires_at", "state", "completed_at", "last_failure_code"])
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.SCENARIO,
                entity_id=0,
                entity_ref=str(removal.id),
                action=AuditAction.UPDATE,
                actor_type=AuditActorType.SYSTEM,
                request_id=removal.request_id,
                new_state={
                    "result": "removal_completed",
                    "organization_uuid": str(removal.organization_uuid),
                    "scenario_id": removal.scenario_id,
                    "package_digest": removal.package_digest,
                },
            ),
            strict=True,
        )
        return True


def _audit(attribution: RequestAudit, user: CredentialContext, event: AuditEvent) -> None:
    """Write a strict audit event with the request's attribution."""
    audit_log(
        replace(
            event,
            **principal_actor_fields(user.principal),
            request_id=attribution.request_id,
            source_ip=attribution.source_ip,
            user_agent=attribution.user_agent,
        ),
        strict=True,
    )


def _view(removal: TenantPackRemoval) -> PackRemovalView:
    """Project a removal for the API."""
    return PackRemovalView(removal.id, removal.scenario_id, removal.state, removal.archives.count())
