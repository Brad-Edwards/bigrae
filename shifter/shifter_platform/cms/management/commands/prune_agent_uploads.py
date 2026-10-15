"""Reconcile expiry cleanup for native scoped upload admissions."""

from django.core.management.base import BaseCommand

from cms.services._scoped_uploads import reconcile_upload_cleanup


class Command(BaseCommand):
    help = "Clean expired scoped agent uploads while preserving installed assets"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        cleaned = reconcile_upload_cleanup(limit=options["limit"])
        self.stdout.write(f"Cleaned {cleaned} upload admissions")
