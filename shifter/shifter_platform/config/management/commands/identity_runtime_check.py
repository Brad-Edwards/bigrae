"""Check that an effect-producing application process may start."""

from django.core.management.base import BaseCommand, CommandError

from config.identity_runtime import IdentityRuntimeNotReady, require_identity_runtime_ready


class Command(BaseCommand):
    """Fail unless identity cutover and the pinned provider binding are active."""

    help = "Verify terminal identity cutover state before starting application runtime"

    def handle(self, *args: object, **options: object) -> None:
        """Refuse runtime startup until the retained identity cutover is ready."""
        try:
            require_identity_runtime_ready()
        except IdentityRuntimeNotReady as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write('{"identity_runtime":"ready"}')
