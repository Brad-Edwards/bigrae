"""Non-secret provisioner identity derived from the installed native task profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings

from shared.cloud import PROVISIONER_CONTAINER_NAME
from shared.cloud.kubernetes.naming import build_idempotent_job_name

from ._config import _get_engine_task_config

if TYPE_CHECKING:
    pass


def provisioner_workload(command: list[str], task_identity: str | None) -> dict[str, object]:
    """Project the installed native task identity without copying credentials."""
    config = _get_engine_task_config()
    if config is None or not task_identity:
        raise ValueError("Provisioner workload unavailable")
    namespace, image, _network = config
    return {
        "kind": "kubernetes",
        "provider": settings.CLOUD_PROVIDER,
        "namespace": namespace,
        "image": image,
        "task_identity": str(task_identity),
        "command": list(command),
        "container_name": PROVISIONER_CONTAINER_NAME,
        "service_account_name": str(getattr(settings, "ENGINE_TASK_SERVICE_ACCOUNT_NAME", "") or ""),
        "effective_service_account_name": str(getattr(settings, "ENGINE_TASK_SERVICE_ACCOUNT_NAME", "") or "default"),
        "job_name": build_idempotent_job_name(PROVISIONER_CONTAINER_NAME, str(task_identity)),
    }


def assert_provisioner_workload(
    expected: dict[str, object] | None, command: list[str], task_identity: str | None
) -> None:
    """Reject dispatch when the installed native workload profile has drifted."""
    if expected != provisioner_workload(command, task_identity):
        raise ValueError("Provisioner workload changed")
