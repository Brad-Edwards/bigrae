"""Enforcing model demand must preserve CTF identity and reject malformed intent."""

from datetime import timedelta
from uuid import uuid4

import pytest

from ctf.services.range.model_allocation import project_event_model_scope
from shared.model_access import ContractError, OwnedReference

from ..engine.services.test_model_admission import _catalog
from .test_model_demand_declaration import _demand

pytestmark = pytest.mark.django_db(transaction=True)


def test_event_scope_carries_server_window_draw_and_authority(ctf_event, settings):
    settings.MODEL_ACCESS_CATALOG = _catalog()
    ctf_event.model_demand = [_demand()]
    ctf_event.save(update_fields=["model_demand", "updated_at"])
    draw = uuid4()
    subject = OwnedReference(owner="ctf", reference=f"draw:{draw}")
    scope = project_event_model_scope(ctf_event, draw, subject)
    assert scope.scope_id == ctf_event.pk
    assert scope.draw_key == draw
    assert scope.subject_ref == subject
    assert scope.window_end == ctf_event.get_cleanup_time()
    assert scope.authority_revisions[0].authority_ref.reference == f"event-launch:{ctf_event.pk}"


def test_participant_range_realization_refreshes_selectors_without_revoking_launch(ctf_participant, monkeypatch):
    from ctf.signals import _PARTICIPANT_AUTHORITY_FIELDS, invalidate_participant_model_access

    captured = []
    monkeypatch.setattr("ctf.bridges.cms_invalidate_model_access_authority", captured.append)
    ctf_participant._model_access_authority_before = {  # type: ignore[attr-defined]
        field: getattr(ctf_participant, field) for field in _PARTICIPANT_AUTHORITY_FIELDS
    }
    ctf_participant.range_instance_id = 41

    invalidate_participant_model_access(type(ctf_participant), ctf_participant)

    assert len(captured) == 1
    assert captured[0].allocation_effect == "selector_only"
    references = {item.reference for item in captured[0].authority_refs}
    assert f"event:{ctf_participant.event_id}" in references
    assert f"participant:{ctf_participant.pk}" not in references
    assert f"event-launch:{ctf_participant.event_id}" not in references


def test_invalid_demand_is_not_dropped_by_enforcing_path(ctf_event, settings):
    settings.MODEL_ACCESS_CATALOG = _catalog()
    ctf_event.model_demand = [_demand(), {"workload_role": "broken"}]
    ctf_event.save(update_fields=["model_demand", "updated_at"])
    with pytest.raises(ContractError, match=r"allocation\.event_demand_invalid"):
        project_event_model_scope(ctf_event, uuid4(), OwnedReference(owner="ctf", reference="draw:test"))


def test_retained_event_spare_accounting_has_exact_authority_and_revokes_on_consumption(
    ctf_event, django_user_model, settings
):
    from django.db import transaction

    from ctf.models import CTFSpareRange
    from engine.models import ModelLaunchPreparationRecord
    from engine.services import prepare_model_launch, record_model_observations
    from engine.services._model_allocation_launch import allocate_launch_models

    from ..cms.test_model_allocation_dispatch import setup_launch
    from ..engine.services.test_model_allocation import allocation_inputs

    _owner, request, instance = setup_launch(django_user_model, settings, owner=ctf_event.created_by)
    ctf_event.model_demand = [_demand()]
    ctf_event.save(update_fields=["model_demand", "updated_at"])
    spare = CTFSpareRange.objects.create(event=ctf_event)
    subject = OwnedReference(owner="ctf", reference=f"draw:{spare.pk}")
    scope = project_event_model_scope(ctf_event, request.draw_key, subject, spare_id=spare.pk)
    instance.range_source = "ctf"
    instance.event_uuid = ctf_event.pk
    instance.model_launch_scope = scope.model_dump(mode="json")
    instance.save(update_fields=["range_source", "event_uuid", "model_launch_scope"])
    _, _, observations = allocation_inputs(django_user_model)
    record_model_observations(settings.MODEL_ACCESS_CATALOG, lambda _: observations)
    with transaction.atomic():
        # Retained Engine accounting consumes closed scope facts directly. The
        # mounted and CMS broker composition remain unavailable under ADR-068.
        prepare_model_launch(
            request_id=request.request_id,
            owner_ref=request.owner_ref,
            needs=(request.need,),
            scope=scope,
            authority_revisions=scope.authority_revisions,
            catalog=settings.MODEL_ACCESS_CATALOG,
        )
        (allocation,) = allocate_launch_models(
            {"resource": "raes-range", "operation": "provision", "request_id": str(request.request_id)},
            request.operation_id,
        )
    assert allocation.grant.state == "pending"
    assert allocation.snapshot["request"]["preparation_authority"] == {"owner": "ctf", "reference": f"spare:{spare.pk}"}
    assert ModelLaunchPreparationRecord.objects.get().intent["scope"]["system_preparation"]["kind"] == "ctf_spare"
    spare.status = "consumed"
    spare.save(update_fields=["status", "updated_at"])
    allocation.grant.refresh_from_db()
    assert allocation.grant.state == "revoked"
    with pytest.raises(ContractError, match=r"allocation\.system_owner_unavailable"):
        project_event_model_scope(ctf_event, request.draw_key, subject, spare_id=spare.pk)


