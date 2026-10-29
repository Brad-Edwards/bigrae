"""Credential-native CMS warm-pool claim tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cms.services._warm_pool_claim import (
    ISOLATION_ACCOUNT,
    WarmClaimRequest,
    _resolve_claim_candidates,
    attempt_warm_claim,
)
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


@pytest.mark.django_db
@pytest.mark.parametrize("claimant_kind", ["human", "service"])
def test_claim_commits_lease_custody_and_exactly_one_activation(
    django_user_model, settings, live_session_credential, allow_all_policy, claimant_kind
):
    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange
    from tests.cms._warm_pool_fixtures import warm_claim_fixture

    claim, generation, instance, _source, workspace = warm_claim_fixture(
        django_user_model, settings, live_session_credential, claimant_kind=claimant_kind
    )
    assert attempt_warm_claim(claim) == generation.request_id
    instance.refresh_from_db()
    generation.refresh_from_db()
    assert generation.state == "claimed"
    assert instance.workspace_id == workspace.pk
    assert instance.owner_principal_uuid == claim.credential.principal.uuid
    assert instance.expires_at is not None and instance.maximum_expires_at >= instance.expires_at
    assert RangeAuthorityChange.objects.get().credential_uuid == claim.credential.credential_uuid
    intent = ProvisionerLaunchIntent.objects.get()
    assert intent.payload["operation"] == "activate"
    assert intent.credential_uuid == claim.credential.credential_uuid
    # A second attempt cannot claim the same generation or enqueue twice.
    assert attempt_warm_claim(claim) is None
    assert ProvisionerLaunchIntent.objects.count() == 1


@pytest.mark.django_db
def test_claim_source_fence_rolls_back_custody_and_lease(
    django_user_model, settings, live_session_credential, allow_all_policy
):
    from cms.exceptions import CMSError
    from cms.services._warm_pool_claim import _run_atomic_claim
    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange
    from tests.cms._warm_pool_fixtures import warm_claim_fixture

    claim, generation, instance, source, _ = warm_claim_fixture(django_user_model, settings, live_session_credential)
    # The declaration still refers to the admitted revision, which has now changed.
    source.package_digest = "sha256:" + "e" * 64
    source.save(update_fields=["package_digest"])
    with pytest.raises(CMSError):
        _run_atomic_claim(claim, [(generation.bucket_id, generation.compatibility_digest)])
    generation.refresh_from_db()
    instance.refresh_from_db()
    assert generation.state == "ready" and generation.claimed_by_request_id is None
    assert instance.scope_kind == "installation" and instance.expires_at is None
    assert not RangeAuthorityChange.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("changed", ["publication", "enabled", "staff_only", "conformance", "lock"])
def test_claim_rejects_source_eligibility_changed_after_admission(
    django_user_model, settings, live_session_credential, allow_all_policy, monkeypatch, changed
):
    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource, ScenarioMetadata
    from cms.services import _model_allocation
    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange
    from tests.cms._warm_pool_fixtures import warm_claim_fixture
    from workspaces.models import WorkspaceQuotaReservation

    claim, generation, instance, source, _ = warm_claim_fixture(django_user_model, settings, live_session_credential)
    original = _model_allocation.prepare_claim_model_sources

    def prepare_then_change(*args, **kwargs):
        prepared = original(*args, **kwargs)
        if changed in {"enabled", "staff_only"}:
            ScenarioMetadata.objects.update_or_create(
                scenario_id=source.scenario_id, defaults={changed: changed != "enabled"}
            )
        else:
            field, value = {
                "publication": ("is_operator_published", False),
                "conformance": ("conformance_status", "failed"),
                "lock": ("lock_digest", "sha256:" + "f" * 64),
            }[changed]
            RaesPackageSource.objects.filter(pk=source.pk).update(**{field: value})
        return prepared

    monkeypatch.setattr(_model_allocation, "prepare_claim_model_sources", prepare_then_change)
    with pytest.raises(CMSError):
        attempt_warm_claim(claim)
    instance.refresh_from_db()
    generation.refresh_from_db()
    assert generation.state == "ready" and generation.claimed_by_request_id is None
    assert instance.scope_kind == "installation" and instance.expires_at is None
    assert not RangeAuthorityChange.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not WorkspaceQuotaReservation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(("before", "after"), [("status-quo", "none"), ("none", "status-quo")])
def test_claim_rechecks_network_policy_under_locked_ancestry(
    django_user_model, settings, live_session_credential, allow_all_policy, monkeypatch, before, after
):
    from dataclasses import replace

    from cms.services import _model_allocation
    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange
    from tests.cms._warm_pool_fixtures import warm_claim_fixture
    from workspaces.models import Workspace

    claim, generation, instance, _source, workspace = warm_claim_fixture(
        django_user_model, settings, live_session_credential
    )
    Workspace.objects.filter(pk=workspace.pk).update(egress_policy=before)
    claim = replace(claim, egress_mode=before)
    candidates = _resolve_claim_candidates(claim, None)
    generation.compatibility_digest = candidates[0][1]
    generation.save(update_fields=["compatibility_digest"])
    prepare = _model_allocation.prepare_claim_model_sources
    checked = []

    def change_after_admission(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        Workspace.objects.filter(pk=workspace.pk).update(egress_policy=after)
        checked.append(True)
        return prepared

    monkeypatch.setattr(_model_allocation, "prepare_claim_model_sources", change_after_admission)
    assert attempt_warm_claim(claim) is None
    assert checked
    instance.refresh_from_db()
    generation.refresh_from_db()
    assert generation.state == "ready" and generation.claimed_by_request_id is None
    assert instance.scope_kind == "installation" and instance.expires_at is None
    assert not RangeAuthorityChange.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    workspace.refresh_from_db()
    assert workspace.egress_policy == after


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
