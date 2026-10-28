"""Multi-event communications console API (#2052, CTF-008/010/012).

Drives the real REST views and services: target discovery, preview without
delivery effects, explicit reviewed-revision release, per-event/channel delivery
aggregates, minimal recipient rows, and the ledger activity timeline. Every read
re-checks complete-target authority, so the role matrix covers admin, owner,
moderator, judge, participant, removed staff, and cross-workspace actors.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

import workspaces.services as ws
from ctf.enums import EventStaffRole, ParticipantStatus, ScheduledTaskStatus
from ctf.enums_communication import DeliveryStatus
from ctf.models import (
    CommunicationCampaign,
    CommunicationIntent,
    CTFEvent,
    CTFEventStaff,
    CTFParticipant,
    CTFScheduledTask,
    DeliveryAttempt,
    ParticipantReceipt,
    RecipientSnapshot,
)
from shared.api_tokens.models import ApiToken
from workspaces.models import WorkspaceMembership

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("personal_token_use_grant")]
URL = "/api/v1/ctf/communications/"


def _client(user) -> APIClient:
    client = APIClient()
    client.force_login(user)
    return client


def _token_client(owner, scopes) -> APIClient:
    _token, raw = ApiToken.create_token(name="console", created_by=owner, scopes=scopes)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def _participant(event, name, email):
    return CTFParticipant.objects.create(
        event=event, email=email, name=name, status=ParticipantStatus.ACTIVE.value, registered_at=timezone.now()
    )


@pytest.fixture
def workspace(organizer_user):
    return ws.resolve_personal_workspace(organizer_user)


@pytest.fixture
def second_event(ctf_event, organizer_user):
    return CTFEvent.objects.create(
        name="Second Event",
        created_by=organizer_user,
        workspace_id=ctf_event.workspace_id,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
        event_timezone="America/New_York",
    )


@pytest.fixture
def roster(ctf_event, second_event):
    """Two participants in the first event and one in the second."""
    return [
        _participant(ctf_event, "Ada", "ada@example.test"),
        _participant(ctf_event, "Bo", "bo@example.test"),
        _participant(second_event, "Cy", "cy@example.test"),
    ]


@pytest.fixture
def staff_member(second_organizer_user, workspace):
    """Add the second organizer to the workspace; tests assign the event role."""
    WorkspaceMembership.objects.create(workspace_id=workspace.workspace_id, user=second_organizer_user, role="member")

    def assign(event, role):
        return CTFEventStaff.objects.create(event=event, user=second_organizer_user, role=role)

    return assign


def _draft(client, workspace, events, *, trigger=None, subject="Kickoff"):
    ids = [str(event.pk) for event in events]
    response = client.post(
        URL,
        {
            "workspace_id": str(workspace.workspace_uuid),
            "title": "Kickoff",
            "target_event_ids": ids,
            "audience_spec": {"kind": "multi_event" if len(ids) > 1 else "event", "event_ids": ids},
            "trigger_spec": trigger or {"kind": "manual"},
            "channels": ["in_app"],
            "subject": subject,
            "body": "Read the **rules**.",
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def _ledger_counts():
    return (
        CommunicationIntent.objects.count(),
        RecipientSnapshot.objects.count(),
        DeliveryAttempt.objects.count(),
        ParticipantReceipt.objects.count(),
        CTFScheduledTask.objects.filter(task_type="release_communication").count(),
    )


def test_compose_preview_send_report_and_timeline(organizer_user, workspace, ctf_event, second_event, roster):
    client = _client(organizer_user)
    campaign = _draft(client, workspace, [ctf_event, second_event])
    assert {t["id"] for t in campaign["target_events"]} == {str(ctf_event.pk), str(second_event.pk)}
    first_revision = campaign["latest_revision"]
    assert first_revision["revision_number"] == 1
    assert campaign["trigger"] == {"kind": "manual", "due_at": None}

    before = _ledger_counts()
    preview = client.get(f"{URL}{campaign['id']}/preview/", {"revision_id": first_revision["id"]})
    assert preview.status_code == 200, preview.content
    body = preview.json()
    assert _ledger_counts() == before  # preview creates no delivery state
    assert body["revision"]["subject"] == "Kickoff"
    assert body["revision"]["content_digest"].startswith("sha256:")
    counts = {row["event_id"]: row["recipient_count"] for row in body["events"]}
    assert counts == {str(ctf_event.pk): 2, str(second_event.pk): 1}
    assert body["recipient_total"] == 3
    assert body["exceeds_audience_cap"] is False
    assert body["channels"] == [{"channel": "in_app", "available": True}]
    assert "ada@example.test" not in preview.content.decode()

    # Another tab revises the draft; the reviewed revision is still what is sent.
    revised = client.post(f"{URL}{campaign['id']}/revisions/", {"subject": "Edited", "body": "New"}, format="json")
    assert revised.status_code == 201
    assert revised.json()["latest_revision"]["revision_number"] == 2
    release = client.post(
        f"{URL}{campaign['id']}/release/",
        {"occurrence_key": "initial", "revision_id": first_revision["id"]},
        format="json",
    )
    assert release.status_code == 202, release.content
    intent = CommunicationIntent.objects.get(pk=release.json()["id"])
    assert intent.revision.subject == "Kickoff"
    conflict = client.post(
        f"{URL}{campaign['id']}/release/",
        {"occurrence_key": "initial", "revision_id": revised.json()["latest_revision"]["id"]},
        format="json",
    )
    assert conflict.status_code == 409

    # Partial delivery stays visible and receipts are not multiplied by channels.
    attempts = list(DeliveryAttempt.objects.filter(intent=intent).order_by("snapshot__participant_public_id"))
    DeliveryAttempt.objects.filter(pk=attempts[0].pk).update(status=DeliveryStatus.ACCEPTED.value)
    DeliveryAttempt.objects.filter(pk=attempts[1].pk).update(status=DeliveryStatus.PERMANENT_FAILURE.value)
    ParticipantReceipt.objects.filter(snapshot__participant=roster[0]).update(read_at=timezone.now())
    report = client.get(f"{URL}{campaign['id']}/delivery/")
    assert report.status_code == 200, report.content
    occurrence = report.json()["results"][0]
    assert occurrence["intent_id"] == str(intent.pk)
    assert occurrence["status"] == "released"
    assert occurrence["revision_number"] == 1
    totals = occurrence["totals"]
    assert totals["recipients"] == 3
    assert totals["inbox_available"] == 3
    assert totals["read"] == 1
    assert totals["acknowledged"] == 0
    in_app = totals["channels"][0]
    assert in_app["channel"] == "in_app"
    assert (in_app["accepted"], in_app["permanent_failure"], in_app["queued"]) == (1, 1, 1)
    per_event = {row["event_id"]: row for row in occurrence["events"]}
    assert per_event[str(second_event.pk)]["recipients"] == 1
    assert sum(row["recipients"] for row in occurrence["events"]) == 3

    rows = client.get(
        f"{URL}{campaign['id']}/recipients/", {"intent_id": str(intent.pk), "event_id": str(ctf_event.pk)}
    )
    assert rows.status_code == 200, rows.content
    names = sorted(row["name"] for row in rows.json()["results"])
    assert names == ["Ada", "Bo"]
    assert "ada@example.test" not in rows.content.decode()
    assert {row["channels"][0]["channel"] for row in rows.json()["results"]} == {"in_app"}

    assert client.post(f"{URL}{campaign['id']}/cancel/", {}, format="json").status_code == 200
    activity = client.get(f"{URL}{campaign['id']}/activity/")
    assert activity.status_code == 200, activity.content
    kinds = [entry["kind"] for entry in activity.json()["entries"]]
    assert kinds == ["created", "revised", "revised", "released", "cancelled"]
    for entry in activity.json()["entries"]:
        assert entry["actor"] == {"label": organizer_user.username, "via": "session"}
    assert "Kickoff" not in activity.content.decode()


def test_scheduled_preview_shows_local_times_and_task_state(organizer_user, workspace, ctf_event, second_event):
    client = _client(organizer_user)
    due = (timezone.now() + timedelta(days=2)).replace(microsecond=0)
    campaign = _draft(
        client, workspace, [ctf_event, second_event], trigger={"kind": "absolute_time", "due_at": due.isoformat()}
    )
    preview = client.get(f"{URL}{campaign['id']}/preview/", {"revision_id": campaign["latest_revision"]["id"]})
    assert preview.json()["trigger"]["kind"] == "absolute_time"
    local = {row["event_id"]: row["local_due_at"] for row in preview.json()["events"]}
    assert local[str(second_event.pk)].endswith(("-04:00", "-05:00"))

    release = client.post(
        f"{URL}{campaign['id']}/release/",
        {"occurrence_key": "initial", "revision_id": campaign["latest_revision"]["id"]},
        format="json",
    )
    assert release.status_code == 202
    occurrence = client.get(f"{URL}{campaign['id']}/delivery/").json()["results"][0]
    assert (occurrence["status"], occurrence["schedule_status"]) == ("scheduled", "pending")
    CTFScheduledTask.objects.filter(task_type="release_communication").update(status=ScheduledTaskStatus.FAILED.value)
    occurrence = client.get(f"{URL}{campaign['id']}/delivery/").json()["results"][0]
    assert occurrence["schedule_status"] == "failed"


def test_targets_list_only_events_the_actor_may_message(
    organizer_user, second_organizer_user, workspace, ctf_event, second_event, staff_member
):
    other = CTFEvent.objects.create(
        name="Other tenant",
        created_by=second_organizer_user,
        workspace_id=ws.resolve_personal_workspace(second_organizer_user).workspace_id,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
    )
    owner = _client(organizer_user).get(f"{URL}targets/")
    assert owner.status_code == 200, owner.content
    assert {e["id"] for e in owner.json()["events"]} == {str(ctf_event.pk), str(second_event.pk)}
    assert owner.json()["workspaces"][0]["id"] == str(workspace.workspace_uuid)
    assert str(other.pk) not in owner.content.decode()

    staff_member(ctf_event, EventStaffRole.MODERATOR.value)
    staff_member(second_event, EventStaffRole.JUDGE.value)
    moderator = _client(second_organizer_user).get(f"{URL}targets/", {"workspace_id": str(workspace.workspace_uuid)})
    assert {e["id"] for e in moderator.json()["events"]} == {str(ctf_event.pk)}


def test_role_matrix_for_campaign_reads(
    organizer_user,
    second_organizer_user,
    participant_user,
    admin_user,
    workspace,
    ctf_event,
    roster,
    staff_member,
):
    campaign = _draft(_client(organizer_user), workspace, [ctf_event])
    intent = _client(organizer_user).post(f"{URL}{campaign['id']}/release/", {"occurrence_key": "a"}, format="json")
    reads = [
        (f"{URL}{campaign['id']}/preview/", {"revision_id": campaign["latest_revision"]["id"]}),
        (f"{URL}{campaign['id']}/delivery/", {}),
        (f"{URL}{campaign['id']}/activity/", {}),
        (f"{URL}{campaign['id']}/recipients/", {"intent_id": intent.json()["id"], "event_id": str(ctf_event.pk)}),
    ]

    def statuses(user):
        return [_client(user).get(url, params).status_code for url, params in reads]

    assert statuses(organizer_user) == [200, 200, 200, 200]

    # Judges hold no notification capability on the event.
    judge = staff_member(ctf_event, EventStaffRole.JUDGE.value)
    assert set(statuses(second_organizer_user)) <= {403, 404}
    CTFEventStaff.objects.filter(pk=judge.pk).update(role=EventStaffRole.MODERATOR.value)
    assert statuses(second_organizer_user) == [200, 200, 200, 200]

    # Removed staff lose every read immediately.
    CTFEventStaff.all_objects.filter(pk=judge.pk).update(deleted_at=timezone.now())
    assert set(statuses(second_organizer_user)) <= {403, 404}

    # Participants never reach organizer communications.
    participant = _participant(ctf_event, "Pat", participant_user.email)
    CTFParticipant.objects.filter(pk=participant.pk).update(user=participant_user)
    assert set(statuses(participant_user)) <= {403, 404}

    # The platform-admin override does not bypass workspace membership.
    assert set(statuses(admin_user)) <= {403, 404}
    WorkspaceMembership.objects.create(workspace_id=workspace.workspace_id, user=admin_user, role="member")
    assert statuses(admin_user) == [200, 200, 200, 200]


def test_cross_workspace_and_cross_event_requests_are_denied(
    organizer_user, second_organizer_user, workspace, ctf_event, staff_member
):
    foreign = CTFEvent.objects.create(
        name="Foreign",
        created_by=second_organizer_user,
        workspace_id=ws.resolve_personal_workspace(second_organizer_user).workspace_id,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
    )
    _participant(foreign, "Secret", "secret@example.test")
    client = _client(organizer_user)
    response = client.post(
        URL,
        {
            "workspace_id": str(workspace.workspace_uuid),
            "title": "Probe",
            "target_event_ids": [str(ctf_event.pk), str(foreign.pk)],
            "audience_spec": {"kind": "multi_event", "event_ids": [str(ctf_event.pk), str(foreign.pk)]},
            "trigger_spec": {"kind": "manual"},
            "channels": ["in_app"],
            "subject": "Probe",
            "body": "Probe",
        },
        format="json",
    )
    assert response.status_code in (403, 404)
    assert not CommunicationCampaign.objects.exists()  # atomic: no partial admission

    # A multi-event campaign is invisible to a moderator of only one target.
    second = CTFEvent.objects.create(
        name="Second",
        created_by=organizer_user,
        workspace_id=ctf_event.workspace_id,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
    )
    campaign = _draft(client, workspace, [ctf_event, second])
    staff_member(ctf_event, EventStaffRole.MODERATOR.value)
    moderator = _client(second_organizer_user)
    assert moderator.get(f"{URL}{campaign['id']}/delivery/").status_code in (403, 404)
    preview = moderator.get(f"{URL}{campaign['id']}/preview/", {"revision_id": campaign["latest_revision"]["id"]})
    assert preview.status_code in (403, 404)


def test_scopes_and_foreign_identifiers(organizer_user, workspace, ctf_event, second_event, roster):
    campaign = _draft(_client(organizer_user), workspace, [ctf_event])
    other = _draft(_client(organizer_user), workspace, [second_event])
    preview_url = f"{URL}{campaign['id']}/preview/"
    revision = campaign["latest_revision"]["id"]
    assert (
        _token_client(organizer_user, ["ctf:communication:read"])
        .get(preview_url, {"revision_id": revision})
        .status_code
        == 200
    )
    write_only = _token_client(organizer_user, ["ctf:communication:write"])
    assert write_only.get(preview_url, {"revision_id": revision}).status_code == 403
    assert write_only.get(f"{URL}targets/").status_code == 403

    client = _client(organizer_user)
    foreign_revision = other["latest_revision"]["id"]
    assert client.get(preview_url, {"revision_id": foreign_revision}).status_code == 404
    intent = client.post(f"{URL}{other['id']}/release/", {"occurrence_key": "a"}, format="json").json()
    rows = client.get(f"{URL}{campaign['id']}/recipients/", {"intent_id": intent["id"], "event_id": str(ctf_event.pk)})
    assert rows.status_code == 404
    own = client.post(f"{URL}{campaign['id']}/release/", {"occurrence_key": "a"}, format="json").json()
    rows = client.get(f"{URL}{campaign['id']}/recipients/", {"intent_id": own["id"], "event_id": str(second_event.pk)})
    assert rows.status_code == 404
    assert client.get(preview_url, {"revision_id": revision, "extra": "1"}).status_code == 400


def test_timeline_attributes_each_actor(
    organizer_user, second_organizer_user, workspace, ctf_event, roster, staff_member
):
    campaign = _draft(_client(organizer_user), workspace, [ctf_event])
    staff_member(ctf_event, EventStaffRole.MODERATOR.value)
    moderator = _client(second_organizer_user)
    assert (
        moderator.post(f"{URL}{campaign['id']}/revisions/", {"subject": "B", "body": "B"}, format="json").status_code
        == 201
    )
    token = _token_client(second_organizer_user, ["ctf:communication:write", "ctf:communication:read"])
    assert token.post(f"{URL}{campaign['id']}/cancel/", {}, format="json").status_code == 200
    entries = _client(organizer_user).get(f"{URL}{campaign['id']}/activity/").json()["entries"]
    actors = [(e["kind"], e["actor"]["label"], e["actor"]["via"]) for e in entries]
    assert actors == [
        ("created", organizer_user.username, "session"),
        ("revised", organizer_user.username, "session"),
        ("revised", second_organizer_user.username, "session"),
        ("cancelled", second_organizer_user.username, "api_token"),
    ]
    stored = CommunicationCampaign.objects.get(pk=campaign["id"])
    assert stored.cancelled_by_user_id == second_organizer_user.pk
    assert stored.cancelled_at is not None


def test_session_writes_require_csrf(organizer_user, workspace, ctf_event):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(organizer_user)
    response = client.post(
        URL,
        {
            "workspace_id": str(workspace.workspace_uuid),
            "title": "x",
            "target_event_ids": [str(ctf_event.pk)],
            "audience_spec": {"kind": "event", "event_ids": [str(ctf_event.pk)]},
            "trigger_spec": {"kind": "manual"},
            "channels": ["in_app"],
            "subject": "x",
            "body": "x",
        },
        format="json",
    )
    assert response.status_code == 403
    assert not CommunicationCampaign.objects.exists()


def test_summaries_select_latest_revision_without_loading_bodies(organizer_user, workspace, ctf_event):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client = _client(organizer_user)
    campaign = _draft(client, workspace, [ctf_event])
    for number in (2, 3):
        content = {"subject": f"S{number}", "body": "B"}
        response = client.post(f"{URL}{campaign['id']}/revisions/", content, format="json")
        assert response.json()["latest_revision"]["revision_number"] == number
    with CaptureQueriesContext(connection) as queries:
        listed = client.get(URL, {"workspace_id": str(workspace.workspace_uuid)})
    assert listed.json()["results"][0]["latest_revision"]["revision_number"] == 3
    revision_reads = [q["sql"] for q in queries.captured_queries if "ctf_communication_message_revision" in q["sql"]]
    assert revision_reads, "latest revision must come from SQL"
    assert not any('"body"' in sql for sql in revision_reads)
    detail = client.get(f"{URL}{campaign['id']}/")
    assert detail.json()["latest_revision"]["revision_number"] == 3