def test_system_preparation_rejects_spare_from_another_event(ctf_event, django_user_model, settings):
    from ctf.models import CTFSpareRange

    from .conftest import make_ctf_event

    settings.MODEL_ACCESS_CATALOG = _catalog()
    ctf_event.model_demand = [_demand()]
    ctf_event.save(update_fields=["model_demand", "updated_at"])
    owner = django_user_model.objects.create_user(username="other-event-spare@system.invalid")
    owner.is_active = False
    owner.set_unusable_password()
    owner.save(update_fields=["is_active", "password"])
    other_event = make_ctf_event(
        created_by_id=ctf_event.created_by_id,
        workspace_id=ctf_event.workspace_id,
        model_demand=[_demand()],
    )
    other_event.save()
    other_spare = CTFSpareRange.objects.create(event=other_event)
    with pytest.raises(ContractError, match=r"allocation\.system_owner_unavailable"):
        project_event_model_scope(
            ctf_event,
            uuid4(),
            OwnedReference(owner="ctf", reference="draw:test"),
            spare_id=other_spare.pk,
        )


def test_delegated_event_range_manager_can_prepare_the_owners_range(
    ctf_event, django_user_model, settings, live_session_credential, allow_all_policy, monkeypatch
):
    from cms.services import _model_allocation
    from shared.operation_authority import credential_reference_fields

    from ..cms.test_model_allocation_dispatch import setup_launch

    _owner, request, instance = setup_launch(django_user_model, settings, owner=ctf_event.created_by)
    instance.range_source = "ctf"
    instance.event_uuid = ctf_event.pk
    instance.save(update_fields=["range_source", "event_uuid"])
    delegate = django_user_model.objects.create_user(username="delegated-model-preparer")
    credential = live_session_credential(delegate)
    authority = credential_reference_fields(credential)
    for name, value in authority.items():
        setattr(instance.request, name, value)
    instance.request.save(update_fields=list(authority))
    seen = []
    check = allow_all_policy.check

    def _check(authority_request):
        seen.append(authority_request)
        return check(authority_request)

    monkeypatch.setattr(allow_all_policy, "check", _check)

    current = _model_allocation._authorize_preparation(
        instance,
        _model_allocation._owner_reference(instance),
        None,
    )

    assert current == credential
    assert seen[0].action == "event.manage_ranges"
    assert seen[0].target.uuid == ctf_event.pk
    assert seen[0].scope.kind == "account"
    assert request.request_id == instance.request.request_id


@pytest.mark.parametrize(
    "field", ["model_demand", "event_start", "event_end", "range_spinup_minutes", "cleanup_delay_hours"]
)
@pytest.mark.parametrize("already_allocated", [False, True])
def test_event_fact_mutation_fences_admission_and_pending_grant(
    ctf_event, django_user_model, settings, organizer_credential, field, already_allocated
):
    from ctf.services.event import update_event
    from engine.models import ModelAllocation, ModelPendingGrant

    from ..engine.services.test_model_allocation import allocate, allocation_inputs

    catalog, request, observations = allocation_inputs(django_user_model)
    settings.MODEL_ACCESS_CATALOG = catalog
    ctf_event.model_demand = [_demand()]
    ctf_event.save(update_fields=["model_demand", "updated_at"])
    scope = project_event_model_scope(ctf_event, request.draw_key, request.subject_ref)
    request = request.model_copy(
        update={
            "scope_kind": scope.kind,
            "scope_id": scope.scope_id,
            "window_start": scope.window_start,
            "window_end": scope.window_end,
            "demand": scope.demands[0],
            "authority_revisions": request.authority_revisions + scope.authority_revisions,
        }
    )
    inputs = catalog, request, observations
    allocation = allocate(inputs) if already_allocated else None
    old_value = getattr(ctf_event, field)
    if field == "model_demand":
        replacement = [{**_demand(), "expected_concurrency": 8}]
    elif field in {"event_start", "event_end"}:
        replacement = old_value + timedelta(minutes=1)
    else:
        replacement = old_value + 1

    # Exercise the organizer's real transaction after the projection lock has
    # been released, not a direct write to the Engine authority projection.
    update_event(ctf_event.pk, {field: replacement}, actor_id=organizer_credential)
    if allocation is None:
        with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
            allocate(inputs)
        assert not ModelAllocation.objects.exists()
        assert not ModelPendingGrant.objects.exists()
    else:
        allocation.grant.refresh_from_db()
        assert allocation.grant.state == "revoked"
        assert allocation.grant.grant_epoch == 2


