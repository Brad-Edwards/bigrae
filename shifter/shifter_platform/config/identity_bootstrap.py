"""Exact, plan-bound grants for the one-time preactivation administrator ceremony."""

from collections.abc import Mapping
from uuid import UUID

from django.core.management.base import CommandError

from shared.authorization import (
    AdministrativeRoleChange,
    AuthorizationProvider,
    AuthorizationRelationshipChange,
    PolicyEffect,
    PolicyRelationshipChange,
    RelationshipSubject,
    TargetRef,
)

PERSONAL_CREDENTIAL_ACTION = "installation.use_personal_tokens"
_BOOTSTRAP_UNRESOLVED = "bootstrap administrator grant is unresolved"


def bootstrap_grants(principal_uuid: UUID, plan: Mapping[str, object]) -> tuple[AuthorizationRelationshipChange, ...]:
    """Select only the initial role and the plan's explicit token-eligibility choice."""
    eligible = plan.get("bootstrap_personal_token_eligibility", False)
    if not isinstance(eligible, bool):
        raise CommandError("bootstrap eligibility plan is invalid")
    subject = RelationshipSubject("principal", principal_uuid)
    target = TargetRef("installation")
    administrator = AdministrativeRoleChange(subject, "application_administrator", target, PolicyEffect.GRANT)
    grants: list[AuthorizationRelationshipChange] = [administrator]
    if eligible:
        grants.append(PolicyRelationshipChange(subject, PERSONAL_CREDENTIAL_ACTION, target, PolicyEffect.GRANT))
    return tuple(grants)


def ensure_bootstrap_grant(provider: AuthorizationProvider, change: AuthorizationRelationshipChange) -> None:
    """Read before writing, preserve explicit denials, and recover a lost write acknowledgement."""
    try:
        before = provider.read_relationships(change)
    except Exception as exc:
        raise CommandError(_BOOTSTRAP_UNRESOLVED) from exc
    if before.deny_present:
        raise CommandError(_BOOTSTRAP_UNRESOLVED)
    write_error: Exception | None = None
    if not before.grant_present:
        try:
            provider.write_relationships(change)
        except Exception as exc:  # noqa: BLE001  # recover uncertain writes by reading the exact grant
            write_error = exc
    try:
        after = provider.read_relationships(change)
    except Exception as exc:
        raise CommandError(_BOOTSTRAP_UNRESOLVED) from exc
    if not after.grant_present or after.deny_present:
        raise CommandError(_BOOTSTRAP_UNRESOLVED) from write_error


def verify_bootstrap_grants(provider: AuthorizationProvider, principal_uuid: UUID, plan: Mapping[str, object]) -> None:
    """Require current provider evidence for every plan-bound grant before activation."""
    for change in bootstrap_grants(principal_uuid, plan):
        try:
            state = provider.read_relationships(change)
        except Exception as exc:
            raise CommandError(_BOOTSTRAP_UNRESOLVED) from exc
        if not state.grant_present or state.deny_present:
            raise CommandError(_BOOTSTRAP_UNRESOLVED)
