"""Principal-owned installer reads use the CMS's actual content scope."""

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import AgentConfig

from ._common import _agent_projection_dict
from ._content_authority import content_scope, content_scope_fields, require_content_scope


def list_scoped_agents(credential, scope):
    require_content_scope(credential, scope, "read_content")
    rows = AgentConfig.objects.filter(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
    return [_agent_projection_dict(row) for row in rows.select_related("os")]


def get_scoped_agent(credential, identifier, *, operation="read_content"):
    if type(identifier) is not int or identifier <= 0:
        raise CMSError("Content unavailable")
    snapshot = AgentConfig.objects.filter(pk=identifier).first()
    scope = content_scope(snapshot)
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
        raise CMSError("Content unavailable")
    return row


def scoped_storage_used(credential, scope):
    require_content_scope(credential, scope, "read_content")
    rows = AgentConfig.objects.filter(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
    return rows.aggregate(total=Sum("file_size_bytes"))["total"] or 0


def delete_scoped_agent(credential, identifier):
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
            raise CMSError("Content unavailable")
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
