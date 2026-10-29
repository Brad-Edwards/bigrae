"""Real autocommit crash windows around the durable CTF/CMS/native launch."""

import pytest
from django.db import connection

from cms.models import RaesPackageSource, RangeInstance
from ctf.models import CTFRangeLaunch, CTFRangeRecovery, CTFSpareRange
from ctf.services.range.provision import provision_participant_range
from ctf.services.range.spares import provision_event_spares
from engine.models import ProvisionerLaunchIntent, Range
from tests.cms.conftest import make_pack as make_pack


class ProcessCrash(BaseException):
    """A process stop bypasses ordinary application exception handling."""


@pytest.mark.django_db(transaction=True)
def test_launch_proof_inputs_and_cancellation_are_immutable(launch_event, ctf_participant, organizer_credential):
    from uuid import uuid4

    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from ctf.services.range.launch_intents import cancel_event_launches

    with connection.execute_wrapper(crash_at("reserve")), pytest.raises(ProcessCrash):
        provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    intent = CTFRangeLaunch.objects.get()
    for values in (
        {"parameters": {"scenario": "changed"}},
        {"credential_uuid": uuid4()},
        {"subject_uuid": uuid4()},
        {"event_id": uuid4()},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            CTFRangeLaunch.all_objects.filter(pk=intent.pk).update(**values)
    CTFRangeLaunch.objects.filter(pk=intent.pk).update(range_instance_id=123, dispatched_at=timezone.now())
    cancel_event_launches(launch_event.pk, organizer_credential)
    for values in ({"deleted_at": None}, {"range_instance_id": 456}, {"dispatched_at": None}):
        with pytest.raises(IntegrityError), transaction.atomic():
            CTFRangeLaunch.all_objects.filter(pk=intent.pk).update(**values)


@pytest.fixture
def launch_event(ctf_event, organizer_user, settings, tmp_path, make_pack):
    from cms.scenarios.pack_validation import pack_digest
    from workspaces.services import resource_scope_from_ids

    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    settings.RAES_PACKAGE_ROOT = tmp_path
    from tests.cms.conftest import IMAGELESS_PACK_SDL

    root = make_pack(tmp_path / "synthetic-crash-test", sdl=IMAGELESS_PACK_SDL)
    scope = resource_scope_from_ids(
        kind=ctf_event.scope_kind,
        account_id=ctf_event.account_id,
        organization_id=ctf_event.organization_id,
        workspace_id=ctf_event.workspace_id,
    )
    source = RaesPackageSource.objects.create(
        scenario_id="synthetic-crash-test",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref=root.name,
        package_version="1.0.0",
        package_digest=pack_digest(root),
        conformance_status="passed",
        registered_by=organizer_user,
        owner_principal_uuid=ctf_event.owner_principal_uuid,
        scope_kind="account",
        account_uuid=scope.account_uuid,
    )
    ctf_event.scenario_id = source.scenario_id
    ctf_event.save(update_fields=["scenario_id"])
    return ctf_event


def crash_at(phase):
    """Inject failure at SQL effects, keeping every owning service real."""

    def execute(execute, sql, params, many, context):
        matches = {
            "reserve": sql.startswith('INSERT INTO "cms_request"'),
            "associate": (sql.startswith('UPDATE "ctf_participant"') and '"range_instance_id" =' in sql)
            or (sql.startswith('UPDATE "ctf_ctfsparerange"') and '"range_instance_id" =' in sql)
            or (sql.startswith('UPDATE "ctf_ctfrangerecovery"') and '"replacement_range_instance_id" =' in sql),
            "outbox": sql.startswith('INSERT INTO "engine_provisioner_launch_intent"'),
            "receipt": sql.startswith('UPDATE "ctf_ctfrangelaunch" SET "dispatched_at"'),
        }
        if matches[phase]:
            raise ProcessCrash(phase)
        return execute(sql, params, many, context)

    return execute


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["participant", "spare", "rebuild"])
@pytest.mark.parametrize("phase", ["reserve", "associate", "outbox", "receipt"])
def test_retry_resumes_one_original_generation(launch_event, ctf_participant, organizer_credential, kind, phase):
    from ctf.services.range.recovery_steps import _ensure_rebuild_replacement_ready
    from shared.model_access import OwnedReference

    recovery = (
        CTFRangeRecovery.objects.create(
            event=launch_event,
            participant=ctf_participant,
            old_range_instance_id=999,
            strategy="rebuild",
        )
        if kind == "rebuild"
        else None
    )

    def launch():
        if kind == "participant":
            return provision_participant_range(ctf_participant.pk, actor=organizer_credential)
        if recovery is not None:
            recovery.refresh_from_db()
            return _ensure_rebuild_replacement_ready(
                recovery,
                ctf_participant,
                OwnedReference(owner="ctf", reference=f"draw:{ctf_participant.pk}"),
                organizer_credential,
            )
        return provision_event_spares(launch_event.pk, 1, operator=organizer_credential)

    with connection.execute_wrapper(crash_at(phase)), pytest.raises(ProcessCrash):
        launch()
    intent = CTFRangeLaunch.objects.get()
    original_request = intent.pk
    assert intent.dispatched_at is None
    assert Range.objects.count() == (0 if phase == "reserve" else 1)
    assert ProvisionerLaunchIntent.objects.count() == (1 if phase == "receipt" else 0)

    launch()
    intent.refresh_from_db()
    assert intent.pk == original_request
    assert intent.dispatched_at is not None
    assert CTFRangeLaunch.objects.count() == RangeInstance.objects.count() == Range.objects.count() == 1
    assert ProvisionerLaunchIntent.objects.count() == 1
    native = Range.objects.get()
    assert native.request.request_id == original_request
    assert native.authority_changes.filter(operation="create").count() == 1
    if kind == "participant":
        ctf_participant.refresh_from_db()
        assert ctf_participant.range_request_id == original_request
        assert ctf_participant.range_instance_id == intent.range_instance_id
    elif kind == "spare":
        spare = CTFSpareRange.objects.get()
        assert spare.request_id == original_request
        assert spare.range_instance_id == intent.range_instance_id
    else:
        recovery.refresh_from_db()
        assert recovery.replacement_request_id == original_request
        assert recovery.replacement_range_instance_id == intent.range_instance_id


