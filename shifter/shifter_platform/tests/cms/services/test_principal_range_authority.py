"""Owning range services validate both projections before a live policy check."""

import asyncio
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cms.exceptions import CMSError
from shared.authorization import AuthorizationDecision, CredentialCeiling, DecisionKind, TargetRef
from shared.credentials import CredentialContext

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def principal_range(settings, monkeypatch, django_user_model):
    from cms.models import RangeInstance
    from cms.models import Request as CmsRequest
    from engine.models import Range
    from engine.models import Request as EngineRequest
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, principal_for_user, resolve_service_credential
    from shared.authorization import port
    from workspaces.services import create_account

    settings.ENGINE_TASK_CLUSTER = "synthetic-provisioner"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic-provisioner@sha256:" + "1" * 64
    account_owner = django_user_model.objects.create_user(username="synthetic-account-owner")
    account = create_account(kind="individual", name="Synthetic range account", owner=principal_for_user(account_owner))
    owner = create_service_principal("Independent range owner")
    administrator = create_service_principal("Range administrator")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=administrator.uuid),
        issuer="https://accounts.google.com",
        subject=str(uuid4()),
    )
    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://example.test/shifter"
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["authorization:range.read", "authorization:range.manage", "authorization:range.access"],
    )
    credential = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)
    request_uuid = uuid4()
    cms_request = CmsRequest.objects.create(
        request_id=request_uuid,
        request_type="range",
        user=None,
        owner_principal_uuid=owner.uuid,
        scope_kind="account",
        account_id=account.id,
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        user_id=None,
        owner_principal_uuid=owner.uuid,
        scope_kind="account",
        account_id=account.id,
        scenario_id="synthetic",
        status="ready",
    )
    engine_request = EngineRequest.objects.create(
        request_id=request_uuid,
        request_type="range",
        user=None,
        owner_principal_uuid=owner.uuid,
    )
    realized = Range.objects.create(
        request=engine_request,
        user=None,
        owner_principal_uuid=owner.uuid,
        cms_range_uuid=instance.uuid,
        resource_generation=uuid4(),
        range_backend="gce",
        scope_kind="account",
        account_id=account.id,
        status="ready",
    )
    instance.range_id = realized.id
    instance.save(update_fields=["range_id"])
    seen = []

    def check(request):
        seen.append(request)
        allowed = request.scope.account_uuid == account.uuid
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED, "policy_allowed" if allowed else "policy_denied"
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    return SimpleNamespace(
        account=account,
        owner=owner,
        credential=credential,
        instance=instance,
        realized=realized,
        request=cms_request,
        seen=seen,
    )


def test_service_administrator_uses_public_range_identity_and_sql_account_scope(principal_range):
    from cms.services import require_range_action

    fixture = principal_range
    for action in ("range.read", "range.manage", "range.access"):
        instance = require_range_action(fixture.credential, fixture.instance.uuid, action)
        assert instance.uuid == fixture.instance.uuid
        assert fixture.seen[-1].principal == fixture.credential.principal
        assert fixture.seen[-1].principal != fixture.owner
        assert fixture.seen[-1].target == TargetRef("range", fixture.instance.uuid)
        assert fixture.seen[-1].scope.account_uuid == fixture.account.uuid
        assert fixture.seen[-1].scope.workspace_uuid is None
        assert fixture.seen[-1].parent == TargetRef("account", fixture.account.uuid)
    assert fixture.realized.uuid != fixture.instance.uuid
    with pytest.raises(CMSError):
        require_range_action(fixture.credential, fixture.realized.uuid, "range.read")


@pytest.mark.parametrize("stale", ["deleted", "actor-disabled"])
def test_stale_range_custody_is_denied_before_policy(principal_range, stale):
    from django.utils import timezone

    from cms.services import require_range_action
    from management.models import Principal

    if stale == "deleted":
        principal_range.instance.deleted_at = timezone.now()
        principal_range.instance.save(update_fields=["deleted_at"])
    else:
        Principal.objects.filter(uuid=principal_range.credential.principal.uuid).update(is_active=False)
    with pytest.raises(CMSError):
        require_range_action(principal_range.credential, principal_range.instance.uuid, "range.access")
    assert not principal_range.seen


