"""Principal-owned installer reads use the CMS's actual content scope."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import AgentConfig

from ._common import _agent_projection_dict
from ._content_authority import content_scope, content_scope_fields, fence_content_scope, require_content_scope

if TYPE_CHECKING:
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope
    from shared.schemas.cms_projections import AgentListItem


_CONTENT_UNAVAILABLE = "Content unavailable"


def list_scoped_agents(credential: CredentialContext, scope: ResourceScope | None) -> list[AgentListItem]:
    """List installer projections only after admitting the actual content parent."""
    if scope is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    require_content_scope(credential, scope, "read_content")
    rows = AgentConfig.objects.filter(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
    return [_agent_projection_dict(row) for row in rows.select_related("os")]


def get_scoped_agent(credential: CredentialContext, identifier: int, *, operation: str = "read_content") -> AgentConfig:
    """Resolve an installer inside its immutable authorized custody."""
    if type(identifier) is not int or identifier <= 0:
        raise CMSError(_CONTENT_UNAVAILABLE)
    snapshot = AgentConfig.objects.filter(pk=identifier).first()
    scope = content_scope(snapshot)
    if scope is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    require_content_scope(credential, scope, operation)
    row = (
        AgentConfig.objects.select_related("os")
        .filter(
            pk=snapshot.pk,
            owner_principal_uuid=snapshot.owner_principal_uuid,
            scope_generation=snapshot.scope_generation,
            s3_key=snapshot.s3_key,
            **content_scope_fields(scope),
        )
        .first()
    )
    if row is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    return row


def scoped_storage_used(credential: CredentialContext, scope: ResourceScope | None) -> int:
    """Count installer bytes within the admitted customer scope."""
    if scope is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    require_content_scope(credential, scope, "read_content")
    rows = AgentConfig.objects.filter(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
    return rows.aggregate(total=Sum("file_size_bytes"))["total"] or 0


def delete_scoped_agent(credential: CredentialContext, identifier: int) -> None:
    """Retire an authorized installer and retain evidence for storage cleanup."""
    from cms.models import AgentUpload, AgentUploadBudget
    from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields

    snapshot = get_scoped_agent(credential, identifier, operation="manage_content")
    scope = content_scope(snapshot)
    current = require_content_scope(credential, scope, "manage_content")
    upload = AgentUpload.objects.filter(agent_id=snapshot.pk, state="completed", install_key=snapshot.s3_key).first()
    if upload is None:
        raise CMSError("Installer cleanup evidence unavailable")
    with transaction.atomic():
        AgentUploadBudget.objects.select_for_update().get(pk=upload.scope_digest)
        row = (
            AgentConfig.objects.select_for_update()
            .filter(
                pk=snapshot.pk,
                owner_principal_uuid=snapshot.owner_principal_uuid,
                scope_generation=snapshot.scope_generation,
                s3_key=snapshot.s3_key,
                **content_scope_fields(scope),
            )
            .first()
        )
        if row is None:
            raise CMSError(_CONTENT_UNAVAILABLE)
        fence_content_scope(current, scope)
        row.deleted_at = timezone.now()
        row.save(update_fields=["deleted_at"])
        AgentUpload.objects.filter(pk=upload.pk).update(cleanup_pending=True)
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.AGENT,
                entity_id=row.pk,
                action=AuditAction.DELETE,
                **principal_actor_fields(current.principal),
                new_state={"upload_id": str(upload.pk)},
            ),
            strict=True,
        )
