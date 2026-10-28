"""CTF decoupling bridge: fire range_status_changed signal for CTF receivers."""

from __future__ import annotations

import logging

from django.db import transaction

from cms.signals import range_status_changed

logger = logging.getLogger(__name__)


def notify_ctf_range_status(
    range_instance_id: int,
    new_status: str,
    previous_status: str,
    cleanup_verified: bool = False,
) -> None:
    """Fire the CMS range_status_changed signal.

    Any layer that depends on CMS (e.g. CTF) can register receivers
    for this signal to react to range status changes.

    ``cleanup_verified`` (#2086, ADR-063-R4/R5) reports whether durable scoped
    provider inventory/readback evidence confirms every owned resource is gone. A
    terminal ``DESTROYED`` status without that evidence is not proof of absence, so
    CTF receivers must retain capacity and linkage until it is True.

    The receivers run in a savepoint inside the caller's status-write
    transaction. A receiver failure rolls back only the receivers' writes and is
    logged, so CTF can neither block the CMS status nor leave the transaction
    unusable. The failed CTF side effect is not retried here; #2577 tracks a
    durable handoff for it.
    """
    try:
        with transaction.atomic():
            range_status_changed.send(
                sender=None,
                range_instance_id=range_instance_id,
                new_status=new_status,
                previous_status=previous_status,
                cleanup_verified=cleanup_verified,
            )
    except Exception:
        logger.exception(
            "CTF range status receiver failed; the CMS status write stands: range_instance_id=%s status=%s",
            range_instance_id,
            new_status,
        )