def test_current_service_administrator_can_manage_a_departed_owners_range(principal_range):
    from cms.services import require_range_action
    from management.models import Principal

    Principal.objects.filter(uuid=principal_range.owner.uuid).update(is_active=False)
    assert (
        require_range_action(principal_range.credential, principal_range.instance.uuid, "range.manage").uuid
        == principal_range.instance.uuid
    )


def test_direct_range_queries_use_policy_and_exact_customer_scope(principal_range):
    from cms.services import get_range, list_ranges
    from shared.identity_scope import ResourceScope

    fixture = principal_range
    rows = list_ranges(fixture.credential, scope=ResourceScope("account", fixture.account.uuid))
    assert [row.uuid for row in rows] == [fixture.instance.uuid]
    assert get_range(fixture.credential, fixture.realized.id).uuid == fixture.instance.uuid
    narrow = CredentialContext(
        fixture.credential.principal,
        fixture.credential.kind,
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"range.manage"})),
        fixture.credential.scopes,
    )
    assert list_ranges(narrow, scope=ResourceScope("account", fixture.account.uuid)) == []
    with pytest.raises(CMSError):
        get_range(narrow, fixture.realized.id)


def test_source_request_status_projects_principal_ownership_and_rechecks_read(principal_range):
    from cms.services import get_range_by_request_id

    fixture = principal_range
    result = get_range_by_request_id(fixture.credential, str(fixture.request.request_id))
    assert result.user_id is None
    assert result.owner_principal_uuid == fixture.owner.uuid
    assert result.request_id == fixture.request.request_id
    narrow = CredentialContext(
        fixture.credential.principal,
        fixture.credential.kind,
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"range.manage"})),
        fixture.credential.scopes,
    )
    with pytest.raises(CMSError):
        get_range_by_request_id(narrow, str(fixture.request.request_id))


def test_source_active_and_history_queries_preserve_scope_and_terminal_reads(principal_range):
    from cms.services import (
        get_active_range,
        get_range_by_request_id,
        has_ready_active_range,
        list_mission_control_range_history,
        require_range_action,
    )
    from shared.identity_scope import ResourceScope

    fixture = principal_range
    scope = ResourceScope("account", fixture.account.uuid)
    assert get_active_range(fixture.credential, scope=scope).request_id == fixture.request.request_id
    assert has_ready_active_range(fixture.credential, scope=scope)
    fixture.instance.status = "failed"
    fixture.instance.save(update_fields=["status"])
    assert get_active_range(fixture.credential, scope=scope) is None
    assert not has_ready_active_range(fixture.credential, scope=scope)
    assert [row.uuid for row in list_mission_control_range_history(fixture.credential, scope=scope)] == [
        fixture.instance.uuid
    ]
    assert get_range_by_request_id(
        fixture.credential, str(fixture.request.request_id), include_terminal=True
    ).is_terminal
    with pytest.raises(CMSError):
        require_range_action(fixture.credential, fixture.instance.uuid, "range.access", include_terminal=True)
    with pytest.raises(CMSError):
        get_range_by_request_id(fixture.credential, str(fixture.request.request_id))


@pytest.mark.parametrize("projection", ["request", "realized"])
def test_stale_scope_and_owner_projections_never_rehome_or_authorize(principal_range, projection, django_user_model):
    from cms.services import require_range_action
    from management.services import principal_for_user
    from workspaces.services import create_account

    fixture = principal_range
    other = create_account(
        kind="individual",
        name="Sibling account",
        owner=principal_for_user(django_user_model.objects.create_user(username="sibling-account-owner")),
    )
    row = getattr(fixture, projection)
    row.account_id = other.id
    row.save(update_fields=["account_id"])
    with pytest.raises(CMSError):
        require_range_action(fixture.credential, fixture.instance.uuid, "range.manage")
    assert not fixture.seen


