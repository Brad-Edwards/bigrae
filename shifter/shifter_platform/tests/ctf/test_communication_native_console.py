"""The incoming console uses current native authority at its owning boundaries."""

from uuid import UUID, uuid4

import pytest
from rest_framework.test import APIClient

from ctf.exceptions import CTFCommunicationError
from ctf.services.communication import AdmissionActor, reporting
from shared.authorization import AuthorizationDecision, DecisionKind, configured_authorization_provider
from tests.ctf.test_communication_rest import URL, body

pytestmark = pytest.mark.django_db


def _client(user):
    client = APIClient()
    client.force_login(user)
    return client


def test_target_options_include_direct_account_without_directory_permission(organizer_user, ctf_event, monkeypatch):
    provider = configured_authorization_provider()
    original = provider.check

    def check(request):
        if request.target.type != "event":
            return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
        return original(request)

    monkeypatch.setattr(provider, "check", check)
    response = _client(organizer_user).get(f"{URL}target-options/")
    assert response.status_code == 200, response.content
    parent = {"kind": "account", "id": body(ctf_event, organizer_user)["account"]}
    assert response.json()["events"][0]["parent"] == parent
    assert [{"kind": row["kind"], "id": row["id"]} for row in response.json()["parents"]] == [parent]


@pytest.mark.parametrize("report", ["preview", "delivery", "recipients", "activity"])
def test_direct_report_requires_current_native_event_authority(
    organizer_user, organizer_credential, ctf_event, monkeypatch, report
):
    client = _client(organizer_user)
    created = client.post(URL, body(ctf_event, organizer_user), format="json")
    assert created.status_code == 201, created.content
    campaign_id = created.json()["id"]
    revision_id = created.json()["latest_revision"]["id"]
    provider = configured_authorization_provider()
    monkeypatch.setattr(provider, "check", lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied"))
    with pytest.raises(CTFCommunicationError) as denied:
        actor = AdmissionActor(credential=organizer_credential)
        if report == "preview":
            reporting.preview(campaign_id, UUID(revision_id), actor=actor)
        elif report == "delivery":
            reporting.delivery_report(campaign_id, actor=actor, limit=10, offset=0)
        elif report == "activity":
            reporting.activity_timeline(campaign_id, actor=actor)
        else:
            reporting.recipient_rows(
                campaign_id, actor=actor, intent_id=uuid4(), event_id=ctf_event.pk, limit=10, offset=0
            )
    assert denied.value.code == "CTF_COMMUNICATION_EVENT_DENIED"


@pytest.mark.parametrize("kind", ["account", "organization", "workspace"])
def test_target_options_exact_native_parent_and_workspace_compatibility(
    organizer_user,
    ctf_event,
    kind,
):
    from ctf.models import CTFEvent
    from workspaces.models import Account, Organization, Workspace

    if kind == "account":
        parent_uuid = body(ctf_event, organizer_user)["account"]
        event = ctf_event
    else:
        account = Account.objects.create(kind="team", name="Synthetic tenant")
        organization = Organization.objects.create(account=account, name="Synthetic organization")
        workspace = Workspace.objects.create(organization=organization, name="Synthetic workspace")
        event = CTFEvent.objects.create(
            name="Scoped event",
            created_by=organizer_user,
            scope_kind="account",
            account_id=account.pk,
            organization_id=organization.pk,
            workspace_id=workspace.pk if kind == "workspace" else None,
            event_start=ctf_event.event_start,
            event_end=ctf_event.event_end,
        )
        parent_uuid = str(workspace.uuid if kind == "workspace" else organization.uuid)
    client = _client(organizer_user)
    response = client.get(f"{URL}target-options/", {kind: parent_uuid})
    assert response.status_code == 200, response.content
    assert [row["id"] for row in response.json()["events"]] == [str(event.pk)]
    assert response.json()["events"][0]["parent"] == {"kind": kind, "id": parent_uuid}
    assert response.json()["parents"][0]["name"]
    compatibility = client.get(f"{URL}targets/")
    assert compatibility.status_code == 200, compatibility.content
    assert [row["id"] for row in compatibility.json()["events"]] == ([str(event.pk)] if kind == "workspace" else [])
    if kind == "workspace":
        assert compatibility.json()["events"][0]["workspace_id"] == parent_uuid


@pytest.mark.parametrize("failure", ["deny", "outage", "empty_ceiling", "expired"])
def test_target_discovery_observes_policy_ceilings_and_current_proofs(
    organizer_user,
    organizer_credential,
    ctf_event,
    monkeypatch,
    failure,
):
    from dataclasses import replace
    from datetime import timedelta

    from django.utils import timezone

    from management.models import PrincipalSession
    from shared.authorization import CredentialCeiling

    actor = organizer_credential
    if failure == "empty_ceiling":
        actor = replace(actor, ceiling=CredentialCeiling(frozenset()))
    if failure == "expired":
        PrincipalSession.objects.filter(credential_uuid=actor.credential_uuid).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
    provider = configured_authorization_provider()
    if failure in {"deny", "outage"}:
        monkeypatch.setattr(
            provider,
            "check",
            lambda request: AuthorizationDecision(
                DecisionKind.EVALUATOR_ERROR if failure == "outage" else DecisionKind.DENIED,
                "evaluator_unavailable" if failure == "outage" else "policy_denied",
            ),
        )
    client = APIClient()
    client.force_authenticate(user=organizer_user, token=actor)
    response = client.get(f"{URL}target-options/")
    if failure in {"deny", "empty_ceiling"}:
        assert response.status_code == 200 and response.json()["events"] == []
        assert response.json()["parents"] == []
    elif failure == "outage":
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dependency_unavailable"
    else:
        assert response.status_code in {403, 404}


def test_recipient_report_needs_independent_exact_event_permission(organizer_user, ctf_event, monkeypatch):
    from django.utils import timezone

    from ctf.models import CTFParticipant

    CTFParticipant.objects.create(
        event=ctf_event,
        name="Private roster canary",
        email="private@example.test",
        status="active",
        registered_at=timezone.now(),
    )
    client = _client(organizer_user)
    created = client.post(URL, body(ctf_event, organizer_user), format="json").json()
    intent = client.post(f"{URL}{created['id']}/release/", {"occurrence_key": "one"}, format="json").json()
    provider = configured_authorization_provider()
    check = provider.check

    def denied(request):
        if request.action == "event.manage_participants":
            return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
        return check(request)

    monkeypatch.setattr(provider, "check", denied)
    assert client.get(f"{URL}{created['id']}/delivery/").status_code == 200
    result = client.get(f"{URL}{created['id']}/recipients/", {"intent_id": intent["id"], "event_id": str(ctf_event.pk)})
    assert result.status_code == 404
    assert "Private roster canary" not in result.content.decode()


def test_service_http_activity_retains_principal_without_human_contact(
    organizer_user, ctf_event, monkeypatch, settings
):
    from config import service_identity
    from management import services
    from management.models import ProviderBinding, ServiceCredentialAdmission

    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    principal = services.create_service_principal("Synthetic communications service", responsible_user=organizer_user)
    services.bind_principal_provider_identity(principal, "https://accounts.google.com", "123456789012345678901")
    admission = ServiceCredentialAdmission.objects.create(
        binding=ProviderBinding.objects.get(principal__uuid=principal.uuid),
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["ctf:communication:read", "ctf:communication:write"],
    )
    claims = {
        "iss": admission.binding.issuer,
        "sub": admission.binding.subject,
        "aud": admission.audience,
        "email": "communications@synthetic-project.iam.gserviceaccount.com",
        "email_verified": True,
    }
    monkeypatch.setattr(service_identity.id_token, "verify_oauth2_token", lambda *args, **kwargs: claims)
    provider = configured_authorization_provider()
    original = provider.check
    monkeypatch.setattr(
        provider,
        "check",
        lambda request: (
            AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")
            if request.principal.uuid == principal.uuid
            else original(request)
        ),
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer header.payload.signature")
    created = client.post(URL, body(ctf_event, organizer_user), format="json")
    assert created.status_code == 201, created.content
    campaign_id = created.json()["id"]
    assert (
        client.post(f"{URL}{campaign_id}/revisions/", {"subject": "New", "body": "New"}, format="json").status_code
        == 201
    )
    assert client.post(f"{URL}{campaign_id}/release/", {"occurrence_key": "service"}, format="json").status_code == 202
    assert client.post(f"{URL}{campaign_id}/cancel/", {}, format="json").status_code == 200
    timeline = client.get(f"{URL}{campaign_id}/activity/")
    assert timeline.status_code == 200, timeline.content
    assert {entry["actor"]["principal_uuid"] for entry in timeline.json()["entries"]} == {str(principal.uuid)}
    assert {entry["actor"]["via"] for entry in timeline.json()["entries"]} == {"credential"}
    assert organizer_user.username not in timeline.content.decode()


def test_target_pagination_excludes_denied_events_before_offset(organizer_user, ctf_event, monkeypatch):
    from datetime import timedelta

    from ctf.models import CTFEvent

    allowed = CTFEvent.objects.create(
        name="Visible later event",
        created_by=organizer_user,
        scope_kind=ctf_event.scope_kind,
        account_id=ctf_event.account_id,
        event_start=ctf_event.event_end,
        event_end=ctf_event.event_end + timedelta(hours=1),
    )
    provider = configured_authorization_provider()
    check = provider.check

    def denied(request):
        if request.target.uuid == ctf_event.pk:
            return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
        return check(request)

    monkeypatch.setattr(provider, "check", denied)
    response = _client(organizer_user).get(f"{URL}target-options/", {"limit": 1})
    assert response.status_code == 200, response.content
    assert [row["id"] for row in response.json()["events"]] == [str(allowed.pk)]
    assert response.json()["next_offset"] is None


def test_revocation_between_report_policy_and_sql_is_a_bounded_denial(
    organizer_user,
    organizer_credential,
    ctf_event,
    monkeypatch,
):
    from datetime import timedelta

    from django.utils import timezone

    from management.models import PrincipalSession

    created = _client(organizer_user).post(URL, body(ctf_event, organizer_user), format="json").json()
    provider = configured_authorization_provider()
    check = provider.check

    def revoke(request):
        decision = check(request)
        if request.action == "event.manage_communications":
            PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).update(
                expire_date=timezone.now() - timedelta(seconds=1)
            )
        return decision

    monkeypatch.setattr(provider, "check", revoke)
    client = APIClient()
    client.force_authenticate(user=organizer_user, token=organizer_credential)
    response = client.get(f"{URL}{created['id']}/activity/")
    assert response.status_code == 404, response.content
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("kind", ["session", "personal"])
def test_discovery_rejects_proof_revocation_during_policy(
    organizer_user,
    organizer_credential,
    ctf_event,
    monkeypatch,
    kind,
):
    from datetime import timedelta

    from django.utils import timezone

    from management.models import PrincipalSession
    from shared.api_tokens.models import ApiToken

    client = APIClient()
    token = None
    if kind == "session":
        client.force_authenticate(user=organizer_user, token=organizer_credential)
    else:
        token, raw = ApiToken.create_token(
            name="Synthetic native console read", created_by=organizer_user, scopes=["ctf:communication:read"]
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    provider = configured_authorization_provider()
    check = provider.check

    def revoke(request):
        decision = check(request)
        if request.action == "event.manage_communications":
            if token is None:
                PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).update(
                    expire_date=timezone.now() - timedelta(seconds=1)
                )
            else:
                token.revoke()
        return decision

    monkeypatch.setattr(provider, "check", revoke)
    result = client.get(f"{URL}target-options/")
    assert result.status_code == 404, result.content
    assert result.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("collection", [False, True])
def test_existing_campaign_reads_recheck_target_deletion_after_policy(
    organizer_user,
    ctf_event,
    monkeypatch,
    collection,
):
    from django.utils import timezone

    from ctf.models import CTFEvent

    client = _client(organizer_user)
    draft = body(ctf_event, organizer_user)
    created = client.post(URL, draft, format="json").json()
    provider = configured_authorization_provider()
    check = provider.check

    def remove(request):
        decision = check(request)
        if request.action == "event.manage_communications":
            CTFEvent.all_objects.filter(pk=ctf_event.pk).update(deleted_at=timezone.now())
        return decision

    monkeypatch.setattr(provider, "check", remove)
    result = client.get(URL, {"account": draft["account"]}) if collection else client.get(f"{URL}{created['id']}/")
    if collection:
        assert result.status_code == 200, result.content
        assert result.json()["results"] == []
    else:
        assert result.status_code == 404, result.content
