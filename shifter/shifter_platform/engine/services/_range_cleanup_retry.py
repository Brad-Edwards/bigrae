"""Retry unverified native teardown through the incumbent destroy workflow."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from django.db import transaction


def retry_range_cleanup_verification(request_id: UUID, *, now: datetime) -> bool:
    """Queue a new exact-custody destroy only after a terminal unknown outcome.

    Attempts retain their immutable outbox/input/results. Delay starts at result
    application, doubles from 60 seconds, and caps at one hour. The obligation
    never expires merely because inventory is unavailable.
    """
    from engine.models import OperationResultInbox, ProvisionerLaunchIntent, Range
    from shared.operation_results import ResultStep

    from ._cleanup_verification import is_range_generation_cleanup_verified_absent
    from ._principal_lifecycle import _commit_destroy, prepare_system_range_cleanup

    with transaction.atomic():
        row = (
            Range.objects.select_for_update(of=("self",))
            .select_related("request")
            .filter(request__request_id=request_id, status="destroyed", owner_principal_uuid__isnull=False)
            .first()
        )
        if (
            row is None
            or row.cms_range_uuid is None
            or row.resource_generation is None
            or row.provisioner_operation_id is None
        ):
            return False
        intent = (
            ProvisionerLaunchIntent.objects.select_for_update()
            .filter(operation_id=row.provisioner_operation_id)
            .first()
        )
        if intent is None or intent.payload.get("operation") != "destroy":
            return False
        terminal = (
            OperationResultInbox.objects.filter(
                operation_id=intent.operation_id,
                request_id=request_id,
                operation="destroy",
                result_step=ResultStep.RAES_TERMINAL_DESTROYED,
                disposition="APPLIED",
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if terminal is None or is_range_generation_cleanup_verified_absent(request_id, row.cms_range_uuid, row.pk):
            return False
        attempts = ProvisionerLaunchIntent.objects.filter(
            authority_resource_uuid=row.cms_range_uuid,
            authority_binding_digest=intent.authority_binding_digest,
            payload__operation="destroy",
        ).count()
        delay = min(60 * 2 ** min(max(attempts - 1, 0), 6), 3600)
        if (terminal.applied_at or terminal.created_at) + timedelta(seconds=delay) > now:
            return False
        return _commit_destroy(prepare_system_range_cleanup(request_id), _verification_retry=True)
