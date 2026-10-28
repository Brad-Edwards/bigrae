"""Launch selections are closed intent and survive replay and preparation."""

from uuid import uuid4

import pytest

from mission_control.api.serializers import LaunchRangeSerializer

pytestmark = pytest.mark.usefixtures("personal_vpn_policy_grant")


def selection():
    return {
        "aliases": [
            {
                "logical_alias": "coding-main",
                "sources": [
                    {"source_id": str(uuid4()), "revision": 1, "weight": 2},
                    {"source_id": str(uuid4()), "revision": 3, "weight": 1},
                ],
            }
        ]
    }


def _organization_launch_scope(actor, request, instance):
    """Move one synthetic launch into a complete shared-account hierarchy."""
    from engine.models import Range
    from shared.principal_port import principal_for_user
    from tests.cms.test_model_allocation_dispatch import _launch_credential
    from workspaces.models import Account, Organization, OrganizationMembership, Workspace
    from workspaces.services import create_account

    account_view = create_account(kind=Account.Kind.TEAM, name="Synthetic model-source account")
    account = Account.objects.get(uuid=account_view.uuid)
    organization = Organization.objects.get(account=account, is_default=True)
    workspace = Workspace.objects.get(organization=organization, is_default=True)
    OrganizationMembership.objects.update_or_create(
        user=actor,
        organization=organization,
        defaults={"role": "admin"},
    )
    principal_uuid = principal_for_user(actor).uuid
    values = {
        "scope_kind": "account",
        "account_id": account.pk,
        "organization_id": organization.pk,
        "workspace_id": workspace.pk,
    }
    for row in (instance, instance.request):
        for name, value in values.items():
            setattr(row, name, value)
        row.save(update_fields=list(values))
    engine_range = Range.objects.select_related("request").get(uuid=request.range_id)
    for row in (engine_range, engine_range.request):
        for name, value in values.items():
            setattr(row, name, value)
        row.save(update_fields=list(values))
    assert instance.owner_principal_uuid == principal_uuid
    return organization, workspace, _launch_credential(request.request_id)


def _session_credential(user):
    """Create one durable test session reference for a human principal."""
    from django.test import Client

    from config.session_credentials import session_credential

    client = Client()
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    return session_credential(user, client.session)


def test_launch_serializer_preserves_weighted_choices_and_rejects_transport_injection():
    chosen = selection()
    serializer = LaunchRangeSerializer(data={"agents": {}, "scenario": "synthetic", "model_sources": chosen})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["model_sources"] == chosen
    chosen["url"] = "https://foreign.example.test"
    invalid = LaunchRangeSerializer(data={"agents": {}, "model_sources": chosen})
    assert not invalid.is_valid()


@pytest.mark.django_db
def test_selected_model_sources_require_native_proof_at_direct_service_boundary(django_user_model):
    from cms.services._model_source_selection import resolve_launch_sources
    from shared.model_access import ContractError

    actor = django_user_model.objects.create_user(username="synthetic-unproven-source-selector")
    with pytest.raises(ContractError, match=r"source\.authority_unavailable"):
        resolve_launch_sources(actor, 1, selection(), catalog=None)
    # Omitted selections keep the deployment's ordinary model policy and do
    # not resolve private tenant sources or publish asynchronous authority.
    assert resolve_launch_sources(actor, None, {"aliases": []}, catalog=None) == (None, ())


@pytest.mark.django_db
def test_retry_intent_cannot_reuse_a_key_for_different_source_choices(django_user_model):
    from cms.services._retry_safe_launch import _digest
    from shared.api.principals import human_session_credential
    from shared.identity_scope import ResourceScope

    actor = django_user_model.objects.create_user(username="source-launcher")
    credential = human_session_credential(actor)
    scope = ResourceScope("installation")
    original = _digest(credential, "synthetic", {"agents": {}}, None, "synthetic", scope, model_sources=selection())
    changed = _digest(credential, "synthetic", {"agents": {}}, None, "synthetic", scope, model_sources=selection())
    assert original != changed
    assert _digest(credential, "synthetic", {"agents": {}}, None, "synthetic", scope) == _digest(
        credential, "synthetic", {"agents": {}}, None, "synthetic", scope, model_sources={"aliases": []}
    )


