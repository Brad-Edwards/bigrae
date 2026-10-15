"""CMS-owned principal reassignment of an existing range's business owner."""

from django.db import transaction

from cms.exceptions import CMSError
from cms.models import RangeInstance, Request
from engine.services import commit_range_owner_move, prepare_range_owner_move
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef
from shared.range_workspace_aggregate import range_in_domain_aggregate

from ._unified_authorization import require_range_action


def reassign_scoped_range_owner(
    credential: CredentialContext, instance_pk: int, new_owner: PrincipalRef, *, rehome: bool
) -> None:
    try:
        if rehome or not isinstance(new_owner, PrincipalRef):
            raise ValueError("Use the explicit scope move command")
        candidate = RangeInstance.objects.get(pk=instance_pk)
        snapshot = require_range_action(credential, candidate.uuid, "range.manage")
        if range_in_domain_aggregate(snapshot.request.request_id, snapshot.pk):
            raise ValueError("Range belongs to an immutable aggregate")
        prepared = prepare_range_owner_move(credential, snapshot.request.request_id, new_owner)
        with transaction.atomic():
            row = RangeInstance.objects.select_for_update().get(pk=snapshot.pk)
            request = Request.objects.select_for_update().get(pk=snapshot.request_id)
            if row.status != snapshot.status or row.owner_principal_uuid != snapshot.owner_principal_uuid:
                raise ValueError("Range owner changed")
            if range_in_domain_aggregate(request.request_id, row.pk):
                raise ValueError("Range aggregate changed")
            changed = commit_range_owner_move(prepared)
            if not changed:
                return
            row.owner_principal_uuid = request.owner_principal_uuid = new_owner.uuid
            row.save(update_fields=["owner_principal_uuid"])
            request.save(update_fields=["owner_principal_uuid"])
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.RANGE,
                    entity_id=row.pk,
                    entity_ref=str(row.uuid),
                    action=AuditAction.UPDATE,
                    **principal_actor_fields(credential.principal),
                    request_id=str(request.request_id),
                    previous_state={"owner_principal_uuid": str(snapshot.owner_principal_uuid)},
                    new_state={"owner_principal_uuid": str(new_owner.uuid)},
                    context="range ownership reassignment",
                ),
                strict=True,
            )
    except Exception:
        raise CMSError("Range ownership unavailable") from None
