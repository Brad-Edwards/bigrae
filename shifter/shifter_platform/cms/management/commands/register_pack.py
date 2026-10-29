"""Operator CLI to register a content pack (#1578, ADR-034).

A thin entrypoint onto :func:`cms.services.register_pack`. The service owns
authorization (WHO may register, not entitlement), foreign-input pack validation,
no-shadow / duplicate rejection, and audit; this command only parses arguments,
resolves the actor, and maps domain failures to a ``CommandError`` (never a
traceback).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError, CommandParser

from cms.exceptions import CMSError
from cms.services import PackRegistrationRequest, register_pack
from shared.api_tokens.cli import credential_from_file
from shared.identity_scope import ResourceScope
from workspaces.services import AccountScopeError, hierarchy_target_scope


class Command(BaseCommand):
    """Register a content pack through the uniform ingestion service."""

    help = "Register a content pack through the uniform, entitlement-blind ingestion service (#1578)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the source-agnostic registration arguments."""
        parser.add_argument("--scenario-id", required=True, help="Catalog id for the pack.")
        parser.add_argument("--source-kind", default="repo", help="repo | object (default: repo).")
        parser.add_argument("--contract-kind", default="raes", help="Package contract kind (default: raes).")
        parser.add_argument("--contract-profile", default="shifter", help="Contract profile (default: shifter).")
        parser.add_argument("--package-ref", required=True, help="Pack root path/key.")
        parser.add_argument("--package-version", required=True, help="Immutable package version/ref.")
        parser.add_argument("--package-digest", required=True, help="Package digest 'sha256:<64 hex>'.")
        parser.add_argument(
            "--expected-package-digest", default="", help="Current digest required for an explicit new version."
        )
        parser.add_argument("--lock-ref", default="", help="Lock artifact path/key.")
        parser.add_argument("--lock-digest", default="", help="Lock artifact digest.")
        parser.add_argument("--provenance", default="", help="JSON object of bounded provenance references.")
        parser.add_argument("--credential-file", required=True, help="Private file containing an issued bearer proof.")
        parser.add_argument(
            "--scope-kind", required=True, choices=("installation", "account", "organization", "workspace")
        )
        for name in ("account", "organization", "workspace"):
            parser.add_argument(f"--{name}", type=UUID)

    def handle(self, *args: Any, **options: Any) -> None:
        """Resolve the actor and register the pack, surfacing failures cleanly."""
        try:
            actor = credential_from_file(options["credential_file"])
            scope = self._scope(options)
        except (ValueError, AccountScopeError):
            raise CommandError("Native registration credential or scope unavailable") from None
        request = PackRegistrationRequest(
            scenario_id=options["scenario_id"],
            source_kind=options["source_kind"],
            contract_kind=options["contract_kind"],
            contract_profile=options["contract_profile"],
            package_ref=options["package_ref"],
            package_version=options["package_version"],
            package_digest=options["package_digest"],
            expected_package_digest=options["expected_package_digest"],
            lock_ref=options["lock_ref"],
            lock_digest=options["lock_digest"],
            provenance=self._parse_provenance(options["provenance"]),
        )
        try:
            result = register_pack(user=actor, request=request, scope=scope)
        except (CMSError, TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Registered pack '{result.scenario_id}' "
                f"({result.source_kind}, conformance={result.conformance_status})"
            )
        )

    @staticmethod
    def _scope(options: dict[str, Any]) -> ResourceScope:
        """Select one explicit parent; owning SQL resolution validates its ancestry."""
        kind = options["scope_kind"]
        selected = {name: options.get(name) for name in ("account", "organization", "workspace") if options.get(name)}
        if kind == "installation" and not selected:
            return ResourceScope("installation")
        if kind not in selected or set(selected) != {kind}:
            raise ValueError("Invalid explicit content scope")
        identity = selected[kind]
        if not isinstance(identity, UUID):
            raise ValueError("Invalid explicit content scope")
        return hierarchy_target_scope(kind, identity)

    def _parse_provenance(self, raw: str) -> dict[str, Any]:
        """Parse the optional ``--provenance`` JSON object argument."""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError("--provenance must be valid JSON") from exc
        if not isinstance(data, dict):
            raise CommandError("--provenance must be a JSON object")
        return data