@pytest.mark.django_db(transaction=True)
def test_authorized_source_compilation_remains_available_but_broker_preparation_is_parked(
    django_user_model, settings, monkeypatch
):
    from unittest.mock import Mock

    from cms.services._model_allocation import prepare_model_access_for_dispatch
    from cms.services._model_source_selection import resolve_launch_sources
    from engine.models import ModelLaunchPreparationRecord
    from engine.services import create_model_source
    from tests.cms.test_model_allocation_dispatch import setup_launch
    from tests.engine.services.test_model_sources import configuration, direct_source_catalog

    actor, request, instance = setup_launch(django_user_model, settings)
    org, _workspace, credential = _organization_launch_scope(actor, request, instance)
    from cms.models import ScenarioModelNeeds

    authored = ScenarioModelNeeds.objects.get(scenario_id=instance.scenario_id)
    authored.needs["participant"]["data_regions"].append("provider-managed")
    authored.save()
    base = direct_source_catalog(request.deployment_id)
    settings.MODEL_ACCESS_CATALOG = base
    store = Mock()
    store.create_owned_secret.return_value = "projects/platform-example/secrets/synthetic/versions/1"
    monkeypatch.setattr("shared.cloud.get_secrets_store", lambda: store)
    source = create_model_source(
        credential,
        org.uuid,
        configuration(region="provider-managed"),
        credential={"api_key": "synthetic-secret"},
    )
    instance.model_sources = {
        "aliases": [
            {"logical_alias": "coding-main", "sources": [{"source_id": str(source.id), "revision": 1, "weight": 1}]}
        ]
    }
    instance.save(update_fields=["model_sources"])
    from shared.model_access import ContractError

    compiled, revisions = resolve_launch_sources(
        credential, instance.workspace_id, instance.model_sources, catalog=base
    )
    assert compiled.contract_version == "model-access-policy/v4"
    assert any(item.source_id == source.id for item in compiled.source_bindings)
    assert any(item.authority_ref.reference == f"model-source:{source.id}" for item in revisions)
    assert "synthetic-secret" not in str(compiled)
    with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
        prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert not ModelLaunchPreparationRecord.objects.exists()


def _allocated_source_launch(django_user_model, settings, monkeypatch, mixed=False):
    """Historical accounting setup through Engine, without reopening composition.

    Native source admission and its application caps still use their real owning
    services. Only the retained accounting contract consumes those closed facts.
    """
    from unittest.mock import Mock

    from django.db import transaction

    from cms.services._model_source_selection import resolve_launch_sources
    from engine.services import create_model_source, project_model_launch_authority
    from engine.services._model_allocation import allocate_model_access
    from engine.services._model_source_observations import launch_model_observations
    from shared.model_access import OwnedReference
    from shared.model_access.core_models import ScenarioNeed
    from shared.model_access.reservation import ModelQuotaObservation
    from tests.cms.test_model_allocation_dispatch import setup_launch
    from tests.engine.services.test_model_sources import configuration, direct_source_catalog

    actor, request, instance = setup_launch(django_user_model, settings)
    org, _workspace, credential = _organization_launch_scope(actor, request, instance)
    from cms.models import ScenarioModelNeeds

    authored = ScenarioModelNeeds.objects.get(scenario_id=instance.scenario_id)
    authored.needs["participant"]["data_regions"].append("provider-managed")
    authored.save()
    catalog = direct_source_catalog(request.deployment_id)
    if mixed:
        from shared.model_access import seal_catalog

        policy = catalog.model_dump(mode="json")
        policy["profiles"][0]["allowed_strategies"] = ["fixed-v1", "weighted-rendezvous-v1"]
        catalog = seal_catalog(policy)
    settings.MODEL_ACCESS_CATALOG = catalog
    store = Mock()
    store.create_owned_secret.return_value = "projects/platform-example/secrets/synthetic/versions/1"
    monkeypatch.setattr("shared.cloud.get_secrets_store", lambda: store)
    source = create_model_source(
        credential,
        org.uuid,
        configuration(region="provider-managed"),
        credential={"api_key": "synthetic-secret"},
    )
    instance.model_sources = {
        "aliases": [
            {"logical_alias": "coding-main", "sources": [{"source_id": str(source.id), "revision": 1, "weight": 1}]}
        ]
    }
    if mixed:
        from cms.models import ScenarioModelNeeds
        from tests.cms.test_launch_model_admission import _need_payload

        need = _need_payload(digest=request.need.scenario_digest)
        need["allowed_strategies"] = ["fixed-v1", "weighted-rendezvous-v1"]
        need["data_regions"].append("provider-managed")
        authored = ScenarioModelNeeds.objects.get(scenario_id=instance.scenario_id)
        authored.needs = {"participant": need}
        authored.save()
        second = create_model_source(
            credential,
            org.uuid,
            configuration(region="provider-managed", name="Second source"),
            credential={"api_key": "synthetic-second-secret"},
        )
        instance.model_sources["aliases"][0]["sources"].append(
            {"source_id": str(second.id), "revision": 1, "weight": 3}
        )
    instance.save(update_fields=["model_sources"])
    compiled, revisions = resolve_launch_sources(
        credential, instance.workspace_id, instance.model_sources, catalog=catalog
    )
    retained_fences = project_model_launch_authority(
        deployment_id=catalog.deployment_id,
        authority_refs=(
            OwnedReference(owner="engine", reference=f"model-launch:{request.range_id}"),
            OwnedReference(owner="workspaces", reference=f"workspace-id:{instance.workspace_id}"),
            OwnedReference(owner="workspaces", reference=f"organization:{org.uuid}"),
        ),
    )
    request = request.model_copy(
        update={
            "need": ScenarioNeed.model_validate(
                ScenarioModelNeeds.objects.get(scenario_id=instance.scenario_id).needs["participant"]
            ),
            "authority_revisions": request.authority_revisions + retained_fences + revisions,
        }
    )
    with transaction.atomic():
        allocation = allocate_model_access(
            request,
            catalog=compiled,
            observations=tuple(
                ModelQuotaObservation.model_validate(item) for item in launch_model_observations(compiled)
            ),
        )
    return actor, request, instance, source, allocation, org, catalog


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mixed", [False, True])
def test_source_application_cap_reaches_allocator_without_deployment_file_write(
    django_user_model, settings, monkeypatch, mixed
):
    _actor, _request, instance, _source, allocation, _org, catalog = _allocated_source_launch(
        django_user_model, settings, monkeypatch, mixed
    )
    choices = instance.model_sources["aliases"][0]["sources"]
    assert allocation.snapshot["shards"]["coding-main"]["credential_ref"]["reference"] in {
        f"source:{c['source_id']}:1" for c in choices
    }
    reading = allocation.draws.get().reservation.observation
    assert reading["source"] == "application_cap"
    assert settings.MODEL_ACCESS_CATALOG.digest == catalog.digest


