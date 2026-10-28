"""Warm-pool reconciler tests (#28).

Two properties matter most and are pinned first: the digest the reconciler stamps
on a prepared generation must equal the digest the launch claim path computes for
the warm target class (or a prepared generation is never claimable), and disabled
policy prevents replenishment while retained cleanup continues. The rest
exercises the reconcile pass: per-bucket retire of
stale/excess generations, bounded replenishment, service-owned preparation,
and fail-closed cleanup -- with the provider dispatch,
admission, and reservation seams patched so no cloud work or launch intent is emitted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cms.services import _warm_pool_reconcile as wpr
from cms.services._warm_pool_claim import ISOLATION_ACCOUNT
from cms.services._warm_pool_reconcile import (
    WARM_PURPOSE,
    WARM_RANGE_SOURCE,
    _bucket_compatibility_digest,
    reconcile_warm_pool,
)
from shared.warm_pool.compatibility import CompatibilityKey, compatibility_digest
from shared.warm_pool.policy import load_policy_json

_ENABLED = load_policy_json(
    '{"enabled": true, "replenish_concurrency": 4, "max_total_ready": 10,'
    ' "buckets": [{"id": "gce-example", "backend": "gce", "scenario": "example",'
    ' "capacity_partition": "default", "target": 2, "minimum": 0, "maximum": 5, "idle_ttl_seconds": 3600,'
    ' "region": "us-central1", "access_mode": "vpn"}]}'
)
_DISABLED = load_policy_json("")


class TestDigestParity:
    def test_reconciler_digest_matches_launch_claim_digest(self):
        """A generation the reconciler prepares is claimable by the warm target launch."""
        bucket = _ENABLED.buckets[0]
        package_digest = "sha256:" + "1" * 64
        lock_digest = "sha256:" + "2" * 64

        reconciler_digest = _bucket_compatibility_digest(bucket, package_digest=package_digest, lock_digest=lock_digest)
        launch_digest = compatibility_digest(
            CompatibilityKey(
                backend="gce",
                instantiation_purpose=WARM_PURPOSE.value,
                range_source=WARM_RANGE_SOURCE.value,
                workspace_isolation_class=ISOLATION_ACCOUNT,
                egress_mode="none",
                scenario=bucket.scenario,
                package_digest=package_digest,
                lock_digest=lock_digest,
            )
        )
        assert reconciler_digest == launch_digest

    def test_reconciler_digest_changes_with_package(self):
        bucket = _ENABLED.buckets[0]
        a = _bucket_compatibility_digest(bucket, package_digest="sha256:a", lock_digest="sha256:x")
        b = _bucket_compatibility_digest(bucket, package_digest="sha256:b", lock_digest="sha256:x")
        assert a != b


@pytest.mark.django_db
class TestReconcilePass:
    def test_disabled_policy_is_noop(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _DISABLED, raising=False)
        assert reconcile_warm_pool() == {
            "provisioned": 0,
            "retired": 0,
            "finalized": 0,
            "buckets": 0,
            "cleanup_cursor": 0,
        }

    def test_enabled_pass_reconciles_each_bucket_and_sweeps(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _ENABLED, raising=False)
        reconciled: list = []
        monkeypatch.setattr(
            wpr, "_reconcile_bucket", lambda bucket, policy, moment, summary: reconciled.append(bucket.id)
        )
        from engine.services import WarmRecoveryProgress

        monkeypatch.setattr(
            "engine.services.reconcile_retained_warm_generations", lambda *a, **k: WarmRecoveryProgress(3, 0, 0)
        )
        monkeypatch.setattr(wpr, "_emit_pool_gauges", lambda policy: None)

        summary = reconcile_warm_pool()
        assert reconciled == ["gce-example"]
        assert summary["buckets"] == 1
        assert summary["retired"] == 3

    def test_bucket_failure_is_isolated(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "WARM_POOL_POLICY", _ENABLED, raising=False)

        def _boom(bucket, policy, moment, summary):
            raise RuntimeError("bucket blew up")

        monkeypatch.setattr(wpr, "_reconcile_bucket", _boom)
        monkeypatch.setattr("engine.services.retire_removed_bucket_generations", lambda ids: 0)
        monkeypatch.setattr(wpr, "_emit_pool_gauges", lambda policy: None)
        # A single bucket's exception must not abort the pass.
        summary = reconcile_warm_pool()
        assert summary["buckets"] == 1


class TestReconcileBucketHelpers:
    def _bucket(self):
        return _ENABLED.buckets[0]

    def test_retire_stale_ready_partitions(self, monkeypatch):
        moment = __import__("django.utils.timezone", fromlist=["now"]).now()
        expired = SimpleNamespace(idle_deadline=moment, compatibility_digest="d")
        incompatible = SimpleNamespace(idle_deadline=None, compatibility_digest="other")
        live = SimpleNamespace(idle_deadline=None, compatibility_digest="d")
        monkeypatch.setattr("engine.services.ready_generations", lambda bucket_id: [expired, incompatible, live])
        monkeypatch.setattr("engine.services.retire_generation", lambda gen: True)
        survivors, retired = wpr._retire_stale_ready(self._bucket(), "d", moment)
        assert survivors == [live]
        assert retired == 2

    def test_scale_down_excess_oldest_first(self, monkeypatch):
        retired_gens: list = []
        monkeypatch.setattr("engine.services.retire_generation", lambda gen: retired_gens.append(gen) or True)
        bucket = SimpleNamespace(target=1)
        policy = SimpleNamespace(scale_down="oldest-first")
        live = [SimpleNamespace(name="a"), SimpleNamespace(name="b"), SimpleNamespace(name="c")]
        active, retired = wpr._scale_down_excess(bucket, policy, live, active=3)
        assert retired == 2
        assert active == 1
        assert [g.name for g in retired_gens] == ["a", "b"]

    def test_scale_down_excess_newest_first(self, monkeypatch):
        retired_gens: list = []
        monkeypatch.setattr("engine.services.retire_generation", lambda gen: retired_gens.append(gen) or True)
        bucket = SimpleNamespace(target=1)
        policy = SimpleNamespace(scale_down="newest-first")
        live = [SimpleNamespace(name="a"), SimpleNamespace(name="b"), SimpleNamespace(name="c")]
        _active, retired = wpr._scale_down_excess(bucket, policy, live, active=3)
        assert retired == 2
        assert [g.name for g in retired_gens] == ["c", "b"]

    def test_scale_down_no_excess_is_noop(self, monkeypatch):
        monkeypatch.setattr("engine.services.retire_generation", lambda gen: True)
        bucket = SimpleNamespace(target=3)
        policy = SimpleNamespace(scale_down="oldest-first")
        active, retired = wpr._scale_down_excess(bucket, policy, [], active=2)
        assert (active, retired) == (2, 0)

    def test_replenish_shortfall_bounded_by_budget(self, monkeypatch):
        monkeypatch.setattr("engine.services.total_active_generation_count", lambda: 0)
        provisioned_calls: list = []
        monkeypatch.setattr(
            wpr, "_provision_warm_generation", lambda bucket, policy, digest: provisioned_calls.append(digest) or True
        )
        # target=2, active=0 -> shortfall 2; maximum 5, ceiling 10, concurrency 4 -> budget 2.
        count = wpr._replenish_shortfall(self._bucket(), _ENABLED, "digest", active=0)
        assert count == 2
        assert len(provisioned_calls) == 2

    def test_replenish_none_when_at_target(self, monkeypatch):
        monkeypatch.setattr("engine.services.total_active_generation_count", lambda: 0)
        monkeypatch.setattr(wpr, "_provision_warm_generation", lambda *a: True)
        assert wpr._replenish_shortfall(self._bucket(), _ENABLED, "digest", active=2) == 0

    def test_replenish_capped_by_ceiling_headroom(self, monkeypatch):
        monkeypatch.setattr("engine.services.total_active_generation_count", lambda: 10)
        monkeypatch.setattr(wpr, "_provision_warm_generation", lambda *a: True)
        # ceiling 10 already fully drawn -> no headroom -> nothing provisioned.
        assert wpr._replenish_shortfall(self._bucket(), _ENABLED, "digest", active=0) == 0


class TestReconcileBucket:
    def test_unresolved_identity_retires_all_ready(self, monkeypatch):
        from django.utils import timezone

        monkeypatch.setattr("engine.services.finalize_retiring_generations", lambda b: 0)
        monkeypatch.setattr("engine.services.recover_stalled_generations", lambda b, **k: 0)
        monkeypatch.setattr(wpr, "_resolve_bucket_identity", lambda bucket: None)
        monkeypatch.setattr("engine.services.ready_generations", lambda b: [object(), object()])
        monkeypatch.setattr("engine.services.retire_generation", lambda gen: True)
        summary = {"provisioned": 0, "retired": 0, "finalized": 0, "buckets": 0}
        wpr._reconcile_bucket(_ENABLED.buckets[0], _ENABLED, timezone.now(), summary)
        assert summary["retired"] == 2
        assert summary["provisioned"] == 0

    def test_resolved_identity_retires_stale_and_replenishes(self, monkeypatch):
        from django.utils import timezone

        monkeypatch.setattr("engine.services.finalize_retiring_generations", lambda b: 1)
        monkeypatch.setattr("engine.services.recover_stalled_generations", lambda b, **k: 1)
        monkeypatch.setattr(wpr, "_resolve_bucket_identity", lambda bucket: ("sha256:p", "sha256:l"))
        monkeypatch.setattr(wpr, "_bucket_compatibility_digest", lambda bucket, **k: "current")
        monkeypatch.setattr(wpr, "_retire_stale_ready", lambda bucket, digest, moment: ([], 1))
        monkeypatch.setattr("engine.services.active_generation_count", lambda b: 0)
        monkeypatch.setattr(wpr, "_scale_down_excess", lambda bucket, policy, live, active: (active, 0))
        monkeypatch.setattr(wpr, "_replenish_shortfall", lambda bucket, policy, digest, active: 2)
        summary = {"provisioned": 0, "retired": 0, "finalized": 0, "buckets": 0}
        wpr._reconcile_bucket(_ENABLED.buckets[0], _ENABLED, timezone.now(), summary)
        assert summary == {"provisioned": 2, "retired": 1, "finalized": 0, "buckets": 0}


class TestGauges:
    def test_emit_pool_gauges_maps_counts(self, monkeypatch):
        monkeypatch.setattr(
            "engine.services.bucket_state_counts",
            lambda bucket_id: {"ready": 2, "provisioning": 1, "unhealthy": 0, "claimed": 3},
        )
        published: list = []
        monkeypatch.setattr("shared.warm_pool.metrics.emit_gauges", lambda snaps: published.append(snaps) or True)
        wpr._emit_pool_gauges(_ENABLED)
        assert len(published) == 1
        snap = published[0][0]
        assert snap.bucket_id == "gce-example"
        assert snap.ready == 2
        assert snap.claimed == 3


@pytest.mark.django_db
class TestServiceOwnedIdentity:
    def test_policy_fingerprint_is_deterministic(self):
        bucket = _ENABLED.buckets[0]
        first = wpr._policy_fingerprint(_ENABLED, bucket)
        second = wpr._policy_fingerprint(_ENABLED, bucket)
        assert first == second
        assert first.startswith("sha256:")

    def test_resolve_bucket_identity_found_and_missing(self):
        from django.contrib.auth import get_user_model

        from cms.models import RaesPackageSource

        bucket = _ENABLED.buckets[0]
        assert wpr._resolve_bucket_identity(bucket) is None
        registrar = get_user_model().objects.create_user(username="registrar@example.com")
        RaesPackageSource.objects.create(
            scenario_id="example",
            contract_kind="raes",
            contract_profile="shifter",
            package_ref="tests/packs/example",
            package_version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            lock_digest="sha256:" + "b" * 64,
            conformance_status="passed",
            registered_by=registrar,
        )
        assert wpr._resolve_bucket_identity(bucket) == ("sha256:" + "a" * 64, "sha256:" + "b" * 64)


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", ["unchanged", "digest_changed", "removed"])
def test_warm_reservation_fences_the_persisted_registered_source(
    django_user_model,
    settings,
    live_session_credential,
    allow_all_policy,
    mutation,
):
    from uuid import uuid4

    from cms.exceptions import CMSError
    from cms.models import RangeInstance
    from cms.services._warm_pool_reconcile import _reserve_warm_intent
    from shared.operation_authority import credential_reference_from_record
    from tests.cms._warm_pool_fixtures import service_credential, warm_claim_fixture

    _, _, _, source, _ = warm_claim_fixture(django_user_model, settings, live_session_credential)
    worker = service_credential(settings)
    original_id = source.pk
    if mutation == "digest_changed":
        type(source).objects.filter(pk=source.pk).update(package_digest="sha256:" + "e" * 64)
    elif mutation == "removed":
        type(source).objects.filter(pk=source.pk).delete()
    request_id = uuid4()
    if mutation == "unchanged":
        _reserve_warm_intent(worker, source, request_id)
        instance = RangeInstance.objects.select_related("request").get(request__request_id=request_id)
        assert instance.owner_principal_uuid == worker.principal.uuid and instance.scope_kind == "installation"
        assert instance.expires_at is None
        assert credential_reference_from_record(instance.request) == worker
    else:
        with pytest.raises((CMSError, ValueError)):
            _reserve_warm_intent(worker, source, request_id)
        assert not RangeInstance.objects.filter(request__request_id=request_id).exists()
        assert not type(source).objects.filter(pk=original_id, package_digest=source.package_digest).exists()