def test_exact_credential_ceiling_and_revoked_principal_deny_direct_service_calls(principal_range):
    from cms.services import require_range_action
    from management.models import Principal

    fixture = principal_range
    read = CredentialContext(
        fixture.credential.principal,
        "service",
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"range.read"}), TargetRef("range", fixture.instance.uuid)),
        fixture.credential.scopes,
    )
    with pytest.raises(CMSError):
        require_range_action(read, fixture.instance.uuid, "range.access")
    assert not fixture.seen
    Principal.objects.filter(uuid=fixture.credential.principal.uuid).update(is_active=False)
    with pytest.raises(CMSError):
        require_range_action(read, fixture.instance.uuid, "range.read")
    assert not fixture.seen


@pytest.mark.postgres
def test_service_administrator_opens_real_terminal_service_without_owner_impersonation(principal_range, monkeypatch):
    from cms.services import connect_range_terminal
    from engine.services import get_ssh_connection_info

    fixture = principal_range
    guest_uuid = str(uuid4())
    fixture.realized.provisioned_instances = [
        {
            "uuid": guest_uuid,
            "os_type": "linux",
            "private_ip": "192.0.2.10",
            "ssh_username": "ubuntu",
            "ssh_key_secret_id": "synthetic-key-reference",
            "participant_access_channels": ["ssh"],
        }
    ]
    fixture.realized.save(update_fields=["provisioned_instances"])
    keys = []
    monkeypatch.setattr("engine.secrets.get_ssh_key", lambda reference: keys.append(reference) or "SYNTHETIC_TEST_KEY")
    captured = []
    transport = object()

    def factory(**facts):
        captured.append(facts)
        return transport

    assert connect_range_terminal(fixture.credential, guest_uuid, connection_factory=factory) is transport
    assert captured[-1]["host"] == "192.0.2.10"
    assert keys == ["synthetic-key-reference"]
    assert get_ssh_connection_info(fixture.credential, guest_uuid)["host"] == "192.0.2.10"
    read = CredentialContext(
        fixture.credential.principal,
        "service",
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"range.read"})),
        fixture.credential.scopes,
    )
    count = len(keys)
    with pytest.raises((CMSError, ValueError, PermissionError)):
        connect_range_terminal(read, guest_uuid, connection_factory=factory)
    assert len(keys) == count


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_idle_service_terminal_revalidation_closes_transport_and_releases_capacity(principal_range, monkeypatch):
    from channels.db import database_sync_to_async
    from channels.testing import WebsocketCommunicator

    from config.websocket_auth import CredentialSessionWebSocketBoundary
    from mission_control.consumers import SSHConsumer
    from mission_control.terminal_sessions import session_registry
    from shared.authorization import port

    fixture = principal_range
    guest_uuid = str(uuid4())
    fixture.realized.provisioned_instances = [
        {
            "uuid": guest_uuid,
            "os_type": "linux",
            "private_ip": "192.0.2.10",
            "ssh_username": "ubuntu",
            "ssh_key_secret_id": "synthetic-key-reference",
            "participant_access_channels": ["ssh"],
        }
    ]
    await database_sync_to_async(fixture.realized.save)(update_fields=["provisioned_instances"])
    monkeypatch.setattr("engine.secrets.get_ssh_key", lambda reference: "SYNTHETIC_TEST_KEY")

    class Terminal:
        is_connected = False
        disconnected = 0

        async def connect(self):
            self.is_connected = True

        async def disconnect(self):
            self.is_connected = False
            self.disconnected += 1

        async def receive(self, timeout):
            await asyncio.Event().wait()

    terminal = Terminal()

    application = CredentialSessionWebSocketBoundary(
        SSHConsumer.as_asgi(connection_factory=lambda **kwargs: terminal),
        recheck_seconds=0.02,
        timeout_seconds=0.1,
    )
    communicator = WebsocketCommunicator(application, f"/ws/terminal/{guest_uuid}/")
    communicator.scope.update(
        {"credential_context": fixture.credential, "url_route": {"kwargs": {"instance_uuid": guest_uuid}}}
    )
    baseline = session_registry.snapshot()["active_sessions"]
    try:
        connected, _ = await communicator.connect()
        assert connected
        assert session_registry.snapshot()["active_sessions"] == baseline + 1
        monkeypatch.setattr(
            port,
            "_provider_factory",
            lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied")),
        )
        started = monotonic()
        assert await communicator.receive_output(timeout=0.5) == {"type": "websocket.close", "code": 4001}
        await communicator.wait(timeout=0.5)
        assert not terminal.is_connected
        assert terminal.disconnected == 1
        assert session_registry.snapshot()["active_sessions"] == baseline
        assert monotonic() - started < 0.5
    finally:
        await communicator.disconnect()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_source_range_status_stream_rechecks_policy_and_discards_subscription(principal_range, monkeypatch):
    from channels.layers import get_channel_layer
    from channels.testing import WebsocketCommunicator

    from config.websocket_auth import CredentialSessionWebSocketBoundary
    from mission_control.consumers import RangeStatusConsumer
    from shared.authorization import port
    from shared.channels.groups import range_event_group

    request_id = str(principal_range.request.request_id)
    application = CredentialSessionWebSocketBoundary(
        RangeStatusConsumer.as_asgi(), recheck_seconds=0.02, timeout_seconds=0.2
    )
    communicator = WebsocketCommunicator(application, f"/ws/range-status/{request_id}/")
    communicator.scope.update(
        {"credential_context": principal_range.credential, "url_route": {"kwargs": {"request_id": request_id}}}
    )
    group = range_event_group(request_id)
    layer = get_channel_layer()
    try:
        assert (await communicator.connect())[0]
        assert (await communicator.receive_json_from())["status"] == "ready"
        assert layer.groups.get(group)
        monkeypatch.setattr(
            port,
            "_provider_factory",
            lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied")),
        )
        assert await communicator.receive_output(timeout=0.5) == {"type": "websocket.close", "code": 4001}
        await communicator.wait(timeout=0.5)
        assert not layer.groups.get(group)
    finally:
        communicator.stop()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_revocation_during_terminal_connect_cancels_work_and_releases_capacity(principal_range, monkeypatch):
    from channels.db import database_sync_to_async
    from channels.testing import WebsocketCommunicator

    from config.websocket_auth import CredentialSessionWebSocketBoundary
    from mission_control.consumers import SSHConsumer
    from mission_control.terminal_sessions import session_registry
    from shared.authorization import port

    fixture = principal_range
    guest = str(uuid4())
    fixture.realized.provisioned_instances = [
        {
            "uuid": guest,
            "os_type": "linux",
            "private_ip": "192.0.2.10",
            "ssh_username": "ubuntu",
            "ssh_key_secret_id": "synthetic-key-reference",
            "participant_access_channels": ["ssh"],
        }
    ]
    await database_sync_to_async(fixture.realized.save)(update_fields=["provisioned_instances"])
    monkeypatch.setattr("engine.secrets.get_ssh_key", lambda reference: "SYNTHETIC_TEST_KEY")
    monkeypatch.setattr("config.session_credentials.validate_socket_session", lambda scope, **kwargs: True)

    class Terminal:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = False
            self.closed = 0

        async def connect(self):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def disconnect(self):
            self.closed += 1

    terminal = Terminal()

    async def authorize(scope):
        from cms.services._range_access import _authorize_instance_access

        await database_sync_to_async(_authorize_instance_access)(scope["user"], guest)
        return True

    boundary = CredentialSessionWebSocketBoundary(
        SSHConsumer.as_asgi(connection_factory=lambda **kw: terminal),
        resource_authorizer=authorize,
        recheck_seconds=0.02,
        timeout_seconds=0.5,
    )
    communicator = WebsocketCommunicator(boundary, f"/ws/terminal/{guest}/")
    communicator.scope.update({"user": fixture.credential, "url_route": {"kwargs": {"instance_uuid": guest}}})
    baseline = session_registry.snapshot()["active_sessions"]
    connection = asyncio.create_task(communicator.connect())
    try:
        await asyncio.wait_for(terminal.started.wait(), 3)
        monkeypatch.setattr(
            port,
            "_provider_factory",
            lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied")),
        )
        started = monotonic()
        assert (await connection)[0] is False
        await communicator.wait(timeout=0.5)
        assert terminal.cancelled and terminal.closed == 1
        assert session_registry.snapshot()["active_sessions"] == baseline
        assert monotonic() - started < 0.3
    finally:
        communicator.stop()


