"""Real PostgreSQL proofs for PLAT-202 sharing authority serialization."""

from __future__ import annotations

import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.utils import timezone

from engine.models import MembershipProjection, Range, SharingAuthorityFence
from engine.services import (
    project_selector_resolution,
    publish_authority_fence,
    reassign_range_owner_by_request,
)
from shared.model_access import SelectorKind, SelectorResolution, SharingSelector, compute_digest
from tests.cms.services.test_principal_range_authority import principal_range

from .test_sharing import _PUBLISHER, _binding_dto, _catalog, _pool_dto, _services

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]
__all__ = ["principal_range"]
User = get_user_model()


def _fence(deployment_id, owner, reference):
    return publish_authority_fence(
        deployment_id=deployment_id,
        authority_ref={"owner": owner, "reference": reference},
        authority_revision=1,
        state="allowed",
    )


def _lock_order_resolution():
    selector = SharingSelector(kind=SelectorKind.ALL_RANGES)
    digest = compute_digest(selector)
    member = {"owner": "deployment", "reference": "range:lock-order"}
    return SelectorResolution(
        contract_version="model-access-selector-resolution/v1",
        selector_digest=digest,
        assessment_count=1,
        member_refs=(member,),
        selector_authority_refs=({"owner": "engine", "reference": "all-ranges"},),
        subject_authorities=(
            {
                "subject_ref": member,
                "authority_ref": {"owner": "cms", "reference": "range:lock-order"},
            },
        ),
        publisher_requirements=(
            {
                "selector_digest": digest,
                "authority_ref": {"owner": "management", "reference": "operator:platform"},
                "scope": "deployment",
            },
        ),
    )


@pytest.mark.parametrize("operation", ("publish", "drain"))
def test_projection_refresh_uses_the_publication_and_drain_lock_order(operation):
    """Refresh waits on projection before fences, avoiding the inverse deadlock."""
    deployment_id = uuid4()
    binding_id = f"lock-order-{operation}"
    pool_id = f"pool-{operation}"
    resolution = _lock_order_resolution()
    svc = _services()
    projection = project_selector_resolution(
        deployment_id=deployment_id,
        sharing_binding_id=binding_id,
        publisher_identity=_PUBLISHER,
        resolution=resolution,
        observed_at=timezone.now(),
        freshness_deadline=timezone.now() + timedelta(minutes=5),
    )
    binding = _binding_dto(
        binding_id=binding_id,
        pool_id=pool_id,
        deployment_id=deployment_id,
    )
    pool = _pool_dto(pool_id=pool_id)
    catalog = _catalog(deployment_id)
    if operation == "drain":
        svc["publish"](
            deployment_id=deployment_id,
            catalog=catalog,
            binding=binding,
            pool=pool,
            publisher_identity=_PUBLISHER,
            expected_definition_revision=0,
        )

    started = threading.Event()
    finished = threading.Event()
    result = {}

    def refresh_projection():
        started.set()
        try:
            result["projection"] = project_selector_resolution(
                deployment_id=deployment_id,
                sharing_binding_id=binding_id,
                publisher_identity=_PUBLISHER,
                resolution=resolution,
                observed_at=timezone.now(),
                freshness_deadline=timezone.now() + timedelta(minutes=5),
            )
        except Exception as exc:
            result["error"] = exc
        finally:
            finished.set()
            connection.close()

    worker = threading.Thread(target=refresh_projection, daemon=True)
    with transaction.atomic():
        MembershipProjection.objects.select_for_update().get(pk=projection.pk)
        worker.start()
        assert started.wait(timeout=10)
        assert not finished.wait(timeout=2), "refresh bypassed the projection lock"

        # A correct refresh holds no fence while it waits for the projection.
        tuple(
            SharingAuthorityFence.objects.select_for_update(nowait=True)
            .filter(deployment_id=deployment_id)
            .order_by("authority_owner", "authority_reference")
        )
        if operation == "publish":
            svc["publish"](
                deployment_id=deployment_id,
                catalog=catalog,
                binding=binding,
                pool=pool,
                publisher_identity=_PUBLISHER,
                expected_definition_revision=0,
            )
        else:
            svc["drain"](
                deployment_id=deployment_id,
                sharing_binding_id=binding_id,
                publisher_identity=_PUBLISHER,
                expected_definition_revision=1,
            )

    assert finished.wait(timeout=30)
    worker.join(timeout=30)
    assert "error" not in result, result.get("error")
    assert result["projection"].membership_revision == 2


