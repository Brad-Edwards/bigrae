"""Run actual contract checks for a post-setup private pack installation."""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cms.services import validate_registered_pack_conformance
from shared.api_tokens.cli import credential_from_file
from shared.exceptions import ValidationError


class Command(BaseCommand):
    """Provide Command."""

    help = "Validate immutable registered pack bytes and the supported backend contract."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--credential-file", required=True)
        parser.add_argument("--scenario-id", required=True)
        parser.add_argument("--expected-package-digest", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            user = credential_from_file(options["credential_file"])
            status = validate_registered_pack_conformance(
                user=user,
                scenario_id=options["scenario_id"],
                expected_package_digest=options["expected_package_digest"],
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise CommandError("Registered pack conformance validation failed") from exc
        self.stdout.write(status)
