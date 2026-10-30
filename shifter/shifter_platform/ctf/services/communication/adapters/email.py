"""Email channel adapter (ADR-051-R14, ADR-071, #1525).

The adapter is the only code that decrypts a recipient coordinate. It reads the
one snapshot named by its command and renders the pinned revision through the
trusted template pair that the campaign's notice kind selects (#580). The pair
receives escaped scalar values only: the authored Markdown source, and the
participant name and event facts of the snapshot's own event. It then makes one
bounded attempt through ``shared.email``. The command's stable idempotency key becomes the
message's Message-ID, so every retry of one command shares it.

It never retries, never decides authorization or audience, and never retargets:
the captured coordinate is used, not a fresh participant or account address. A
missing snapshot or an erased coordinate is terminal for its command only; an
unreadable coordinate (missing or rotated field key) is a retriable dependency
fault within the worker's budget.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from django.utils import timezone

from ctf.enums_communication import CommunicationChannel, NoticeKind
from ctf.models import CommunicationIntent, RecipientSnapshot
from shared.email import EmailOutcome, attempt_email, delivery_available, render_template
from shared.site_url import SiteUrlUnavailable, validated_site_url

from .contract import DeliveryCommand, DeliveryOutcome, OutcomeClass

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = "ctf/email/notice"
_OUTCOMES = {
    EmailOutcome.ACCEPTED: OutcomeClass.ACCEPTED,
    EmailOutcome.RETRIABLE: OutcomeClass.RETRIABLE,
    EmailOutcome.TERMINAL: OutcomeClass.TERMINAL,
    # The backend became unavailable after the claim: keep the command retriable.
    EmailOutcome.UNAVAILABLE: OutcomeClass.RETRIABLE,
}


class EmailAdapter:
    """One bounded email attempt per claimed command."""

    channel = CommunicationChannel.EMAIL.value

    @staticmethod
    def available() -> bool:
        """Email is available only with a delivering backend in this deployment."""
        return delivery_available()

    @staticmethod
    def deliver(command: DeliveryCommand, *, timeout: float) -> DeliveryOutcome:
        """Send the pinned revision to the snapshot's captured coordinate."""
        coordinate, failure = _read_coordinate(command)
        if failure is not None:
            return failure
        intent = CommunicationIntent.objects.select_related("revision", "campaign").get(pk=command.intent_id)
        kind = NoticeKind(intent.campaign.notice_kind)
        try:
            event_url = validated_site_url() + _event_path(kind, command)
        except SiteUrlUnavailable:
            logger.warning("email portal origin unavailable for attempt %s", command.attempt_id)
            return DeliveryOutcome(OutcomeClass.RETRIABLE, reason="site_url_unavailable")
        revision = intent.revision
        subject = " ".join(revision.subject.splitlines())
        context = {
            "subject": subject,
            "body": revision.body,
            "locale": revision.default_locale,
            "event_url": event_url,
            **_recipient_facts(command),
        }
        template = f"{_TEMPLATE_DIR}/{kind.value}"
        html, text = render_template(template, context)
        result = attempt_email(coordinate, subject, html, text, timeout=timeout, message_key=command.idempotency_key)
        return DeliveryOutcome(_OUTCOMES[result.outcome], reason=result.reason, provider_receipt=result.receipt)


def _event_path(kind: NoticeKind, command: DeliveryCommand) -> str:
    """Link organizer alerts to the event's organizer Alerts tab, and every other notice to the participant event."""
    if kind is NoticeKind.ORGANIZER_ALERT:
        return reverse("ctf:admin_event_alerts", kwargs={"event_id": command.event_id})
    return reverse("ctf:participant_event")


def _recipient_facts(command: DeliveryCommand) -> dict[str, str]:
    """Return the scalar facts of the snapshot's own event and live participant.

    A multi-event campaign renders each recipient with the event that qualifies
    that recipient. A removed participant renders without a name.
    """
    facts = (
        RecipientSnapshot.objects.filter(pk=command.snapshot_id, intent_id=command.intent_id)
        .values("event__name", "event__event_start", "event__event_end", "event__event_timezone", "participant__name")
        .get()
    )
    try:
        zone = ZoneInfo(facts["event__event_timezone"] or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return {
        "participant_name": facts["participant__name"] or "",
        "event_name": facts["event__name"],
        "event_start": _local_time(facts["event__event_start"], zone),
        "event_end": _local_time(facts["event__event_end"], zone),
        "event_timezone": zone.key,
    }


def _local_time(value: datetime | None, zone: ZoneInfo) -> str:
    """Format an instant in the event timezone, or return an empty string."""
    return timezone.localtime(value, zone).strftime("%Y-%m-%d %H:%M") if value else ""


def _read_coordinate(command: DeliveryCommand) -> tuple[str, DeliveryOutcome | None]:
    """Decrypt the one coordinate bound to ``command``, or return why it cannot be used."""
    bound = RecipientSnapshot.objects.filter(
        pk=command.snapshot_id, intent_id=command.intent_id, event_id=command.event_id
    )
    try:
        # The encrypted field decrypts while the row loads.
        coordinates = list(bound.values_list("delivery_coordinate", flat=True))
    # Field decryption failed: no key (ImproperlyConfigured), or a rotated key or
    # corrupt token (ValueError).
    except (ValueError, ImproperlyConfigured):
        logger.warning("email coordinate unreadable for snapshot %s", command.snapshot_id)
        return "", DeliveryOutcome(OutcomeClass.RETRIABLE, reason="coordinate_unreadable")
    coordinate = coordinates[0] if coordinates else ""
    failure = None
    if not coordinates:
        failure = DeliveryOutcome(OutcomeClass.TERMINAL, reason="snapshot_unavailable")
    elif not coordinate:
        failure = DeliveryOutcome(OutcomeClass.TERMINAL, reason="coordinate_unavailable")
    return coordinate, failure
