"""Persisted native custody for warm-claim integration tests."""

from uuid import uuid4

from django.utils import timezone


def service_credential(settings, *, scopes=None):
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential_reference

    settings.GCP_SERVICE_TOKEN_AUDIENCE = settings.AWS_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    principal = Principal.objects.create(kind="service", name="Synthetic warm worker")
    binding = ProviderBinding.objects.create(
        principal=principal, issuer="https://accounts.google.com", subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=scopes or ["authorization:installation.launch_range", "authorization:installation.read_content"],
    )
    return resolve_service_credential_reference(admission.uuid, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE)


def warm_claim_fixture(django_user_model, settings, live_session_credential, *, claimant_kind="human"):
    from cms.models import RangeInstance, Request
    from cms.services._warm_pool_claim import WarmClaimRequest, _resolve_claim_candidates
    from engine.models import Range, WarmRangeGeneration
    from engine.models import Request as EngineRequest
    from shared.enums import RangeSource
    from shared.operation_authority import credential_reference_fields
    from shared.range_instantiation_policy import InstantiationPurpose
    from shared.warm_pool.policy import load_policy_json
    from tests.cms.test_launch_model_admission import _register
    from tests.engine.test_operation_input_raes import _plan
    from workspaces.models import Workspace
    from workspaces.services import add_account_member, create_account, hierarchy_target_scope

    worker = service_credential(settings)
    user = django_user_model.objects.create_user(username=f"claimant-{uuid4()}")
    credential = (
        live_session_credential(user)
        if claimant_kind == "human"
        else service_credential(
            settings, scopes=["authorization:workspace.launch_range", "authorization:workspace.read_content"]
        )
    )
    account = create_account(kind="team", name="Synthetic warm destination")
    add_account_member(account.uuid, credential.principal)
    workspace = Workspace.objects.get(organization__account_id=account.id)
    workspace.egress_policy = "none"
    workspace.save(update_fields=["egress_policy"])
    scope = hierarchy_target_scope("workspace", workspace.uuid)
    source = _register(user)
    source.owner_principal_uuid = worker.principal.uuid
    source.scope_kind = "installation"
    source.is_operator_published = True
    source.lock_digest = "sha256:" + "b" * 64
    source.save(update_fields=["lock_digest", "owner_principal_uuid", "scope_kind", "is_operator_published"])
    settings.WARM_POOL_POLICY = load_policy_json(
        '{"enabled":true,"buckets":[{"id":"synthetic-warm","backend":"gce","scenario":"scn","capacity_partition":"default","target":1,"minimum":0,"maximum":2,"idle_ttl_seconds":3600}]}'
    )
    settings.CLOUD_PROVIDER = "gcp"
    settings.ENGINE_TASK_CLUSTER = "synthetic-jobs"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic-provisioner@sha256:" + "1" * 64
    settings.ENGINE_TASK_SERVICE_ACCOUNT_NAME = "synthetic-provisioner"
    claim = WarmClaimRequest(
        credential=credential,
        scope=scope,
        scenario="scn",
        package_digest=source.package_digest,
        lock_digest=source.lock_digest,
        backend="gce",
        instantiation_purpose=InstantiationPurpose.LIVE_FIRE,
        range_source=RangeSource.MISSION_CONTROL,
        egress_mode="none",
        request_id=uuid4(),
    )
    request_uuid = uuid4()
    cms_request = Request.objects.create(
        request_id=request_uuid,
        request_type="range",
        scope_kind="installation",
        owner_principal_uuid=worker.principal.uuid,
        **credential_reference_fields(worker),
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        scenario_id="scn",
        scope_kind="installation",
        owner_principal_uuid=worker.principal.uuid,
        range_source="mission_control",
        status="provisioning",
        model_package_digest=source.package_digest,
    )
    engine_request = EngineRequest.objects.create(
        request_id=request_uuid,
        request_type="range",
        scope_kind="installation",
        owner_principal_uuid=worker.principal.uuid,
    )
    runtime = Range.objects.create(
        request=engine_request,
        scope_kind="installation",
        owner_principal_uuid=worker.principal.uuid,
        cms_range_uuid=instance.uuid,
        range_backend="gce",
        range_config=_plan(),
        status="provisioning",
        resource_generation=uuid4(),
    )
    instance.range_id = runtime.pk
    instance.save(update_fields=["range_id"])
    _, digest = _resolve_claim_candidates(claim, None)[0]
    generation = WarmRangeGeneration.objects.create(
        request_id=request_uuid,
        range=runtime,
        state="ready",
        bucket_id="synthetic-warm",
        operation_id=uuid4(),
        compatibility_digest=digest,
        effective_policy_fingerprint="sha256:" + "c" * 64,
        backend="gce",
        range_source="mission_control",
        capacity_partition="default",
        capacity_scope_ref=uuid4(),
        capacity_draw_key=uuid4(),
        idle_deadline=timezone.now() + timezone.timedelta(hours=1),
    )
    return claim, generation, instance, source, workspace
