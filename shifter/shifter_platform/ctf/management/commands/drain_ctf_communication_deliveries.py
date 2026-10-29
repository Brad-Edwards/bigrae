"""Supervised delivery worker for scoped communications (ADR-051-R12, #2098).

Claims and processes the durable ``DeliveryAttempt`` commands that #2048's admission
committed, over the available channel adapters (in-app, and email when a
delivering backend is configured; ADR-071). Runs
as a long-lived, deployment-supervised process -- the same model as
``run_ctf_scheduler`` / the engine drain workers -- so a web-worker crash can never
take it down and many replicas can run safely (``select_for_update(skip_locked)``).

Usage::

    python manage.py drain_ctf_communication_deliveries            # one bounded batch
    python manage.py drain_ctf_communication_deliveries --loop --interval 10

Health monitoring: touches a heartbeat file after each cycle. Graceful shutdown:
SIGTERM/SIGINT finish only the attempt in flight, release the other claimed but
unattempted commands, and stop claiming new work. The in-flight attempt is bounded
by the transport timeout, well inside the default termination grace period.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import tempfile
import time
from argparse import ArgumentParser
from pathlib import Path
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand

from ctf.services.communication.delivery import run_once

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "ctf-communication-worker-heartbeat"


class Command(BaseCommand):
    """Drain due scoped-communication delivery commands over registered adapters."""

    help = "Deliver due scoped-communication commands over the available channel adapters."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._shutdown = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register loop/interval options (batch/lease/retry policy is settings-owned)."""
        parser.add_argument("--loop", action="store_true", default=False, help="Run continuously.")
        parser.add_argument("--interval", type=int, default=10, help="Seconds between loop cycles (default 10).")

    def handle(self, *args: Any, **options: Any) -> None:
        """Drain once, or continuously until a graceful shutdown is requested."""
        signal.signal(signal.SIGTERM, self._request_shutdown)
        signal.signal(signal.SIGINT, self._request_shutdown)
        loop: bool = options["loop"]
        interval: int = max(int(options["interval"]), 1)

        logger.info("drain_ctf_communication_deliveries: starting loop=%s", loop)
        while True:
            # Refresh liveness after every attempt so a long serial batch cannot
            # trip the two-minute liveness probe mid-batch (#2098 review).
            stats = run_once(heartbeat=self._touch_heartbeat, should_stop=self._stopping)
            self._touch_heartbeat()
            self.stdout.write(
                f"claimed={stats.claimed} accepted={stats.accepted} retried={stats.retried} "
                f"failed={stats.failed} expired={stats.expired} suppressed={stats.suppressed} stale={stats.stale} "
                f"reclaimed={stats.reclaimed} released={stats.released}"
            )
            if not loop or self._shutdown:
                break
            self._interruptible_sleep(interval)
        self._cleanup_heartbeat()

    def _request_shutdown(self, _signum: int, _frame: FrameType | None) -> None:
        """Request a graceful stop after the attempt in flight completes."""
        self._shutdown = True

    def _stopping(self) -> bool:
        """Return True once a graceful stop was requested."""
        return self._shutdown

    def _interruptible_sleep(self, interval: int) -> None:
        """Sleep up to ``interval`` seconds, waking promptly on a shutdown request."""
        for _ in range(interval):
            if self._shutdown:
                return
            time.sleep(1)

    def _touch_heartbeat(self) -> None:
        with contextlib.suppress(OSError):
            HEARTBEAT_FILE.touch()

    def _cleanup_heartbeat(self) -> None:
        with contextlib.suppress(OSError):
            HEARTBEAT_FILE.unlink(missing_ok=True)
