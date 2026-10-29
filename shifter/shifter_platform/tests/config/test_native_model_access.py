"""ADR-068 cannot be bypassed by flags, native proofs or legacy SQL roles."""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db
ROUTES = tuple(
    f"/api/v1/model-access/bindings/{operation}/"
    for operation in ("validate", "selector-preview", "policy-preview", "publish", "drain")
)


@pytest.fixture(params=[False, True], ids=["disabled", "enabled"])
def model_catalog(request, settings):
    from shared.model_access import seal_catalog
    from tests.engine.services.test_sharing import _catalog

    payload = _catalog().model_dump(mode="json")
    payload["enabled"] = request.param
    catalog = seal_catalog(payload)
    settings.MODEL_ACCESS_ENABLED = request.param
    settings.MODEL_ACCESS_CATALOG = catalog
    return catalog


@pytest.fixture(params=["human", "service", "legacy-owner", "legacy-operator", "narrow-proof"])
def authenticated_client(request, django_user_model, live_session_credential, settings):
    from shared.authorization import CredentialCeiling

    client = APIClient()
    if request.param == "service":
        from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
        from management.services import resolve_service_credential_reference

        principal = Principal.objects.create(kind="service", name="Parked sharing caller")
        binding = ProviderBinding.objects.create(
            principal=principal, issuer="https://accounts.google.com", subject=str(uuid4())
        )
        admission = ServiceCredentialAdmission.objects.create(
            binding=binding,
            audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
            scopes=["authorization:workspace.publish_model_access", "model-access:sharing:read"],
        )
        credential = resolve_service_credential_reference(admission.uuid, audience=admission.audience)
        client.force_authenticate(user=None, token=credential)
        return client

    user = django_user_model.objects.create_user(
        username=f"parked-sharing-{request.param}",
        is_staff=request.param == "legacy-operator",
        is_superuser=request.param == "legacy-operator",
    )
    if request.param == "legacy-owner":
        from workspaces.models import Workspace, WorkspaceMembership
        from workspaces.services import create_account

        account = create_account(kind="team", name="Parked sharing legacy workspace")
        workspace = Workspace.objects.get(organization__account_id=account.id)
        WorkspaceMembership.objects.create(workspace=workspace, user=user, role="owner")
    credential = None
    if request.param in {"human", "narrow-proof"}:
        credential = live_session_credential(user)
        if request.param == "narrow-proof":
            credential = replace(credential, ceiling=CredentialCeiling(frozenset()))
    client.force_authenticate(user=user, token=credential)
    return client


def test_mounted_sharing_is_unavailable_before_parsing_or_domain_queries(authenticated_client, model_catalog):
    # Malformed JSON would fail parsing if the parked permission ran too late.
    for route in ROUTES:
        with CaptureQueriesContext(connection) as queries:
            response = authenticated_client.post(route, "{", content_type="application/json")
        assert response.status_code == 503, (route, response.data)
        assert response.data["error"]["code"] == "model_access_unavailable"
        assert len(queries) == 0, (route, queries.captured_queries)


def test_anonymous_sharing_keeps_authentication_contract(model_catalog):
    for route in ROUTES:
        response = APIClient().post(route, {}, format="json")
        assert response.status_code == 401, (route, response.data)


@pytest.mark.parametrize("operation", ["selector", "publish", "drain", "refresh", "cms", "ctf"])
def test_direct_sharing_boundaries_refuse_before_input_or_database_access(operation, model_catalog):
    from cms.services import resolve_model_access_selector as cms_selector
    from config.model_access_sharing import (
        drain_model_access_binding,
        publish_model_access_binding,
        refresh_model_launch_projections,
        resolve_model_access_selector,
    )
    from ctf.services import resolve_model_access_selector as ctf_selector
    from shared.model_access.availability import ModelAccessParked

    # These inputs deliberately have no publisher or selector attributes. The
    # guard must precede both validation and all owner-domain reads/effects.
    calls = {
        "selector": lambda: resolve_model_access_selector(object(), {}),
        "publish": lambda: publish_model_access_binding(
            actor=object(),
            deployment_id=model_catalog.deployment_id,
            catalog=model_catalog,
            binding=object(),
            pool=object(),
            expected_definition_revision=0,
        ),
        "drain": lambda: drain_model_access_binding(
            actor=object(),
            deployment_id=model_catalog.deployment_id,
            sharing_binding_id="unread-binding",
            expected_definition_revision=0,
        ),
        "refresh": lambda: refresh_model_launch_projections(model_catalog.deployment_id),
        "cms": lambda: cms_selector(object(), {}),
        "ctf": lambda: ctf_selector(object(), {}),
    }
    with CaptureQueriesContext(connection) as queries, pytest.raises(ModelAccessParked) as denied:
        calls[operation]()
    assert denied.value.code == "runtime.model_access_parked"
    assert len(queries) == 0, queries.captured_queries
