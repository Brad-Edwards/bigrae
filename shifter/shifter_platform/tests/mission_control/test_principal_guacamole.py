"""Guacamole work and handoff retain the originating non-secret authority."""

from uuid import uuid4

import pytest

from tests.cms.services.test_principal_range_authority import principal_range

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]
__all__ = ["principal_range"]


class Gateway:
    def __init__(self):
        self.closed = []
        self.termination_available = True
        self.created = lambda: None

    def create_ssh_url(self, request):
        self.last_identity = request.username
        self.created()
        return "https://example.test/guacamole/#/client/synthetic?token=SYNTHETIC_NATIVE_TOKEN"

    def create_rdp_url(self, request):
        return self.create_ssh_url(request)

    def terminate_session(self, token):
        self.closed.append(token)
        return self.termination_available


def test_gateway_renewal_denies_revoked_original_credential(remote):
    from management.models import ServiceCredentialAdmission
    from mission_control.guacamole_authority import renew_gateway_authority

    fixture, _, _ = remote
    submitted = launch(remote)
    assert renew_gateway_authority(str(submitted.bootstrap_id))
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    assert not renew_gateway_authority(str(submitted.bootstrap_id))


def test_signed_json_identity_binds_only_the_server_created_bootstrap(remote):
    _, _, gateway = remote
    submitted = launch(remote)
    assert gateway.last_identity == "access-" + str(submitted.bootstrap_id)


def test_gateway_renewal_denies_unknown_binding_and_pending_termination(remote):
    from mission_control.guacamole_authority import renew_gateway_authority
    from mission_control.models import GuacamoleBootstrapRequest

    submitted = launch(remote)
    assert not renew_gateway_authority(str(uuid4()))
    GuacamoleBootstrapRequest.objects.filter(pk=submitted.bootstrap_id).update(termination_pending=True)
    assert not renew_gateway_authority(str(submitted.bootstrap_id))


def test_gateway_renewal_allows_running_admission_but_denies_failed_admission(remote):
    from django.utils import timezone

    from mission_control.guacamole_authority import renew_gateway_authority
    from mission_control.models import GuacamoleBootstrapRequest

    submitted = launch(remote)
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    row.status = "running"
    row.native_session_token = ""
    row.save(update_fields=["status", "native_session_token"])
    assert renew_gateway_authority(str(row.id))
    GuacamoleBootstrapRequest.objects.filter(pk=row.id).update(expires_at=timezone.now())
    assert not renew_gateway_authority(str(row.id))


@pytest.fixture
def remote(principal_range, settings, monkeypatch):
    settings.GUACAMOLE_BOOTSTRAP_INLINE = True
    target = "provision.node.client#0"
    principal_range.realized.provisioned_instances = [
        {
            "uuid": target,
            "os_type": "linux",
            "private_ip": "192.0.2.10",
            "ssh_username": "ubuntu",
            "ssh_key_secret_id": "synthetic-key-reference",
            "participant_access_channels": ["ssh"],
        }
    ]
    principal_range.realized.save(update_fields=["provisioned_instances"])
    monkeypatch.setattr("engine.secrets.get_ssh_key", lambda reference: "SYNTHETIC_TEST_KEY")
    return principal_range, target, Gateway()


def launch(remote):
    from mission_control.guacamole_session import launch_guacamole_session

    fixture, target, gateway = remote
    return launch_guacamole_session(
        user=fixture.credential, protocol="range_ssh", target_id=target, guacamole_client=gateway
    )


