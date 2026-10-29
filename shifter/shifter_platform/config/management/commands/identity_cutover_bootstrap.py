"""Grant the single first application administrator during the S8 cutover."""

from __future__ import annotations

from typing import cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from config.identity_bootstrap import bootstrap_grants, ensure_bootstrap_grant
from config.identity_cutover import load_identity_bootstrap_facts
from config.openfga_authorization import configured_authorization_provider
from management.services import (
    PrincipalConflictError,
    bootstrap_principal_uuid,
    prepare_empty_installation_bootstrap,
)
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log
from workspaces.services import (
    IdentityCutoverConflict,
    bind_identity_cutover_provider,
    get_identity_cutover_state,
    record_identity_cutover_bootstrap,
)


class Command(BaseCommand):
    """Bind one exact active provider subject to the first administrator grant."""

    help = "Grant the one-time first application administrator for identity cutover"

    def add_arguments(self, parser: CommandParser) -> None:
        """Require the bootstrap facts and either the accepted plan digest or preparation."""
        parser.add_argument("--facts", required=True)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--plan-digest")
        mode.add_argument(
            "--prepare",
            action="store_true",
            help="Before the plan, create the bootstrap principal on an empty installation",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Grant and record the one-time administrator from verified bootstrap facts."""
        try:
            facts = load_identity_bootstrap_facts(cast(str, options["facts"]))
        except ValueError as exc:
            raise CommandError("bootstrap facts document is invalid") from exc
        if options["prepare"]:
            self._prepare(facts.issuer, facts.subject)
            return
        state = get_identity_cutover_state(plan_digest=cast(str, options["plan_digest"]))
        if state is None:
            raise CommandError("cutover policy state is unavailable")
        if state.state != "policy_pending":
            raise CommandError("cutover policy state is not awaiting bootstrap")
        principal_uuid = bootstrap_principal_uuid(facts.issuer, facts.subject)
        if principal_uuid is None:
            raise CommandError("bootstrap provider identity is unavailable")
        principal_subject = f"principal:{principal_uuid}"
        provider = configured_authorization_provider()
        _assert_provider_binding(state.plan_digest, provider)
        try:
            current = provider.read_relationship_subjects("direct_administrator", "installation:root", limit=2)
        except Exception as exc:
            raise CommandError("bootstrap relationship lookup is unavailable") from exc
        if current and current != (principal_subject,):
            raise CommandError("authorization store already has an administrator")
        changes = bootstrap_grants(principal_uuid, state.plan_payload)
        for change in changes:
            ensure_bootstrap_grant(provider, change)
        with transaction.atomic():
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.AUTHORIZATION_OPERATION,
                    entity_id=1,
                    entity_ref="identity-cutover-bootstrap",
                    action=AuditAction.AUTHORIZATION_CONFIRMED,
                    actor_type="principal",
                    actor_principal_uuid=principal_uuid,
                    new_state={
                        "policy": "application_administrator",
                        "personal_token_eligibility": state.plan_payload.get(
                            "bootstrap_personal_token_eligibility", False
                        ),
                        "plan_digest": state.plan_digest,
                        "state": "verified",
                    },
                    context="identity_cutover",
                ),
                strict=True,
            )
            try:
                record_identity_cutover_bootstrap(state.plan_digest, principal_uuid)
            except IdentityCutoverConflict as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write('{"state":"bootstrap_verified"}')

    def _prepare(self, issuer: str, subject: str) -> None:
        """Bind the first administrator of an empty installation before the plan (#2671)."""
        if get_identity_cutover_state() is not None:
            raise CommandError("cutover has started; preparation runs only before the plan")
        try:
            principal_uuid = prepare_empty_installation_bootstrap(issuer, subject)
        except PrincipalConflictError as exc:
            raise CommandError("bootstrap provider identity is unavailable") from exc
        self.stdout.write('{"prepared":%s}' % ("true" if principal_uuid is not None else "false"))


def _assert_provider_binding(plan_digest: str, provider: object) -> None:
    """Pin bootstrap to the same provider selected by relationship replay."""
    store_id = str(getattr(provider, "store_id", ""))
    model_id = str(getattr(provider, "model_id", ""))
    if not store_id or not model_id:
        raise CommandError("authorization provider binding is invalid")
    try:
        bind_identity_cutover_provider(plan_digest, store_id, model_id)
    except IdentityCutoverConflict as exc:
        raise CommandError(str(exc)) from exc
