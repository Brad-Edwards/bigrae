"""Behavior tests for the Range API endpoints.

These tests drive the real Django URLs with a real database and assert
observable behavior: HTTP status, response JSON, and persisted ORM state
(Range rows, audit log rows). Range provisioning dispatches to ECS only when
configured; under the test settings it is unconfigured, so create/cancel/
destroy complete without any cloud call and no boundary mock is required.

Fixtures (windows_os, make_agent, hydratable_scenario, launch_range_via_api)
come from tests/mission_control/conftest.py; authenticated_client from the
root conftest.
"""

import json

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from engine.models import Range
from shared.audit import AuditAction
from shared.models import AuditLog, RaesOperationRecord, RaesParticipantRuntimeRecord
from shared.raes.contracts import SHIFTER_BACKEND_PROFILE
from shared.schemas.raes_operation import canonical_raes_payload_digest
from shared.schemas.raes_participant_runtime import (
    canonical_raes_payload_digest as canonical_participant_payload_digest,
)

pytestmark = pytest.mark.django_db


def _json(response):
    return json.loads(response.content)


def _seed_raes_status(request_id, status="running"):
    """Seed one operation-status sidecar row for a range's request_id."""
    payload = {"operation_id": "op-1", "status": status}
    return RaesOperationRecord.objects.create(
        request_id=request_id,
        operation_id=payload["operation_id"],
        idempotency_key=f"operation_status:{request_id}",
        contract_kind=RaesOperationRecord.ContractKind.RAES,
        contract_version="operation-status-v1",
        contract_profile=SHIFTER_BACKEND_PROFILE,
        record_kind=RaesOperationRecord.RecordKind.OPERATION_STATUS,
        source_timestamp=timezone.now(),
        payload_digest=canonical_raes_payload_digest(payload),
        payload=payload,
    )