def test_event_source_selection_requires_an_identified_current_organizer(ctf_event, settings):
    settings.MODEL_ACCESS_CATALOG = _catalog()
    ctf_event.model_demand = [_demand()]
    ctf_event.model_sources = {
        "aliases": [
            {"logical_alias": "coding-main", "sources": [{"source_id": str(uuid4()), "revision": 1, "weight": 1}]}
        ]
    }
    ctf_event.save(update_fields=["model_demand", "model_sources"])
    with pytest.raises(ContractError, match=r"source\.sponsor_unavailable"):
        project_event_model_scope(ctf_event, uuid4(), OwnedReference(owner="ctf", reference="draw:test"))


def test_event_uses_authored_scenario_envelope_when_no_manual_demand_is_set(ctf_event, django_user_model, settings):
    from tests.cms.test_model_allocation_dispatch import setup_launch

    _, request, instance = setup_launch(django_user_model, settings)
    ctf_event.scenario_id = instance.scenario_id
    ctf_event.model_demand = []
    ctf_event.max_participants = 12
    ctf_event.save(update_fields=["scenario_id", "model_demand", "max_participants"])
    scope = project_event_model_scope(ctf_event, request.draw_key, request.subject_ref)
    assert scope is not None
    assert scope.demands[0].expected_concurrency == 12
    assert scope.demands[0].workload_role == "participant"
    assert scope.scope_id == ctf_event.pk


def test_event_sponsor_source_is_retained_but_broker_dispatch_remains_parked(
    ctf_event, django_user_model, settings, organizer_credential, monkeypatch
):
    from unittest.mock import Mock

    from django.db import transaction

    from cms.models import ScenarioModelNeeds
    from cms.services._model_allocation import prepare_model_access_for_dispatch
    from cms.services._model_source_selection import resolve_sponsored_sources
    from ctf.models import CTFEvent
    from ctf.services.event.model_sources import set_event_model_sources
    from engine.models import ModelAllocation, ModelLaunchPreparationRecord
    from engine.services import create_model_source
    from tests.cms.test_model_allocation_dispatch import setup_launch
    from tests.engine.services.test_model_sources import configuration, direct_source_catalog
    from workspaces.models import Account, Organization, OrganizationMembership, Workspace, WorkspaceMembership
    from workspaces.services import create_account

    participant, request, instance = setup_launch(django_user_model, settings)
    sponsor = ctf_event.created_by
    account = create_account(kind=Account.Kind.TEAM, name="Event sponsor account")
    org = Organization.objects.get(account_id=account.id, is_default=True)
    workspace = Workspace.objects.get(organization=org, is_default=True)
    OrganizationMembership.objects.create(organization=org, user=sponsor, role="admin")
    WorkspaceMembership.objects.create(workspace=workspace, user=sponsor, role="owner")
    ctf_event = CTFEvent.objects.create(
        name="Event sponsor model source",
        created_by=sponsor,
        scope_kind="account",
        account_id=account.id,
        organization_id=org.pk,
        workspace_id=workspace.pk,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
        scenario_id=ctf_event.scenario_id,
    )
    assert not WorkspaceMembership.objects.filter(user=participant, workspace__organization=org).exists()
    authored = ScenarioModelNeeds.objects.get(scenario_id=instance.scenario_id)
    authored.needs["participant"]["data_regions"].append("provider-managed")
    authored.save()
    settings.MODEL_ACCESS_CATALOG = direct_source_catalog(request.deployment_id)
    store = Mock()
    store.create_owned_secret.return_value = "synthetic-owned-reference"
    monkeypatch.setattr("shared.cloud.get_secrets_store", lambda: store)
    source = create_model_source(
        organizer_credential,
        org.uuid,
        configuration(),
        credential={"api_key": "synthetic-secret"},
    )
    ctf_event.model_demand = [_demand()]
    ctf_event.scenario_id = instance.scenario_id
    with transaction.atomic():
        set_event_model_sources(
            ctf_event,
            organizer_credential,
            {"aliases": [{"logical_alias": "coding-main", "sources": [{"source_id": str(source.id), "revision": 1}]}]},
            expected_revision=0,
        )
        ctf_event.save()
    scope = project_event_model_scope(
        ctf_event, request.draw_key, OwnedReference(owner="ctf", reference=f"draw:{request.draw_key}")
    )
    instance.range_source = "ctf"
    instance.model_launch_scope = scope.model_dump(mode="json")
    instance.save(update_fields=["range_source", "model_launch_scope"])
    compiled, _ = resolve_sponsored_sources(scope.source_sponsorship, catalog=settings.MODEL_ACCESS_CATALOG)
    assert any(binding.source_id == source.id for binding in compiled.source_bindings)
    assert "synthetic-secret" not in str(compiled)
    with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
        prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert not ModelLaunchPreparationRecord.objects.exists()
    assert not ModelAllocation.objects.exists()