def test_bootstrap_retains_exact_range_context_when_raes_member_keys_repeat(remote):
    from cms.models import RangeInstance
    from cms.models import Request as CmsRequest
    from cms.services import admit_range_remote_authority, get_range_ssh_connection_info
    from engine.models import Range
    from engine.models import Request as EngineRequest
    from management.services import create_service_principal
    from mission_control.guacamole_authority import renew_gateway_authority
    from mission_control.guacamole_session import launch_guacamole_session
    from workspaces.services import create_account

    actor, member, gateway = remote
    foreign = create_account(kind="team", name="Synthetic sibling remote account")
    request_uuid = uuid4()
    foreign_owner = create_service_principal("Synthetic sibling range owner")
    fields = {"owner_principal_uuid": foreign_owner.uuid, "scope_kind": "account", "account_id": foreign.id}
    request = CmsRequest.objects.create(request_id=request_uuid, request_type="range", **fields)
    leaf = RangeInstance.objects.create(request=request, scenario_id="synthetic", status="ready", **fields)
    native_request = EngineRequest.objects.create(
        request_id=request_uuid, request_type="range", owner_principal_uuid=foreign_owner.uuid
    )
    native = Range.objects.create(
        request=native_request,
        cms_range_uuid=leaf.uuid,
        status="ready",
        range_backend="gce",
        resource_generation=uuid4(),
        provisioned_instances=actor.realized.provisioned_instances,
        **fields,
    )
    leaf.range_id = native.pk
    leaf.save(update_fields=["range_id"])
    with pytest.raises(PermissionError):
        admit_range_remote_authority(actor.credential, member)
    with pytest.raises(PermissionError):
        admit_range_remote_authority(actor.credential, member, range_uuid=leaf.uuid)
    selected = actor.instance.uuid
    assert get_range_ssh_connection_info(actor.credential, member, range_uuid=selected)["host"] == "192.0.2.10"
    submitted = launch_guacamole_session(
        user=actor.credential,
        protocol="range_ssh",
        target_id=member,
        range_uuid=selected,
        guacamole_client=gateway,
    )
    assert submitted.status == "succeeded"
    assert renew_gateway_authority(str(submitted.bootstrap_id))


def test_native_member_key_supports_terminal_and_rdp_service_access(remote, settings):
    from cms.services import connect_range_terminal, get_range_rdp_connection_info
    from mission_control.guacamole_authority import renew_gateway_authority
    from mission_control.guacamole_session import launch_guacamole_session
    from tests.engine.services.conftest import boto3_secrets, make_secrets_client

    actor, member, gateway = remote
    terminal = connect_range_terminal(actor.credential, member, range_uuid=actor.instance.uuid)
    assert terminal.host == "192.0.2.10"
    settings.CLOUD_PROVIDER = "aws"
    actor.realized.provisioned_instances[0].update(
        os_type="windows",
        ssh_username="Administrator",
        participant_access_channels=["rdp"],
        rdp_password_secret_arn="arn:aws:secretsmanager:us-east-2:1:secret:synthetic-rdp",
        participant_sftp_enabled=False,
    )
    actor.realized.save(update_fields=["provisioned_instances"])
    with boto3_secrets(make_secrets_client(value="SYNTHETIC_GUEST_PASSWORD")):
        info = get_range_rdp_connection_info(actor.credential, member, range_uuid=actor.instance.uuid)
        assert info["rdp_password"] == "SYNTHETIC_GUEST_PASSWORD"
        submitted = launch_guacamole_session(
            user=actor.credential,
            protocol="rdp",
            target_id=member,
            range_uuid=actor.instance.uuid,
            guacamole_client=gateway,
        )
    assert submitted.status == "succeeded"
    assert renew_gateway_authority(str(submitted.bootstrap_id))


def test_principal_bootstrap_delivers_once_and_retains_encrypted_termination_reference(remote):
    from django.db import connection

    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, _ = remote
    submitted = launch(remote)
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.status == "succeeded"
    assert row.user_id is None
    assert row.initiating_principal_uuid == fixture.credential.principal.uuid
    assert row.credential_uuid == fixture.credential.credential_uuid
    url = consume_ready_url(request_id=row.id, credential=fixture.credential)
    assert "SYNTHETIC_NATIVE_TOKEN" in url
    assert consume_ready_url(request_id=row.id, credential=fixture.credential) is None
    row.refresh_from_db()
    assert row.result_url == ""
    assert row.native_session_token == "SYNTHETIC_NATIVE_TOKEN"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT native_session_token FROM mission_control_guacamolebootstraprequest WHERE id = %s", [row.id]
        )
        assert "SYNTHETIC_NATIVE_TOKEN" not in str(cursor.fetchone())


