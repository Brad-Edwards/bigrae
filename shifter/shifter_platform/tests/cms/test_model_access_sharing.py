"""Retained CMS correlation and invalidation contracts; public selectors stay parked."""

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from cms import services as cms_services
from cms.models import RangeInstance
from cms.models import Request as CmsRequest
from engine.models import Range
from engine.models import Request as EngineRequest
from shared.enums import RequestType
from shared.model_access import SelectorKind, SharingSelector
from shared.model_access.availability import ModelAccessParked
from workspaces.models import Organization, Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.mark.parametrize(
    "kind",
    [
        SelectorKind.USER,
        SelectorKind.AUTH_GROUP,
        SelectorKind.WORKSPACE,
        SelectorKind.ORGANIZATION,
        SelectorKind.ALL_RANGES,
        SelectorKind.SELECTED_RANGES,
    ],
)
def test_public_cms_selector_is_parked_before_owner_data(kind):
    ids = () if kind == SelectorKind.ALL_RANGES else (str(uuid4()),)
    selector = SharingSelector(kind=kind, ids=ids)
    operator = User.objects.create_superuser("parked-cms-selector")
    with CaptureQueriesContext(connection) as queries, pytest.raises(ModelAccessParked):
        cms_services.resolve_model_access_selector(operator, selector)
    assert len(queries) == 0


@pytest.mark.parametrize("with_group", [False, True])
def test_retained_user_fences_advance_when_principal_ownership_changes(with_group):
    from engine.models import SharingAuthorityFence
    from engine.services import publish_authority_fence
    from workspaces.services import create_account

    owner = User.objects.create_user("fenced-owner@test")
    replacement = User.objects.create_user("fenced-new-owner@test")
    account = create_account(kind="individual", name="Retained accounting owner", owner=owner.identity_principal)
    runtime = Range.objects.create(
        user=owner,
        status=Range.Status.READY,
        owner_principal_uuid=owner.identity_principal.uuid,
        scope_kind="account",
        account_id=account.id,
    )
    refs = [f"user:{owner.pk}"]
    if with_group:
        group = Group.objects.create(name="Retained selector group")
        group.user_set.add(owner)
        refs.append(f"auth-group:{group.pk}")
    deployment = uuid4()
    # Retained accounting/invalidation contract setup, not runtime publication.
    for reference in refs:
        publish_authority_fence(
            deployment_id=deployment,
            authority_ref={"owner": "management", "reference": reference},
            authority_revision=1,
            state="allowed",
        )
    runtime.owner_principal_uuid = replacement.identity_principal.uuid
    runtime.save(update_fields=["owner_principal_uuid"])
    for reference in refs:
        fence = SharingAuthorityFence.objects.get(
            deployment_id=deployment, authority_owner="management", authority_reference=reference
        )
        if reference.startswith("user:"):
            assert fence.authority_revision > 1
            assert fence.state == "unknown"
        else:
            # Range custody changed; membership of the group did not.
            assert fence.authority_revision == 1 and fence.state == "allowed"


def test_selected_range_correlation_maps_engine_uuid_to_active_cms_instance():
    owner = User.objects.create_user("correlation-owner@test")
    organization = Organization.objects.create(name="Correlation Org")
    workspace = Workspace.objects.create(organization=organization, name="Correlation Workspace")
    request_uuid = uuid4()
    cms_request = CmsRequest.objects.create(
        request_id=request_uuid,
        request_type=RequestType.RANGE.value,
        user=owner,
        workspace_id=workspace.pk,
    )
    engine_request = EngineRequest.objects.create(
        request_id=request_uuid,
        request_type=RequestType.RANGE.value,
        user=owner,
    )
    engine_range = Range.objects.create(
        request=engine_request,
        user=owner,
        workspace_id=workspace.pk,
        status=Range.Status.READY,
    )
    cms_range = RangeInstance.objects.create(
        request=cms_request,
        scenario_id="basic",
        user_id=owner.pk,
        workspace_id=workspace.pk,
        status=Range.Status.READY,
    )

    resolved = cms_services.resolve_model_access_selected_ranges((engine_range.uuid,))

    assert resolved[0].range_instance_id == cms_range.pk
    assert resolved[0].range_view.range_ref.reference == f"range:{engine_range.uuid}"
