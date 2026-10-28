"""Resume persisted hierarchy publication after an interrupted creation request."""

from django.core.management.base import BaseCommand, CommandError

from shared.authorization import AuthorizationProviderBindingError, configured_authorization_provider
from workspaces.services import reconcile_hierarchy_publications


class Command(BaseCommand):
    help = "Resume a bounded batch of previously authorized hierarchy publication intents."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        try:
            count = reconcile_hierarchy_publications(configured_authorization_provider(), limit=options["limit"])
        except (AuthorizationProviderBindingError, ValueError):
            raise CommandError("Hierarchy publication is unavailable.") from None
        self.stdout.write(f"Confirmed {count} pending hierarchy publications.")