def test_queued_range_effect_revalidates_initiator_and_preserves_original_ceiling(principal_range):
    from django.utils import timezone

    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import ProvisionerLaunchIntent
    from engine.services._launch_authority import admit_launch_authority
    from management.models import Principal

    fixture = principal_range
    operation_id = uuid4()
    fixture.realized.status = "resuming"
    fixture.realized.provisioner_operation_id = operation_id
    fixture.realized.save(update_fields=["status", "provisioner_operation_id"])
    payload = {
        "version": 1,
        "resource": "raes-range",
        "operation": "resume",
        "request_id": str(fixture.request.request_id),
    }
    fields = admit_launch_authority(payload, fixture.credential)
    queued = ProvisionerLaunchIntent.objects.create(
        operation_id=operation_id,
        idempotency_key=str(uuid4()),
        payload=payload,
        next_attempt_at=timezone.now(),
        **fields,
    )
    assert queued.initiating_principal_uuid == fixture.credential.principal.uuid
    assert queued.authority_owner_uuid == fixture.owner.uuid
    assert queued.credential_uuid == fixture.credential.credential_uuid
    from engine.operation_inputs import materialize_operation_input

    materialize_operation_input(payload, operation_id, fixture.realized)
    authorize_provisioner_payload(payload, expected_operation_id=operation_id)
    Principal.objects.filter(uuid=fixture.credential.principal.uuid).update(is_active=False)
    with pytest.raises((ValueError, PermissionError)):
        authorize_provisioner_payload(payload, expected_operation_id=operation_id)


