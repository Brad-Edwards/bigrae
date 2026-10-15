"""Pin the incumbent native preparation worker independently of its initiator."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import PreparationAttempt

from typing import Any
from uuid import UUID

from shared.cloud.preparation_runtime import preparation_task
from shared.preparation_grant import PreparationGrantConfiguration


def preparation_workload(
    input_data: dict[str, Any], phase: str, operation_id: UUID, attempt_id: UUID
) -> dict[str, Any]:
    """Reuse the installed role/profile and exact Job identity; no synthetic principal."""
    grant = PreparationGrantConfiguration.model_validate(input_data["grant"])
    if grant.digest != input_data["grant_digest"]:
        raise ValueError("Preparation execution profile changed")
    if phase == "cleanup":
        image = grant.cleanup_image
    else:
        image = input_data["adapter"]["worker_image" if phase == "build" else "verifier_image"]
    task = preparation_task(grant, phase, image, operation_id, attempt_id)
    return {"kind": "kubernetes", "namespace": grant.namespace, **task.expected_identity}


def verify_preparation_workload(attempt: PreparationAttempt) -> None:
    """Native attribution must agree with the immutable role, executable and grant."""
    if attempt.operation.authority_kind and attempt.executing_workload != preparation_workload(
        attempt.input, attempt.phase, attempt.operation_id, attempt.id
    ):
        raise ValueError("Preparation execution profile changed")
