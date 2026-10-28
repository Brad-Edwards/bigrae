"""Resume an interrupted event initialization using its original authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Unpack
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from ctf.exceptions import CTFError
from ctf.models import CTFEvent
from ctf.services.event.initialization import abort_event_initialization, resume_event_initialization
from shared.model_access.sources import ModelSourceCredentialReference

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class _InitializationOptions(TypedDict):
    """Parsed declaration and optional recovery reference supplied by Django."""

    event_uuid: UUID
    credential_reference: str | None
    abort: bool


class Command(BaseCommand):
    """Resume retained event initialization under its original live authority."""

    help = "Resume a pending event initialization from its exact persisted intent."

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the event identity, recovery proof reference and abort flag."""
        parser.add_argument("event_uuid", type=UUID)
        parser.add_argument(
            "--credential-reference",
            help="Current non-secret credential reference as a JSON object; never a bearer token.",
        )
        parser.add_argument(
            "--abort", action="store_true", help="Abort a pending declaration instead of initializing it."
        )

    def handle(self, *args: object, **options: Unpack[_InitializationOptions]) -> None:
        """Resume or abort the retained intent under original or supplied current proof."""
        try:
            credential = (
                ModelSourceCredentialReference.model_validate_json(options["credential_reference"]).credential()
                if options["credential_reference"]
                else None
            )
            if options["abort"]:
                if credential is None:
                    raise CommandError("Abort requires a current credential reference.")
                abort_event_initialization(options["event_uuid"], credential)
            else:
                resume_event_initialization(options["event_uuid"], recovery_credential=credential)
        except (CTFError, CTFEvent.DoesNotExist, ValueError):
            raise CommandError("Event initialization is unavailable.") from None
        self.stdout.write("Event initialization aborted." if options["abort"] else "Event initialization confirmed.")