def test_principal_owned_enqueue_cannot_omit_initiating_authority(principal_range):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import OperationInput, ProvisionerLaunchIntent

    fixture = principal_range
    fixture.realized.status = "resuming"
    fixture.realized.save(update_fields=["status"])
    command = ["raes-range", "resume", "--request-id", str(fixture.request.request_id)]
    with pytest.raises(PermissionError, match="Operation authority unavailable"):
        enqueue_provisioner_launch(command)
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()
    fixture.realized.refresh_from_db()
    assert fixture.realized.provisioner_operation_id is None


def test_dispatch_rejects_operation_input_that_loses_its_original_authority(principal_range):
    from engine.launch_intents import authorize_provisioner_payload, enqueue_provisioner_launch
    from engine.models import OperationInput, ProvisionerLaunchIntent

    fixture = principal_range
    fixture.realized.status = "resuming"
    fixture.realized.save(update_fields=["status"])
    identifier = enqueue_provisioner_launch(
        ["range", "resume", "--request-id", str(fixture.request.request_id)], credential=fixture.credential
    )
    intent = ProvisionerLaunchIntent.objects.get(intent_id=identifier)
    row = OperationInput.objects.get(operation_id=intent.operation_id)
    changed = dict(row.envelope, authority_digest="sha256:" + "0" * 64)
    OperationInput.objects.filter(pk=row.pk).update(envelope=changed)
    with pytest.raises(ValueError):
        authorize_provisioner_payload(intent.payload, expected_operation_id=intent.operation_id)


def test_rejected_dispatch_still_records_failure_for_its_current_generation(principal_range):
    from django.db import transaction

    from engine.launch_intents import enqueue_provisioner_launch, fail_current_provisioner_operation
    from engine.models import OperationInput, ProvisionerLaunchIntent

    fixture = principal_range
    fixture.realized.status = "resuming"
    fixture.realized.save(update_fields=["status"])
    identifier = enqueue_provisioner_launch(
        ["range", "resume", "--request-id", str(fixture.request.request_id)], credential=fixture.credential
    )
    intent = ProvisionerLaunchIntent.objects.get(intent_id=identifier)
    OperationInput.objects.filter(operation_id=intent.operation_id).update(envelope={})
    with transaction.atomic():
        fail_current_provisioner_operation(intent.payload, intent.operation_id)
    fixture.realized.refresh_from_db()
    assert fixture.realized.status == "failed"
    assert fixture.realized.provisioner_operation_id is None


