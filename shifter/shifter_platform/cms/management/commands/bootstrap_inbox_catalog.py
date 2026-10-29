"""Initialize installation content through authenticated ordinary CMS services."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from cms.exceptions import CMSError
from cms.scenarios.inbox import register_inbox_packs
from shared.api_tokens.cli import credential_from_file
from shared.exceptions import ValidationError


class Command(BaseCommand):
    """Register, qualify and publish the declared seed after native activation."""

    help = "Initialize the installation's in-box seed with an issued native credential."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--credential-file", required=True, help="Private file containing an issued bearer proof.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            actor = credential_from_file(options["credential_file"])
            registered = register_inbox_packs(actor=actor)
        except (CMSError, ValidationError, DatabaseError, TypeError, ValueError) as exc:
            raise CommandError(
                "Catalog initialization incomplete; check native authority and pending pack qualification"
            ) from exc
        self.stdout.write(self.style.SUCCESS(f"Registered {len(registered)} in-box pack(s)."))