def _seed_participant_runtime(request_id, participant_ref="ctf-participant-1", status="running"):
    """Seed one participant-runtime sidecar row for a range's request_id."""
    payload = {"participant_ref": participant_ref, "status": status}
    return RaesParticipantRuntimeRecord.objects.create(
        request_id=request_id,
        participant_ref=participant_ref,
        idempotency_key=f"participant_runtime:{participant_ref}:{request_id}",
        contract_kind=RaesParticipantRuntimeRecord.ContractKind.RAES,
        contract_version="participant-runtime-v1",
        contract_profile=SHIFTER_BACKEND_PROFILE,
        participant_runtime_profile="shifter-provisioning",
        record_kind=RaesParticipantRuntimeRecord.RecordKind.PARTICIPANT_RUNTIME,
        source_timestamp=timezone.now(),
        payload_digest=canonical_participant_payload_digest(payload),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# get_range
# ---------------------------------------------------------------------------


class TestGetRange:
    def test_requires_login(self):
        response = Client().get(reverse("v1:mission_control:range-current"))
        assert response.status_code == 401

    def test_returns_no_range_when_none_exists(self, authenticated_client):
        client, _ = authenticated_client(email="norange@example.com")
        response = client.get(reverse("v1:mission_control:range-current"))
        assert response.status_code == 200
        data = _json(response)
        assert data["has_range"] is False
        assert data["range"] is None
        assert data["connection_urls"] == []
        assert data["lifecycle"] is None
        assert data["vpn_profile_available"] is False

    def test_returns_active_range_after_launch(self, authenticated_client, launch_range_via_api):
        client, user = authenticated_client(email="active@example.com")
        launch_resp, _agent, scenario_id = launch_range_via_api(client, user)
        assert launch_resp.status_code == 200

        response = client.get(reverse("v1:mission_control:range-current"))
        assert response.status_code == 200
        data = _json(response)
        assert data["has_range"] is True
        assert data["range"]["scenario_id"] == scenario_id
        assert data["range"]["user_id"] == user.id
        # CMS records the user-visible dispatch state before handing off to
        # engine so failed dispatch can roll the owned row to FAILED.
        assert data["range"]["status"] == "provisioning"
        assert data["range"]["is_active"] is True
        assert data["range"]["is_terminal"] is False
        # The launched range is the one returned.
        assert data["range"]["request_id"] == _json(launch_resp)["range"]["request_id"]
        assert data["lifecycle"]["extension_days"] == 30
        assert data["lifecycle"]["can_extend"] is True
        assert data["lifecycle"]["expires_at"] < data["lifecycle"]["maximum_expires_at"]

    def test_does_not_return_another_users_range(self, authenticated_client, launch_range_via_api):
        owner_client, owner = authenticated_client(email="owner@example.com")
        launch_range_via_api(owner_client, owner)

        other_client, _other = authenticated_client(email="other@example.com")
        response = other_client.get(reverse("v1:mission_control:range-current"))
        assert response.status_code == 200
        assert _json(response)["has_range"] is False

    def test_raes_projection_present_when_records_exist(self, authenticated_client, launch_range_via_api):
        client, user = authenticated_client(email="raes-backed@example.com")
        launch_resp, _agent, _scenario_id = launch_range_via_api(client, user)
        request_id = _json(launch_resp)["range"]["request_id"]
        _seed_raes_status(request_id, status="succeeded")

        data = _json(client.get(reverse("v1:mission_control:range-current")))
        assert data["has_range"] is True
        projection = data["raes_projection"]
        assert projection is not None
        assert projection["status"] == "succeeded"
        assert projection["status_label"] == "Operation succeeded"

    def test_raes_participant_runtime_null_when_no_range(self, authenticated_client):
        client, _ = authenticated_client(email="no-range-participant-runtime@example.com")
        data = _json(client.get(reverse("v1:mission_control:range-current")))
        assert data["has_range"] is False
        assert data["raes_participant_runtime"] is None

    def test_raes_participant_runtime_present_when_records_exist(self, authenticated_client, launch_range_via_api):
        client, user = authenticated_client(email="participant-runtime-backed@example.com")
        launch_resp, _agent, _scenario_id = launch_range_via_api(client, user)
        request_id = _json(launch_resp)["range"]["request_id"]
        _seed_participant_runtime(request_id, status="running")

        data = _json(client.get(reverse("v1:mission_control:range-current")))
        assert data["has_range"] is True
        participant_runtime = data["raes_participant_runtime"]
        assert participant_runtime is not None
        assert participant_runtime["participants"][0]["participant_ref"] == "ctf-participant-1"
        assert participant_runtime["participants"][0]["runtime"]["status"] == "running"
        # The fixture plan has no interactive members, so only the range-level
        # backend command channel is projected.
        channels = {c["channel"] for c in participant_runtime["access_channels"]}
        assert channels == {"backend_command"}
        backend_commands = [c for c in participant_runtime["access_channels"] if c["channel"] == "backend_command"]
        assert len(backend_commands) == 1
        assert backend_commands[0]["target_ref"] == request_id
        # Shifter range status stays untouched by the RAES participant/runtime projection.
        assert data["range"]["status"] == "provisioning"


class TestExtendRangeLease:
    def test_extends_owned_mission_control_range_without_accepting_a_deadline(
        self, authenticated_client, launch_range_via_api
    ):
        client, user = authenticated_client(email="extend-range@example.com")
        launch_range_via_api(client, user)
        before = _json(client.get(reverse("v1:mission_control:range-current")))["lifecycle"]

        response = client.post(
            reverse("v1:mission_control:range-extend"),
            data="",
            content_type="application/json",
        )

        assert response.status_code == 200
        payload = _json(response)
        assert payload["lifecycle"]["expires_at"] > before["expires_at"]
        assert payload["lifecycle"]["maximum_expires_at"] == before["maximum_expires_at"]

    def test_rejects_extension_input(self, authenticated_client, launch_range_via_api):
        client, user = authenticated_client(email="extend-input@example.com")
        launch_range_via_api(client, user)

        response = client.post(
            reverse("v1:mission_control:range-extend"),
            data=json.dumps({"days": 365}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert _json(response)["error"]["code"] == "invalid"

    def test_cannot_extend_another_users_range(self, authenticated_client, launch_range_via_api):
        owner_client, owner = authenticated_client(email="extend-owner@example.com")
        launch_range_via_api(owner_client, owner)
        other_client, _ = authenticated_client(email="extend-other@example.com")

        response = other_client.post(
            reverse("v1:mission_control:range-extend"),
            data="",
            content_type="application/json",
        )

        assert response.status_code == 404

    def test_range_at_hard_limit_returns_conflict(self, authenticated_client, launch_range_via_api):
        from cms.models import RangeInstance

        client, user = authenticated_client(email="extend-limit@example.com")
        launch_range_via_api(client, user)
        instance = RangeInstance.objects.get(user_id=user.pk)
        instance.expires_at = instance.maximum_expires_at
        instance.save(update_fields=["expires_at", "updated_at"])

        response = client.post(
            reverse("v1:mission_control:range-extend"),
            data="",
            content_type="application/json",
        )

        assert response.status_code == 409
        assert _json(response)["error"]["code"] == "range_extension_unavailable"


# ---------------------------------------------------------------------------
# launch_range
# ---------------------------------------------------------------------------


class TestLaunchWorkspaceDiscovery:
    def test_requires_login(self):
        response = Client().get(reverse("v1:mission_control:launch-workspaces-list"))
        assert response.status_code == 401

    def test_lists_policy_authorized_shared_workspace_independently(self, authenticated_client):
        from shared.principal_port import principal_for_user
        from workspaces.models import Account, Workspace
        from workspaces.services import add_account_member, create_account

        client, user = authenticated_client(email="workspace-options@example.com")
        account = create_account(kind=Account.Kind.TEAM, name="Launch account")
        add_account_member(account.uuid, principal_for_user(user))
        workspace = Workspace.objects.get(organization__account_id=account.id, is_default=True)

        response = client.get(reverse("v1:mission_control:launch-workspaces-list"))

        assert response.status_code == 200
        payload = _json(response)
        assert payload["personal_available"] is True
        assert payload["count"] == 1
        assert payload["workspaces"] == [
            {
                "uuid": str(workspace.uuid),
                "name": workspace.name,
                "organization_uuid": str(workspace.organization.uuid),
                "organization_name": workspace.organization.name,
            }
        ]

    def test_pages_to_a_workspace_beyond_the_first_hundred(self, authenticated_client):
        from shared.principal_port import principal_for_user
        from workspaces.models import Organization, Workspace
        from workspaces.services import add_account_member, create_account

        client, user = authenticated_client(email="paged-workspaces@example.test")
        account = create_account(kind="team", name="Paged launch account")
        add_account_member(account.uuid, principal_for_user(user))
        organization = Organization.objects.get(account_id=account.id)
        Workspace.objects.bulk_create(
            [Workspace(organization=organization, name=f"Workspace {index}") for index in range(100)]
        )
        response = client.get(reverse("v1:mission_control:launch-workspaces-list"), {"offset": 100, "limit": 100})
        assert response.status_code == 200
        assert response.json()["count"] == 101
        assert len(response.json()["workspaces"]) == 1
        assert client.get(reverse("v1:mission_control:launch-workspaces-list"), {"limit": 101}).status_code == 400


class TestLaunchRange:
    def _launch(self, client, body):
        return client.post(
            reverse("v1:mission_control:range-launch"),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_catalog_denial_during_launch_is_translated(self, authenticated_client, make_agent, monkeypatch):
        from shared.exceptions import CMSError

        client, user = authenticated_client(email="launch-catalog-denied@example.com")
        agent = make_agent(user)

        def denied(*args, **kwargs):
            raise CMSError("SYNTHETIC_PRIVATE_PROVIDER_TEXT")

        monkeypatch.setattr("mission_control.api.ranges.cms_list_launchable_scenarios", denied)
        response = self._launch(client, {"scenario": "synthetic", "agent_id": agent.pk})
        assert response.status_code == 403
        assert _json(response)["error"]["code"] == "content_not_available"
        assert "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in response.content.decode()

    def test_requires_login(self):
        response = Client().post(
            reverse("v1:mission_control:range-launch"),
            data="{}",
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_rejects_invalid_json(self, authenticated_client):
        client, _ = authenticated_client(email="badjson@example.com")
        response = client.post(
            reverse("v1:mission_control:range-launch"),
            data="not json",
            content_type="application/json",
        )
        assert response.status_code == 400
        assert _json(response)["error"]["code"] == "parse_error"

    def test_requires_agent(self, authenticated_client, hydratable_scenario):
        client, _ = authenticated_client(email="noagent@example.com")
        response = self._launch(client, {"scenario": hydratable_scenario.scenario_id})
        assert response.status_code == 400
        assert "agent" in json.dumps(_json(response)["error"]["details"]).lower()

    def test_rejects_invalid_scenario(self, authenticated_client, make_agent):
        client, user = authenticated_client(email="badscenario@example.com")
        agent = make_agent(user)
        response = self._launch(client, {"agent_id": agent.id, "scenario": "does-not-exist"})
        assert response.status_code == 400
        assert "scenario" in _json(response)["error"]["message"].lower()

    def test_rejects_nonexistent_agent(self, authenticated_client, hydratable_scenario):
        client, _ = authenticated_client(email="ghostagent@example.com")
        response = self._launch(client, {"agent_id": 999999, "scenario": hydratable_scenario.scenario_id})
        assert response.status_code == 400

    def test_rejects_non_launchable_raes_scenario(self, authenticated_client, make_agent):
        from cms.models import RaesPackageSource

        client, user = authenticated_client(email="raesnonlaunch@example.com")
        agent = make_agent(user)
        RaesPackageSource.objects.create(
            scenario_id="example-pending",
            contract_kind="raes",
            contract_profile="shifter",
            package_ref="scenario-dev/example/content-packages/example",
            package_version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            conformance_status="pending",
            registered_by=user,
        )
        response = self._launch(client, {"agent_id": agent.id, "scenario": "example-pending"})
        assert response.status_code == 400
        assert "scenario" in _json(response)["error"]["message"].lower()

    def test_successful_launch_creates_range_and_audit(self, authenticated_client, make_agent, hydratable_scenario):
        client, user = authenticated_client(email="launch@example.com")
        agent = make_agent(user)

        assert Range.objects.count() == 0
        response = self._launch(client, {"agent_id": agent.id, "scenario": hydratable_scenario.scenario_id})

        assert response.status_code == 200
        data = _json(response)
        assert data["success"] is True
        assert data["range"]["scenario_id"] == hydratable_scenario.scenario_id
        assert data["range"]["status"] == "provisioning"
        # A real range row was persisted.
        assert Range.objects.count() == 1
        # The provision was audited.
        assert AuditLog.objects.filter(action=AuditAction.PROVISION).exists()

    def test_rejects_second_concurrent_range(self, authenticated_client, make_agent, hydratable_scenario):
        client, user = authenticated_client(email="double@example.com")
        agent = make_agent(user)
        first = self._launch(client, {"agent_id": agent.id, "scenario": hydratable_scenario.scenario_id})
        assert first.status_code == 200

        second = self._launch(client, {"agent_id": agent.id, "scenario": hydratable_scenario.scenario_id})
        assert second.status_code == 400
        assert "active range" in _json(second)["error"]["message"].lower()
        # No second range row was created.
        assert Range.objects.count() == 1

    @staticmethod
    def _member_workspace(user):
        from shared.principal_port import principal_for_user
        from workspaces.models import Account, Workspace
        from workspaces.services import add_account_member, create_account

        principal = principal_for_user(user)
        account = create_account(kind=Account.Kind.TEAM, name="API Shared Account")
        add_account_member(account.uuid, principal)
        return Workspace.objects.get(organization__account_id=account.id, is_default=True)

    def test_launch_with_member_workspace_uuid_binds_that_workspace(
        self, authenticated_client, make_agent, hydratable_scenario
    ):
        client, user = authenticated_client(email="wslaunch@example.com")
        agent = make_agent(user)
        workspace = self._member_workspace(user)

        response = self._launch(
            client,
            {
                "agent_id": agent.id,
                "scenario": hydratable_scenario.scenario_id,
                "workspace_uuid": str(workspace.uuid),
            },
        )

        assert response.status_code == 200
        assert Range.objects.get().workspace_id == workspace.id

    def test_launch_rejects_a_malformed_workspace_uuid_at_the_serializer(
        self, authenticated_client, make_agent, hydratable_scenario
    ):
        client, user = authenticated_client(email="wsbad@example.com")
        agent = make_agent(user)

        response = self._launch(
            client,
            {"agent_id": agent.id, "scenario": hydratable_scenario.scenario_id, "workspace_uuid": "not-a-uuid"},
        )

        assert response.status_code == 400
        assert Range.objects.count() == 0

    def test_launch_with_a_non_member_workspace_is_denied(
        self, authenticated_client, make_agent, hydratable_scenario, django_user_model
    ):
        from workspaces.models import Organization, Workspace

        client, user = authenticated_client(email="wsnonmember@example.com")
        agent = make_agent(user)
        organization = Organization.objects.create(name="Foreign Org")
        workspace = Workspace.objects.create(organization=organization, name="Foreign")

        response = self._launch(
            client,
            {
                "agent_id": agent.id,
                "scenario": hydratable_scenario.scenario_id,
                "workspace_uuid": str(workspace.uuid),
            },
        )

        # Authorized-shape but unavailable scope is an opaque 403 (ADR-046-R9),
        # distinct from the serializer's 400 for a malformed UUID.
        assert response.status_code == 403
        assert Range.objects.count() == 0


# ---------------------------------------------------------------------------
# cancel_range / destroy_range
# ---------------------------------------------------------------------------


class TestCancelRange:
    def test_requires_login(self):
        response = Client().post(
            reverse("v1:mission_control:range-cancel"),
            data="{}",
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_requires_identifier(self, authenticated_client):
        client, _ = authenticated_client(email="cancelnoid@example.com")
        response = client.post(
            reverse("v1:mission_control:range-cancel"),
            data="{}",
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "request_id or range_id" in json.dumps(_json(response)["error"]["details"])

    def test_cancel_nonexistent_range(self, authenticated_client):
        client, _ = authenticated_client(email="cancelghost@example.com")
        response = client.post(
            reverse("v1:mission_control:range-cancel"),
            data=json.dumps({"request_id": "00000000-0000-0000-0000-000000000000"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_successful_cancel_of_launched_range(self, authenticated_client, launch_range_via_api):
        client, user = authenticated_client(email="cancelok@example.com")
        launch_resp, _agent, _scenario = launch_range_via_api(client, user)
        request_id = _json(launch_resp)["range"]["request_id"]

        response = client.post(
            reverse("v1:mission_control:range-cancel"),
            data=json.dumps({"request_id": request_id}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert _json(response)["success"] is True
        # The cancel was audited.
        assert AuditLog.objects.filter(action=AuditAction.CANCEL).exists()


class TestParticipantOnlyLifecycleGuard:
    """A CTF participant-only account is rejected server-side on every range
    lifecycle verb (#944), even though the UI hides those verbs. Read endpoints
    and non-participant users are unaffected.
    """

    LIFECYCLE_VIEWS = [
        "v1:mission_control:range-launch",
        "v1:mission_control:range-cancel",
        "v1:mission_control:range-destroy",
        "v1:mission_control:range-extend",
        "v1:mission_control:range-pause",
        "v1:mission_control:range-resume",
    ]

    def _participant_only(self, authenticated_client, email):
        from django.contrib.auth.models import Group

        client, user = authenticated_client(email=email)
        group, _ = Group.objects.get_or_create(name="CTF Participant")
        user.groups.add(group)
        from datetime import timedelta

        from ctf.models import CTFEvent, CTFParticipant
        from management.models import UserProfile
        from tests._mission_control_fixtures import individual_account

        account = individual_account(user)
        now = timezone.now()
        event = CTFEvent.objects.create(
            name="Synthetic temporary event",
            creator_principal_uuid=user.identity_principal.uuid,
            owner_principal_uuid=user.identity_principal.uuid,
            scope_kind="account",
            account_id=account.pk,
            status="active",
            event_start=now - timedelta(hours=1),
            event_end=now + timedelta(hours=1),
        )
        CTFParticipant.objects.create(event=event, user=user, name="Temporary participant", email=user.email)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.user_type = "ctf_participant"
        profile.is_ctf_account = True
        profile.active_ctf_event_id = event.pk
        profile.save(update_fields=["user_type", "is_ctf_account", "active_ctf_event_id"])
        client.logout()
        client.force_login(user)
        return client, user

    @pytest.mark.parametrize("view_name", LIFECYCLE_VIEWS)
    def test_participant_only_account_is_forbidden(self, authenticated_client, view_name):
        client, _ = self._participant_only(authenticated_client, email=f"p-{view_name.split(':')[1]}@example.com")
        response = client.post(
            reverse(view_name),
            data=json.dumps({"request_id": "00000000-0000-0000-0000-000000000000"}),
            content_type="application/json",
        )
        assert response.status_code == 403
        assert _json(response)["error"]["code"] == "permission_denied"
        assert _json(response)["error"]["message"] == "Permission denied"

    def test_participant_only_launch_creates_no_range(
        self, authenticated_client, windows_os, make_agent, hydratable_scenario
    ):
        client, user = self._participant_only(authenticated_client, email="p-launch-state@example.com")
        agent = make_agent(user)
        response = client.post(
            reverse("v1:mission_control:range-launch"),
            data=json.dumps({"agent_id": agent.id, "scenario": hydratable_scenario.scenario_id}),
            content_type="application/json",
        )
        assert response.status_code == 403
        # The guard runs before any CMS call, so nothing is provisioned.
        assert not Range.objects.filter(user_id=user.id).exists()

    def test_participant_only_destroy_writes_no_audit(self, authenticated_client):
        client, _ = self._participant_only(authenticated_client, email="p-destroy-audit@example.com")
        response = client.post(
            reverse("v1:mission_control:range-destroy"),
            data=json.dumps({"request_id": "00000000-0000-0000-0000-000000000000"}),
            content_type="application/json",
        )
        assert response.status_code == 403
        assert not AuditLog.objects.filter(action=AuditAction.DEPROVISION).exists()

    def test_temporary_event_credential_cannot_read_global_range_surface(self, authenticated_client):
        client, _ = self._participant_only(authenticated_client, email="p-read@example.com")
        response = client.get(reverse("v1:mission_control:range-current"))
        assert response.status_code == 403

    def test_non_participant_destroy_is_not_forbidden(self, authenticated_client):
        # Regression: a plain (non-CTF) user is not blocked by the guard. The
        # nonexistent range yields a 400 from the CMS layer, never a 403.
        client, _ = authenticated_client(email="plain-destroy@example.com")
        response = client.post(
            reverse("v1:mission_control:range-destroy"),
            data=json.dumps({"request_id": "00000000-0000-0000-0000-000000000000"}),
            content_type="application/json",
        )
        assert response.status_code == 400


class TestDestroyRange:
    def test_requires_login(self):
        response = Client().post(
            reverse("v1:mission_control:range-destroy"),
            data="{}",
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_destroy_nonexistent_range(self, authenticated_client):
        client, _ = authenticated_client(email="destroyghost@example.com")
        response = client.post(
            reverse("v1:mission_control:range-destroy"),
            data=json.dumps({"request_id": "00000000-0000-0000-0000-000000000000"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_successful_destroy_of_launched_range(self, authenticated_client, launch_range_via_api):
        client, user = authenticated_client(email="destroyok@example.com")
        launch_resp, _agent, _scenario = launch_range_via_api(client, user)
        request_id = _json(launch_resp)["range"]["request_id"]

        response = client.post(
            reverse("v1:mission_control:range-destroy"),
            data=json.dumps({"request_id": request_id}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert _json(response)["success"] is True
        assert AuditLog.objects.filter(action=AuditAction.DEPROVISION).exists()
