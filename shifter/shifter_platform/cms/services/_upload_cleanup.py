"""Retained installer cleanup, independent of the initiating proof's liveness."""

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import AgentConfig, AgentUpload, AgentUploadBudget
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log
from shared.cloud import get_object_storage
from shared.operation_authority import authority_resource_scope

from ._upload_scope import _scope_digest


def _cleanup_upload(row: AgentUpload) -> bool:
    """Delete only recorded upload objects and retain failed cleanup for retry."""

    _verify_upload_cleanup_keys(row)
    storage = get_object_storage()
    storage.delete_object(bucket=row.storage_bucket, key=row.staging_key)
    retired = _retired_installer(row)
    if row.state != "completed" or retired:
        storage.delete_object(bucket=row.storage_bucket, key=row.install_key)
    with transaction.atomic():
        # Installer deletion holds this same budget lock through retirement and
        # cleanup request publication. Never acknowledge an older live snapshot.
        AgentUploadBudget.objects.select_for_update().get(pk=row.scope_digest)
        current = AgentUpload.objects.select_for_update().get(pk=row.pk)
        if current.state != row.state:
            raise CMSError("Upload cleanup state changed")
        now_retired = _retired_installer(current)
        if now_retired != retired:
            raise CMSError("Upload cleanup retirement changed")
        if current.state == "finalizing":
            # Age and one absent observation cannot settle an interrupted COPY.
            # Keep exact keys and budget reserved for a possible late write.
            current.save(update_fields=["updated_at"])
            return False
        was_pending = current.cleanup_pending
        current.cleanup_pending = False
        current.save(update_fields=["cleanup_pending", "updated_at"])
        if was_pending:
            _record_upload_cleanup(current)
        return True


def _record_upload_cleanup(row: AgentUpload) -> None:
    """Audit first completion; repeated exact-key tombstone sweeps do not repeat it."""
    from shared.audit import AuditActorType

    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.AGENT,
            entity_id=0,
            entity_ref=str(row.id),
            action=AuditAction.DELETE,
            actor_type=AuditActorType.SYSTEM,
            new_state={"upload_id": str(row.id), "cleanup_reason": "capability_expired"},
        ),
        strict=True,
    )


def reconcile_upload_cleanup(*, limit: int = 100) -> int:
    """Bounded native reconciliation, including revoked and uncertain admissions."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid upload cleanup limit")
    now = timezone.now()
    from django.db.models import Q

    # An authorized PUT may finish after URL expiry. Retained rows remain exact
    # cleanup tombstones, including once a prior sweep observed absence.
    rows = (
        AgentUpload.objects.filter(expires_at__lte=now)
        .filter(Q(cleanup_pending=True) | Q(updated_at__lte=now - timedelta(minutes=15)))
        .order_by("updated_at", "id")[:limit]
    )
    cleaned = 0
    for candidate in rows:
        try:
            row = _expired_cleanup_candidate(candidate, now)
            if row is None:
                continue
            cleaned += _cleanup_upload(row)
        except Exception:
            # Retain the original resource evidence and reservation on uncertainty.
            AgentUpload.objects.filter(pk=candidate.pk).update(updated_at=now)
    return cleaned


def _retired_installer(row: AgentUpload) -> bool:
    """Only the exact deleted installer permits cleanup of its retained install key."""
    return (
        row.agent_id is not None
        and AgentConfig.all_objects.filter(pk=row.agent_id, s3_key=row.install_key, deleted_at__isnull=False).exists()
    )


def _expired_cleanup_candidate(candidate: AgentUpload, now: datetime) -> AgentUpload | None:
    """Lock budget and upload before converting an expired, settled admission to cleanup."""
    with transaction.atomic():
        AgentUploadBudget.objects.select_for_update().get(pk=candidate.scope_digest)
        row = AgentUpload.objects.select_for_update().get(pk=candidate.pk)
        if not _cleanup_due(row, now):
            return None
        if row.state == "issued" or (row.state == "finalizing" and not row.validated_identity):
            row.state = "cancelled"
            row.save(update_fields=["state", "updated_at"])
    return row


def _cleanup_due(row: AgentUpload, now: datetime) -> bool:
    """Retain recently active finalization and throttle already-observed tombstones."""
    cutoff = now - timedelta(minutes=15)
    return (
        row.expires_at <= now
        and (row.cleanup_pending or row.updated_at <= cutoff)
        and (row.state != "finalizing" or row.updated_at <= cutoff)
    )


def _verify_upload_cleanup_keys(row: AgentUpload) -> None:
    """Cleanup admits only the recorded proof reference and exact native storage namespace."""
    from shared.operation_authority import credential_reference_from_record

    # Explicit expiry cleanup is bound to this admission's namespace and both
    # immutable keys. It neither resolves a revoked initiator nor acquires a grant.
    credential_reference_from_record(row)
    scope = authority_resource_scope(
        {
            name: getattr(row, name)
            for name in (
                "authority_scope_kind",
                "authority_account_uuid",
                "authority_organization_uuid",
                "authority_workspace_uuid",
            )
        }
    )
    root = f"agents/scoped/{_scope_digest(scope).removeprefix('sha256:')}/{row.id}"
    if row.scope_digest != _scope_digest(scope) or (row.staging_key, row.install_key) != (
        f"{root}/staging",
        f"{root}/installed",
    ):
        raise CMSError("Upload cleanup binding unavailable")
    if row.storage_provider != str(settings.CLOUD_PROVIDER):
        raise CMSError("Upload storage provider unavailable")
