"""Direct organizer read probes preserve dependency failures and explicit denials."""

from types import SimpleNamespace

import pytest
from django.urls import reverse

from shared.authorization import AuthorizationDecision, DecisionKind, port

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("surface", ["attachment", "timeline"])
@pytest.mark.parametrize("decision", ["outage", "evaluator_error", "denied"])
def test_direct_read_policy_failure_contract(
    authenticated_organizer_client, ctf_challenge, ctf_participant, monkeypatch, surface, decision
):
    from ctf.models import CTFChallengeFile

    if surface == "attachment":
        attachment = CTFChallengeFile.objects.create(
            challenge=ctf_challenge,
            filename="synthetic.txt",
            display_name="Synthetic attachment",
            file_size_bytes=4,
            content_type="text/plain",
            s3_key="synthetic/attachment",
            sha256_hash="1" * 64,
        )
        url = reverse("v1:ctf:api_file_download", kwargs={"file_id": attachment.pk})
    else:
        url = reverse("v1:ctf:api_score_timeline", kwargs={"participant_id": ctf_participant.pk})

    def check(_request):
        if decision == "outage":
            raise RuntimeError("SYNTHETIC_PROVIDER_DETAIL")
        kind = DecisionKind.EVALUATOR_ERROR if decision == "evaluator_error" else DecisionKind.DENIED
        return AuthorizationDecision(
            kind, "evaluator_unavailable" if decision == "evaluator_error" else "policy_denied"
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    response = authenticated_organizer_client.get(url)
    assert response.status_code == (403 if decision == "denied" else 503)
    assert response.json()["error"]["code"] == (
        "permission_denied" if decision == "denied" else "dependency_unavailable"
    )
    assert "SYNTHETIC_PROVIDER_DETAIL" not in response.content.decode()