@pytest.mark.django_db(transaction=True)
def test_member_cannot_edit_another_ranges_model_policy(django_user_model, settings, monkeypatch):
    from cms.services import change_range_model_sources
    from shared.authorization import AuthorizationDecision, DecisionKind, port
    from tests.cms.test_model_allocation_dispatch import setup_launch
    from workspaces.services import WorkspaceAuthorizationError

    actor, request, instance = setup_launch(django_user_model, settings)
    _organization_launch_scope(actor, request, instance)
    outsider = django_user_model.objects.create_user(username="source-foreign-editor", is_staff=True)
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: type(
            "DenyProvider",
            (),
            {"check": lambda self, request: AuthorizationDecision(DecisionKind.DENIED, "policy_denied")},
        )(),
    )
    with pytest.raises(WorkspaceAuthorizationError, match="Range access denied"):
        change_range_model_sources(
            _session_credential(outsider),
            request_id=request.request_id,
            expected_revision=0,
            selection={"aliases": []},
        )


@pytest.mark.django_db(transaction=True)
def test_tenant_admin_can_inspect_range_model_sources_without_workspace_seat(django_user_model, settings):
    from cms.services import get_range_model_sources
    from tests.cms.test_model_allocation_dispatch import setup_launch
    from workspaces.models import OrganizationMembership

    actor, request, instance = setup_launch(django_user_model, settings)
    organization, _workspace, _credential = _organization_launch_scope(actor, request, instance)
    admin = django_user_model.objects.create_user(username="tenant-model-admin", is_staff=False)
    OrganizationMembership.objects.create(user=admin, organization=organization, role="admin")
    result = get_range_model_sources(_session_credential(admin), request_id=request.request_id)
    assert result["request_id"] == request.request_id
    assert result["selection"] == {"aliases": []}


@pytest.mark.django_db(transaction=True)
def test_live_admin_policy_edit_preserves_generation_and_old_liability(django_user_model, settings, monkeypatch):
    from cms.services import change_range_model_sources
    from engine.models import ModelAllocation, Range
    from engine.services import create_model_source
    from shared.model_access import ContractError
    from tests.engine.services.test_model_sources import configuration

    _actor, request, _instance, _source, old, org, _catalog = _allocated_source_launch(
        django_user_model, settings, monkeypatch
    )
    from tests.cms.test_model_allocation_dispatch import _launch_credential

    credential = _launch_credential(request.request_id)
    row = Range.objects.get(uuid=request.range_id)
    row.status = Range.Status.READY
    row.save(update_fields=["status"])
    old.unresolved_liabilities = 1
    old.save(update_fields=["unresolved_liabilities"])
    replacement = create_model_source(
        credential,
        org.uuid,
        configuration(region="provider-managed", quota_identity="account:second"),
        credential={"api_key": "synthetic-replacement"},
    )
    selection = {
        "aliases": [{"logical_alias": "coding-main", "sources": [{"source_id": str(replacement.id), "revision": 1}]}]
    }
    result = change_range_model_sources(
        credential, request_id=request.request_id, expected_revision=0, selection=selection
    )
    assert result["error"] == "source.admission_unavailable"
    assert result["revision"] == 1
    assert result["runtime"]["state"] == "unavailable"
    old.refresh_from_db()
    assert old.grant.state == "revoked" and old.unresolved_liabilities == 1
    assert not ModelAllocation.objects.filter(request_id=request.request_id, source_policy_revision=1).exists()
    with pytest.raises(ContractError, match=r"source\.revision_conflict"):
        change_range_model_sources(credential, request_id=request.request_id, expected_revision=0, selection=selection)