def test_revocation_blocks_handoff_and_closes_native_session_even_if_browser_is_idle(remote):
    from management.models import ServiceCredentialAdmission
    from mission_control.guacamole_authority import revalidate_guacamole_sessions
    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, gateway = remote
    submitted = launch(remote)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    assert consume_ready_url(request_id=submitted.bootstrap_id, credential=fixture.credential) is None
    assert revalidate_guacamole_sessions(client=gateway) == 1
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.native_terminated_at is not None
    assert row.native_session_token == ""
    assert gateway.closed == ["SYNTHETIC_NATIVE_TOKEN"]


def test_gateway_failure_retains_private_reference_and_retries_cleanup(remote):
    from django.utils import timezone

    from management.models import ServiceCredentialAdmission
    from mission_control.guacamole_authority import revalidate_guacamole_sessions
    from mission_control.guacamole_bootstrap import prune_expired_bootstrap_requests
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, gateway = remote
    submitted = launch(remote)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    gateway.termination_available = False
    GuacamoleBootstrapRequest.objects.filter(pk=submitted.bootstrap_id).update(expires_at=timezone.now())
    assert revalidate_guacamole_sessions(client=gateway) == 0
    assert prune_expired_bootstrap_requests() == 0
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.native_session_token and row.native_terminated_at is None
    gateway.termination_available = True
    assert revalidate_guacamole_sessions(client=gateway) == 1
    assert prune_expired_bootstrap_requests() == 1


def test_expired_undelivered_success_retires_without_credential_revocation(remote):
    from django.utils import timezone

    from mission_control.guacamole_authority import (
        authorize_bootstrap,
        renew_gateway_authority,
        revalidate_guacamole_sessions,
    )
    from mission_control.guacamole_bootstrap import consume_ready_url, prune_expired_bootstrap_requests
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, gateway = remote
    submitted = launch(remote)
    GuacamoleBootstrapRequest.objects.filter(pk=submitted.bootstrap_id).update(expires_at=timezone.now())
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.status == "succeeded" and row.delivered_at is None
    assert authorize_bootstrap(row)  # The original authority is still valid.
    assert not renew_gateway_authority(str(row.id))
    assert consume_ready_url(request_id=row.id, credential=fixture.credential) is None
    gateway.termination_available = False
    assert revalidate_guacamole_sessions(client=gateway) == 0
    row.refresh_from_db()
    assert row.termination_pending and row.native_session_token
    assert prune_expired_bootstrap_requests() == 0
    gateway.termination_available = True
    assert revalidate_guacamole_sessions(client=gateway) == 1
    assert gateway.closed == ["SYNTHETIC_NATIVE_TOKEN", "SYNTHETIC_NATIVE_TOKEN"]
    assert prune_expired_bootstrap_requests() == 1


def test_delivered_session_remains_authorized_after_bootstrap_expiry(remote):
    from django.utils import timezone

    from mission_control.guacamole_authority import renew_gateway_authority, revalidate_guacamole_sessions
    from mission_control.guacamole_bootstrap import consume_ready_url, prune_expired_bootstrap_requests
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, gateway = remote
    submitted = launch(remote)
    assert consume_ready_url(request_id=submitted.bootstrap_id, credential=fixture.credential)
    GuacamoleBootstrapRequest.objects.filter(pk=submitted.bootstrap_id).update(expires_at=timezone.now())
    assert renew_gateway_authority(str(submitted.bootstrap_id))
    assert revalidate_guacamole_sessions(client=gateway) == 0
    assert prune_expired_bootstrap_requests() == 0
    assert gateway.closed == []


def test_expiry_cleanup_does_not_retire_a_concurrently_delivered_session(remote):
    from django.utils import timezone

    from mission_control.guacamole_authority import terminate_bootstrap
    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, gateway = remote
    submitted = launch(remote)
    stale = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert consume_ready_url(request_id=submitted.bootstrap_id, credential=fixture.credential)
    GuacamoleBootstrapRequest.objects.filter(pk=submitted.bootstrap_id).update(expires_at=timezone.now())
    assert not terminate_bootstrap(stale, gateway, only_undelivered=True)
    assert gateway.closed == []
    stale.refresh_from_db()
    assert stale.delivered_at is not None and not stale.termination_pending and stale.native_session_token


