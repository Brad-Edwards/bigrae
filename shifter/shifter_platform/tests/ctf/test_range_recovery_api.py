"""CTF range recovery + spare-pool API flow tests (issue #1018).

Split out of ``test_api_view_flows.py`` to keep each test module behavior-scoped
under the platform test-suite size guard (``test_test_suite_structure.py``).
Integration-style, real DB fixtures; the recovery service runs end-to-end (no
first-party ``ctf.services.range`` patch, per ADR-019).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.ctf._api_flow_helpers import call_json as _json

if TYPE_CHECKING:
    from django.test import Client

    from ctf.models import CTFEvent, CTFParticipant

pytestmark = pytest.mark.django_db


class TestRangeRecoveryApi:
    """Organizer range-recovery endpoint (issue #1018)."""

    def test_recover_participant_range(
        self,
        authenticated_organizer_client: Client,
        organizer_user,
        organizer_credential,
        ctf_participant: CTFParticipant,
        second_participant_user,
        settings,
    ):
        """Organizer recovers a participant's range in their own event -> 200 + recovery result (issue #1018).

        Drives the real ``recover_participant_range`` service end-to-end
        (ADR-019 boundary-mock policy: no first-party ``ctf.services.range``
        patch) via a real CTF-sourced old range + a pooled event spare,
        mirroring ``tests/ctf/test_services/test_range_recovery.py``.
        """
        from tests.ctf.test_services.test_range_recovery import (
            _make_pooled_spare,
            _make_spare_range,
            _retain_initial_range_custody,
        )

        settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
        settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64

        old_range = _make_spare_range(event=ctf_participant.event, owner=ctf_participant.user)
        ctf_participant.range_instance_id = old_range.pk
        ctf_participant.save(update_fields=["range_instance_id"])
        _retain_initial_range_custody(old_range, organizer_credential)
        _spare, spare_range = _make_pooled_spare(
            event=ctf_participant.event,
            owner=ctf_participant.event.created_by,
            credential=organizer_credential,
            scenario_id=ctf_participant.event.scenario_id,
        )

        resp = _json(
            authenticated_organizer_client,
            "post",
            "api_recover_participant_range",
            kwargs={"participant_id": ctf_participant.id},
            body={
                "strategy": "reassign_spare",
                "spare_range_instance_id": spare_range.pk,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "completed"
        assert body["strategy"] == "reassign_spare"
        assert body["old_range_instance_id"] == old_range.pk
        assert body["replacement_range_instance_id"] == spare_range.pk

        ctf_participant.refresh_from_db()
        assert ctf_participant.range_instance_id == spare_range.pk

        from ctf.models import CTFRangeRecovery

        recovery = CTFRangeRecovery.objects.get(participant=ctf_participant)
        assert recovery.created_by_id == organizer_user.id


@pytest.fixture
def event_with_spare_scenario(ctf_event: CTFEvent, organizer_user, monkeypatch, settings):
    """CTF event configured with a RAES source and cloud dispatch held at its seam."""
    from cms.models import RaesPackageSource
    from cms.raes.dispatch import CmsRaesDispatchPort
    from workspaces.services import resource_scope_from_ids

    monkeypatch.setattr("engine.services._raes_range.start_raes_range_provisioning", lambda *_a, **_kw: None)
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64

    def dispatch(request_id, credential, source, options):
        port = CmsRaesDispatchPort(
            user_id=None,
            request_id=str(request_id),
            workspace_id=options.workspace_id,
            backend_admission=options.backend_admission,
            egress_mode=options.egress_mode,
            credential=credential,
            scope=options.principal_scope,
            source=source,
            owner_principal=options.owner_principal,
            authority_action=options.authority_action,
            authority_target=options.authority_target,
        )
        port.realize({"kind": "raes_provisioning_plan", "raes_version": "2.0", "resources": {}})

    monkeypatch.setattr("cms.services._raes_dispatch.dispatch_repo_raes_package", dispatch)
    scope = resource_scope_from_ids(
        kind=ctf_event.scope_kind,
        account_id=ctf_event.account_id,
        organization_id=ctf_event.organization_id,
        workspace_id=ctf_event.workspace_id,
    )
    source = RaesPackageSource.objects.create(
        scenario_id="ctf-spare-pool-api-flow-test",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="tests/packs/ctf-spare-pool-api-flow-test",
        package_version="1.0.0",
        package_digest="sha256:" + "a" * 64,
        conformance_status="passed",
        registered_by=organizer_user,
        owner_principal_uuid=ctf_event.owner_principal_uuid,
        scope_kind="account",
        account_uuid=scope.account_uuid,
    )
    ctf_event.scenario_id = source.scenario_id
    ctf_event.range_config = {"agents_by_os": {}, "ngfw_enabled": False}
    ctf_event.save(update_fields=["scenario_id", "range_config"])
    return ctf_event


class TestSpareRangePoolApi:
    """Operator endpoint for managing an event's spare-range pool (issue #1018)."""

    def test_organizer_provisions_spares(
        self, authenticated_organizer_client: Client, event_with_spare_scenario: CTFEvent
    ):
        resp = _json(
            authenticated_organizer_client,
            "post",
            "api_provision_event_spares",
            kwargs={"event_id": event_with_spare_scenario.id},
            body={"count": 2},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "event_id": str(event_with_spare_scenario.id),
            "target_count": 2,
            "existing": 0,
            "created": 2,
        }

        from ctf.models import CTFSpareRange

        assert CTFSpareRange.objects.filter(event=event_with_spare_scenario).count() == 2
        event_with_spare_scenario.refresh_from_db()
        assert event_with_spare_scenario.spare_range_count == 2