@pytest.mark.django_db(transaction=True)
def test_revoked_original_proof_cannot_resume_a_committed_native_range(
    launch_event,
    ctf_participant,
    organizer_credential,
):
    from ctf.exceptions import CTFPermissionError
    from management.models import PrincipalSession

    with connection.execute_wrapper(crash_at("associate")), pytest.raises(ProcessCrash):
        provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).delete()
    with pytest.raises(CTFPermissionError):
        provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    assert Range.objects.count() == 1
    assert ProvisionerLaunchIntent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_reserved_parent_does_not_make_an_unassociated_range_runnable(
    launch_event,
    ctf_participant,
    organizer_credential,
):
    from cms.exceptions import CMSError
    from cms.services import dispatch_bound_event_range

    with connection.execute_wrapper(crash_at("associate")), pytest.raises(ProcessCrash):
        provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    instance = RangeInstance.objects.get()
    with pytest.raises(CMSError):
        dispatch_bound_event_range(organizer_credential, launch_event.pk, instance.pk)
    assert ProvisionerLaunchIntent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("native_exists", [False, True])
def test_current_administrator_cleans_original_intent_after_initiator_logout(
    launch_event,
    ctf_participant,
    organizer_credential,
    organizer_user,
    client,
    native_exists,
):
    from cms.services import destroy_event_range_for_deletion
    from config.session_credentials import session_credential

    def stop(execute, sql, params, many, context):
        if (not native_exists and sql.startswith('INSERT INTO "engine_request"')) or (
            native_exists and (sql.startswith('UPDATE "ctf_participant"') and '"range_instance_id" =' in sql)
        ):
            raise ProcessCrash()
        return execute(sql, params, many, context)

    with connection.execute_wrapper(stop), pytest.raises(ProcessCrash):
        provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    intent = CTFRangeLaunch.objects.get()
    client.logout()
    client.force_login(organizer_user)
    current = session_credential(organizer_user, client.session, provider_check=False)
    assert current.credential_uuid != organizer_credential.credential_uuid
    destroy_event_range_for_deletion(current, launch_event.pk, None, request_id=intent.pk)
    instance = RangeInstance.all_objects.get()
    assert instance.status == ("destroying" if native_exists else "failed")
    if native_exists:
        cleanup = ProvisionerLaunchIntent.objects.get()
        assert cleanup.authority_kind == "cleanup"
        assert cleanup.credential_uuid == organizer_credential.credential_uuid
    else:
        assert ProvisionerLaunchIntent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_cleanup_caller_revoked_after_policy_cannot_queue_system_cleanup(
    launch_event,
    ctf_participant,
    organizer_credential,
    monkeypatch,
):
    from cms.exceptions import CMSError
    from cms.services import destroy_event_range_for_deletion
    from management.models import PrincipalSession
    from shared.authorization import configured_authorization_provider

    with connection.execute_wrapper(crash_at("associate")), pytest.raises(ProcessCrash):
        provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    intent = CTFRangeLaunch.objects.get()
    provider = configured_authorization_provider()
    check = provider.check

    def revoke(request):
        decision = check(request)
        if request.action == "event.delete":
            PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).delete()
        return decision

    monkeypatch.setattr(provider, "check", revoke)
    with pytest.raises(CMSError):
        destroy_event_range_for_deletion(organizer_credential, launch_event.pk, None, request_id=intent.pk)
    assert RangeInstance.objects.get().status == "provisioning"
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["participant", "spare", "rebuild"])
@pytest.mark.parametrize("phase", ["reserve", "native", "associate"])
def test_event_cleanup_cancels_unbound_launch_without_resurrection(
    launch_event,
    ctf_participant,
    organizer_credential,
    kind,
    phase,
):
    from ctf.services.range.launch_intents import execute_launch
    from ctf.services.range.lifecycle import cleanup_event_ranges
    from ctf.services.range.recovery_steps import _ensure_rebuild_replacement_ready
    from shared.model_access import OwnedReference

    recovery = (
        CTFRangeRecovery.objects.create(
            event=launch_event, participant=ctf_participant, old_range_instance_id=999, strategy="rebuild"
        )
        if kind == "rebuild"
        else None
    )

    def launch():
        if kind == "participant":
            return provision_participant_range(ctf_participant.pk, actor=organizer_credential)
        if recovery is not None:
            return _ensure_rebuild_replacement_ready(
                recovery,
                ctf_participant,
                OwnedReference(owner="ctf", reference=f"draw:{ctf_participant.pk}"),
                organizer_credential,
            )
        return provision_event_spares(launch_event.pk, 1, operator=organizer_credential)

    def stop(execute, sql, params, many, context):
        if phase == "native" and sql.startswith('INSERT INTO "engine_request"'):
            raise ProcessCrash()
        if phase != "native":
            return crash_at(phase)(execute, sql, params, many, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(stop), pytest.raises(ProcessCrash):
        launch()
    intent = CTFRangeLaunch.objects.get()
    result = cleanup_event_ranges(launch_event.pk, actor=organizer_credential)
    assert result["failed"] == 0
    intent.refresh_from_db()
    assert intent.deleted_at is not None
    if phase == "associate":
        assert RangeInstance.all_objects.get().status == "destroying"
        assert ProvisionerLaunchIntent.objects.get().authority_kind == "cleanup"
    else:
        assert not Range.objects.exists()
        assert not ProvisionerLaunchIntent.objects.exists()
        if kind == "participant":
            ctf_participant.refresh_from_db()
            assert ctf_participant.range_request_id is None
            assert ctf_participant.range_status == ""
        if kind == "spare":
            assert CTFSpareRange.objects.get().status == "failed"
    native_count = Range.objects.count()
    from cms.exceptions import CMSError

    with pytest.raises(CMSError):
        execute_launch(intent)
    assert Range.objects.count() == native_count
    assert not ProvisionerLaunchIntent.objects.filter(authority_kind="principal").exists()


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("window", ["before-reservation", "during-native-commit"])
def test_cleanup_serializes_with_inflight_creation(
    launch_event,
    ctf_participant,
    organizer_credential,
    window,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ctf.exceptions import CTFRangeError
    from ctf.services.range.lifecycle import cleanup_event_ranges

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock contention")
    creator_paused, release_creator, cleanup_waiting = Event(), Event(), Event()

    def create():
        armed = paused = False

        def pause(execute, sql, params, many, context):
            nonlocal armed, paused
            if sql.startswith('INSERT INTO "ctf_ctfrangelaunch"'):
                armed = True
            if (
                armed
                and not paused
                and (
                    (window == "before-reservation" and 'FROM "ctf_event"' in sql and "FOR UPDATE" in sql)
                    or (window == "during-native-commit" and sql.startswith('INSERT INTO "mission_control_range"'))
                )
            ):
                paused = True
                creator_paused.set()
                assert release_creator.wait(12)
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(pause):
                try:
                    return provision_participant_range(ctf_participant.pk, actor=organizer_credential)
                except CTFRangeError as exc:
                    return str(exc)
        finally:
            connection.close()

    def clean():
        def observe(execute, sql, params, many, context):
            if 'FROM "ctf_event"' in sql and "FOR UPDATE" in sql:
                cleanup_waiting.set()
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(observe):
                return cleanup_event_ranges(launch_event.pk, actor=organizer_credential)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(create)
        assert creator_paused.wait(12), (
            creating.result(timeout=1) if creating.done() else "Creator did not reach SQL fence"
        )
        cleaning = pool.submit(clean)
        assert cleanup_waiting.wait(12)
        if window == "before-reservation":
            assert cleaning.result(timeout=12)["failed"] == 0
        release_creator.set()
        creating.result(timeout=25)
        assert cleaning.result(timeout=25)["failed"] == 0
    assert not CTFRangeLaunch.objects.exists()
    if window == "before-reservation":
        assert not RangeInstance.objects.exists()
        assert not Range.objects.exists()
    else:
        assert RangeInstance.all_objects.get().status == "destroying"
        assert ProvisionerLaunchIntent.objects.filter(authority_kind="cleanup").exists()


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_keep_one_launch_and_one_outbox_operation(
    launch_event,
    ctf_participant,
    organizer_credential,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ctf.exceptions import CTFRangeError

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock contention")
    start = Barrier(2)

    def launch():
        entered = False

        def synchronize(execute, sql, params, many, context):
            nonlocal entered
            if not entered and sql.startswith("SELECT") and '"ctf_participant"' in sql:
                entered = True
                start.wait(timeout=8)
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(synchronize):
                try:
                    return provision_participant_range(ctf_participant.pk, actor=organizer_credential)
                except CTFRangeError as exc:
                    assert "already has a range" in str(exc)
                    return None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(launch) for _ in range(2)]
        assert any(future.result(timeout=25) is not None for future in futures)
        for future in futures:
            future.result(timeout=25)
    assert CTFRangeLaunch.objects.count() == RangeInstance.objects.count() == Range.objects.count() == 1
    assert ProvisionerLaunchIntent.objects.count() == 1
