"""Defense-in-depth authority fencing for canonical Engine range mutations."""

from __future__ import annotations

from uuid import UUID

from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from engine.models import Range, WarmRangeGeneration
from shared.model_access import AuthorityInvalidation, AuthorityState, OwnedReference
from shared.model_access.authority_port import invalidate_authority

_PREVIOUS = "_model_access_range_authority_before"
_FIELDS = ("owner_principal_uuid", "workspace_id", "status", "destroyed_at")


def _references(range_obj: Range, previous: dict[str, object] | None = None) -> tuple[OwnedReference, ...]:
    """Build only the authority references whose underlying facts changed."""
    refs = {
        ("engine", "all-ranges"),
        ("engine", f"range:{range_obj.uuid}"),
    }
    if previous is None or previous.get("owner_principal_uuid") != range_obj.owner_principal_uuid:
        from management.services import model_access_user_references_for_principals

        owners = []
        if range_obj.owner_principal_uuid is not None:
            refs.add(("management", f"principal:{range_obj.owner_principal_uuid}"))
            owners.append(range_obj.owner_principal_uuid)
        previous_owner = previous.get("owner_principal_uuid") if previous is not None else None
        if previous_owner is not None:
            if not isinstance(previous_owner, UUID):
                raise ValueError("Previous range owner identity unavailable")
            refs.add(("management", f"principal:{previous_owner}"))
            owners.append(previous_owner)
        refs.update((ref.owner, ref.reference) for ref in model_access_user_references_for_principals(tuple(owners)))
    if previous is None or previous.get("workspace_id") != range_obj.workspace_id:
        if range_obj.workspace_id is not None:
            refs.add(("workspaces", f"workspace-id:{range_obj.workspace_id}"))
        if previous is not None and previous.get("workspace_id") is not None:
            refs.add(("workspaces", f"workspace-id:{previous['workspace_id']}"))
    return tuple(OwnedReference(owner=owner, reference=reference) for owner, reference in sorted(refs))


def _invalidate_selectors(range_obj: Range, previous: dict[str, object] | None = None) -> None:
    """Refresh selector evidence without revoking a generation-stable launch."""
    invalidate_authority(
        AuthorityInvalidation(
            deployment_id=None,
            authority_refs=_references(range_obj, previous),
            state=AuthorityState.UNKNOWN,
            reason="range-selector-changed",
            allocation_effect="selector_only",
        )
    )


def _invalidate_launch(range_obj: Range) -> None:
    """Revoke only this range's launch authority after ownership or removal changes."""
    invalidate_authority(
        AuthorityInvalidation(
            deployment_id=None,
            authority_refs=(OwnedReference(owner="engine", reference=f"model-launch:{range_obj.uuid}"),),
            state=AuthorityState.UNKNOWN,
            reason="range-launch-authority-changed",
        )
    )


@receiver(pre_save, sender=Range, dispatch_uid="engine.model_access.range.capture")
def capture_range_authority(sender: type[Range], instance: Range, **kwargs: object) -> None:
    """Capture persisted range authority before saving."""
    if instance._state.adding:
        setattr(instance, _PREVIOUS, None)
        return
    setattr(instance, _PREVIOUS, sender.objects.filter(pk=instance.pk).values(*_FIELDS).first())


@receiver(post_save, sender=Range, dispatch_uid="engine.model_access.range.invalidate")
def invalidate_range_authority(sender: type[Range], instance: Range, **kwargs: object) -> None:
    """Invalidate range authority when security-relevant fields change."""
    previous = getattr(instance, _PREVIOUS, None)
    if previous is None or any(previous[field] != getattr(instance, field) for field in _FIELDS):
        _invalidate_selectors(instance, previous)
    if previous is not None and any(
        previous[field] != getattr(instance, field)
        for field in ("owner_principal_uuid", "workspace_id", "destroyed_at")
    ):
        _invalidate_launch(instance)


@receiver(pre_delete, sender=Range, dispatch_uid="engine.model_access.range.delete")
def invalidate_deleted_range_authority(sender: type[Range], instance: Range, **kwargs: object) -> None:
    """Invalidate range authority before deletion."""
    _invalidate_selectors(instance)
    _invalidate_launch(instance)


_WARM_FIELDS = (
    "state",
    "claimed_by_request_id",
    "request_id",
    "range_id",
    "capacity_scope_ref",
    "capacity_draw_key",
    "idle_deadline",
)


@receiver(pre_save, sender=WarmRangeGeneration, dispatch_uid="engine.model_access.warm.capture")
def capture_warm_authority(sender: type[WarmRangeGeneration], instance: WarmRangeGeneration, **kwargs: object) -> None:
    """Capture the ledger facts from which preparation authority is derived."""
    previous = None if instance._state.adding else sender.objects.filter(pk=instance.pk).values(*_WARM_FIELDS).first()
    instance.__dict__["_model_access_warm_before"] = previous


@receiver(post_save, sender=WarmRangeGeneration, dispatch_uid="engine.model_access.warm.invalidate")
def invalidate_warm_authority(
    sender: type[WarmRangeGeneration], instance: WarmRangeGeneration, **kwargs: object
) -> None:
    """A normal preparation-to-ready transition retains the same authority."""
    previous = getattr(instance, "_model_access_warm_before", None)
    if previous is None:
        return
    current = {field: getattr(instance, field) for field in _WARM_FIELDS}
    for snapshot in (previous, current):
        snapshot["state"] = snapshot["state"] in WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES
    if current != previous:
        from engine.services._model_warm_authority import invalidate_warm_preparation

        invalidate_warm_preparation(instance)


@receiver(pre_delete, sender=WarmRangeGeneration, dispatch_uid="engine.model_access.warm.delete")
def invalidate_deleted_warm_authority(
    sender: type[WarmRangeGeneration], instance: WarmRangeGeneration, **kwargs: object
) -> None:
    """Removing the ledger never leaves preparation authority behind."""
    from engine.services._model_warm_authority import invalidate_warm_preparation

    invalidate_warm_preparation(instance)
