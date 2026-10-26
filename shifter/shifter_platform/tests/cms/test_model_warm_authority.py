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
from shared.authorization import CredentialCeiling, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef
from shared.model_access import ContractError

from ..engine.services.test_model_allocation import allocation_inputs
from .test_model_allocation_dispatch import setup_warm_launch

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def warm_credential(monkeypatch):
    credential = CredentialContext(
        PrincipalRef(uuid4(), "service"),
        "service",
        uuid4(),
        CredentialCeiling(frozenset({"installation.launch_range"}), TargetRef("installation")),
        frozenset(),
    )
    monkeypatch.setattr("cms.services._model_allocation.resolve_current_credential", lambda original: original)
    return credential


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
        monkeypatch.setattr(
            "cms.services._model_allocation.resolve_current_credential",
            lambda original: (_ for _ in ()).throw(ValueError("revoked")),
        )
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
    prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
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
    prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    with pytest.raises(ContractError, match=r"allocation\.system_preparation_only"):
        allocate_preparation(request, "activate")
    assert not ModelAllocation.objects.exists()
