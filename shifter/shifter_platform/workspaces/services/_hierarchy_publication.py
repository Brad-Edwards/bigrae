"""Durable publication of SQL-owned account hierarchy relationships."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from shared.authorization import (
    AuthorizationProvider,
    AuthorizationProviderBindingError,
    HierarchyRelationshipChange,
)
from workspaces.models import Account, HierarchyPublication, Organization, Workspace

_UNAVAILABLE = "Authorization hierarchy publication is unavailable"


class HierarchyPublicationPending(AuthorizationProviderBindingError):
    """A committed object whose exact hierarchy intent can be retried."""

    def __init__(self, object_uuid: UUID, publication_receipt: UUID):
        super().__init__(_UNAVAILABLE)
        self.object_uuid = object_uuid
        self.publication_receipt = publication_receipt


_KIND_ORDER = {"account": 0, "organization": 1, "workspace": 2}


@dataclass(frozen=True, slots=True)
class _PublicationSnapshot:
    publication_id: UUID
    object_kind: str
    object_uuid: UUID
    parent_kind: str
    parent_uuid: UUID | None
    store_id: str
    model_id: str


def _stage(object_kind: str, object_uuid: UUID, parent_kind: str, parent_uuid: UUID | None) -> None:
    """Record exact relationship intent in the caller's SQL transaction."""
    HierarchyPublication.objects.create(
        object_kind=object_kind,
        object_uuid=object_uuid,
        parent_kind=parent_kind,
        parent_uuid=parent_uuid,
    )


def stage_account_hierarchy(account: Account) -> None:
    """Stage a new account and any structural defaults created with it."""
    _stage("account", account.uuid, "installation", None)
    organizations = tuple(Organization.objects.filter(account=account).order_by("pk"))
    for organization in organizations:
        _stage("organization", organization.uuid, "account", account.uuid)
        for workspace in Workspace.objects.filter(organization=organization).order_by("pk"):
            _stage("workspace", workspace.uuid, "organization", organization.uuid)


def stage_organization_hierarchy(organization: Organization) -> None:
    """Stage a new organization and the default workspace created with it."""
    if organization.account_id is None:
        raise ValueError("organization parent unavailable")
    account_uuid = Account.objects.values_list("uuid", flat=True).get(pk=organization.account_id)
    _stage("organization", organization.uuid, "account", account_uuid)
    for workspace in Workspace.objects.filter(organization=organization).order_by("pk"):
        _stage("workspace", workspace.uuid, "organization", organization.uuid)


def stage_workspace_hierarchy(workspace: Workspace) -> None:
    """Stage a new workspace beneath its exact persisted organization."""
    organization_uuid = Organization.objects.values_list("uuid", flat=True).get(pk=workspace.organization_id)
    _stage("workspace", workspace.uuid, "organization", organization_uuid)


def _live_parent(object_kind: str, object_uuid: UUID) -> tuple[str, UUID | None]:
    """Resolve current SQL ancestry without consulting authorization data."""
    if object_kind == "account" and Account.objects.filter(uuid=object_uuid).exists():
        return "installation", None
    if object_kind == "organization":
        organization = Organization.objects.select_related("account").filter(uuid=object_uuid).first()
        if organization is not None:
            account = organization.account
            if account is not None:
                return "account", account.uuid
    if object_kind == "workspace":
        workspace = Workspace.objects.select_related("organization").filter(uuid=object_uuid).first()
        if workspace is not None:
            return "organization", workspace.organization.uuid
    raise ValueError("hierarchy object unavailable")


def _snapshot(publication: HierarchyPublication, store_id: str, model_id: str) -> _PublicationSnapshot:
    parent_kind, parent_uuid = _live_parent(publication.object_kind, publication.object_uuid)
    if (publication.parent_kind, publication.parent_uuid) != (parent_kind, parent_uuid):
        raise ValueError("hierarchy binding changed")
    return _PublicationSnapshot(
        publication.pk,
        publication.object_kind,
        publication.object_uuid,
        publication.parent_kind,
        publication.parent_uuid,
        store_id,
        model_id,
    )


def _change(snapshot: _PublicationSnapshot) -> HierarchyRelationshipChange:
    parent = (
        "installation:root"
        if snapshot.parent_kind == "installation"
        else f"{snapshot.parent_kind}:{snapshot.parent_uuid}"
    )
    return HierarchyRelationshipChange(
        parent,
        snapshot.parent_kind,
        f"{snapshot.object_kind}:{snapshot.object_uuid}",
    )


def _write_and_readback(provider: AuthorizationProvider, change: HierarchyRelationshipChange) -> bool:
    """Recover a lost acknowledgement using a primary relationship read."""
    with suppress(Exception):
        provider.write_relationships(change)
    try:
        observed = provider.read_relationships(change)
    except Exception:
        return False
    return bool(observed.grant_present)


def _mark_unresolved(publication_id: UUID, reason: str) -> None:
    HierarchyPublication.objects.filter(pk=publication_id).exclude(state="confirmed").update(
        state="unresolved",
        outcome_reason=reason,
    )


