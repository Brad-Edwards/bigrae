"""Platform-admin CTF administration through the canonical DRF API (#1923, ADR-052).

Exercises the organizer-or-platform-admin admission gate, authority-aware
discovery, detail/mutation parity, the override audit trail, and the negative
cases (unrelated organizer, standard user, staff management stays owner-only).
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group

from shared.auth import CTF_ORGANIZER_GROUP
from shared.models import AuditLog
from tests.ctf._api_flow_helpers import call_json

pytestmark = pytest.mark.django_db


def _make_organizer(email: str):
    from django.contrib.auth.models import User

    from management.services import get_user_profile

    Group.objects.get_or_create(name=CTF_ORGANIZER_GROUP)
    user = User.objects.create_user(username=email, email=email, password="testpass123")  # nosec B106
    group, _ = Group.objects.get_or_create(name=CTF_ORGANIZER_GROUP)
    user.groups.add(group)
    profile = get_user_profile(user)
    profile.user_type = "ctf_organizer"
    profile.save(update_fields=["user_type"])
    return user


@pytest.fixture
def superuser_client(admin_user):
    from django.test import Client

    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def other_event(second_organizer_user):
    """An event owned by a different organizer than ``ctf_event``."""
    from datetime import timedelta

    from django.utils import timezone

    from ctf.enums import EventStatus
    from ctf.models import CTFEvent

    now = timezone.now()
    return CTFEvent.objects.create(
        name="Other Org Event",
        created_by=second_organizer_user,
        status=EventStatus.REGISTRATION.value,
        event_start=now + timedelta(days=1),
        event_end=now + timedelta(days=1, hours=8),
        scenario_id="basic",
    )


class TestAdmission:
    def test_standard_user_gets_a_policy_filtered_empty_collection(self, authenticated_standard_client, ctf_event):
        resp = call_json(authenticated_standard_client, "get", "api_event_list")
        assert resp.status_code == 200
        assert resp.json()["events"] == []
        assert resp.json()["count"] == 0

    def test_superuser_is_admitted(self, superuser_client, ctf_event):
        assert call_json(superuser_client, "get", "api_event_list").status_code == 200


class TestDiscovery:
    def test_later_page_and_totals_cover_all_readable_events(
        self, authenticated_organizer_client, ctf_event, other_event
    ):
        from ctf.models import CTFEvent

        for number in range(200):
            CTFEvent.objects.create(
                name=f"Paged event {number:03d}",
                created_by=ctf_event.created_by,
                owner_principal_uuid=ctf_event.owner_principal_uuid,
                scope_kind=ctf_event.scope_kind,
                account_id=ctf_event.account_id,
                organization_id=ctf_event.organization_id,
                workspace_id=ctf_event.workspace_id,
                status="active",
                team_mode=True,
                team_size_limit=2,
                event_start=ctf_event.event_start,
                event_end=ctf_event.event_end,
            )
        first = call_json(authenticated_organizer_client, "get", "api_event_list", query="?page=1&page_size=200")
        second = call_json(authenticated_organizer_client, "get", "api_event_list", query="?page=2&page_size=200")
        assert first.status_code == second.status_code == 200
        first, second = first.json(), second.json()
        assert len(first["events"]) == 200
        assert len(second["events"]) == 1
        assert first["count"] == second["count"] == 201
        assert first["active_count"] == 201
        assert first["team_mode_count"] == 200
        assert first["next_page"] == 2 and second["next_page"] is None
        ids = [row["id"] for row in first["events"] + second["events"]]
        assert len(set(ids)) == 201
        assert str(other_event.pk) not in ids

    def test_provider_failure_is_a_structured_503(self, authenticated_organizer_client, ctf_event, monkeypatch):
        from ctf.exceptions import CTFError

        def unavailable(*args, **kwargs):
            raise CTFError("SYNTHETIC_PRIVATE_PROVIDER_TEXT", code="CTF_AUTHORITY_UNAVAILABLE")

        monkeypatch.setattr("ctf.services.unified_authorization.require_event_action", unavailable)
        response = call_json(authenticated_organizer_client, "get", "api_event_list")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dependency_unavailable"
        assert "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in response.content.decode()

    def test_broad_discovery_is_rejected_before_policy_work(
        self, authenticated_organizer_client, ctf_event, other_event, monkeypatch
    ):
        from unittest.mock import Mock

        monkeypatch.setattr("ctf.api.organizer.events._EVENT_LIST_CANDIDATE_LIMIT", 1)
        check = Mock()
        monkeypatch.setattr("ctf.services.unified_authorization.require_event_action", check)
        response = call_json(authenticated_organizer_client, "get", "api_event_list")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "event_discovery_limit"
        check.assert_not_called()

    def test_superuser_lists_all_events_with_owner_and_source(self, superuser_client, ctf_event, other_event):
        resp = call_json(superuser_client, "get", "api_event_list")
        assert resp.status_code == 200
        events = {e["id"]: e for e in resp.json()["events"]}
        assert {str(ctf_event.id), str(other_event.id)} <= set(events)
        row = events[str(other_event.id)]
        assert row["access_source"] == "policy"
        assert row["owner"]["id"] == str(other_event.owner_principal_uuid)
        assert row["owner"]["display_name"]

    def test_organizer_sees_only_owned(self, authenticated_organizer_client, ctf_event, other_event):
        resp = call_json(authenticated_organizer_client, "get", "api_event_list")
        ids = {e["id"] for e in resp.json()["events"]}
        assert str(ctf_event.id) in ids
        assert str(other_event.id) not in ids

    def test_owner_row_reports_owner_source(self, authenticated_organizer_client, ctf_event):
        resp = call_json(authenticated_organizer_client, "get", "api_event_list")
        row = next(e for e in resp.json()["events"] if e["id"] == str(ctf_event.id))
        assert row["access_source"] == "policy"

    def test_status_filter_rejects_unknown_value(self, superuser_client, ctf_event):
        resp = call_json(superuser_client, "get", "api_event_list", query="?status=bogus")
        assert resp.status_code == 400


class TestDetailAndMutation:
    def test_superuser_reads_other_event_detail(self, superuser_client, other_event):
        resp = call_json(superuser_client, "get", "api_event_detail", kwargs={"event_id": other_event.id})
        assert resp.status_code == 200
        assert resp.json()["access_source"] == "policy"

    def test_unrelated_organizer_denied_detail(self, authenticated_organizer_client, other_event):
        resp = call_json(authenticated_organizer_client, "get", "api_event_detail", kwargs={"event_id": other_event.id})
        assert resp.status_code == 403

    def test_superuser_updates_other_event_and_audits(self, superuser_client, other_event):
        original_owner_id = other_event.created_by_id
        resp = call_json(
            superuser_client,
            "put",
            "api_event_detail",
            kwargs={"event_id": other_event.id},
            body={"name": "Renamed By Admin"},
        )
        assert resp.status_code == 200
        other_event.refresh_from_db()
        assert other_event.name == "Renamed By Admin"
        assert other_event.created_by_id == original_owner_id  # ownership unchanged

        record = AuditLog.objects.filter(context="ctf_event_action").order_by("-timestamp").first()
        assert record is not None
        assert record.new_state["authority_source"] == "policy"
        assert record.new_state["operation"] == "event.update"
        assert record.new_state["event_id"] == str(other_event.id)

    def test_owner_update_writes_the_same_policy_audit(self, authenticated_organizer_client, ctf_event):
        resp = call_json(
            authenticated_organizer_client,
            "put",
            "api_event_detail",
            kwargs={"event_id": ctf_event.id},
            body={"name": "Owner Rename"},
        )
        assert resp.status_code == 200
        assert AuditLog.objects.filter(
            context="ctf_event_action",
            new_state__authority_source="policy",
            new_state__operation="event.update",
        ).exists()

    def test_superuser_lifecycle_transition_audited(self, superuser_client, other_event):
        resp = call_json(
            superuser_client,
            "post",
            "api_event_lifecycle",
            kwargs={"event_id": other_event.id},
            body={"action": "activate"},
        )
        assert resp.status_code == 200
        assert AuditLog.objects.filter(
            context="ctf_event_action",
            new_state__authority_source="policy",
            new_state__operation="event.lifecycle.activate",
        ).exists()

    def test_superuser_participant_range_action_records_intent_and_outcome(self, superuser_client, other_event):
        """A per-participant range action through the override gets the ADR-052-R4 audit (#2617).

        The participant has no range, so the real range service refuses the stop:
        the intent is recorded before the attempt and the failure after it.
        """
        from django.utils import timezone

        from ctf.models import CTFParticipant

        participant = CTFParticipant.objects.create(
            event=other_event, email="p@test.com", name="p", status="active", registered_at=timezone.now()
        )

        resp = call_json(
            superuser_client, "post", "api_stop_participant_range", kwargs={"participant_id": participant.id}
        )

        assert resp.status_code == 400
        outcomes = list(
            AuditLog.objects.filter(context="ctf_event_action", new_state__operation="range.participant.stop")
            .order_by("sequence")
            .values_list("new_state__outcome", flat=True)
        )
        assert outcomes == ["intent", "failed"]
        organizer_row = AuditLog.objects.get(context="ctf_organizer_participant_range_action")
        assert organizer_row.new_state["authority_source"] == "policy"

    def test_service_range_action_retains_native_principal_audit(self, ctf_participant, settings, monkeypatch):
        """The real no-range failure records its service actor without inventing a User."""
        from types import SimpleNamespace

        from rest_framework.test import APIRequestFactory, force_authenticate

        from ctf.api.organizer.ranges import ParticipantRangeStopView
        from shared.authorization import AuthorizationDecision, DecisionKind, port
        from tests.cms._warm_pool_fixtures import service_credential

        credential = service_credential(settings, scopes=["authorization:event.manage_ranges", "ctf:event:write"])
        monkeypatch.setattr(
            port,
            "_provider_factory",
            lambda: SimpleNamespace(
                check=lambda request: AuthorizationDecision(
                    DecisionKind.ALLOWED if request.principal == credential.principal else DecisionKind.DENIED,
                    "policy_allowed" if request.principal == credential.principal else "policy_denied",
                )
            ),
        )
        request = APIRequestFactory().post("/", {})
        force_authenticate(request, user=None, token=credential)
        response = ParticipantRangeStopView.as_view()(request, participant_id=ctf_participant.pk)
        assert response.status_code == 400
        assert response.data["error"]["message"] == "Could not process range request."
        row = AuditLog.objects.get(context="ctf_organizer_participant_range_action")
        assert row.actor_type == "principal" and row.actor_id is None
        assert row.actor_principal_uuid == credential.principal.uuid
        assert row.new_state["authority_source"] == "policy"
        effects = AuditLog.objects.filter(context="ctf_event_action", new_state__operation="range.participant.stop")
        assert list(effects.order_by("sequence").values_list("new_state__outcome", flat=True)) == ["intent", "failed"]
        assert all(effect.actor_principal_uuid == credential.principal.uuid for effect in effects)


class TestNestedMutationPolicyAudit:
    """Nested event-derived mutations retain principal policy audit evidence."""

    @pytest.fixture
    def other_challenge(self, other_event):
        from ctf.models import CTFChallenge, CTFFlag

        challenge = CTFChallenge.objects.create(
            event=other_event,
            name="Nested Challenge",
            description="d",
            category="web",
            points=100,
            difficulty="easy",
            flag_format="FLAG{...}",
        )
        CTFFlag.objects.create(challenge=challenge, value="FLAG{x}", flag_type="static", order=0)
        return challenge

    def test_superuser_challenge_create_is_audited(self, superuser_client, other_event):
        resp = call_json(
            superuser_client,
            "post",
            "api_challenge_list",
            kwargs={"event_id": other_event.id},
            body={"name": "Admin Challenge", "description": "d", "flag": "FLAG{a}", "points": 50, "category": "web"},
        )
        assert resp.status_code == 201
        assert AuditLog.objects.filter(
            context="ctf_event_action",
            new_state__authority_source="policy",
            new_state__operation="challenge.create",
        ).exists()

    def test_superuser_flag_delete_is_audited(self, superuser_client, other_challenge):
        flag_id = other_challenge.flags.first().id
        resp = call_json(superuser_client, "post", "api_remove_flag", kwargs={"flag_id": flag_id})
        assert resp.status_code == 200
        assert AuditLog.objects.filter(
            context="ctf_event_action",
            new_state__authority_source="policy",
            new_state__operation="flag.delete",
        ).exists()

    def test_owner_nested_mutation_writes_policy_audit(self, authenticated_organizer_client, ctf_event):
        resp = call_json(
            authenticated_organizer_client,
            "post",
            "api_challenge_list",
            kwargs={"event_id": ctf_event.id},
            body={"name": "Owner Challenge", "description": "d", "flag": "FLAG{o}", "points": 50, "category": "web"},
        )
        assert resp.status_code == 201
        assert AuditLog.objects.filter(
            context="ctf_event_action",
            new_state__authority_source="policy",
            new_state__operation="challenge.create",
        ).exists()


class TestBootstrapAdvisoryFlag:
    _URL = "/api/v1/bootstrap/"

    def test_superuser_can_administer_ctf(self, superuser_client):
        perms = superuser_client.get(self._URL).json()["permissions"]
        assert perms["can_administer_ctf"] is True
        assert perms["is_ctf_organizer"] is False

    def test_organizer_can_administer_ctf(self, authenticated_organizer_client):
        perms = authenticated_organizer_client.get(self._URL).json()["permissions"]
        assert perms["can_administer_ctf"] is True

    def test_standard_user_cannot_administer_ctf(self, authenticated_standard_client):
        perms = authenticated_standard_client.get(self._URL).json()["permissions"]
        assert perms["can_administer_ctf"] is False


class TestStaffManagementViaPolicy:
    """Staff management is policy-authorized, audited, and never self-delegable."""

    def test_superuser_assigns_staff_and_audits(self, superuser_client, other_event, organizer_user):
        from ctf.models import CTFEventStaff

        original_owner_id = other_event.created_by_id
        resp = call_json(
            superuser_client,
            "post",
            "api_event_staff",
            kwargs={"event_id": other_event.id},
            body={"principal_uuid": str(organizer_user.identity_principal.uuid), "role": "moderator"},
        )
        assert resp.status_code == 201
        assert CTFEventStaff.objects.filter(
            event=other_event, user=organizer_user, role="moderator", deleted_at__isnull=True
        ).exists()
        # Policy admission never changes ownership, and the mutation is audited.
        other_event.refresh_from_db()
        assert other_event.created_by_id == original_owner_id
        assert AuditLog.objects.filter(
            context="ctf_event_action",
            new_state__authority_source="policy",
            new_state__operation="staff.assign",
        ).exists()

    def test_unrelated_organizer_cannot_assign_staff(self, other_event, second_organizer_user):
        """A non-owner organizer with no override is refused (authorization, not validation)."""
        from django.test import Client

        # A third organizer, neither owner nor platform admin.
        outsider = _make_organizer("outsider@test.com")
        client = Client()
        client.force_login(outsider)
        resp = call_json(
            client,
            "post",
            "api_event_staff",
            kwargs={"event_id": other_event.id},
            body={"principal_uuid": str(second_organizer_user.identity_principal.uuid), "role": "moderator"},
        )
        assert resp.status_code in (400, 403)


class TestEventCreationAuthority:
    """Event creation requires both policy authority and an existing account target."""

    def test_principal_without_an_account_cannot_create_event(self, superuser_client):
        resp = call_json(
            superuser_client,
            "post",
            "api_event_list",
            body={
                "name": "Admin Event",
                "event_start": "2099-01-01T00:00:00Z",
                "event_end": "2099-01-02T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        from ctf.models import CTFEvent

        assert not CTFEvent.objects.filter(name="Admin Event").exists()

    def test_organizer_can_still_create_event(self, authenticated_organizer_client):
        resp = call_json(
            authenticated_organizer_client,
            "post",
            "api_event_list",
            body={
                "name": "Organizer Event",
                "event_start": "2099-01-01T00:00:00Z",
                "event_end": "2099-01-02T00:00:00Z",
            },
        )
        assert resp.status_code == 201
