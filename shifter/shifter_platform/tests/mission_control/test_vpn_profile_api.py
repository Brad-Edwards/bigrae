"""Mission Control OpenVPN delivery security and response-contract tests (#1696)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.utils import timezone
from rest_framework.test import APIClient

from cms.models import RangeInstance
from cms.models import Request as CmsRequest
from engine.models import Range
from engine.models import Request as EngineRequest
from shared.api_tokens import scopes
from shared.api_tokens.models import ApiToken
from shared.audit import AuditAction, AuditEntityType
from shared.enums import RangeSource, RequestType, ResourceStatus
from shared.models import AuditLog
from tests._mission_control_fixtures import native_scopes
from tests.engine.services.conftest import boto3_secrets, make_secrets_client

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("personal_vpn_policy_grant")]
User = get_user_model()

URL = "/api/v1/mission-control/range/vpn-profile/"
PROFILE = (
    "client\ndev tun\nproto udp\nremote vpn.example.test 1194\nnobind\npersist-key\npersist-tun\n"
    "remote-cert-tls server\nverify-x509-name shifter-openvpn-server name\n"
    "auth-nocache\nverb 3\n<ca>\nCA\n</ca>\n<cert>\nCERT\n</cert>\n"
    "<key>\nKEY\n</key>\n<tls-crypt>\nTLS\n</tls-crypt>\n"
)


@pytest.fixture(autouse=True)
def _clear_vpn_rate_limit_cache():
    cache = caches["launch_rate_limit"]
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _trust_engine_certificate_verification(monkeypatch):
    """This API suite exercises delivery; Engine tests own certificate cryptography."""
    monkeypatch.setattr("engine.services._vpn_profile_delivery._verify_profile_material", lambda row, material: None)


@pytest.fixture
def standard_user():
    return User.objects.create_user(username="mc-vpn-user@example.com", email="mc-vpn-user@example.com")


def _token(user, *granted_scopes: str) -> str:
    _token_obj, raw = ApiToken.create_token(name="mc-vpn", created_by=user, scopes=native_scopes(list(granted_scopes)))
    return raw


def _ready_range(user, *, source=RangeSource.MISSION_CONTROL, secret_ref=None):
    from shared.principal_port import principal_for_user
    from shared.remote_access import build_openvpn_capability
    from workspaces.models import Account, AccountMembership
    from workspaces.services import create_account

    principal = principal_for_user(user)
    membership = AccountMembership.objects.filter(principal_uuid=principal.uuid).select_related("account").first()
    if membership is None:
        created = create_account(kind="team", name="Synthetic VPN account")
        account = Account.objects.get(uuid=created.uuid)
        AccountMembership.objects.create(account=account, principal_uuid=principal.uuid)
    else:
        account = membership.account
    request_id = uuid4()
    cms_request = CmsRequest.objects.create(
        owner_principal_uuid=principal.uuid,
        scope_kind="account",
        account_id=account.id,
        request_id=request_id,
        request_type=RequestType.RANGE.value,
        user=user,
    )
    cms_range = RangeInstance.objects.create(
        owner_principal_uuid=principal.uuid,
        scope_kind="account",
        account_id=account.id,
        request=cms_request,
        scenario_id="basic",
        user_id=user.id,
        status=ResourceStatus.READY.value,
        range_source=source.value,
        expires_at=timezone.now() + timedelta(days=30),
        maximum_expires_at=timezone.now() + timedelta(days=365),
    )
    engine_request = EngineRequest.objects.create(
        request_id=request_id,
        request_type=RequestType.RANGE.value,
        user=user,
        owner_principal_uuid=principal.uuid,
        scope_kind="account",
        account_id=account.id,
    )
    target_ref = "provision.node.kali#0"
    engine_range = Range.objects.create(
        owner_principal_uuid=principal.uuid,
        scope_kind="account",
        account_id=account.id,
        cms_range_uuid=cms_range.uuid,
        request=engine_request,
        user=user,
        status=Range.Status.READY,
        resource_generation=uuid4(),
        remote_access_capability=build_openvpn_capability(target_ref, timezone.now() + timedelta(days=30)),
        provisioned_instances=[
            {
                "uuid": target_ref,
                "role": "raes-node",
                "private_ip": "10.10.0.10",
                "participant_access_channels": ["ssh"],
            }
        ],
        vpn_access_binding={
            "endpoint": "vpn.example.test",
            "port": 1194,
            "secret_ref": secret_ref or f"arn:aws:secretsmanager:eu-central-1:123:secret:mc-vpn-{request_id}",
        },
    )
    cms_range.range_id = engine_range.pk
    cms_range.save(update_fields=["range_id"])
    return cms_range


def _seed_ready_profile(user, cms_range, *, secret_ref=None):
    from engine.models import Range, VpnAccessProfile
    from engine.services._vpn_profiles import admit_vpn_profile_authority
    from shared.api_tokens.policy import personal_credential_for_token

    token = ApiToken.objects.get(created_by=user)
    credential = personal_credential_for_token(token)
    runtime = Range.objects.get(cms_range_uuid=cms_range.uuid)
    return VpnAccessProfile.objects.create(
        range=runtime,
        generation=runtime.request.request_id,
        resource_generation=runtime.resource_generation,
        expires_at=timezone.now() + timedelta(days=1),
        state="ready",
        certificate_serial="1234",
        certificate_fingerprint="ab" * 32,
        issuer_fingerprint="cd" * 32,
        secret_ref=secret_ref or runtime.vpn_access_binding["secret_ref"],
        **admit_vpn_profile_authority(credential, runtime.request.request_id),
    )


def test_exact_scope_downloads_owned_mission_control_profile(settings, standard_user):
    settings.CLOUD_PROVIDER = "aws"
    cms_range = _ready_range(standard_user)
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    _seed_ready_profile(standard_user, cms_range)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

    with boto3_secrets(make_secrets_client(PROFILE)):
        response = client.post(URL)

    assert response.status_code == 200
    assert response.content == PROFILE.encode()
    assert response["Content-Type"] == "application/x-openvpn-profile"
    assert response["Content-Disposition"] == 'attachment; filename="bigrae-range.ovpn"'
    assert response["Cache-Control"] == "private, no-store"
    assert "ETag" not in response
    audit = AuditLog.objects.get(
        entity_type=AuditEntityType.CREDENTIAL,
        entity_id=cms_range.pk,
        action=AuditAction.DOWNLOAD,
    )
    assert audit.actor_id is None
    assert audit.actor_principal_uuid == standard_user.identity_principal.uuid
    assert audit.new_state["product"] == "mission_control"
    assert audit.new_state["channel"] == "openvpn"


def test_range_read_scope_does_not_grant_private_key_delivery(standard_user):
    _ready_range(standard_user)
    raw = _token(standard_user, scopes.MISSION_CONTROL_RANGE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

    assert client.post(URL).status_code == 403


def test_session_post_requires_csrf(standard_user):
    _ready_range(standard_user)
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(standard_user)

    assert client.post(URL).status_code == 403


def test_endpoint_selects_mission_control_source_not_same_users_ctf_range(settings, standard_user):
    settings.CLOUD_PROVIDER = "aws"
    _ready_range(standard_user, source=RangeSource.CTF, secret_ref="ctf-secret")
    cms_range = _ready_range(standard_user, source=RangeSource.MISSION_CONTROL, secret_ref="mission-control-secret")
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    _seed_ready_profile(standard_user, cms_range, secret_ref="mission-control-secret")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    secrets = make_secrets_client(PROFILE)

    with boto3_secrets(secrets):
        response = client.post(URL)

    assert response.status_code == 200
    assert secrets.get_secret_value.call_args.kwargs["SecretId"] == "mission-control-secret"


def test_endpoint_is_bodyless_and_non_enumerating(standard_user):
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

    assert client.post(URL, {"range_id": 42}, format="json").status_code == 400
    assert client.post(URL).status_code == 404


def test_policy_revocation_hides_profile_before_secret_resolution(settings, standard_user, monkeypatch):
    from types import SimpleNamespace

    from shared.authorization import AuthorizationDecision, DecisionKind, port

    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(
            check=lambda request: AuthorizationDecision(
                DecisionKind.ALLOWED if request.action == "installation.use_personal_tokens" else DecisionKind.DENIED,
                "policy_allowed" if request.action == "installation.use_personal_tokens" else "policy_denied",
            )
        ),
    )
    settings.CLOUD_PROVIDER = "aws"
    _ready_range(standard_user)
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    secrets = make_secrets_client(PROFILE)

    with boto3_secrets(secrets):
        response = client.post(URL)

    assert response.status_code == 404
    secrets.get_secret_value.assert_not_called()


def test_rate_limit_blocks_before_secret_resolution(settings, standard_user):
    settings.CLOUD_PROVIDER = "aws"
    _ready_range(standard_user)
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    caches["launch_rate_limit"].set(
        f"credential-delivery:{standard_user.identity_principal.uuid}",
        50,
        3600,
    )
    secrets = make_secrets_client(PROFILE)

    with boto3_secrets(secrets):
        response = client.post(URL)

    assert response.status_code == 429
    assert response["Retry-After"] == "3600"
    secrets.get_secret_value.assert_not_called()


def test_rate_limit_backend_failure_fails_closed(monkeypatch, standard_user):
    _ready_range(standard_user)
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

    def fail_add(*args, **kwargs):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(caches["launch_rate_limit"], "add", fail_add)

    response = client.post(URL)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "vpn_profile_unavailable"


def test_profile_conflict_is_reported_without_provider_resolution(settings, standard_user):
    settings.CLOUD_PROVIDER = "aws"
    _ready_range(standard_user)
    Range.objects.filter(user=standard_user).update(status=Range.Status.PROVISIONING)
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    secrets = make_secrets_client(PROFILE)

    with boto3_secrets(secrets):
        response = client.post(URL)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vpn_not_ready"
    secrets.get_secret_value.assert_not_called()


def test_invalid_profile_binding_is_unavailable_without_secret_resolution(settings, standard_user):
    settings.CLOUD_PROVIDER = "aws"
    _ready_range(standard_user)
    Range.objects.filter(user=standard_user).update(vpn_access_binding={})
    raw = _token(standard_user, scopes.MISSION_CONTROL_VPN_PROFILE_READ)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    secrets = make_secrets_client(PROFILE)

    with boto3_secrets(secrets):
        response = client.post(URL)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "vpn_profile_unavailable"
    secrets.get_secret_value.assert_not_called()
