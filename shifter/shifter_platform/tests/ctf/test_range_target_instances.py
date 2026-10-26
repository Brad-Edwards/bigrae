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

import ctf.bridges as bridges
from ctf.services.participant.accounts import create_participant_accounts
from ctf.services.range.status import get_range_status

_PROJECTED = {"uuid": "u1", "name": "dc01", "private_ip": "10.1.2.56", "os_type": "windows"}


def test_bridge_projects_instances_to_safe_allowlist(monkeypatch):
    # CMS returns rich dicts; the bridge must drop everything outside the allowlist.
    raw = [
        {
            **_PROJECTED,
            "role": "target",
            "rdp_password": "super-secret",
            "provider": "aws",
            "channel_binding": "participant-a",
            "range_instance_id": 42,
        }
    ]
    monkeypatch.setattr("cms.services.get_principal_range_target_instances", lambda _credential, _range_id: raw)

    result = bridges.cms_get_range_target_instances(SimpleNamespace(pk=7), 42)

    assert result == [_PROJECTED]
    # No leaked keys — the projection is the security boundary, not the serializer.
    assert set(result[0]) == {"uuid", "name", "private_ip", "os_type"}


@pytest.mark.django_db
def test_range_status_exposes_targets_only_when_ready(ctf_event_active, organizer_credential, monkeypatch):
    monkeypatch.setattr("ctf.services.participant.accounts.request_event_provisioning", lambda *_a, **_kw: None)
    monkeypatch.setattr("ctf.bridges.cms_has_openvpn_profile", lambda *_a, **_kw: False)
    monkeypatch.setattr("ctf.bridges.cms_get_range_target_instances", lambda _credential, _range_id: [dict(_PROJECTED)])
    participant = create_participant_accounts(ctf_event_active.id, count=1, actor=organizer_credential)[0]
    participant.range_instance_id = 42
    participant.range_status = "ready"
    participant.save(update_fields=["range_instance_id", "range_status"])

    monkeypatch.setattr("ctf.bridges.cms_get_range_status", lambda _rid: "ready")
    ready = get_range_status(participant.pk, credential=organizer_credential)

    assert ready["status"] == "ready"
    assert ready["target_instances"] == [_PROJECTED]

    # A provisioning range must not leak targets.
    monkeypatch.setattr("ctf.bridges.cms_get_range_status", lambda _rid: "provisioning")
    provisioning = get_range_status(participant.pk, credential=organizer_credential)

    assert provisioning["status"] == "provisioning"
    assert provisioning["target_instances"] == []


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
