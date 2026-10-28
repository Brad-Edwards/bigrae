"""Scoped branch of the incumbent signed-token upload and immutable-copy flow."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import AgentConfig, AgentType, AgentUpload, AgentUploadBudget
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.cloud import get_object_storage
from shared.operation_authority import (
    authority_resource_scope,
    credential_reference_fields,
    credential_reference_from_record,
)
from shared.operation_envelope import canonical_payload_digest
from shared.s3 import sanitize_s3_filename

from ._content_authority import content_scope_fields, require_content_scope

if TYPE_CHECKING:
    from typing import Any

    from shared.cloud.types import ObjectStorage
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope
    from shared.schemas.cms_projections import UploadInitiation


logger = logging.getLogger(__name__)

_INVALID_UPLOAD_REFERENCE = "Invalid upload reference"
_UPLOAD_UNAVAILABLE = "Upload unavailable"
_INVALID_UPLOAD_METADATA = "Invalid upload metadata"


_CONTENT_UNAVAILABLE = "Content unavailable"


def _scope_digest(scope: ResourceScope) -> str:
    """Derive the storage budget identity from resolved content ancestry."""
    return canonical_payload_digest(
        {name: str(value) if value is not None else None for name, value in content_scope_fields(scope).items()}
    )


def _upload_values(
    credential: CredentialContext,
    scope: ResourceScope,
    name: str,
    filename: str,
    file_size: int,
    agent_type: str,
) -> dict[str, Any]:
    """Validate installer metadata and pin its original scope and storage keys."""
    from shared.authorization.policy import scope_parent

    current = require_content_scope(credential, scope, "manage_content")
    os_slug = _validated_upload_metadata(name, filename, file_size, agent_type)
    scope_digest = _scope_digest(scope)
    identity = uuid4()
    root = f"agents/scoped/{scope_digest.removeprefix('sha256:')}/{identity}"
    parent = scope_parent(scope)
    return {
        **credential_reference_fields(current),
        "id": identity,
        "authority_kind": "principal",
        "authority_action": f"{parent.type}.manage_content",
        "authority_target_type": parent.type,
        "authority_target_uuid": parent.uuid,
        "authority_scope_kind": scope.kind,
        "authority_account_uuid": scope.account_uuid,
        "authority_organization_uuid": scope.organization_uuid,
        "authority_workspace_uuid": scope.workspace_uuid,
        "authority_owner_uuid": current.principal.uuid,
        "scope_digest": scope_digest,
        "name": name.strip(),
        "filename": sanitize_s3_filename(filename),
        "os_slug": os_slug,
        "file_size": file_size,
        "agent_type": agent_type,
        "storage_provider": str(settings.CLOUD_PROVIDER),
        "storage_bucket": settings.AWS_S3_BUCKET_NAME,
        "staging_key": f"{root}/staging",
        "install_key": f"{root}/installed",
        "expires_at": timezone.now() + timedelta(seconds=min(300, settings.AGENT_UPLOAD_URL_EXPIRES)),
    }


def _reserve_upload(values: dict[str, Any], scope: ResourceScope) -> AgentUpload:
    """Reserve upload capacity and durable cleanup evidence together."""
    with transaction.atomic():
        AgentUploadBudget.objects.get_or_create(pk=values["scope_digest"])
        AgentUploadBudget.objects.select_for_update().get(pk=values["scope_digest"])
        used = AgentConfig.objects.filter(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
        used_bytes = used.aggregate(total=Sum("file_size_bytes"))["total"] or 0
        pending = AgentUpload.objects.filter(scope_digest=values["scope_digest"], cleanup_pending=True).exclude(
            state="completed", agent__deleted_at__isnull=True
        )
        reserved_bytes = pending.aggregate(total=Sum("file_size"))["total"] or 0
        if used_bytes + reserved_bytes + values["file_size"] > settings.AGENT_USER_STORAGE_QUOTA_MB * 1024 * 1024:
            raise CMSError("Storage quota exceeded")
        row = AgentUpload.objects.create(**values)

        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.AGENT,
                entity_id=0,
                entity_ref=str(row.id),
                action=AuditAction.CREATE,
                **principal_actor_fields(credential_reference_from_record(row).principal),
                new_state={"upload_id": str(row.id), "state": row.state, "file_size": row.file_size},
            ),
            strict=True,
        )
        return row


def initiate_scoped_upload(
    credential: CredentialContext,
    scope: ResourceScope | None,
    name: str,
    filename: str,
    file_size: int,
    agent_type: str,
) -> UploadInitiation:
    """Admit a bounded upload and issue its existing signed staging token."""
    from cms.assets.upload_token import generate_scoped_upload_token

    if scope is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    values = _upload_values(credential, scope, name, filename, file_size, agent_type)
    row = _reserve_upload(values, scope)
    try:
        url = get_object_storage().generate_presigned_upload_url(
            bucket=row.storage_bucket,
            key=row.staging_key,
            content_type="application/octet-stream",
            expires_in=max(1, int((row.expires_at - timezone.now()).total_seconds())),
            content_length=row.file_size,
        )
        if scope is None:
            raise CMSError(_CONTENT_UNAVAILABLE)
        require_content_scope(credential, scope, "manage_content")
        token = generate_scoped_upload_token(row)
        return {"presigned_url": url, "s3_key": row.staging_key, "upload_token": token, "expected_os": row.os_slug}
    except Exception:  # noqa: BLE001 - value-free upload boundary; storage and token errors stay out of the chain
        # A provider timeout can have issued a capability. Preserve custody and
        # cleanup evidence; cancellation cannot make that URL expire early.
        AgentUpload.objects.filter(pk=row.pk).update(state="cancelled")
        raise CMSError(_UPLOAD_UNAVAILABLE) from None


def _authorized_upload(credential: CredentialContext, token: str) -> tuple[AgentUpload, ResourceScope]:
    """Revalidate the signed upload reference and original credential authority."""
    from uuid import UUID

    from cms.assets.upload_token import verify_upload_token
    from shared.operation_authority import credential_reference_from_record

    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError(_INVALID_UPLOAD_REFERENCE)
        payload = verify_upload_token(token, credential.principal.uuid)
        row = AgentUpload.objects.get(pk=UUID(str(payload["upload_id"])))
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
        if scope is None:
            raise CMSError(_CONTENT_UNAVAILABLE)
        require_content_scope(credential, scope, "manage_content")
        require_content_scope(credential_reference_from_record(row), scope, "manage_content")
        if row.expires_at <= timezone.now() or row.scope_digest != _scope_digest(scope):
            raise ValueError(_INVALID_UPLOAD_REFERENCE)
        if row.storage_provider != str(settings.CLOUD_PROVIDER) or payload["s3_key"] != row.staging_key:
            raise ValueError("Invalid storage binding")
        return row, scope
    except Exception:  # noqa: BLE001 - value-free upload boundary; storage and token errors stay out of the chain
        raise CMSError(_UPLOAD_UNAVAILABLE) from None


def _validated_object(row: AgentUpload, storage: ObjectStorage) -> dict[str, Any]:
    """Verify the exact staged object before installing its bytes."""
    from cms.assets.validation import enforce_max_file_size_bytes, validate_file_extension
    from shared.uploads.inspection import validate_magic_bytes

    identity = storage.head_object(bucket=row.storage_bucket, key=row.staging_key)
    if type(identity.get("content_length")) is not int or identity["content_length"] != row.file_size:
        raise CMSError("Uploaded object size does not match")
    enforce_max_file_size_bytes(row.file_size)
    header = storage.read_object_header(
        bucket=row.storage_bucket,
        key=row.staging_key,
        max_bytes=settings.UPLOAD_INSPECTION_MAX_HEADER_BYTES,
        expected_identity=identity,
    )
    validate_magic_bytes(header, validate_file_extension(row.filename))
    return identity


def _commit_installer(
    credential: CredentialContext, token: str, expected: AgentUpload, scope: ResourceScope
) -> AgentConfig:
    """Commit an installer only while its admitted custody and object identity match."""
    from cms.models import OperatingSystem

    snapshot, _ = _authorized_upload(credential, token)
    with transaction.atomic():
        AgentUploadBudget.objects.select_for_update().get(pk=expected.scope_digest)
        row = AgentUpload.objects.select_for_update().get(pk=expected.pk)
        if row.state == "completed" and row.agent_id is not None:
            return _completed_installer(row, scope)
        if row.state != "finalizing" or row.validated_identity != expected.validated_identity or snapshot.pk != row.pk:
            raise CMSError(_UPLOAD_UNAVAILABLE)
        agent = AgentConfig.objects.create(
            user=None,
            owner_principal_uuid=row.authority_owner_uuid,
            **content_scope_fields(scope),
            name=row.name,
            os=OperatingSystem.objects.get(slug=row.os_slug),
            agent_type=row.agent_type,
            s3_key=row.install_key,
            original_filename=row.filename,
            file_size_bytes=row.file_size,
        )
        row.agent = agent
        row.state = "completed"
        row.save(update_fields=["agent", "state", "updated_at"])
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.AGENT,
                entity_id=agent.pk,
                action=AuditAction.CREATE,
                **principal_actor_fields(credential.principal),
                new_state={"upload_id": str(row.id), "scope_generation": agent.scope_generation},
            ),
            strict=True,
        )
        return agent


def complete_scoped_upload(credential: CredentialContext, token: str) -> AgentConfig:
    """Install validated bytes without changing the admitted upload destination."""
    snapshot, scope = _authorized_upload(credential, token)
    with transaction.atomic():
        AgentUploadBudget.objects.select_for_update().get(pk=snapshot.scope_digest)
        row = AgentUpload.objects.select_for_update().get(pk=snapshot.pk)
        if row.state == "completed" and row.agent_id is not None:
            return _completed_installer(row, scope)
        if row.state != "issued":
            raise CMSError("Upload is unavailable or already being finalized")
        row.state = "finalizing"
        row.save(update_fields=["state", "updated_at"])
    try:
        storage = get_object_storage()
        identity = _validated_object(row, storage)
        _authorized_upload(credential, token)
        AgentUpload.objects.filter(pk=row.pk, state="finalizing").update(validated_identity=identity)
        row.validated_identity = identity
        storage.copy_object_conditional(
            bucket=row.storage_bucket,
            src_key=row.staging_key,
            dst_key=row.install_key,
            expected_identity=identity,
        )
        storage.tag_object(bucket=row.storage_bucket, key=row.install_key, tags={"status": "completed"})
        return _commit_installer(credential, token, row, scope)
    except Exception:  # noqa: BLE001 - value-free upload boundary; storage and token errors stay out of the chain
        # Keep the original generation and both storage keys for bounded cleanup.
        AgentUpload.objects.filter(pk=row.pk).exclude(state="completed").update(state="cancelled", cleanup_pending=True)
        raise CMSError("Upload could not be finalized") from None


def cancel_scoped_upload(credential: CredentialContext, token: str) -> None:
    """Cancel admitted upload work while preserving uncertain storage cleanup."""
    from uuid import UUID

    from cms.assets.upload_token import verify_upload_token

    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError(_INVALID_UPLOAD_REFERENCE)
        payload = verify_upload_token(token, None, allow_expired=True)
        snapshot = AgentUpload.objects.get(pk=UUID(str(payload["upload_id"])))
        scope = authority_resource_scope(
            {
                name: getattr(snapshot, name)
                for name in (
                    "authority_scope_kind",
                    "authority_account_uuid",
                    "authority_organization_uuid",
                    "authority_workspace_uuid",
                )
            }
        )
        current = require_content_scope(credential, scope, "manage_content")
        if payload["user_id"] != str(snapshot.authority_owner_uuid) or payload["s3_key"] != snapshot.staging_key:
            raise ValueError("Invalid upload binding")
        with transaction.atomic():
            AgentUploadBudget.objects.select_for_update().get(pk=snapshot.scope_digest)
            row = AgentUpload.objects.select_for_update().get(pk=snapshot.pk)
            if row.state == "completed":
                raise CMSError("The upload has already completed")
            if row.state == "cancelled":
                return
            row.state = "cancelled"
            row.save(update_fields=["state", "updated_at"])
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.AGENT,
                    entity_id=0,
                    entity_ref=str(row.id),
                    action=AuditAction.DELETE,
                    **principal_actor_fields(current.principal),
                    new_state={"state": "cancelled"},
                ),
                strict=True,
            )
    except CMSError:
        raise
    except Exception:  # noqa: BLE001 - value-free upload boundary; storage and token errors stay out of the chain
        raise CMSError(_UPLOAD_UNAVAILABLE) from None


def _completed_installer(row: AgentUpload, scope: ResourceScope) -> AgentConfig:
    """Project the installed agent only when its durable upload binding matches."""
    agent = AgentConfig.objects.filter(
        pk=row.agent_id,
        s3_key=row.install_key,
        owner_principal_uuid=row.authority_owner_uuid,
        **content_scope_fields(scope),
    ).first()
    if agent is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    return agent


def _cleanup_upload(row: AgentUpload) -> None:
    """Delete only recorded upload objects and retain failed cleanup for retry."""
    from shared.audit import AuditActorType
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
    storage = get_object_storage()
    storage.delete_object(bucket=row.storage_bucket, key=row.staging_key)
    retired = (
        row.agent_id is not None
        and AgentConfig.all_objects.filter(pk=row.agent_id, s3_key=row.install_key, deleted_at__isnull=False).exists()
    )
    if row.state != "completed" or retired:
        storage.delete_object(bucket=row.storage_bucket, key=row.install_key)
    with transaction.atomic():
        # Installer deletion holds this same budget lock through retirement and
        # cleanup request publication. Never acknowledge an older live snapshot.
        AgentUploadBudget.objects.select_for_update().get(pk=row.scope_digest)
        current = AgentUpload.objects.select_for_update().get(pk=row.pk)
        if current.state != row.state:
            raise CMSError("Upload cleanup state changed")
        now_retired = (
            current.agent_id is not None
            and AgentConfig.all_objects.filter(
                pk=current.agent_id, s3_key=current.install_key, deleted_at__isnull=False
            ).exists()
        )
        if now_retired != retired:
            raise CMSError("Upload cleanup retirement changed")
        current.cleanup_pending = False
        current.save(update_fields=["cleanup_pending", "updated_at"])
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
    rows = AgentUpload.objects.filter(cleanup_pending=True, expires_at__lte=now).order_by("updated_at", "id")[:limit]
    cleaned = 0
    for candidate in rows:
        try:
            with transaction.atomic():
                AgentUploadBudget.objects.select_for_update().get(pk=candidate.scope_digest)
                row = AgentUpload.objects.select_for_update().get(pk=candidate.pk)
                if not row.cleanup_pending or row.expires_at > now:
                    continue
                if row.state == "finalizing" and row.updated_at > now - timedelta(minutes=15):
                    continue
                if row.state in ("issued", "finalizing"):
                    row.state = "cancelled"
                    row.save(update_fields=["state", "updated_at"])
            _cleanup_upload(row)
            cleaned += 1
        except Exception as exc:  # noqa: BLE001 - storage errors carry provider payloads; the row is retried
            # Retain the original resource evidence and reservation on uncertainty.
            logger.warning(
                "reconcile_upload_cleanup: cleanup failed upload_id=%s exc_type=%s",
                candidate.pk,
                type(exc).__name__,
            )
            AgentUpload.objects.filter(pk=candidate.pk).update(updated_at=now)
    return cleaned


def _validated_upload_metadata(name: str, filename: str, file_size: int, agent_type: str) -> str:
    """Validate installer metadata against the incumbent registry and size bound."""
    from cms.assets.validation import ValidationError as AssetValidationError
    from cms.assets.validation import enforce_max_file_size_bytes, validate_file_extension

    _bounded_upload_label(name, 100)
    _bounded_upload_label(filename, 255)
    if type(file_size) is not int or file_size <= 0 or agent_type not in AgentType.values:
        raise CMSError(_INVALID_UPLOAD_METADATA)
    try:
        enforce_max_file_size_bytes(file_size)
        file_format = validate_file_extension(filename)
    except (ValueError, AssetValidationError):
        raise CMSError(_INVALID_UPLOAD_METADATA) from None
    if file_format.os_slug is None or not settings.AWS_S3_BUCKET_NAME:
        raise CMSError(_UPLOAD_UNAVAILABLE)
    return file_format.os_slug


def _bounded_upload_label(value: object, maximum: int) -> None:
    """Reject blank or oversized installer names and filenames."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CMSError(_INVALID_UPLOAD_METADATA)
