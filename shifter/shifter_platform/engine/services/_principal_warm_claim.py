"""Atomic transfer of a quarantined installation warm range to a customer principal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope

if TYPE_CHECKING:
    from engine.models import Range


@dataclass(frozen=True, slots=True)
class PreparedWarmRangeClaim:
    """Live parent decision plus exact destination ancestry, resolved before locks."""

    credential: CredentialContext
    scope: ResourceScope
    scope_ids: tuple[str, int | None, int | None, int | None]


def prepare_warm_range_claim(credential: CredentialContext, scope: ResourceScope) -> PreparedWarmRangeClaim:
    """Authorize the customer launch target without holding a SQL transaction."""
    from workspaces.services import resolve_range_scope_binding

    if not isinstance(scope, ResourceScope) or scope.kind != "account":
        raise ValueError("Warm claim scope unavailable")
    current = resolve_current_credential(credential)
    resolved = resolve_range_scope_binding(scope, member=current.principal)
    parent = scope_parent(scope)
    require_authorization(
        AuthorizationRequest(current.principal, f"{parent.type}.launch_range", parent, scope, current.ceiling)
    )
    if resolve_current_credential(current) != current:
        raise ValueError("Warm claim authority changed")
    return PreparedWarmRangeClaim(
        current,
        scope,
        (resolved.kind, resolved.account_id, resolved.organization_id, resolved.workspace_id),
    )


def commit_warm_range_claim(
    prepared: PreparedWarmRangeClaim,
    *,
    request_id: UUID,
    claimant_request_id: UUID,
    cms_range_uuid: UUID,
    user_id: int | None,
) -> int:
    """Fence the admitted destination and move Engine owner and scope together."""
    from engine.models import Range, Request, WarmRangeGeneration
    from workspaces.services import resolve_range_scope_binding

    current = _current_claim_credential(prepared, user_id)
    with transaction.atomic():
        resolved = resolve_range_scope_binding(prepared.scope, member=current.principal, for_update=True)
        if (resolved.kind, resolved.account_id, resolved.organization_id, resolved.workspace_id) != prepared.scope_ids:
            raise ValueError("Warm claim scope changed")
        generation = (
            WarmRangeGeneration.objects.select_for_update()
            .filter(
                request_id=request_id,
                state=WarmRangeGeneration.State.CLAIMED,
                claimed_by_request_id=claimant_request_id,
            )
            .first()
        )
        row = (
            Range.objects.select_for_update(of=("self",))
            .select_related("request")
            .filter(request__request_id=request_id)
            .first()
        )
        if generation is None or row is None or row.request is None or row.cms_range_uuid != cms_range_uuid:
            raise ValueError("Warm generation changed")
        _require_quarantined_custody(row)
        from ._launch_authority import range_runtime_binding_digest
        from ._receipt import _revoke_receipt_verifier_for_range

        previous_digest = range_runtime_binding_digest(row)
        _revoke_receipt_verifier_for_range(row)
        if row.request_id is None:
            raise ValueError("Warm generation request unavailable")
        engine_request = Request.objects.select_for_update().get(pk=row.request_id)
        kind, account_id, organization_id, workspace_id = prepared.scope_ids
        row.user_id = user_id
        row.cms_user_id = user_id
        row.owner_principal_uuid = current.principal.uuid
        row.scope_kind = kind
        row.account_id = account_id
        row.organization_id = organization_id
        row.workspace_id = workspace_id
        row.save(
            update_fields=[
                "user_id",
                "cms_user_id",
                "owner_principal_uuid",
                "scope_kind",
                "account_id",
                "organization_id",
                "workspace_id",
                "updated_at",
            ]
        )
        engine_request.user_id = user_id
        engine_request.owner_principal_uuid = current.principal.uuid
        engine_request.scope_kind = kind
        engine_request.account_id = account_id
        engine_request.organization_id = organization_id
        engine_request.workspace_id = workspace_id
        engine_request.save(
            update_fields=[
                "user_id",
                "owner_principal_uuid",
                "scope_kind",
                "account_id",
                "organization_id",
                "workspace_id",
            ]
        )
        _record_warm_claim_authority(row, current, previous_digest)
        return row.pk


def _record_warm_claim_authority(row: Range, credential: CredentialContext, previous_digest: str) -> None:
    """Retain the exact launch decision as current cleanup custody evidence."""
    from engine.models import RangeAuthorityChange
    from shared.operation_authority import credential_reference_fields
    from shared.raes.runtime_target import is_raes_provisioning_plan

    from ._launch_authority import range_runtime_binding_digest
    from ._range_authority import project_range_authority

    if row.request is None:
        raise ValueError("Warm generation request unavailable")
    resource_generation = row.resource_generation
    if resource_generation is None:
        raise ValueError("Warm generation identity unavailable")
    projection = project_range_authority(row.request.request_id)
    parent = projection.parent
    RangeAuthorityChange.objects.create(
        range=row,
        request_uuid=projection.request_uuid,
        resource="raes-range" if is_raes_provisioning_plan(row.range_config) else "range",
        operation="scope",
        resource_generation=resource_generation,
        previous_binding_digest=previous_digest,
        **credential_reference_fields(credential),
        authority_kind="principal",
        authority_action=f"{parent.type}.launch_range",
        authority_target_type=parent.type,
        authority_target_uuid=parent.uuid,
        authority_parent_type=parent.type,
        authority_parent_uuid=parent.uuid,
        authority_scope_kind=projection.scope.kind,
        authority_account_uuid=projection.scope.account_uuid,
        authority_organization_uuid=projection.scope.organization_uuid,
        authority_workspace_uuid=projection.scope.workspace_uuid,
        authority_owner_uuid=projection.owner_principal_uuid,
        authority_resource_uuid=projection.public_uuid,
        authority_binding_digest=range_runtime_binding_digest(row),
    )


def _current_claim_credential(prepared: PreparedWarmRangeClaim, user_id: int | None) -> CredentialContext:
    """Require the admitted local proof and its real optional human projection before the custody write."""
    from management.services import human_principal_user_id

    if not isinstance(prepared, PreparedWarmRangeClaim):
        raise ValueError("Warm claim authority unavailable")
    current = resolve_local_credential(prepared.credential)
    expected_user = human_principal_user_id(current.principal) if current.principal.kind == "human" else None
    if current != prepared.credential or user_id != expected_user:
        raise ValueError("Warm claim identity changed")
    return current


def _require_quarantined_custody(row: Range) -> None:
    """Only an installation-owned quarantined generation can become a customer claim."""
    from engine.models import Range

    if row.owner_principal_uuid is None or row.request is None:
        raise ValueError("Warm generation custody unavailable")
    if (
        row.scope_kind,
        row.account_id,
        row.organization_id,
        row.workspace_id,
        row.request.owner_principal_uuid,
        row.status,
        row.event_uuid,
        row.vpn_access_binding,
    ) != ("installation", None, None, None, row.owner_principal_uuid, Range.Status.PROVISIONING, None, None):
        raise ValueError("Warm generation custody unavailable")
