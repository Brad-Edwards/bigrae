"""Participant-safe target-instance projection for CTF range access (issue #1740).

The SPA workspace renders per-box access from the CTF range-status API. The CTF
bridge must project CMS instances down to the {uuid, name, private_ip, os_type}
allowlist so range-internal metadata (roles, provider details, channel bindings,
secret references) never crosses into the participant surface, and the status
service must only expose targets once the range is ready.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctf.services.participant.accounts import create_participant_accounts
from tests.cms.conftest import make_pack as make_pack
from tests.ctf.test_range_launch_crash_recovery import launch_event as launch_event


@pytest.fixture
def ready_temporary_targets(launch_event, organizer_credential):
    from datetime import timedelta
    from uuid import uuid4

    from django.test import Client
    from django.utils import timezone

    from cms.models import RangeInstance
    from config.session_credentials import session_credential
    from ctf.services.range.provision import provision_participant_range
    from engine.models import Range
    from management.services import set_ctf_password_change_required

    launch_event.status = "active"
    launch_event.event_start = timezone.now() - timedelta(hours=1)
    launch_event.event_end = timezone.now() + timedelta(hours=1)
    launch_event.save(update_fields=["status", "event_start", "event_end"])
    participants = create_participant_accounts(launch_event.pk, count=2, actor=organizer_credential)
    ranges = []
    for index, participant in enumerate(participants, 1):
        provision_participant_range(participant.pk, actor=organizer_credential)
        participant.refresh_from_db()
        row = RangeInstance.objects.get(pk=participant.range_instance_id)
        RangeInstance.objects.filter(pk=row.pk).update(status="ready")
        Range.objects.filter(request__request_id=row.request.request_id).update(
            status="ready",
            provisioned_instances=[
                {
                    "uuid": str(uuid4()),
                    "name": f"target-{index}",
                    "private_ip": f"10.1.1.{index}",
                    "os_type": "linux",
                    "role": "target",
                    "participant_access_channels": ["ssh"],
                    "provider_internal": "private-projection-canary",
                }
            ],
        )
        participant.range_status = "ready"
        participant.save(update_fields=["range_status"])
        set_ctf_password_change_required(participant.user, False)
        ranges.append(row)
    client = Client()
    client.force_login(participants[0].user)
    return SimpleNamespace(
        client=client,
        participant=participants[0],
        other=participants[1],
        range=ranges[0],
        credential=session_credential(participants[0].user, client.session, provider_check=False),
        event=launch_event,
    )


@pytest.mark.django_db
def test_temporary_participant_status_and_challenge_use_exact_native_targets(ready_temporary_targets):
    from ctf.models import CTFChallenge

    setup = ready_temporary_targets
    response = setup.client.get("/api/v1/ctf/range/status/")
    assert response.status_code == 200, response.content
    targets = response.json()["target_instances"]
    assert len(targets) == 1 and targets[0]["name"] == "target-1"
    assert set(targets[0]) == {"uuid", "name", "private_ip", "os_type"}
    assert "private-projection-canary" not in response.content.decode()
    challenge = CTFChallenge.objects.create(
        event=setup.event,
        name="Synthetic endpoint",
        description="Connect to your seat",
        points=100,
        category="synthetic",
        target_instance_name="target-1",
        target_port=2222,
    )
    response = setup.client.get(f"/api/v1/ctf/me/challenges/{challenge.pk}/")
    assert response.status_code == 200, response.content
    assert response.json()["connection_info"] == {
        "host": "10.1.1.1",
        "port": 2222,
        "instance_name": "target-1",
        "os_type": "linux",
    }
    challenge.target_instance_name = "target-2"
    challenge.save(update_fields=["target_instance_name"])
    assert setup.client.get(f"/api/v1/ctf/me/challenges/{challenge.pk}/").json()["connection_info"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("defect", ["not_ready", "unassigned", "native_custody", "ineligible", "request_mismatch"])
def test_temporary_target_discovery_retains_lifecycle_and_custody_fences(ready_temporary_targets, defect):
    from uuid import uuid4

    from cms.exceptions import CMSError
    from cms.models import RangeInstance
    from cms.services import get_principal_range_target_instances
    from ctf.models import CTFParticipant
    from engine.models import Range

    setup = ready_temporary_targets
    if defect == "not_ready":
        RangeInstance.objects.filter(pk=setup.range.pk).update(status="provisioning")
    elif defect == "unassigned":
        CTFParticipant.objects.filter(pk=setup.participant.pk).update(range_instance_id=None)
    elif defect == "native_custody":
        Range.objects.filter(request__request_id=setup.range.request.request_id).update(
            owner_principal_uuid=setup.other.principal_uuid
        )
    elif defect == "request_mismatch":
        CTFParticipant.objects.filter(pk=setup.participant.pk).update(range_request_id=uuid4())
    else:
        CTFParticipant.objects.filter(pk=setup.participant.pk).update(status="disqualified")
    if defect == "not_ready":
        assert get_principal_range_target_instances(setup.credential, setup.range.pk) == []
    else:
        with pytest.raises(CMSError):
            get_principal_range_target_instances(setup.credential, setup.range.pk)


@pytest.mark.django_db
def test_principal_target_lookup_uses_the_exact_assigned_range(
    ctf_event_active, organizer_credential, participant_user
):
    from uuid import uuid4

    from cms.models import RangeInstance
    from cms.models import Request as CmsRequest
    from engine.models import Range
    from engine.models import Request as EngineRequest
    from engine.services import get_principal_ready_range_instances
    from shared.enums import RequestType

    custody = {
        "owner_principal_uuid": organizer_credential.principal.uuid,
        "scope_kind": ctf_event_active.scope_kind,
        "account_id": ctf_event_active.account_id,
        "organization_id": ctf_event_active.organization_id,
        "workspace_id": ctf_event_active.workspace_id,
    }
    ranges = []
    for index in (1, 2):
        request_id = uuid4()
        cms_request = CmsRequest.objects.create(
            request_id=request_id,
            request_type=RequestType.RANGE.value,
            user=participant_user,
            **custody,
        )
        cms_range = RangeInstance.objects.create(
            request=cms_request,
            user_id=participant_user.pk,
            event_uuid=ctf_event_active.pk,
            scenario_id="basic",
            range_source="ctf",
            status="ready",
            **custody,
        )
        engine_request = EngineRequest.objects.create(
            request_id=request_id,
            request_type=RequestType.RANGE.value,
            user=participant_user,
            **custody,
        )
        engine_range = Range.objects.create(
            request=engine_request,
            user=participant_user,
            cms_range_uuid=cms_range.uuid,
            event_uuid=ctf_event_active.pk,
            status="ready",
            provisioned_instances=[
                {
                    "uuid": str(uuid4()),
                    "name": f"target-{index}",
                    "private_ip": f"10.0.0.{index}",
                    "os_type": "linux",
                    "role": "target",
                }
            ],
            **custody,
        )
        cms_range.range_id = engine_range.pk
        cms_range.save(update_fields=["range_id"])
        ranges.append(cms_range)

    result = get_principal_ready_range_instances(
        organizer_credential.principal.uuid,
        request_id=ranges[0].request.request_id,
        cms_range_uuid=ranges[0].uuid,
        workspace_id=ranges[0].workspace_id,
    )

    assert [item["name"] for item in result] == ["target-1"]