def test_native_enqueue_binds_original_authority_and_equal_replay(principal_range):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import OperationInput, ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    fixture = principal_range
    fixture.realized.status = "resuming"
    fixture.realized.save(update_fields=["status"])
    exact = CredentialContext(
        fixture.credential.principal,
        "service",
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"range.manage"}), TargetRef("range", fixture.instance.uuid)),
        fixture.credential.scopes,
    )
    command = ["raes-range", "resume", "--request-id", str(fixture.request.request_id)]
    identifier = enqueue_provisioner_launch(command, credential=exact)
    queued = ProvisionerLaunchIntent.objects.get(intent_id=identifier)
    assert queued.credential_ceiling_actions == "range.manage"
    assert queued.credential_target_uuid == fixture.instance.uuid
    assert queued.initiating_principal_uuid == fixture.credential.principal.uuid
    snapshot = OperationInput.objects.get(operation_id=queued.operation_id)
    assert snapshot.envelope["contract_version"] == "2"
    assert snapshot.envelope["authority_digest"].startswith("sha256:")
    assert enqueue_provisioner_launch(command, credential=exact) == identifier
    with pytest.raises(ValueError):
        enqueue_provisioner_launch(command, credential=fixture.credential)
    ServiceCredentialAdmission.objects.filter(uuid=exact.credential_uuid).update(is_active=False)
    with pytest.raises((ValueError, PermissionError)):
        enqueue_provisioner_launch(command, credential=exact)
    assert ProvisionerLaunchIntent.objects.count() == 1


@pytest.mark.postgres
@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_uuid", uuid4()),
        ("initiating_principal_uuid", uuid4()),
        ("credential_ceiling_actions", "range.read\nrange.manage"),
        ("authority_kind", "cleanup"),
        ("payload", {"version": 1, "resource": "raes-range", "operation": "destroy"}),
    ],
)
def test_durable_authority_cannot_be_rewritten_by_bulk_updates(principal_range, field, value):
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from engine.models import ProvisionerLaunchIntent
    from engine.services._launch_authority import admit_launch_authority

    fixture = principal_range
    payload = {
        "version": 1,
        "resource": "raes-range",
        "operation": "resume",
        "request_id": str(fixture.request.request_id),
    }
    queued = ProvisionerLaunchIntent.objects.create(
        operation_id=uuid4(),
        idempotency_key=str(uuid4()),
        payload=payload,
        next_attempt_at=timezone.now(),
        **admit_launch_authority(payload, fixture.credential),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ProvisionerLaunchIntent.objects.filter(pk=queued.pk).update(**{field: value})
    queued.refresh_from_db()
    assert queued.credential_uuid == fixture.credential.credential_uuid


def test_explicit_cleanup_outlives_initiator_but_cannot_launch_or_change_resource_generation(principal_range):
    from engine.launch_intents import authorize_provisioner_payload, enqueue_provisioner_launch
    from engine.models import ProvisionerLaunchIntent
    from management.models import Principal

    fixture = principal_range
    fixture.realized.status = "resuming"
    fixture.realized.save(update_fields=["status"])
    resume = ["raes-range", "resume", "--request-id", str(fixture.request.request_id)]
    original_id = enqueue_provisioner_launch(resume, credential=fixture.credential)
    original = ProvisionerLaunchIntent.objects.get(intent_id=original_id)
    Principal.objects.filter(uuid=fixture.credential.principal.uuid).update(is_active=False)
    fixture.realized.refresh_from_db()
    fixture.realized.status = "destroying"
    fixture.realized.save(update_fields=["status"])
    destroy = ["raes-range", "destroy", "--request-id", str(fixture.request.request_id)]
    with pytest.raises(PermissionError):
        enqueue_provisioner_launch(destroy)
    identifier = enqueue_provisioner_launch(destroy, cleanup_for=original.operation_id)
    cleanup = ProvisionerLaunchIntent.objects.get(intent_id=identifier)
    assert cleanup.authority_kind == "cleanup"
    assert cleanup.initiating_principal_uuid == original.initiating_principal_uuid
    assert cleanup.operation_id != original.operation_id
    authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)
    with pytest.raises(ValueError):
        enqueue_provisioner_launch(resume, cleanup_for=original.operation_id)
    fixture.realized.resource_generation = uuid4()
    fixture.realized.save(update_fields=["resource_generation"])
    with pytest.raises((ValueError, PermissionError)):
        authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)