def test_group_removal_serializes_on_owner_row_then_invalidates_projected_revision():
    """A resolver's Group lock and m2m removal share one PostgreSQL mutex."""
    deployment_id = uuid4()
    member = User.objects.create_user("group-race-member@example.test")
    group = Group.objects.create(name="group-race")
    member.groups.add(group)
    range_obj = Range.objects.create(user=member, workspace_id=1, status=Range.Status.READY)
    # Retained Engine invalidation setup: no runtime selector resolution.
    observed = _fence(deployment_id, "management", f"auth-group:{group.pk}").authority_revision
    started = threading.Event()
    finished = threading.Event()
    result = {}

    def remove_member():
        started.set()
        try:
            worker_member = User.objects.get(pk=member.pk)
            worker_group = Group.objects.get(pk=group.pk)
            worker_member.groups.remove(worker_group)
        except Exception as exc:
            result["error"] = exc
        finally:
            finished.set()
            connection.close()

    worker = threading.Thread(target=remove_member, daemon=True)
    with transaction.atomic():
        Group.objects.select_for_update().get(pk=group.pk)
        worker.start()
        assert started.wait(timeout=10)
        assert not finished.wait(timeout=2), "group removal bypassed the resolver's owner lock"

    assert finished.wait(timeout=30)
    worker.join(timeout=30)
    assert "error" not in result, result.get("error")
    fence = SharingAuthorityFence.objects.get(
        deployment_id=deployment_id,
        authority_owner="management",
        authority_reference=f"auth-group:{group.pk}",
    )
    assert fence.authority_revision == observed + 1
    assert fence.state == "unknown"
    assert range_obj.pk


def test_large_group_bulk_change_is_bounded_and_invalidates_every_fence():
    deployment_id = uuid4()
    member = User.objects.create_user("bulk-group-member@example.test")
    groups = tuple(Group.objects.create(name=f"bulk-group-{index}") for index in range(70))
    for group in groups:
        _fence(deployment_id, "management", f"auth-group:{group.pk}")

    member.groups.add(*groups)

    fences = SharingAuthorityFence.objects.filter(
        deployment_id=deployment_id,
        authority_owner="management",
        authority_reference__startswith="auth-group:",
    )
    assert fences.count() == 70
    assert not fences.exclude(authority_revision=2, state="unknown").exists()


def test_range_owner_transfer_invalidates_old_new_and_range_authority(principal_range):
    from management.services import principal_for_user
    from workspaces.services import add_account_member

    fixture = principal_range
    deployment_id = uuid4()
    new_owner = User.objects.create_user("transfer-new@example.test")
    new_principal = principal_for_user(new_owner)
    add_account_member(fixture.account.uuid, new_principal)
    refs = (
        ("engine", f"range:{fixture.realized.uuid}"),
        ("management", f"principal:{fixture.owner.uuid}"),
        ("management", f"principal:{new_principal.uuid}"),
        ("management", f"user:{new_owner.pk}"),
    )
    for owner, reference in refs:
        _fence(deployment_id, owner, reference)

    assert (
        reassign_range_owner_by_request(fixture.request.request_id, new_principal, credential=fixture.credential)
        is True
    )

    for owner, reference in refs:
        fence = SharingAuthorityFence.objects.get(
            deployment_id=deployment_id,
            authority_owner=owner,
            authority_reference=reference,
        )
        assert (fence.authority_revision, fence.state) == (2, "unknown")


def test_parked_publication_cannot_create_or_retry_membership_authority(settings):
    from django.test.utils import CaptureQueriesContext

    from config.model_access_sharing import publish_model_access_binding
    from shared.model_access.availability import ModelAccessParked

    deployment_id = uuid4()
    actor = User.objects.create_superuser("parked-retry-operator")
    catalog = _catalog(deployment_id)
    binding = _binding_dto(deployment_id=deployment_id)
    settings.MODEL_ACCESS_ENABLED = True
    for _ in range(2):
        with CaptureQueriesContext(connection) as queries, pytest.raises(ModelAccessParked):
            publish_model_access_binding(
                actor=actor,
                deployment_id=deployment_id,
                catalog=catalog,
                binding=binding,
                pool=_pool_dto(),
                expected_definition_revision=0,
            )
        assert len(queries) == 0
    assert not MembershipProjection.objects.filter(deployment_id=deployment_id).exists()
    assert not SharingAuthorityFence.objects.filter(deployment_id=deployment_id).exists()
