"""Principal-native CTF spare-pool coverage for the identity hard cut."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cms.models import RaesPackageSource, RangeInstance
from ctf.enums import SpareRangeStatus
from ctf.exceptions import CTFNotFoundError, CTFPermissionError
from ctf.models import CTFEvent, CTFSpareRange
from ctf.services.range.spares import cleanup_event_spares, get_event_spare_summary, provision_event_spares
from engine.models import Range
from shared.audit import AuditAction
from shared.models import AuditLog


@pytest.fixture
def event_with_scenario(ctf_event, organizer_user, monkeypatch, settings) -> CTFEvent:
    """Give an event an account-owned source and hold provider dispatch at the seam."""
    from cms.raes.dispatch import CmsRaesDispatchPort
    from workspaces.services import resource_scope_from_ids

    monkeypatch.setattr("engine.services._raes_range.start_raes_range_provisioning", lambda *_a, **_kw: None)
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64

    def dispatch(request_id, credential, source, options):
        port = CmsRaesDispatchPort(
            user_id=None,
            request_id=str(request_id),
            workspace_id=options.workspace_id,
            backend_admission=options.backend_admission,
            egress_mode=options.egress_mode,
            credential=credential,
            scope=options.principal_scope,
            source=source,
            owner_principal=options.owner_principal,
            authority_action=options.authority_action,
            authority_target=options.authority_target,
        )
        port.realize({"kind": "raes_provisioning_plan", "raes_version": "2.0", "resources": {}})

    monkeypatch.setattr("cms.services._raes_dispatch.dispatch_repo_raes_package", dispatch)

    scope = resource_scope_from_ids(
        kind=ctf_event.scope_kind,
        account_id=ctf_event.account_id,
        organization_id=ctf_event.organization_id,
        workspace_id=ctf_event.workspace_id,
    )
    source = RaesPackageSource.objects.create(
        scenario_id="ctf-spare-pool-test",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="tests/packs/ctf-spare-pool-test",
        package_version="1.0.0",
        package_digest="sha256:" + "a" * 64,
        conformance_status="passed",
        registered_by=organizer_user,
        owner_principal_uuid=ctf_event.owner_principal_uuid,
        scope_kind="account",
        account_uuid=scope.account_uuid,
    )
    ctf_event.scenario_id = source.scenario_id
    ctf_event.range_config = {"agents_by_os": {}, "ngfw_enabled": False}
    ctf_event.save(update_fields=["scenario_id", "range_config"])
    return ctf_event


@pytest.mark.django_db
def test_spares_are_event_principal_owned_without_placeholder_users(
    event_with_scenario, organizer_user, organizer_credential
):
    from django.contrib.auth import get_user_model

    before_users = get_user_model().objects.count()
    result = provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)

    assert result == {
        "event_id": str(event_with_scenario.pk),
        "target_count": 2,
        "existing": 0,
        "created": 2,
    }
    assert get_user_model().objects.count() == before_users
    spares = list(CTFSpareRange.objects.filter(event=event_with_scenario))
    assert len(spares) == 2
    for spare in spares:
        instance = RangeInstance.objects.get(pk=spare.range_instance_id)
        runtime = Range.objects.get(request__request_id=instance.request.request_id)
        assert instance.user_id == organizer_user.pk
        assert runtime.user_id == organizer_user.pk
        assert instance.owner_principal_uuid == event_with_scenario.owner_principal_uuid
        assert runtime.owner_principal_uuid == event_with_scenario.owner_principal_uuid
        assert instance.event_uuid == event_with_scenario.pk
        assert runtime.event_uuid == event_with_scenario.pk
        assert runtime.egress_mode == "none"

    audit = AuditLog.objects.get(action=AuditAction.SPARE_PROVISION)
    assert audit.actor_id is None
    assert audit.actor_principal_uuid == event_with_scenario.owner_principal_uuid
    assert audit.new_state["created"] == 2


@pytest.mark.django_db
def test_spare_top_up_is_idempotent_and_replaces_terminal_rows(event_with_scenario, organizer_credential):
    provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    first = set(CTFSpareRange.objects.values_list("pk", flat=True))
    same = provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    assert same["existing"] == 2 and same["created"] == 0
    assert set(CTFSpareRange.objects.values_list("pk", flat=True)) == first

    CTFSpareRange.objects.filter(pk=next(iter(first))).update(status=SpareRangeStatus.FAILED.value)
    replaced = provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    assert replaced["existing"] == 1 and replaced["created"] == 1
    assert CTFSpareRange.objects.count() == 3


@pytest.mark.django_db
def test_spare_provision_requires_live_event_range_authority(event_with_scenario, participant_user):
    with pytest.raises(CTFPermissionError):
        provision_event_spares(event_with_scenario.pk, 1, operator=participant_user)
    assert not CTFSpareRange.objects.exists()


@pytest.mark.django_db
def test_spare_provision_requires_an_initiating_credential(event_with_scenario):
    with pytest.raises(ValueError, match="authority unavailable"):
        provision_event_spares(event_with_scenario.pk, 1)
    assert not CTFSpareRange.objects.exists()


@pytest.mark.django_db
def test_spare_provision_failure_is_recorded(ctf_event, organizer_credential, monkeypatch):
    monkeypatch.setattr("ctf.bridges.cms_create_range", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    result = provision_event_spares(ctf_event.pk, 2, operator=organizer_credential)
    assert result["created"] == 0
    assert set(CTFSpareRange.objects.values_list("status", flat=True)) == {SpareRangeStatus.FAILED.value}


@pytest.mark.django_db
def test_spare_summary_counts_statuses(event_with_scenario, organizer_credential):
    provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    rows = list(CTFSpareRange.objects.order_by("pk"))
    rows[0].status = SpareRangeStatus.READY.value
    rows[0].save(update_fields=["status", "updated_at"])
    summary = get_event_spare_summary(event_with_scenario.pk)
    assert summary["target_count"] == 2
    assert summary["counts"][SpareRangeStatus.READY.value] == 1
    assert summary["counts"][SpareRangeStatus.PROVISIONING.value] == 1
    assert summary["available"] == 1


@pytest.mark.django_db
def test_cleanup_dispatches_with_operator_credential(event_with_scenario, organizer_credential, monkeypatch):
    provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    seen = []

    def destroy(credential, event_id, range_instance_id):
        seen.append((credential, event_id, range_instance_id))

    monkeypatch.setattr("ctf.bridges.cms_destroy_range", destroy)
    result = cleanup_event_spares(event_with_scenario.pk, actor=organizer_credential)
    assert result == {"event_id": str(event_with_scenario.pk), "destroyed": 2, "failed": 0}
    assert len(seen) == 2
    assert all(item[0].principal.uuid == event_with_scenario.owner_principal_uuid for item in seen)
    assert all(item[1] == event_with_scenario.pk for item in seen)
    assert set(CTFSpareRange.objects.values_list("status", flat=True)) == {SpareRangeStatus.FAILED.value}


@pytest.mark.django_db
def test_cleanup_preserves_retryable_row_after_dispatch_failure(event_with_scenario, organizer_credential, monkeypatch):
    provision_event_spares(event_with_scenario.pk, 1, operator=organizer_credential)
    monkeypatch.setattr("ctf.bridges.cms_destroy_range", lambda *_args: (_ for _ in ()).throw(RuntimeError("down")))
    result = cleanup_event_spares(event_with_scenario.pk, actor=organizer_credential)
    assert result["destroyed"] == 0 and result["failed"] == 1
    assert CTFSpareRange.objects.get().status == SpareRangeStatus.PROVISIONING.value


@pytest.mark.django_db
def test_unknown_event_is_not_found(organizer_user):
    missing = uuid4()
    with pytest.raises(CTFNotFoundError):
        provision_event_spares(missing, 1, operator=organizer_user)
    with pytest.raises(CTFNotFoundError):
        cleanup_event_spares(missing, actor=organizer_user)
