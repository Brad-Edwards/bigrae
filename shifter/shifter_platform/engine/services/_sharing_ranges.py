"""Bounded Engine range queries for model-access selector owners."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import Q

from shared.model_access import ModelAccessRangePage, ModelAccessRangePagination, ModelAccessRangeView, OwnedReference

from ._sharing_persistence import SharingError

_RANGE_RESOLUTION_SHAPE = "sharing.range_resolution_shape"
_RANGE_MEMBERSHIP_CHANGED = "sharing.range_membership_changed"
_RANGE_MEMBERSHIP_UNAVAILABLE = "sharing.range_membership_unavailable"
_DEFAULT_PAGINATION = ModelAccessRangePagination()


def model_access_range_reference(range_uuid: UUID) -> OwnedReference:
    """Return the canonical model-access sharing identity of one Engine range."""
    return OwnedReference(owner="deployment", reference=f"range:{range_uuid}")


def resolve_model_access_range_reference(request_uuid: UUID) -> OwnedReference:
    """Return the sharing identity of the range realized for ``request_uuid``, in any state.

    Membership selectors resolve live ranges only. A replacement launch instead
    needs the identity of the exact range it replaces, which may already be
    FAILED or DESTROYING, so published restrictions bound to that range carry
    over (PLAT-202, #2462). An unknown or ambiguous request fails closed.
    """
    from engine.models import Range

    range_uuids = tuple(Range.objects.filter(request__request_id=request_uuid).values_list("uuid", flat=True)[:2])
    if len(range_uuids) != 1:
        raise SharingError(_RANGE_MEMBERSHIP_UNAVAILABLE)
    return model_access_range_reference(range_uuids[0])


def _query_shape(
    *,
    range_uuids: tuple[UUID, ...] | None,
    request_uuids: tuple[UUID, ...] | None,
    user_ids: tuple[int, ...] | None,
    user_principal_uuids: tuple[UUID, ...] | None,
    workspace_ids: tuple[int, ...] | None,
    all_ranges: bool,
) -> tuple[tuple[object, ...], tuple[object, ...] | None, Q]:
    """Validate one query mode and return its supplied values and ORM filters."""
    modes = (range_uuids, request_uuids, user_ids, workspace_ids)
    if sum(value is not None for value in modes) + int(all_ranges) != 1:
        raise SharingError(_RANGE_RESOLUTION_SHAPE)

    explicit_values: tuple[object, ...] | None = None
    filters = Q()
    if range_uuids is not None:
        explicit_values = tuple(range_uuids)
        filters = Q(uuid__in=explicit_values)
    elif request_uuids is not None:
        explicit_values = tuple(request_uuids)
        filters = Q(request__request_id__in=explicit_values)
    elif user_ids is not None:
        if user_principal_uuids is None or len(user_principal_uuids) != len(user_ids):
            raise SharingError(_RANGE_RESOLUTION_SHAPE)
        filters = Q(owner_principal_uuid__in=user_principal_uuids) | Q(
            owner_principal_uuid__isnull=True,
            user_id__in=tuple(user_ids),
        )
    elif workspace_ids is not None:
        filters = Q(workspace_id__in=tuple(workspace_ids))
    elif user_principal_uuids is not None:
        raise SharingError(_RANGE_RESOLUTION_SHAPE)
    supplied = next((value for value in modes if value is not None), ())
    return supplied, explicit_values, filters


def _validate_supplied_values(supplied: tuple[object, ...], explicit_values: tuple[object, ...] | None) -> None:
    """Reject duplicate, overlong explicit, or boolean identifiers."""
    invalid = (
        len(supplied) != len(set(supplied))
        or (explicit_values is not None and len(supplied) > 1000)
        or any(isinstance(value, bool) for value in supplied)
    )
    if invalid:
        raise SharingError(_RANGE_RESOLUTION_SHAPE)


def _validate_page_shape(
    *, page_size: int, continuation: UUID | None, explicit_values: tuple[object, ...] | None
) -> None:
    """Validate page bounds and the explicit-query pagination prohibition."""
    invalid = (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
        or (continuation is not None and not isinstance(continuation, UUID))
        or (explicit_values is not None and continuation is not None)
        or (explicit_values is not None and page_size < len(explicit_values))
    )
    if invalid:
        raise SharingError(_RANGE_RESOLUTION_SHAPE)


def resolve_model_access_range_page(
    *,
    range_uuids: tuple[UUID, ...] | None = None,
    request_uuids: tuple[UUID, ...] | None = None,
    user_ids: tuple[int, ...] | None = None,
    user_principal_uuids: tuple[UUID, ...] | None = None,
    workspace_ids: tuple[int, ...] | None = None,
    all_ranges: bool = False,
    pagination: ModelAccessRangePagination = _DEFAULT_PAGINATION,
) -> ModelAccessRangePage:
    """Lock one bounded keyset page from an exact range-membership query.

    Explicit identity queries are exact: an unknown or terminal range makes the
    whole resolution fail closed. Automatic collections expose an assessment
    count and continuation so deployment growth never turns into a hard result
    ceiling. Collection queries may legitimately resolve to an empty set.
    """
    from engine.models import Range

    continuation, page_size = pagination.continuation, pagination.page_size
    supplied, explicit_values, filters = _query_shape(
        range_uuids=range_uuids,
        request_uuids=request_uuids,
        user_ids=user_ids,
        user_principal_uuids=user_principal_uuids,
        workspace_ids=workspace_ids,
        all_ranges=all_ranges,
    )
    _validate_supplied_values(supplied, explicit_values)
    _validate_page_shape(page_size=page_size, continuation=continuation, explicit_values=explicit_values)

    unavailable = (Range.Status.DESTROYING, Range.Status.DESTROYED, Range.Status.FAILED)
    with transaction.atomic():
        eligible = Range.objects.filter(filters).exclude(status__in=unavailable)
        assessment_count = eligible.count()
        if continuation is not None:
            eligible = eligible.filter(uuid__gt=continuation)
        page_rows = tuple(
            eligible.select_for_update(of=("self",)).select_related("request").order_by("uuid")[: page_size + 1]
        )
        if explicit_values is not None and assessment_count != len(explicit_values):
            raise SharingError(_RANGE_MEMBERSHIP_UNAVAILABLE)
        rows = page_rows[:page_size]
        items = tuple(
            ModelAccessRangeView(
                range_ref=model_access_range_reference(row.uuid),
                authority_ref=OwnedReference(owner="engine", reference=f"range:{row.uuid}"),
                range_uuid=row.uuid,
                owner_user_id=row.user_id,
                owner_principal_uuid=row.owner_principal_uuid,
                workspace_id=row.workspace_id,
                request_uuid=row.request.request_id if row.request is not None else None,
            )
            for row in rows
        )
        return ModelAccessRangePage(
            items=items,
            assessment_count=assessment_count,
            continuation=items[-1].range_uuid if len(page_rows) > page_size else None,
        )


def resolve_model_access_range_views(
    *,
    range_uuids: tuple[UUID, ...] | None = None,
    request_uuids: tuple[UUID, ...] | None = None,
    user_ids: tuple[int, ...] | None = None,
    user_principal_uuids: tuple[UUID, ...] | None = None,
    workspace_ids: tuple[int, ...] | None = None,
    all_ranges: bool = False,
) -> tuple[ModelAccessRangeView, ...]:
    """Resolve a complete collection through stable bounded keyset pages."""
    items: list[ModelAccessRangeView] = []
    continuation = None
    assessment_count = None
    while True:
        page = resolve_model_access_range_page(
            range_uuids=range_uuids,
            request_uuids=request_uuids,
            user_ids=user_ids,
            user_principal_uuids=user_principal_uuids,
            workspace_ids=workspace_ids,
            all_ranges=all_ranges,
            pagination=ModelAccessRangePagination(continuation=continuation),
        )
        if assessment_count is None:
            assessment_count = page.assessment_count
        elif page.assessment_count != assessment_count:
            raise SharingError(_RANGE_MEMBERSHIP_CHANGED)
        items.extend(page.items)
        if page.continuation is None:
            break
        if continuation == page.continuation:
            raise SharingError(_RANGE_MEMBERSHIP_CHANGED)
        continuation = page.continuation
    if len(items) != assessment_count:
        raise SharingError(_RANGE_MEMBERSHIP_CHANGED)
    return tuple(items)