@pytest.mark.django_db(transaction=True)
def test_source_organization_membership_loss_revokes_live_allocation(django_user_model, settings, monkeypatch):
    from workspaces.models import OrganizationMembership

    actor, _request, _instance, _source, allocation, org, _catalog = _allocated_source_launch(
        django_user_model, settings, monkeypatch
    )
    OrganizationMembership.objects.filter(user=actor, organization=org).delete()
    allocation.grant.refresh_from_db()
    assert allocation.grant.state == "revoked"


@pytest.mark.django_db(transaction=True)
def test_pending_range_edit_retries_same_policy_after_interrupted_admission(django_user_model, settings, monkeypatch):
    from cms.models import RangeInstance
    from cms.services import _model_allocation, change_range_model_sources
    from engine.models import Range

    _actor, request, instance, _source, allocation, _org, _catalog = _allocated_source_launch(
        django_user_model, settings, monkeypatch
    )
    from tests.cms.test_model_allocation_dispatch import _launch_credential

    credential = _launch_credential(request.request_id)
    Range.objects.filter(uuid=request.range_id).update(status=Range.Status.READY)
    prepare = _model_allocation.prepare_model_access_for_dispatch

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic interruption between commits")

    monkeypatch.setattr(_model_allocation, "prepare_model_access_for_dispatch", interrupted)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        change_range_model_sources(
            credential, request_id=request.request_id, expected_revision=0, selection=instance.model_sources
        )
    current = RangeInstance.objects.get(pk=instance.pk)
    assert current.model_source_policy_revision == 1 and current.model_source_policy_error == "source.admission_pending"
    allocation.grant.refresh_from_db()
    assert allocation.grant.state == "revoked"
    monkeypatch.setattr(_model_allocation, "prepare_model_access_for_dispatch", prepare)
    result = change_range_model_sources(
        credential, request_id=request.request_id, expected_revision=1, selection=instance.model_sources
    )
    assert result["revision"] == 1 and result["error"] == "source.admission_unavailable"
    assert result["runtime"]["state"] == "unavailable"


@pytest.mark.django_db(transaction=True)
def test_rotated_credential_with_unresolved_usage_is_retained(django_user_model, settings, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    import shared.cloud
    from engine.models import ModelSourceRevision
    from engine.services import retire_unused_model_source_credentials, update_model_source
    from tests.engine.services.test_model_sources import configuration

    _actor, request, _instance, source, allocation, org, _catalog = _allocated_source_launch(
        django_user_model, settings, monkeypatch
    )
    from tests.cms.test_model_allocation_dispatch import _launch_credential

    credential = _launch_credential(request.request_id)
    allocation.unresolved_liabilities = 1
    allocation.save(update_fields=["unresolved_liabilities"])
    ModelSourceRevision.objects.filter(source_id=source.id).update(created_at=timezone.now() - timedelta(minutes=20))
    update_model_source(
        credential,
        org.uuid,
        source.id,
        expected_revision=1,
        configuration=configuration(),
        credential={"api_key": "synthetic-new"},
    )
    assert retire_unused_model_source_credentials(credential, org.uuid, source.id) == 0
    shared.cloud.get_secrets_store().retire_owned_secret.assert_not_called()
    assert ModelSourceRevision.objects.get(source_id=source.id, revision=1).retired_at is None


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_model_policy_write_revisions_do_not_coerce_json_scalars(value):
    from cms.api.model_sources import ModelSourceUpdateSerializer
    from cms.api.range_model_sources import RangeModelSourcesWriteSerializer
    from ctf.api.serializers.organizer import EventWriteSerializer
    from tests.engine.services.test_model_sources import configuration

    writes = [
        ModelSourceUpdateSerializer(data={"configuration": configuration(), "expected_revision": value}),
        RangeModelSourcesWriteSerializer(data={"selection": {"aliases": []}, "expected_revision": value}),
        EventWriteSerializer(data={"expected_model_source_revision": value}, partial=True),
    ]
    for serializer in writes:
        assert not serializer.is_valid()
        assert any("revision" in key for key in serializer.errors)
