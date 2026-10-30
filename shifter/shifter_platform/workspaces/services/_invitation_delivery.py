"""Observed delivery outcome of a workspace invitation send (ADR-071, #1525).

The invitation service renders the message and registers one send for after
commit. This module performs that send and records its outcome for the current
generation only: a later resend or revoke fences an earlier send.
"""

from __future__ import annotations

import uuid

from django.utils import timezone

from shared.email import INTERACTIVE_TIMEOUT_SECONDS, EmailOutcome, attempt_email
from workspaces.models import InvitationDeliveryStatus, WorkspaceInvitation

_DELIVERY_STATUS = {
    EmailOutcome.ACCEPTED: InvitationDeliveryStatus.ACCEPTED,
    EmailOutcome.RETRIABLE: InvitationDeliveryStatus.FAILED,
    EmailOutcome.TERMINAL: InvitationDeliveryStatus.FAILED,
    EmailOutcome.UNAVAILABLE: InvitationDeliveryStatus.UNAVAILABLE,
}


def reset_delivery(invitation: WorkspaceInvitation) -> list[str]:
    """Mark a rotated generation as not yet sent; return the changed field names."""
    invitation.delivery_status = InvitationDeliveryStatus.PENDING
    invitation.delivery_reason = ""
    invitation.delivery_observed_at = None
    return ["delivery_status", "delivery_reason", "delivery_observed_at"]


def deliver_invitation(
    invitation_id: int, generation: uuid.UUID, recipient: str, subject: str, html: str, text: str
) -> None:
    """Make one bounded send for a still-current generation and record its outcome.

    A later resend or revoke fences this send: a superseded, revoked, or accepted
    invitation is not sent, and the outcome is written only while the generation
    still matches (ADR-071).
    """
    current = WorkspaceInvitation.objects.filter(
        pk=invitation_id, generation=generation, accepted_at__isnull=True, revoked_at__isnull=True
    )
    if not current.exists():
        return
    result = attempt_email(recipient, subject, html, text, timeout=INTERACTIVE_TIMEOUT_SECONDS)
    WorkspaceInvitation.objects.filter(pk=invitation_id, generation=generation).update(
        delivery_status=_DELIVERY_STATUS[result.outcome],
        delivery_reason=result.reason,
        delivery_observed_at=timezone.now(),
    )
