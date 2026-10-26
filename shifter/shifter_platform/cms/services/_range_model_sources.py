"""Tenant-admin live source transitions through the incumbent range boundary."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction

from cms.models import RangeInstance
from engine.services import (
    admit_range_model_policy_change,
    begin_range_model_policy_change,
    resolve_model_access_range_views,
)
from shared.credentials import CredentialContext, resolve_local_credential
from shared.model_access import ContractError
from workspaces.services import WorkspaceAuthorizationError

from ._model_source_selection import resolve_model_source_sponsorship

REVISION_CONFLICT = "source.revision_conflict"


def _authorized_instance(
    actor: CredentialContext, request_id: UUID, action: str, *, locked: bool = False
) -> RangeInstance:
    """Resolve one range and require its exact policy action."""
    from ._unified_authorization import require_range_action

    query = RangeInstance.objects.select_related("request")
    instance = query.filter(request__request_id=request_id).first()
    if instance is None:
        raise WorkspaceAuthorizationError("Range access denied")
    require_range_action(actor, instance.uuid, action)
    if not locked:
        return instance
    if resolve_local_credential(actor) != actor:
        raise WorkspaceAuthorizationError("Range access denied")
    locked_instance = query.select_for_update(of=("self",)).filter(pk=instance.pk).first()
    if locked_instance is None:
        raise WorkspaceAuthorizationError("Range access denied")
    return locked_instance


def get_range_model_sources(actor: CredentialContext, *, request_id: UUID) -> dict[str, object]:
    """Read selected policy and effective grant state without credentials."""
    from engine.services import get_range_model_policy_status

    instance = _authorized_instance(actor, request_id, "range.read")
    from workspaces.services import resource_scope_from_ids

    scope = resource_scope_from_ids(
        kind=instance.scope_kind,
        account_id=instance.account_id,
        organization_id=instance.organization_id,
        workspace_id=instance.workspace_id,
    )
    effective_selection = instance.model_sources or (
        (instance.model_launch_scope or {}).get("source_sponsorship") or {}
    ).get("selection", {})
    return {
        "request_id": request_id,
        "workspace": scope.workspace_uuid,
        "scenario": instance.scenario_id,
        "revision": instance.model_source_policy_revision,
        "selection": effective_selection or {"aliases": []},
        "error": instance.model_source_policy_error,
        "runtime": get_range_model_policy_status(request_id=request_id),
    }


def change_range_model_sources(
    actor: CredentialContext, *, request_id: UUID, expected_revision: int, selection: object
) -> dict[str, object]:
    """Commit revocation, then attempt replacement; failure stays visibly blocked.

    The two commits deliberately prevent a failed replacement from restoring
    the old credential epoch. Neither phase holds locks across provider I/O.
    """
    from ._model_allocation import _snapshot_needs, prepare_model_access_for_dispatch

    with transaction.atomic():
        instance = _authorized_instance(actor, request_id, "range.manage", locked=True)
        if instance.model_source_policy_revision != expected_revision:
            raise ContractError(REVISION_CONFLICT)
        if not _snapshot_needs(instance):
            raise ContractError("source.range_unavailable")
        workspace_id = instance.workspace_id
        if workspace_id is None:
            raise ContractError("source.range_unavailable")
        sponsorship = resolve_model_source_sponsorship(actor, workspace_id, selection)
        retry_pending = (
            instance.model_source_policy_error == "source.admission_pending"
            and instance.model_source_sponsorship == sponsorship.model_dump(mode="json")
        )
        revision = (
            expected_revision
            if retry_pending
            else begin_range_model_policy_change(
                request_id=request_id,
                expected_revision=expected_revision,
                actor_principal_uuid=actor.principal.uuid,
            )
        )
        instance.model_sources = sponsorship.selection.model_dump(mode="json")
        instance.model_source_sponsorship = sponsorship.model_dump(mode="json")
        instance.model_source_policy_revision = revision
        instance.model_source_policy_error = "source.admission_pending"
        instance.save(
            update_fields=[
                "model_sources",
                "model_source_sponsorship",
                "model_source_policy_revision",
                "model_source_policy_error",
            ]
        )
    try:
        with transaction.atomic():
            current = _authorized_instance(actor, request_id, "range.manage", locked=True)
            if current.model_source_policy_revision != revision:
                raise ContractError(REVISION_CONFLICT)
            (view,) = resolve_model_access_range_views(request_uuids=(request_id,))
            prepare_model_access_for_dispatch(request_id, range_id=view.range_uuid, renew=True)
            admitted = admit_range_model_policy_change(request_id=request_id, expected_revision=revision)
            current.model_source_policy_error = "" if admitted else "source.admission_unavailable"
            current.save(update_fields=["model_source_policy_error"])
    except ContractError as exc:
        RangeInstance.objects.filter(request__request_id=request_id, model_source_policy_revision=revision).update(
            model_source_policy_error="source.admission_unavailable"
        )
        if exc.code == REVISION_CONFLICT:
            raise
    return get_range_model_sources(actor, request_id=request_id)


def revoke_range_model_sources(actor: CredentialContext, *, request_id: UUID) -> dict[str, object]:
    """Revoke a range's model grants under range-administration authority (M09, #2126).

    Authorizes the actor over the range's workspace, then transactionally fences
    the current generation's model authorities and dispatch leases in Engine with
    real-actor audit. Returns the refreshed range source status; re-enrollment is
    the existing selection-change (renew) flow, never a browser-delivered token.
    """
    from engine.services import revoke_range_model_access

    _authorized_instance(actor, request_id, "range.manage")
    revoke_range_model_access(request_id=request_id, actor_principal_uuid=actor.principal.uuid)
    return get_range_model_sources(actor, request_id=request_id)


def get_range_model_policy_status_for_instance(range_instance_id: int) -> dict[str, object] | None:
    """Return one range instance's current model-policy runtime status, or ``None``.

    Ownership is the caller's responsibility (the participant surface passes only
    the participant's own range); this resolves the instance's provisioning request
    and returns the bounded engine runtime status without workspace authority.
    """
    from engine.services import get_range_model_policy_status

    instance = RangeInstance.objects.filter(pk=range_instance_id).select_related("request").first()
    if instance is None or instance.request_id is None:
        return None
    return get_range_model_policy_status(request_id=instance.request.request_id)


def list_organization_model_ranges(
    actor: CredentialContext, *, organization_uuid: UUID, page: int = 1
) -> dict[str, object]:
    """Page ranges in the tenant's active workspaces without exposing guest data."""
    from engine.services import authorize_model_source_organization
    from workspaces.services import active_workspace_ids_for_organization

    authorize_model_source_organization(actor, organization_uuid)
    with transaction.atomic():
        workspace_ids = active_workspace_ids_for_organization(organization_uuid)
        query = RangeInstance.objects.filter(workspace_id__in=workspace_ids, request__isnull=False).order_by("-pk")
        count = query.count()
        rows = list(query.select_related("request")[(page - 1) * 25 : page * 25])
    return {
        "count": count,
        "page": page,
        "has_next": page * 25 < count,
        "results": [
            {
                "request_id": row.request.request_id,
                "scenario": row.scenario_id,
                "status": row.status,
                "revision": row.model_source_policy_revision,
                "error": row.model_source_policy_error,
            }
            for row in rows
        ],
    }
