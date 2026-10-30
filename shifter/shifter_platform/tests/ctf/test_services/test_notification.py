"""Retired notification writes and ledger-staged notices."""

from uuid import uuid4

import pytest

from ctf.exceptions import CTFCommunicationError
from ctf.services import notification


@pytest.mark.parametrize(
    "name,args",
    [
        ("send_login_info", (uuid4(),)),
        ("send_credentials", (uuid4(),)),
        ("send_announcement", (uuid4(), "Subject", "Body", None)),
        ("schedule_notification", (uuid4(), None)),
        ("cancel_scheduled_notification", (uuid4(),)),
        ("deliver_scheduled_notification", (uuid4(),)),
        ("_deliver_announcement", (None,)),
    ],
)
def test_legacy_export_cannot_send(name, args):
    with pytest.raises(CTFCommunicationError) as exc:
        getattr(notification, name)(*args)
    assert exc.value.code == "CTF_COMMUNICATION_RETIRED"


@pytest.mark.parametrize(
    "name",
    [
        "notify_organizer_provision_failure",
        "notify_organizer_capacity_outcome",
    ],
)
def test_unsupported_organizer_alerts_stay_unavailable(name):
    assert getattr(notification, name)(uuid4(), {}) == {"outcome": "channel_unavailable"}


@pytest.mark.django_db
def test_reminder_retry_uses_same_staged_campaign(ctf_event, settings):
    settings.EMAIL_DELIVERY_AVAILABLE = False
    from ctf.models import CommunicationCampaign, CommunicationIntent, CTFNotification

    first = notification.send_reminder(ctf_event.pk, hours_before=1)
    assert notification.send_reminder(ctf_event.pk, hours_before=1) == first
    assert first["outcome"] == "channel_unavailable"
    assert CommunicationCampaign.objects.count() == 1
    assert not CommunicationIntent.objects.exists()
    assert not CTFNotification.objects.exists()