def test_native_absence_reconciles_a_lost_database_acknowledgement(remote):
    from django.db import DatabaseError, connection
    from django.utils import timezone

    from management.models import ServiceCredentialAdmission
    from mission_control.guacamole_authority import revalidate_guacamole_sessions
    from mission_control.guacamole_bootstrap import prune_expired_bootstrap_requests
    from mission_control.models import GuacamoleBootstrapRequest
    from tests.mission_control.test_guacamole_termination import client, server

    fixture, _, _ = remote
    submitted = launch(remote)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    GuacamoleBootstrapRequest.objects.filter(pk=submitted.bootstrap_id).update(expires_at=timezone.now())

    def lose_acknowledgement(execute, sql, params, many, context):
        if sql.startswith("UPDATE") and '"native_terminated_at" =' in sql:
            raise DatabaseError("Synthetic termination acknowledgement outage")
        return execute(sql, params, many, context)

    with server() as (endpoint, _), connection.execute_wrapper(lose_acknowledgement), pytest.raises(DatabaseError):
        revalidate_guacamole_sessions(client=client(endpoint))
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.termination_pending and row.native_session_token and row.native_terminated_at is None
    with server(403, body={"type": "PERMISSION_DENIED", "message": "Permission Denied."}) as (endpoint, _):
        assert revalidate_guacamole_sessions(client=client(endpoint)) == 1
    row.refresh_from_db()
    assert not row.termination_pending and not row.native_session_token and row.native_terminated_at is not None
    assert prune_expired_bootstrap_requests() == 1


def test_revocation_during_token_creation_closes_session_without_delivery(remote):
    from management.models import ServiceCredentialAdmission
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, _, gateway = remote
    gateway.created = lambda: ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(
        is_active=False
    )
    submitted = launch(remote)
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.status == "failed" and row.result_url == ""
    assert row.native_terminated_at is not None
    assert gateway.closed == ["SYNTHETIC_NATIVE_TOKEN"]


def test_native_cleanup_survives_signing_secret_removal(remote, settings):
    from management.models import ServiceCredentialAdmission
    from mission_control.guacamole_authority import revalidate_guacamole_sessions
    from tests.mission_control.test_guacamole_termination import server

    fixture, _, _ = remote
    launch(remote)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    settings.GUACAMOLE_JSON_AUTH_SECRET = ""
    with server() as (endpoint, received):
        settings.GUACAMOLE_API_BASE_URL = endpoint
        assert revalidate_guacamole_sessions() == 1
    assert received == [("/guacamole/api/session", "SYNTHETIC_NATIVE_TOKEN")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_uuid", uuid4()),
        ("credential_ceiling_actions", "range.manage"),
        ("authority_binding_digest", "sha256:" + "0" * 64),
        ("target_id", str(uuid4())),
    ],
)
def test_persisted_remote_authority_is_immutable(remote, field, value):
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from cms.services import admit_range_remote_authority
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, target, _ = remote
    fields = admit_range_remote_authority(fixture.credential, target)
    row = GuacamoleBootstrapRequest.objects.create(
        protocol="range_ssh", target_id=target, expires_at=timezone.now(), **fields
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        GuacamoleBootstrapRequest.objects.filter(pk=row.id).update(**{field: value})


@pytest.mark.django_db
def test_prune_preserves_inflight_source_bootstrap_for_late_native_cleanup(remote):
    from django.utils import timezone

    from cms.services import admit_range_remote_authority
    from mission_control.guacamole_bootstrap import prune_expired_bootstrap_requests
    from mission_control.models import GuacamoleBootstrapRequest

    fixture, target, _ = remote
    row = GuacamoleBootstrapRequest.objects.create(
        protocol="range_ssh",
        target_id=target,
        status="running",
        expires_at=timezone.now(),
        **admit_range_remote_authority(fixture.credential, target),
    )
    assert prune_expired_bootstrap_requests() == 0
    assert GuacamoleBootstrapRequest.objects.filter(pk=row.id).exists()
