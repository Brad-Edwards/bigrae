"""Participant OpenVPN profile retrieval through the CTF service boundary."""

from __future__ import annotations

from uuid import UUID

from django.utils import timezone

from ctf.services.range.status import _get_participant_with_range
from shared.remote_access import OpenVpnProfile


def admit_assigned_range_vpn(credential, request_id, event_uuid):
    from ctf.bridges import cms_find_range_instance_id
    from ctf.services.principal_participation import participant_for_credential

    participant = participant_for_credential(credential, event_uuid)
    instance_id = cms_find_range_instance_id(request_id)
    if instance_id is None or participant.range_instance_id != instance_id:
        raise ValueError("Participation denied")


def vpn_participant_for_credential(credential, *, event_uuid=None):
    """Resolve a live participation, requiring selection when it is ambiguous."""
    from ctf.models import CTFParticipant
    from ctf.services.participant import eligible_participant_q
    from ctf.services.principal_participation import participant_for_credential
    from shared.credentials import resolve_current_credential

    credential = resolve_current_credential(credential)
    event_uuid = event_uuid or credential.event_uuid
    if event_uuid is None:
        now = timezone.now()
        events = list(
            CTFParticipant.objects.filter(
                eligible_participant_q(),
                principal_uuid=credential.principal.uuid,
                event__status__in=("active", "paused"),
                event__event_start__lte=now,
                event__event_end__gt=now,
            ).values_list("event_id", flat=True)[:2]
        )
        if len(events) != 1:
            raise ValueError("Participation denied")
        event_uuid = events[0]
    return participant_for_credential(credential, event_uuid)


def get_vpn_profile(participant_id: UUID, *, credential=None) -> OpenVpnProfile:
    """Resolve the current participant-owned profile; accept no caller target ids."""
    from ctf.bridges import cms_get_openvpn_profile

    if credential is not None:
        from ctf.models import CTFParticipant
        from ctf.services.principal_participation import participant_for_credential
        from shared.credentials import resolve_current_credential

        current = resolve_current_credential(credential)
        target = CTFParticipant.objects.get(pk=participant_id)
        participant = participant_for_credential(current, target.event_id)
        if participant.pk != participant_id or participant.range_instance_id is None:
            raise ValueError("Participation denied")
        return cms_get_openvpn_profile(current, participant.range_instance_id)

    participant = _get_participant_with_range(participant_id)
    assert participant.user is not None
    assert participant.range_instance_id is not None
    return cms_get_openvpn_profile(participant.user, participant.range_instance_id)
