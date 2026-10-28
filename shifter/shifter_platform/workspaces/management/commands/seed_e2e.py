"""Seed deterministic synthetic actors for authenticated Playwright E2E.

DEV/TEST ONLY. This command refuses to run in a production posture, mirroring
the ``config.dev_auth`` gate: it is allowed only when ``DEBUG`` is true or
``ENVIRONMENT`` is ``development``/``test``. It never runs in production.

The browser suite uses native principals, account hierarchy and explicit policy
relationships against the same released OpenFGA server as other integration
tests. Django flags and groups remain presentation fixtures only. Re-running
this command repairs only these named synthetic fixtures in a dev/test stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from shared.auth import THREAT_RESEARCH_GROUP
from shared.authorization import (
    AdministrativeRoleChange,
    PolicyEffect,
    PolicyRelationshipChange,
    RelationshipSubject,
    TargetRef,
    configured_authorization_provider,
)
from shared.principal_port import principal_for_user
from workspaces.models import Account
from workspaces.services import create_account, reconcile_hierarchy_publications

if TYPE_CHECKING:
    from shared.authorization import AuthorizationProvider, AuthorizationRelationshipChange

if TYPE_CHECKING:
    from django.contrib.auth.models import User as DjangoUser

User = get_user_model()

#: Stable synthetic-actor identities the Playwright suite logs in as.
STAFF_EMAIL = "e2e-staff@example.com"
THREAT_EMAIL = "e2e-threat@example.com"
STANDARD_EMAIL = "e2e-standard@example.com"
ORGANIZER_EMAIL = "e2e-organizer@example.com"
PARTICIPANT_EMAIL = "e2e-participant@example.com"
BUSINESS_ACCOUNT_NAME = "E2E Team"

#: Non-production environments in which seeding is permitted.
_ALLOWED_ENVIRONMENTS = frozenset({"development", "test"})


def _seed_allowed() -> bool:
    """Return True only in a development/test posture (never production)."""
    return bool(settings.DEBUG) or getattr(settings, "ENVIRONMENT", "production") in _ALLOWED_ENVIRONMENTS


class Command(BaseCommand):
    """Seed synthetic E2E actors and preconditions (dev/test only)."""

    help = "Seed deterministic synthetic actors for authenticated Playwright E2E (dev/test only)."

    def handle(self, *args: object, **options: object) -> None:
        """Create the synthetic actors idempotently, refusing in production."""
        if not _seed_allowed():
            raise CommandError("seed_e2e is disabled outside development/test (refusing production posture).")

        staff = self._ensure_user(STAFF_EMAIL, is_staff=True)
        standard = self._ensure_user(STANDARD_EMAIL)

        threat = self._ensure_user(THREAT_EMAIL)
        group, _ = Group.objects.get_or_create(name=THREAT_RESEARCH_GROUP)
        threat.groups.add(group)
        organizer = self._ensure_user(ORGANIZER_EMAIL)
        participant = self._ensure_user(PARTICIPANT_EMAIL)
        provider = configured_authorization_provider()
        business = Account.objects.filter(kind="team", name=BUSINESS_ACCOUNT_NAME).first()
        if business is None:
            business = Account.objects.get(uuid=create_account(kind="team", name=BUSINESS_ACCOUNT_NAME).uuid)
        self._grant(
            provider,
            AdministrativeRoleChange(
                RelationshipSubject("principal", principal_for_user(staff).uuid),
                "account_administrator",
                TargetRef("account", business.uuid),
                PolicyEffect.GRANT,
            ),
        )
        self._grant(
            provider,
            PolicyRelationshipChange(
                RelationshipSubject("principal", principal_for_user(staff).uuid),
                "installation.read_audit",
                TargetRef("installation"),
                PolicyEffect.GRANT,
            ),
        )
        for user in (standard, threat, organizer, participant):
            principal = principal_for_user(user)
            personal = Account.objects.filter(kind="individual", individual_principal_uuid=principal.uuid).first()
            if personal is None:
                personal = Account.objects.get(
                    uuid=create_account(kind="individual", name="Personal account", owner=principal).uuid
                )
            self._grant(
                provider,
                AdministrativeRoleChange(
                    RelationshipSubject("principal", principal.uuid),
                    "account_administrator",
                    TargetRef("account", personal.uuid),
                    PolicyEffect.GRANT,
                ),
            )
        while reconcile_hierarchy_publications(provider, limit=100):
            # Each call publishes one bounded batch; continue until the queue is empty.
            continue

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded E2E actors: "
                f"{STAFF_EMAIL} (staff + native team administration), "
                f"{THREAT_EMAIL} (threat-research), "
                f"{STANDARD_EMAIL} (standard)."
            )
        )

    @staticmethod
    def _grant(provider: AuthorizationProvider, change: AuthorizationRelationshipChange) -> None:
        provider.write_relationships(change)
        state = provider.read_relationships(change)
        if not state.grant_present or state.deny_present:
            raise CommandError("Synthetic policy grant was not confirmed")

    @staticmethod
    def _ensure_user(email: str, *, is_staff: bool = False) -> DjangoUser:
        """Get-or-create a synthetic user, elevating (never downgrading) flags."""
        user, created = User.objects.get_or_create(
            username=email,
            defaults={"email": email, "is_active": True, "is_staff": is_staff},
        )
        if created:
            return user
        fields: list[str] = []
        if not user.is_active:
            user.is_active = True
            fields.append("is_active")
        if is_staff and not user.is_staff:
            user.is_staff = True
            fields.append("is_staff")
        if fields:
            user.save(update_fields=fields)
        return user