def _publish_one(publication_id: UUID, provider: AuthorizationProvider, store_id: str, model_id: str) -> None:
    """Publish one parent edge with no provider I/O under a SQL transaction."""
    try:
        with transaction.atomic():
            publication = HierarchyPublication.objects.select_for_update().get(pk=publication_id)
            if publication.state == "confirmed":
                if publication.provider_store_id != store_id or publication.model_id != model_id:
                    raise ValueError("provider binding changed")
                return
            if publication.provider_store_id and (
                publication.provider_store_id != store_id or publication.model_id != model_id
            ):
                raise ValueError("provider binding changed")
            snapshot = _snapshot(publication, store_id, model_id)
            publication.state = "requested"
            publication.provider_store_id = store_id
            publication.model_id = model_id
            publication.outcome_reason = "accepted"
            publication.save(update_fields=["state", "provider_store_id", "model_id", "outcome_reason", "updated_at"])
        if not _write_and_readback(provider, _change(snapshot)):
            _mark_unresolved(publication_id, "provider_outcome_unknown")
            raise ValueError("relationship readback failed")
        with transaction.atomic():
            publication = HierarchyPublication.objects.select_for_update().get(pk=publication_id)
            if _snapshot(publication, store_id, model_id) != snapshot:
                publication.state = "unresolved"
                publication.outcome_reason = "hierarchy_binding_changed"
                publication.save(update_fields=["state", "outcome_reason", "updated_at"])
                raise ValueError("hierarchy binding changed")
            publication.state = "confirmed"
            publication.outcome_reason = "primary_readback_confirmed"
            publication.save(update_fields=["state", "outcome_reason", "updated_at"])
    except Exception:
        _mark_unresolved(publication_id, "provider_outcome_unknown")
        raise


def _publication_ids(root_kind: str, root_uuid: UUID) -> tuple[UUID, ...]:
    """Select the exact staged subtree in parent-before-child order."""
    if root_kind == "account":
        account = Account.objects.filter(uuid=root_uuid).first()
        if account is None:
            raise ValueError("account unavailable")
        organization_uuids = tuple(Organization.objects.filter(account=account).values_list("uuid", flat=True))
        workspace_uuids = tuple(Workspace.objects.filter(organization__account=account).values_list("uuid", flat=True))
        target_uuids = {"account": (root_uuid,), "organization": organization_uuids, "workspace": workspace_uuids}
    elif root_kind == "organization":
        organization = Organization.objects.filter(uuid=root_uuid).first()
        if organization is None:
            raise ValueError("organization unavailable")
        target_uuids = {
            "organization": (root_uuid,),
            "workspace": tuple(Workspace.objects.filter(organization=organization).values_list("uuid", flat=True)),
        }
    elif root_kind == "workspace":
        target_uuids = {"workspace": (root_uuid,)}
    else:
        raise ValueError("hierarchy kind unavailable")
    rows: list[HierarchyPublication] = []
    for kind, uuids in target_uuids.items():
        rows.extend(HierarchyPublication.objects.filter(object_kind=kind, object_uuid__in=uuids))
    if len(rows) != sum(len(uuids) for uuids in target_uuids.values()):
        raise ValueError("hierarchy publication intent unavailable")
    rows.sort(key=lambda row: (_KIND_ORDER[row.object_kind], str(row.object_uuid)))
    return tuple(row.pk for row in rows)


def publish_hierarchy_relationships(
    root_kind: str,
    root_uuid: UUID,
    provider: AuthorizationProvider,
) -> None:
    """Publish a staged hierarchy subtree and require exact primary readback."""
    try:
        store_id = str(provider.store_id)
        model_id = str(provider.model_id)
        if not store_id or not model_id:
            raise ValueError("provider binding unavailable")
        for publication_id in _publication_ids(root_kind, root_uuid):
            _publish_one(publication_id, provider, store_id, model_id)
    except AuthorizationProviderBindingError:
        raise
    except Exception as exc:
        raise AuthorizationProviderBindingError(_UNAVAILABLE) from exc


def reconcile_hierarchy_publications(provider: AuthorizationProvider, *, limit: int = 100) -> int:
    """Resume a bounded batch of existing hierarchy intents, parents before children."""
    from django.db.models import Case, IntegerField, Value, When

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid publication batch limit")
    order = Case(
        *(When(object_kind=kind, then=Value(position)) for kind, position in _KIND_ORDER.items()),
        output_field=IntegerField(),
    )
    pending = tuple(
        HierarchyPublication.objects.exclude(state="confirmed")
        .order_by(order, "created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    try:
        store_id, model_id = str(provider.store_id), str(provider.model_id)
        if not store_id or not model_id:
            raise ValueError("provider binding unavailable")
        for publication_id in pending:
            _publish_one(publication_id, provider, store_id, model_id)
    except Exception as exc:
        raise AuthorizationProviderBindingError(_UNAVAILABLE) from exc
    return len(pending)


def publish_created_hierarchy(root_kind: str, root_uuid: UUID, provider: AuthorizationProvider) -> None:
    """Return a durable receipt when a committed creation still needs publication."""
    try:
        publish_hierarchy_relationships(root_kind, root_uuid, provider)
    except AuthorizationProviderBindingError as exc:
        receipt = HierarchyPublication.objects.get(object_kind=root_kind, object_uuid=root_uuid)
        raise HierarchyPublicationPending(root_uuid, receipt.pk) from exc


def creation_for_receipt(receipt_uuid: UUID, kind: str, parent_uuid: UUID | None):
    """Resolve only the exact creation beneath the caller's authorized parent."""
    from ._account import AccountScopeError

    receipt = HierarchyPublication.objects.filter(pk=receipt_uuid, object_kind=kind, parent_uuid=parent_uuid).first()
    if receipt is None:
        raise AccountScopeError("Scope unavailable")
    models: dict[str, type[Account] | type[Organization] | type[Workspace]] = {
        "account": Account,
        "organization": Organization,
        "workspace": Workspace,
    }
    model = models[kind]
    obj = model.objects.filter(uuid=receipt.object_uuid).first()
    if obj is None or _live_parent(kind, obj.uuid) != (receipt.parent_kind, receipt.parent_uuid):
        raise AccountScopeError("Scope unavailable")
    return obj
