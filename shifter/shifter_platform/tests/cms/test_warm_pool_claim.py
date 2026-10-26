"""Credential-native CMS warm-pool claim tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cms.services._warm_pool_claim import (
    ISOLATION_ACCOUNT,
    WarmClaimRequest,
    _ClaimOutcome,
    _commit_cms_warm_claim,
    _resolve_claim_candidates,
    attempt_warm_claim,
)
from engine.services._principal_warm_claim import PreparedWarmRangeClaim
from shared.authorization import CredentialCeiling, TargetRef
from shared.credentials import CredentialContext
from shared.enums import RangeSource
from shared.identity_scope import PrincipalRef, ResourceScope
from shared.range_instantiation_policy import InstantiationPurpose
from shared.warm_pool.policy import WarmPoolOverride, load_policy_json

_DISABLED = load_policy_json("")
_ENABLED_GCE = load_policy_json(
    '{"enabled": true, "buckets": [{"id": "gce-example", "backend": "gce", "scenario": "example",'
    ' "capacity_partition": "default", "target": 1, "minimum": 0, "maximum": 2, "idle_ttl_seconds": 3600}]}'
)
_PACK_DIGEST = "sha256:" + "a" * 64


def _credential(*, target: TargetRef | None = None, action: str = "account.launch_range") -> CredentialContext:
    return CredentialContext(
        PrincipalRef(uuid4(), "service"),
        "service",
        uuid4(),
        CredentialCeiling(frozenset({action}), target),
        frozenset(),
    )


def _request(backend: str, scenario: str) -> WarmClaimRequest:
    account_uuid = uuid4()
    scope = ResourceScope("account", account_uuid)
    return WarmClaimRequest(
        credential=_credential(target=TargetRef("account", account_uuid)),
        scope=scope,
        scenario=scenario,
        package_digest=_PACK_DIGEST,
        lock_digest="sha256:bbb",
        backend=backend,
        instantiation_purpose=InstantiationPurpose.LIVE_FIRE,
        range_source=RangeSource.MISSION_CONTROL,
        egress_mode="status-quo",
        request_id=uuid4(),
    )


class TestDecisionGates:
    def test_disabled_policy_cold_falls_back(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _DISABLED, raising=False)
        assert attempt_warm_claim(_request("gce", "example")) is None

    def test_unsupported_backend_cold_falls_back(self, monkeypatch):
        from django.conf import settings

        policy = load_policy_json(
            '{"enabled": true, "buckets": [{"id": "gdc-x", "backend": "gdc", "scenario": "example",'
            ' "capacity_partition": "default", "target": 1, "minimum": 0, "maximum": 2, '
            '"idle_ttl_seconds": 3600}]}'
        )
        monkeypatch.setattr(settings, "WARM_POOL_POLICY", policy, raising=False)
        assert attempt_warm_claim(_request("gdc", "example")) is None

    def test_no_matching_bucket_cold_falls_back(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _ENABLED_GCE, raising=False)
        assert attempt_warm_claim(_request("gce", "other")) is None

    def test_event_policy_workspace_never_claims_unpinned_generation(self, monkeypatch):
        from dataclasses import replace

        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _ENABLED_GCE, raising=False)
        request = replace(_request("gce", "example"), range_source=RangeSource.CTF, policy_workspace_id=2)
        assert attempt_warm_claim(request) is None

    def test_only_account_scope_is_eligible(self, monkeypatch):
        from dataclasses import replace

        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _ENABLED_GCE, raising=False)
        request = replace(_request("gce", "example"), scope=ResourceScope("installation"))
        assert _resolve_claim_candidates(request, None) == []

    def test_narrowed_policy_excludes_an_unauthorized_candidate(self, monkeypatch):
        from django.conf import settings

        policy = load_policy_json(
            '{"enabled": true, "buckets": ['
            '{"id": "authorized", "backend": "gce", "scenario": "example", "capacity_partition": "default", '
            '"target": 1, "minimum": 0, "maximum": 2, "idle_ttl_seconds": 3600},'
            '{"id": "excluded", "backend": "gce", "scenario": "example", "capacity_partition": "default", '
            '"target": 1, "minimum": 0, "maximum": 2, "idle_ttl_seconds": 3600}'
            "]}"
        )
        monkeypatch.setattr(settings, "WARM_POOL_POLICY", policy, raising=False)
        candidates = _resolve_claim_candidates(_request("gce", "example"), WarmPoolOverride(bucket_ids=("authorized",)))
        assert [bucket_id for bucket_id, _digest in candidates] == ["authorized"]

    def test_digest_uses_account_compatibility_class(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _ENABLED_GCE, raising=False)
        candidates = _resolve_claim_candidates(_request("gce", "example"), None)
        assert candidates[0][0] == "gce-example"
        assert ISOLATION_ACCOUNT == "account"


class TestClaimOrchestration:
    def test_hit_enqueues_after_committed_claim(self, monkeypatch):
        from django.conf import settings

        settings.WARM_POOL_POLICY = _ENABLED_GCE
        outcome = _ClaimOutcome(uuid4(), "gce-example", uuid4())
        monkeypatch.setattr("cms.services._warm_pool_claim._can_claim_base_range", lambda request: True)
        monkeypatch.setattr("cms.services._warm_pool_claim._run_atomic_claim", lambda request, candidates: outcome)
        enqueued: list = []
        emitted: list = []
        monkeypatch.setattr(
            "engine.services.enqueue_range_activation",
            lambda request_id, **kwargs: enqueued.append(request_id),
        )
        monkeypatch.setattr("shared.warm_pool.metrics.emit_claim_outcome", lambda **kwargs: emitted.append(kwargs))

        assert attempt_warm_claim(_request("gce", "example")) == outcome.request_id
        assert enqueued == [outcome.request_id]
        assert emitted[-1]["outcome"] == "hit"

    def test_model_path_does_not_enqueue_activation_twice(self, monkeypatch):
        from django.conf import settings

        settings.WARM_POOL_POLICY = _ENABLED_GCE
        outcome = _ClaimOutcome(uuid4(), "gce-example", uuid4(), activation_enqueued=True)
        monkeypatch.setattr("cms.services._warm_pool_claim._can_claim_base_range", lambda request: True)
        monkeypatch.setattr("cms.services._warm_pool_claim._run_atomic_claim", lambda request, candidates: outcome)
        enqueued: list = []
        monkeypatch.setattr(
            "engine.services.enqueue_range_activation",
            lambda request_id, **kwargs: enqueued.append(request_id),
        )
        monkeypatch.setattr("shared.warm_pool.metrics.emit_claim_outcome", lambda **kwargs: None)

        assert attempt_warm_claim(_request("gce", "example")) == outcome.request_id
        assert enqueued == []

    def test_miss_cold_falls_back(self, monkeypatch):
        from django.conf import settings

        settings.WARM_POOL_POLICY = _ENABLED_GCE
        monkeypatch.setattr("cms.services._warm_pool_claim._can_claim_base_range", lambda request: True)
        monkeypatch.setattr("cms.services._warm_pool_claim._run_atomic_claim", lambda request, candidates: None)
        assert attempt_warm_claim(_request("gce", "example")) is None


@pytest.mark.django_db
def test_cms_claim_moves_installation_custody_to_exact_account_without_a_user(monkeypatch):
    from cms.models import RangeInstance, Request
    from shared.operation_authority import credential_reference_fields
    from workspaces.services import create_account

    worker = _credential(target=TargetRef("installation"), action="installation.launch_range")
    account = create_account(kind="team", name="Warm claim target")
    claimant = _credential(target=TargetRef("account", account.uuid))
    cms_request = Request.objects.create(
        request_id=uuid4(),
        request_type="range",
        user=None,
        owner_principal_uuid=worker.principal.uuid,
        scope_kind="installation",
        **credential_reference_fields(worker),
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        scenario_id="example",
        user_id=None,
        owner_principal_uuid=worker.principal.uuid,
        scope_kind="installation",
        range_source=RangeSource.MISSION_CONTROL.value,
        status="provisioning",
    )
    prepared = PreparedWarmRangeClaim(
        claimant,
        ResourceScope("account", account.uuid),
        ("account", account.id, None, None),
    )
    monkeypatch.setattr("shared.credentials.resolve_local_credential", lambda credential: credential)

    _commit_cms_warm_claim(instance, prepared, None)

    instance.refresh_from_db()
    cms_request.refresh_from_db()
    assert instance.user_id is None and cms_request.user_id is None
    assert instance.owner_principal_uuid == claimant.principal.uuid
    assert cms_request.owner_principal_uuid == claimant.principal.uuid
    assert instance.scope_kind == cms_request.scope_kind == "account"
    assert instance.account_id == cms_request.account_id == account.id


@pytest.mark.django_db
def test_system_range_lookup_accepts_only_the_correlated_request():
    from cms.models import RangeInstance, Request
    from cms.services._warm_pool_claim import _system_range_instance_for

    request = Request.objects.create(request_id=uuid4(), request_type="range", scope_kind="installation")
    instance = RangeInstance.objects.create(
        request=request,
        scenario_id="example",
        range_source=RangeSource.MISSION_CONTROL.value,
        status="provisioning",
        scope_kind="installation",
    )
    assert _system_range_instance_for(request.request_id).pk == instance.pk
    assert _system_range_instance_for(uuid4()) is None


@pytest.mark.django_db
def test_engine_claim_moves_owner_scope_and_retains_claimant_authority(monkeypatch):
    from django.utils import timezone

    from engine.models import Range, RangeAuthorityChange, Request, WarmRangeGeneration
    from engine.services import commit_warm_range_claim
    from workspaces.models import Account, AccountMembership
    from workspaces.services import create_account

    worker = _credential(target=TargetRef("installation"), action="installation.launch_range")
    claimant = _credential()
    account = create_account(kind="team", name="Engine warm claim target")
    account_row = Account.objects.get(uuid=account.uuid)
    AccountMembership.objects.create(account=account_row, principal_uuid=claimant.principal.uuid)
    request = Request.objects.create(
        request_id=uuid4(),
        request_type="range",
        owner_principal_uuid=worker.principal.uuid,
        scope_kind="installation",
    )
    public_uuid = uuid4()
    generation_id = uuid4()
    row = Range.objects.create(
        request=request,
        cms_range_uuid=public_uuid,
        owner_principal_uuid=worker.principal.uuid,
        scope_kind="installation",
        status=Range.Status.PROVISIONING,
        resource_generation=generation_id,
    )
    claimant_request_id = uuid4()
    WarmRangeGeneration.objects.create(
        request_id=request.request_id,
        range=row,
        state=WarmRangeGeneration.State.CLAIMED,
        claimed_by_request_id=claimant_request_id,
        claimed_at=timezone.now(),
        bucket_id="account-warm",
        compatibility_digest="sha256:" + "a" * 64,
        effective_policy_fingerprint="sha256:" + "b" * 64,
        backend="gce",
        range_source=RangeSource.MISSION_CONTROL.value,
        capacity_partition="default",
        capacity_scope_ref=uuid4(),
        capacity_draw_key=uuid4(),
    )
    prepared = PreparedWarmRangeClaim(
        claimant,
        ResourceScope("account", account.uuid),
        ("account", account.id, None, None),
    )
    monkeypatch.setattr("engine.services._principal_warm_claim.resolve_local_credential", lambda value: value)

    assert (
        commit_warm_range_claim(
            prepared,
            request_id=request.request_id,
            claimant_request_id=claimant_request_id,
            cms_range_uuid=public_uuid,
            user_id=None,
        )
        == row.pk
    )

    row.refresh_from_db()
    request.refresh_from_db()
    assert row.owner_principal_uuid == request.owner_principal_uuid == claimant.principal.uuid
    assert row.scope_kind == request.scope_kind == "account"
    assert row.account_id == request.account_id == account.id
    change = RangeAuthorityChange.objects.get(range=row)
    assert change.initiating_principal_uuid == claimant.principal.uuid
    assert change.authority_action == "account.launch_range"
    assert change.authority_account_uuid == account.uuid
