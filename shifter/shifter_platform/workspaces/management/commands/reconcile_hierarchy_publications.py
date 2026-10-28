"""Resume persisted hierarchy publication after an interrupted creation request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from shared.authorization import AuthorizationProviderBindingError, configured_authorization_provider
from workspaces.services import reconcile_hierarchy_publications

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    """Retry retained SQL hierarchy publication through the configured provider."""

    help = "Resume a bounded batch of previously authorized hierarchy publication intents."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options) -> None:
        try:
            count = reconcile_hierarchy_publications(configured_authorization_provider(), limit=options["limit"])
        except (AuthorizationProviderBindingError, ValueError):
            raise CommandError("Hierarchy publication is unavailable.") from None
        self.stdout.write(f"Confirmed {count} pending hierarchy publications.")
