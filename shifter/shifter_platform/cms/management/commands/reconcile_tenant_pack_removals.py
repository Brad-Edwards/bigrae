"""Tenant pack removal cleanup worker command (ADR-034-R13, #2520).

Drives :func:`cms.services.reconcile_tenant_pack_removals`: deletes the captured
archive objects of pending removals and records completion. Runs once by default
or continuously with ``--loop``, touching a liveness heartbeat each iteration.
It must run under the identity that holds delete authority on the pack bucket.

A backfilled legacy archive that is not found at its candidate location keeps
its removal pending (``legacy_location_unverified``). An operator then either
relocates it (``--relocate-archive ID --bucket B --object-key K``) or confirms it
is absent everywhere (``--confirm-archive-absent ID``) before the pass runs.
"""

from __future__ import annotations

import logging
import signal
import tempfile
import threading
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "worker-pack-removal-reconciler-heartbeat"


class Command(BaseCommand):
    """Retry pending tenant pack archive cleanup until every object is absent."""

    help = "Reconcile pending tenant pack removals. Runs once by default; use --loop for persistent polling."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--loop", action="store_true", help="Run continuously instead of one pass.")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between passes with --loop.")
        parser.add_argument("--batch-size", type=int, default=20, help="Removals attempted per pass.")
        reconcile = parser.add_mutually_exclusive_group()
        reconcile.add_argument(
            "--relocate-archive",
            type=int,
            metavar="ARCHIVE_ID",
            help="Point a pending legacy archive at the bucket and key where it was uploaded.",
        )
        reconcile.add_argument(
            "--confirm-archive-absent",
            type=int,
            metavar="ARCHIVE_ID",
            help="Record that a pending legacy archive is verified absent in every location.",
        )
        parser.add_argument("--bucket", default="", help="Bucket for --relocate-archive.")
        parser.add_argument("--object-key", default="", help="Object key for --relocate-archive.")

    def handle(self, *args: Any, **options: Any) -> None:
        from cms.services import (
            confirm_tenant_pack_archive_absent,
            reconcile_tenant_pack_removals,
            relocate_tenant_pack_archive,
        )
        from cms.services._scoped_uploads import reconcile_upload_cleanup

        if not 1 <= options["interval"] <= 3600 or not 1 <= options["batch_size"] <= 100:
            raise CommandError("Invalid tenant pack removal reconciler bounds")
        try:
            if options["relocate_archive"] is not None:
                relocate_tenant_pack_archive(options["relocate_archive"], options["bucket"], options["object_key"])
            elif options["confirm_archive_absent"] is not None:
                confirm_tenant_pack_archive_absent(options["confirm_archive_absent"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        stopped = threading.Event()
        if options["loop"]:
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
        while not stopped.is_set():
            HEARTBEAT_FILE.touch()
            failed = False
            try:
                completed = reconcile_tenant_pack_removals(limit=options["batch_size"])
            except Exception as exc:
                completed, failed = 0, True
                logger.error("Pack cleanup pass failed error_type=%s", type(exc).__name__)
            HEARTBEAT_FILE.touch()
            try:
                uploads = reconcile_upload_cleanup(limit=options["batch_size"])
            except Exception as exc:
                uploads, failed = 0, True
                logger.error("Installer cleanup pass failed error_type=%s", type(exc).__name__)
            HEARTBEAT_FILE.touch()
            logger.info("CMS cleanup pass complete packs=%d uploads=%d", completed, uploads)
            if not options["loop"]:
                if failed:
                    raise CommandError("A CMS cleanup pass failed; retained cleanup will retry")
                return
            stopped.wait(options["interval"])
