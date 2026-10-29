"""Seed a synthetic CTF event and inbox messages for authenticated Playwright E2E.

DEV/TEST ONLY. Like ``seed_e2e``, this command refuses to run unless ``DEBUG`` is
true or ``ENVIRONMENT`` is ``development``/``test``.

Run it after ``seed_e2e``, which gives the E2E organizer its individual account
and native account administration. It seeds what ``dev_login`` cannot establish:
one event in that account, owned by the organizer, with its authorization
relationships published; a live participation and active event for the E2E
inbox participant; and two released communications (#2053). The first goes
through the real authoring and admission services. The second carries a stored
hostile body written below the admission validator, as a row admitted before a
validator change would be; the browser journey proves the renderer and the
enforced CSP keep it inert. Idempotent: safe to re-run.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

import workspaces.services as ws
from ctf.communication_contracts import CONTENT_PROFILE_V1, canonical_digest
from ctf.enums import EventStatus
from ctf.models import CommunicationCampaign, CTFEvent, CTFParticipant, MessageRevision
from ctf.services.communication import CampaignDraft, create_campaign, release_campaign
from ctf.services.event import publish_event_relationships
from management.services import set_active_ctf_event
from shared.auth import CTF_PARTICIPANT_GROUP
from shared.principal_port import principal_for_user

if TYPE_CHECKING:
    from django.contrib.auth.models import User as DjangoUser

User = get_user_model()

#: Stable synthetic identities shared with ``frontend/e2e/support/actors.ts``.
ORGANIZER_EMAIL = "e2e-organizer@example.com"
INBOX_PARTICIPANT_EMAIL = "e2e-inbox@example.com"
EVENT_NAME = "E2E Inbox Event"
SAFE_SUBJECT = "Welcome to the E2E event"
HOSTILE_SUBJECT = "Stored hostile content"
HOSTILE_BODY = (
    "Stored hostile content.\n\n"
    '<img src="x" onerror="window.__inboxXss = 1">\n\n'
    "<script>window.__inboxXss = 1</script>\n\n"
    "[run](javascript:window.__inboxXss=1)\n\n"
    "![remote](https://untrusted.example/x.png)\n"
)

_ALLOWED_ENVIRONMENTS = frozenset({"development", "test"})


def _seed_allowed() -> bool:
    """Return True only in a development/test posture (never production)."""
    return bool(settings.DEBUG) or getattr(settings, "ENVIRONMENT", "production") in _ALLOWED_ENVIRONMENTS


def _ensure_user(email: str) -> DjangoUser:
    """Get or create one synthetic user by its email username."""
    user, _ = User.objects.get_or_create(username=email, defaults={"email": email, "is_active": True})
    return user


class Command(BaseCommand):
    """Seed the synthetic CTF inbox fixture (dev/test only)."""

    help = "Seed a synthetic CTF event and inbox messages for Playwright E2E (dev/test only)."

    def handle(self, *args: object, **options: object) -> None:
        """Create the event, participation, and messages idempotently, refusing in production."""
        if not _seed_allowed():
            raise CommandError("seed_e2e_ctf is disabled outside development/test (refusing production posture).")

        organizer = _ensure_user(ORGANIZER_EMAIL)
        principal = principal_for_user(organizer)
        try:
            scope = ws.individual_account_scope(principal)
            binding = ws.resolve_resource_scope(scope)
        except ws.AccountScopeError:
            raise CommandError("Run seed_e2e first: the E2E organizer needs its individual account.") from None

        now = timezone.now()
        event, _ = CTFEvent.objects.get_or_create(
            name=EVENT_NAME,
            created_by=organizer,
            defaults={
                "creator_principal_uuid": principal.uuid,
                "owner_principal_uuid": principal.uuid,
                "scope_kind": "account",
                "account_id": binding.account_id,
                # As the event service does: publication writes the provider
                # relationships and confirms only after readback.
                "authorization_publication_state": "requested",
                "status": EventStatus.ACTIVE.value,
                "event_start": now - timedelta(hours=1),
                "event_end": now + timedelta(days=30),
                "scenario_id": "basic",
            },
        )
        publish_event_relationships(event.pk)
        account_uuid = str(scope.account_uuid)

        participant_user = _ensure_user(INBOX_PARTICIPANT_EMAIL)
        CTFParticipant.objects.get_or_create(
            event=event,
            user=participant_user,
            defaults={"email": INBOX_PARTICIPANT_EMAIL, "name": "E2E Inbox", "status": "active", "registered_at": now},
        )
        participant_user.groups.add(Group.objects.get_or_create(name=CTF_PARTICIPANT_GROUP)[0])
        set_active_ctf_event(participant_user, event.pk)

        safe = self._campaign(
            organizer, account_uuid, event, SAFE_SUBJECT, "Read the **rules** on the [help page](/ctf/help/)."
        )
        release_campaign(safe, occurrence_key="e2e-safe", actor_user_id=organizer.pk)

        hostile = self._campaign(organizer, account_uuid, event, HOSTILE_SUBJECT, "Placeholder.", policy="none")
        revision, _ = MessageRevision.objects.get_or_create(
            campaign=hostile,
            revision_number=2,
            defaults={
                "subject": HOSTILE_SUBJECT,
                "body": HOSTILE_BODY,
                "content_profile": CONTENT_PROFILE_V1,
                "content_digest": canonical_digest(
                    {"subject": HOSTILE_SUBJECT, "body": HOSTILE_BODY, "profile": CONTENT_PROFILE_V1}
                ),
                "actor_user_id": organizer.pk,
                "actor_principal_uuid": principal.uuid,
            },
        )
        release_campaign(hostile, occurrence_key="e2e-hostile", actor_user_id=organizer.pk, revision=revision)

        self.stdout.write(self.style.SUCCESS(f"Seeded E2E CTF inbox: {EVENT_NAME} for {INBOX_PARTICIPANT_EMAIL}."))

    @staticmethod
    def _campaign(
        organizer: DjangoUser, account_uuid: str, event: CTFEvent, subject: str, body: str, *, policy: str = "explicit"
    ) -> CommunicationCampaign:
        existing = CommunicationCampaign.objects.filter(created_by=organizer, title=subject).first()
        if existing is not None:
            return existing
        return create_campaign(
            organizer,
            account_uuid,
            CampaignDraft(
                title=subject,
                origin="organizer_staff",
                target_event_ids=[event.pk],
                audience_spec={"kind": "event", "event_ids": [str(event.pk)]},
                trigger_spec={"kind": "manual"},
                channels=["in_app"],
                subject=subject,
                body=body,
                acknowledgement_policy=policy,
            ),
            parent_type="account",
        )
