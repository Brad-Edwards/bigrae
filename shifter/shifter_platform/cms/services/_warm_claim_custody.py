"""Local CMS warm-claim custody fence, committed with the Engine claim."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cms.models import RangeInstance
    from engine.services import PreparedWarmRangeClaim


class _WarmClaimRollback(Exception):
    """Internal sentinel to roll back an inconsistent claim to cold fallback."""


def _commit_cms_warm_claim(
    range_instance: RangeInstance, prepared: PreparedWarmRangeClaim, user_id: int | None
) -> None:
    """Move CMS intent and projection from installation custody to the claimant."""
    from cms.models import Request
    from shared.credentials import resolve_local_credential
    from shared.operation_authority import credential_reference_fields, credential_reference_from_record

    request = range_instance.request
    if request is None:
        raise _WarmClaimRollback
    original = resolve_local_credential(credential_reference_from_record(request))
    if (
        original.principal.kind,
        original.principal.uuid,
        request.owner_principal_uuid,
        range_instance.scope_kind,
        request.scope_kind,
        range_instance.status,
        range_instance.account_id,
        range_instance.organization_id,
        range_instance.workspace_id,
        request.account_id,
        request.organization_id,
        request.workspace_id,
    ) != (
        "service",
        range_instance.owner_principal_uuid,
        range_instance.owner_principal_uuid,
        "installation",
        "installation",
        "provisioning",
        None,
        None,
        None,
        None,
        None,
        None,
    ):
        raise _WarmClaimRollback
    kind, account_id, organization_id, workspace_id = prepared.scope_ids
    fields = {
        "user_id": user_id,
        "owner_principal_uuid": prepared.credential.principal.uuid,
        "scope_kind": kind,
        "account_id": account_id,
        "organization_id": organization_id,
        "workspace_id": workspace_id,
    }
    locked_request = Request.objects.select_for_update().get(pk=request.pk)
    for name, value in fields.items():
        setattr(range_instance, name, value)
        setattr(locked_request, "user_id" if name == "user_id" else name, value)
    authority = credential_reference_fields(prepared.credential)
    for name, authority_value in authority.items():
        setattr(locked_request, name, authority_value)
    range_instance.save(update_fields=[*fields, "updated_at"])
    locked_request.save(update_fields=[*fields, *authority])
