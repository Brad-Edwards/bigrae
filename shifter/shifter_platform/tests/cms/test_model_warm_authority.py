"""Warm preparation authority follows service custody and the ledger lifecycle."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import transaction
from django.utils import timezone

from cms.services._model_allocation import prepare_model_access_for_dispatch
from engine.models import ModelAllocation, ModelLaunchPreparationRecord, Range, WarmRangeGeneration
from engine.services import record_model_observations
from engine.services._model_allocation_launch import allocate_launch_models
from shared.model_access import ContractError

from ..engine.services.test_model_allocation import allocation_inputs
from .test_model_allocation_dispatch import setup_warm_launch

# Observations require real autocommit, while retained native provider bindings
# cannot be DELETE-flushed by SQLite. PostgreSQL TRUNCATE keeps both contracts.
pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def warm_credential(settings):
    from tests.cms._warm_pool_fixtures import service_credential

    return service_credential(settings)


def warm_launch(django_user_model, settings, credential):
    owner, request, _instance = setup_warm_launch(django_user_model, settings, credential)
    generation = WarmRangeGeneration.objects.create(
        request_id=request.request_id,
        range=Range.objects.get(uuid=request.range_id),
        bucket_id="model-warm",
        compatibility_digest="sha256:" + "a" * 64,
        effective_policy_fingerprint="sha256:" + "b" * 64,
        backend="gce",
        range_source="mission_control",
        capacity_partition="default",
        capacity_scope_ref=uuid4(),
        capacity_draw_key=uuid4(),
        idle_deadline=request.window_end,
    )
    _, _, observations = allocation_inputs(django_user_model)
    record_model_observations(settings.MODEL_ACCESS_CATALOG, lambda _: observations)
    return owner, request, generation


def allocate_preparation(request, operation="provision"):
    with transaction.atomic():
        return allocate_launch_models(
            {"resource": "raes-range", "operation": operation, "request_id": str(request.request_id)},
            request.operation_id,
        )


def prepare_retained_warm_accounting(request, credential, catalog):
    """Exercise the retained Engine contract without reopening CMS publication."""
    from engine.services import get_model_warm_scope, prepare_model_launch
    from shared.model_access import OwnedReference
    from shared.model_access.reservation import ModelLaunchScope, WarmPreparationAuthority

    with transaction.atomic():
        warm = get_model_warm_scope(request.request_id, deployment_id=catalog.deployment_id)
        assert warm is not None
        owner = OwnedReference(owner="management", reference=f"principal:{credential.principal.uuid}")
        prepare_model_launch(
            request_id=request.request_id,
            owner_ref=owner,
            needs=(request.need,),
            catalog=catalog,
            authority_revisions=warm.authority_revisions,
            scope=ModelLaunchScope(
                kind="warm",
                scope_id=warm.scope_id,
                draw_key=warm.draw_key,
                subject_ref=OwnedReference(owner="deployment", reference=f"range:{request.range_id}"),
                window_start=warm.window_start,
                window_end=warm.window_end,
                authority_revisions=warm.authority_revisions,
                demands=(request.demand,),
                system_preparation=WarmPreparationAuthority(owner_ref=owner, generation_id=warm.generation_id),
            ),
        )


@pytest.mark.parametrize(
    ("invalid", "error_code"),
    [
        ("credential-revoked", r"allocation\.authority_unavailable"),
        ("user-present", r"allocation\.authority_unavailable"),
        ("account-scope", r"allocation\.authority_unavailable"),
        ("expired", r"allocation\.scope_unavailable"),
        ("retired", r"allocation\.scope_unavailable"),
        ("absent", r"allocation\.scope_unavailable"),
    ],
)
def test_warm_preparation_requires_real_system_identity_and_live_ledger(
    django_user_model, settings, warm_credential, monkeypatch, invalid, error_code
):
    _owner, request, generation = warm_launch(django_user_model, settings, warm_credential)
    if invalid == "credential-revoked":
        from management.models import ServiceCredentialAdmission

        ServiceCredentialAdmission.objects.filter(uuid=warm_credential.credential_uuid).update(is_active=False)
    elif invalid == "user-present":
        from cms.models import RangeInstance

        user = django_user_model.objects.create_user(username="invalid-warm-user")
        RangeInstance.objects.filter(request__request_id=request.request_id).update(user_id=user.pk)
    elif invalid == "account-scope":
        from cms.models import RangeInstance

        RangeInstance.objects.filter(request__request_id=request.request_id).update(scope_kind="account", account_id=1)
    elif invalid == "expired":
        generation.idle_deadline = timezone.now() - timedelta(seconds=1)
        generation.save(update_fields=["idle_deadline"])
    elif invalid == "retired":
        generation.state = "retiring"
        generation.save(update_fields=["state"])
    else:
        generation.delete()
    with pytest.raises(ContractError, match=error_code):
        prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert not ModelLaunchPreparationRecord.objects.exists()
    assert not ModelAllocation.objects.exists()


@pytest.mark.parametrize("allocated", [False, True])
@pytest.mark.parametrize("mutation", ["claim", "retire", "deadline", "scope", "draw"])
def test_warm_ledger_mutation_invalidates_preparation(
    django_user_model, settings, warm_credential, allocated, mutation
):
    _, request, generation = warm_launch(django_user_model, settings, warm_credential)
    prepare_retained_warm_accounting(request, warm_credential, settings.MODEL_ACCESS_CATALOG)
    allocation = allocate_preparation(request)[0] if allocated else None
    with transaction.atomic():
        if mutation == "claim":
            generation.state = "ready"
            generation.save(update_fields=["state"])
            from engine.services import claim_ready_generation

            assert (
                claim_ready_generation(
                    candidates=[(generation.bucket_id, generation.compatibility_digest)],
                    backend=generation.backend,
                    range_source=generation.range_source,
                    claimant_request_id=uuid4(),
                )
                is not None
            )
        elif mutation == "retire":
            generation.state = "retiring"
            generation.save(update_fields=["state"])
        elif mutation == "deadline":
            generation.idle_deadline -= timedelta(minutes=1)
            generation.save(update_fields=["idle_deadline"])
        else:
            field = "capacity_scope_ref" if mutation == "scope" else "capacity_draw_key"
            setattr(generation, field, uuid4())
            generation.save(update_fields=[field])
        if allocation is not None:
            allocation.grant.refresh_from_db()
            assert allocation.grant.state == "revoked"
        else:
            with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
                allocate_preparation(request)
            assert not ModelAllocation.objects.exists()


def test_preparation_authority_cannot_activate_a_claimant(django_user_model, settings, warm_credential):
    _, request, _ = warm_launch(django_user_model, settings, warm_credential)
    prepare_retained_warm_accounting(request, warm_credential, settings.MODEL_ACCESS_CATALOG)
    with pytest.raises(ContractError, match=r"allocation\.system_preparation_only"):
        allocate_preparation(request, "activate")
    assert not ModelAllocation.objects.exists()


@pytest.mark.parametrize("path", ["cold", "warm-claim"])
def test_model_backed_launch_is_parked_before_reservation_or_custody_transfer(
    django_user_model, settings, live_session_credential, allow_all_policy, monkeypatch, path
):
    from cms.models import Request
    from cms.services import create_raes_native_range
    from cms.services._warm_pool_claim import _resolve_claim_candidates, _run_atomic_claim
    from engine.models import ProvisionerLaunchIntent
    from shared.exceptions import CMSError
    from shared.model_access import projection_port
    from tests.cms._warm_pool_fixtures import warm_claim_fixture
    from tests.cms.test_launch_model_admission import _author, _catalog

    claim, generation, instance, source, _ = warm_claim_fixture(django_user_model, settings, live_session_credential)
    _author(source.registered_by, authored=source.package_digest)
    settings.MODEL_ACCESS_ENABLED = True
    settings.MODEL_ACCESS_CATALOG = _catalog()
    refreshed = []
    refresh = projection_port.refresh_launch_projections

    def observe_refresh(deployment_id):
        # Observe the real composition/guard call; never substitute its outcome.
        refreshed.append(deployment_id)
        return refresh(deployment_id)

    monkeypatch.setattr(projection_port, "refresh_launch_projections", observe_refresh)
    original_request_count = Request.objects.count()
    original_owner = instance.owner_principal_uuid
    if path == "cold":
        from shared.identity_scope import ResourceScope

        cold_scope = ResourceScope(
            "account", claim.scope.account_uuid, claim.scope.organization_uuid, claim.scope.workspace_uuid
        )
        with pytest.raises(CMSError) as denied:
            create_raes_native_range(claim.credential, source.scenario_id, scope=cold_scope)
        assert str(denied.value) == "Range launch unavailable"
    else:
        with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
            _run_atomic_claim(claim, _resolve_claim_candidates(claim, None))
    assert refreshed == [settings.MODEL_ACCESS_CATALOG.deployment_id]
    generation.refresh_from_db()
    instance.refresh_from_db()
    assert generation.state == "ready" and generation.claimed_by_request_id is None
    assert instance.owner_principal_uuid == original_owner
    assert Request.objects.count() == original_request_count
    assert not ModelLaunchPreparationRecord.objects.exists()
    assert not ModelAllocation.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
