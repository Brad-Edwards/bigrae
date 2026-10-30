"""Register the in-box catalog as the bootstrap administrator of the installation (#2679).

A GitHub-less install has no human who can paste a credential before the first
deploy ends. The identity cutover records the one bootstrap administrator from
the operator's bootstrap facts. This command acts only as that principal: it
issues a short-lived installation content credential for it, registers the
release's in-box packs through the ordinary content services, and revokes the
credential. Workload IAM grants nothing here; the content services check the
administrator's own policy for each step.
"""

from __future__ import annotations

from typing import cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from cms.services import register_inbox_catalog
from config.identity_cutover import load_identity_bootstrap_facts
from management.services import (
    bootstrap_principal_uuid,
    issue_installation_catalog_credential,
    revoke_installation_catalog_credential,
)
from shared.api_tokens.cli import CredentialFileError, credential_from_proof
from shared.exceptions import CMSError, ValidationError
from workspaces.services import get_identity_cutover_state

_INCOMPLETE = "Catalog initialization incomplete; check the bootstrap administrator and pending pack qualification"


class Command(BaseCommand):
    """Register the in-box seed as the recorded bootstrap administrator."""

    help = "Register the in-box catalog as the installation's bootstrap administrator."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require the bootstrap facts that name the administrator."""
        parser.add_argument("--bootstrap-facts", required=True, help="Private bootstrap facts file.")

    def handle(self, *args: object, **options: object) -> None:
        """Act only as the activated cutover's bootstrap administrator."""
        try:
            facts = load_identity_bootstrap_facts(cast(str, options["bootstrap_facts"]))
        except ValueError as exc:
            raise CommandError("bootstrap facts document is invalid") from exc
        state = get_identity_cutover_state()
        if state is None or state.state != "activated":
            raise CommandError("Catalog initialization requires the activated identity cutover")
        principal_uuid = bootstrap_principal_uuid(facts.issuer, facts.subject)
        if principal_uuid is None or principal_uuid != state.bootstrap_principal_uuid:
            raise CommandError("The bootstrap facts do not name the installation's bootstrap administrator")
        token, proof = issue_installation_catalog_credential(principal_uuid)
        try:
            registered = register_inbox_catalog(actor=credential_from_proof(proof))
        except (CredentialFileError, CMSError, ValidationError, DatabaseError, TypeError, ValueError) as exc:
            raise CommandError(_INCOMPLETE) from exc
        finally:
            revoke_installation_catalog_credential(token)
        self.stdout.write(self.style.SUCCESS(f"Registered {len(registered)} in-box pack(s)."))
