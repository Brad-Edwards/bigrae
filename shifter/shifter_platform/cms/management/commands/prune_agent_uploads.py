"""Reconcile expiry cleanup for native scoped upload admissions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.core.management.base import CommandParser

from django.core.management.base import BaseCommand

from cms.services._scoped_uploads import reconcile_upload_cleanup


class Command(BaseCommand):
    """Remove expired upload work through the owning cleanup service."""

    help = "Clean expired scoped agent uploads while preserving installed assets"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options) -> None:
        cleaned = reconcile_upload_cleanup(limit=options["limit"])
        self.stdout.write(f"Cleaned {cleaned} upload admissions")
