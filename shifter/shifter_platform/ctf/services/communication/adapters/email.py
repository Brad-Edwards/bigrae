"""Email channel adapter (ADR-051-R14, ADR-071, #1525).

The adapter is the only code that decrypts a recipient coordinate. It reads the
one snapshot named by its command, renders the pinned revision through a trusted
HTML wrapper that escapes the authored Markdown source (the plain-text part
carries the source unchanged), and makes one bounded attempt
through ``shared.email``. The command's stable idempotency key becomes the
message's Message-ID, so every retry of one command shares it.

It never retries, never decides authorization or audience, and never retargets:
the captured coordinate is used, not a fresh participant or account address. A
missing snapshot or an erased coordinate is terminal for its command only; an
unreadable coordinate (missing or rotated field key) is a retriable dependency
fault within the worker's budget.
"""

from __future__ import annotations

import logging

from django.template.loader import render_to_string

from ctf.enums_communication import CommunicationChannel
from ctf.models import CommunicationIntent, RecipientSnapshot
from shared.email import EmailOutcome, attempt_email, delivery_available

from .contract import DeliveryCommand, DeliveryOutcome, OutcomeClass

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = "ctf/email/communication.html"
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

    def available(self) -> bool:
        """Email is available only with a delivering backend in this deployment."""
        return delivery_available()

    def deliver(self, command: DeliveryCommand, *, timeout: float) -> DeliveryOutcome:
        """Send the pinned revision to the snapshot's captured coordinate."""
        coordinate, failure = _read_coordinate(command)
        if failure is not None:
            return failure
        revision = CommunicationIntent.objects.select_related("revision").get(pk=command.intent_id).revision
        subject = " ".join(revision.subject.splitlines())
        html = render_to_string(
            _HTML_TEMPLATE, {"subject": subject, "body": revision.body, "locale": revision.default_locale}
        )
        # Plain text carries the authored source unchanged; it has no markup context.
        text = f"{subject}\n\n{revision.body}\n"
        result = attempt_email(coordinate, subject, html, text, timeout=timeout, message_key=command.idempotency_key)
        return DeliveryOutcome(_OUTCOMES[result.outcome], reason=result.reason, provider_receipt=result.receipt)


def _read_coordinate(command: DeliveryCommand) -> tuple[str, DeliveryOutcome | None]:
    """Decrypt the one coordinate bound to ``command``, or return why it cannot be used."""
    bound = RecipientSnapshot.objects.filter(
        pk=command.snapshot_id, intent_id=command.intent_id, event_id=command.event_id
    )
    try:
        # The encrypted field decrypts while the row loads.
        coordinates = list(bound.values_list("delivery_coordinate", flat=True))
    # Field decryption failed (key missing/rotated or corrupt token).
    except Exception:
        logger.warning("email coordinate unreadable for snapshot %s", command.snapshot_id)
        return "", DeliveryOutcome(OutcomeClass.RETRIABLE, reason="coordinate_unreadable")
    coordinate = coordinates[0] if coordinates else ""
    failure = None
    if not coordinates:
        failure = DeliveryOutcome(OutcomeClass.TERMINAL, reason="snapshot_unavailable")
    elif not coordinate:
        failure = DeliveryOutcome(OutcomeClass.TERMINAL, reason="coordinate_unavailable")
    return coordinate, failure
