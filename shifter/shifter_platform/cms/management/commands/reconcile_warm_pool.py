"""Warm-pool reconciler worker command (#28).

Drives :func:`cms.services.reconcile_warm_pool` on the managed-worker conventions
this repo already uses for ``reconcile_range_events``: runs one bounded page by
default or continuously with ``--loop`` and a bounded interval,
touching a liveness heartbeat each iteration. It performs no provider I/O in a
transaction -- provision and destroy travel through the durable RAES intents the
service enqueues. Disabled policy stops replenishment but retains cleanup.
Production uses ``--loop`` to retain the page cursor and cover all obligations.
"""

from __future__ import annotations

import logging
import tempfile
import time
from argparse import ArgumentParser
from pathlib import Path

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "worker-warm-pool-reconciler-heartbeat"


class Command(BaseCommand):
    """Converge the warm pool toward its declared policy each pass."""

    help = (
        "Reconcile the range warm pool (#28): finalize retiring generations, retire "
        "expired/incompatible/excess ones, and replenish shortfalls. Runs once by "
        "default for one bounded recovery page; use --loop for complete periodic recovery."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--loop",
            action="store_true",
            default=False,
            help="Run in a continuous loop instead of exiting after one pass.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=300,
            help="Seconds between loop iterations (only used with --loop). Default: 300.",
        )

    def handle(self, *args, **options) -> None:
        from cms.services import reconcile_warm_pool

        loop: bool = options["loop"]
        interval: int = options["interval"]
        logger.info("reconcile_warm_pool: starting (loop=%s interval=%d)", loop, interval)
        cleanup_cursor = 0
        while True:
            summary = reconcile_warm_pool(cleanup_cursor=cleanup_cursor)
            cleanup_cursor = summary["cleanup_cursor"]
            logger.info(
                "reconcile_warm_pool: pass complete buckets=%d provisioned=%d retired=%d finalized=%d",
                summary["buckets"],
                summary["provisioned"],
                summary["retired"],
                summary["finalized"],
            )
            if not loop:
                break
            self._touch_heartbeat()
            time.sleep(interval)

    def _touch_heartbeat(self) -> None:
        """Touch the liveness heartbeat file after each loop iteration."""
        try:
            HEARTBEAT_FILE.touch()
        except OSError:
            logger.warning("reconcile_warm_pool: could not touch heartbeat file")
