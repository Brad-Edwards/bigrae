"""Organizer award API flows (CTF-204)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from tests.ctf._api_flow_helpers import call_json as _json

if TYPE_CHECKING:
    from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["award", "page", "staff", "webhook"])
def test_created_response_matches_published_status_and_body(
    kind, authenticated_organizer_client, ctf_participant, second_organizer_user
):
    import json
    from pathlib import Path

    from django.urls import reverse

    event_id = ctf_participant.event_id
    commands = {
        "award": (
            "api_participant_awards",
            {"participant_id": ctf_participant.pk},
            {"points": 50, "reason": "Synthetic award"},
            "participants/{participant_id}/awards/",
        ),
        "page": (
            "api_event_pages",
            {"event_id": event_id},
            {"title": "Synthetic page", "body": "Synthetic content"},
            "events/{event_id}/pages/",
        ),
        "staff": (
            "api_event_staff",
            {"event_id": event_id},
            {"principal_uuid": str(second_organizer_user.identity_principal.uuid), "role": "moderator"},
            "events/{event_id}/staff-members/",
        ),
        "webhook": (
            "api_event_webhooks",
            {"event_id": event_id},
            {"url": "https://hooks.example.test/ctf", "subscribed_events": []},
            "events/{event_id}/webhooks/",
        ),
    }
    name, kwargs, body, path = commands[kind]
    response = authenticated_organizer_client.post(
        reverse(f"v1:ctf:{name}", kwargs=kwargs), data=json.dumps(body), content_type="application/json"
    )
    assert response.status_code == (201 if kind == "staff" else 200), response.content
    document = json.loads((Path(__file__).resolve().parents[2] / "openapi/v1.json").read_text())
    published = document["paths"][f"/api/v1/ctf/{path}"]["post"]["responses"][str(response.status_code)]
    schema = published["content"]["application/json"]["schema"]
    schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    assert set(schema.get("required", ())) <= response.json().keys()
    assert response.json().keys() <= schema["properties"].keys()


class TestParticipantAwardApi:
    def test_grant_list_and_revoke(self, authenticated_organizer_client: Client, ctf_participant):
        resp = _json(
            authenticated_organizer_client,
            "post",
            "api_participant_awards",
            kwargs={"participant_id": ctf_participant.id},
            body={"points": 50, "reason": "Creative solve"},
        )
        assert resp.status_code == 200
        award_id = resp.json()["id"]
        assert resp.json()["points"] == 50

        ctf_participant.refresh_from_db()
        assert ctf_participant.cached_score == 50

        resp = _json(
            authenticated_organizer_client,
            "get",
            "api_participant_awards",
            kwargs={"participant_id": ctf_participant.id},
        )
        assert resp.status_code == 200
        assert resp.json()["awards"][0]["reason"] == "Creative solve"

        detail = _json(
            authenticated_organizer_client,
            "get",
            "api_participant_detail",
            kwargs={"participant_id": ctf_participant.id},
        )
        assert detail.status_code == 200
        assert detail.json()["awards"][0]["points"] == 50

        resp = _json(
            authenticated_organizer_client,
            "post",
            "api_award_delete",
            kwargs={"award_id": award_id},
        )
        assert resp.status_code == 200
        ctf_participant.refresh_from_db()
        assert ctf_participant.cached_score == 0

    def test_negative_award_deducts(self, authenticated_organizer_client: Client, ctf_participant):
        resp = _json(
            authenticated_organizer_client,
            "post",
            "api_participant_awards",
            kwargs={"participant_id": ctf_participant.id},
            body={"points": -25, "reason": "Rule violation"},
        )
        assert resp.status_code == 200
        ctf_participant.refresh_from_db()
        assert ctf_participant.cached_score == -25

    def test_revoke_unowned_award_is_not_found(self, authenticated_organizer_client: Client):
        resp = _json(
            authenticated_organizer_client,
            "post",
            "api_award_delete",
            kwargs={"award_id": uuid4()},
        )
        assert resp.status_code == 404
