"""Real launcher and cancellation calls share target-before-intent locking."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction

from tests.cms.conftest import make_pack as make_pack
from tests.cms.services.test_principal_range_launch import content_actor as content_actor
from tests.cms.services.test_principal_range_launch import launch_actor as launch_actor
from tests.engine.test_provisioner_launch_outbox import k8s_boundary as k8s_boundary

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("settlement", ["launch", "failure"])
def test_cancellation_serializes_before_launch_or_failure(launch_actor, k8s_boundary, settlement):
    """Observe locks acquired by production services; never supply locks in the test."""
    from cms.services import create_raes_native_range
    from engine.management.commands.drain_provisioner_launch_outbox import Command
    from engine.models import InterruptState, ProvisionerLaunchIntent, Range
    from engine.services._principal_lifecycle import commit_prepared_range_lifecycle, prepare_system_range_cleanup

    assert connection.vendor == "postgresql"
    actor = launch_actor
    launch = create_raes_native_range(actor.credential, actor.source.scenario_id, scope=actor.scope)
    runtime = Range.objects.get(request__request_id=launch.request_id)
    claimed = Command._claim_next()
    assert claimed is not None
    ProvisionerLaunchIntent.objects.filter(pk=claimed.pk).update(max_attempts=1)
    prepared = prepare_system_range_cleanup(launch.request_id)
    assert prepared.operation == "cancel"
    range_table = connection.ops.quote_name(Range._meta.db_table)
    intent_table = connection.ops.quote_name(ProvisionerLaunchIntent._meta.db_table)
    cancel_holds_range = threading.Event()
    worker_attempts_lock = threading.Event()
    release_cancel = threading.Event()
    preflight_complete = threading.Event()
    worker_locks = []

    def observe_cancel(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "FOR UPDATE" in sql and range_table in sql and not cancel_holds_range.is_set():
            cancel_holds_range.set()
            assert release_cancel.wait(8)
        return result

    def cancel():
        if settlement == "launch":
            assert preflight_complete.wait(8)
        try:
            with connection.execute_wrapper(observe_cancel):
                return commit_prepared_range_lifecycle(prepared)
        finally:
            connection.close()

    def observe_worker(execute, sql, params, many, context):
        if settlement == "launch" and "FOR UPDATE" in sql and range_table in sql and not preflight_complete.is_set():
            result = execute(sql, params, many, context)

            def after_preflight():
                preflight_complete.set()
                assert cancel_holds_range.wait(8)

            transaction.on_commit(after_preflight)
            return result
        if "FOR UPDATE" in sql and any(table in sql for table in (range_table, intent_table)):
            worker_locks.append(sql)
            worker_attempts_lock.set()
        return execute(sql, params, many, context)

    def run_worker():
        if settlement == "failure":
            assert cancel_holds_range.wait(8)
        try:
            with connection.execute_wrapper(observe_worker):
                if settlement == "launch":
                    Command()._launch(claimed)
                else:
                    Command()._record_failure(claimed, RuntimeError("synthetic dispatch failure"))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(cancel)
        worker_future = executor.submit(run_worker)
        try:
            assert worker_attempts_lock.wait(8)
            assert range_table in worker_locks[0], "Worker took the intent before the cancellation target"
        finally:
            release_cancel.set()
        assert cancel_future.result(timeout=15)
        worker_future.result(timeout=15)

    runtime.refresh_from_db()
    claimed.refresh_from_db()
    assert runtime.status == "destroying"
    assert claimed.interrupt_state == InterruptState.REQUESTED
    assert claimed.attempts == 0
    k8s_boundary.batch_api.create_namespaced_job.assert_not_called()


def test_revocation_after_preflight_prevents_job_submission(launch_actor, k8s_boundary):
    """Revoke the real proof after its preflight transaction commits."""
    from cms.services import create_raes_native_range
    from engine.management.commands.drain_provisioner_launch_outbox import Command
    from engine.models import Range
    from management.models import ServiceCredentialAdmission

    actor = launch_actor
    create_raes_native_range(actor.credential, actor.source.scenario_id, scope=actor.scope)
    claimed = Command._claim_next()
    assert claimed is not None
    range_table = connection.ops.quote_name(Range._meta.db_table)
    revoked = []

    def observe_preflight(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "FOR UPDATE" in sql and range_table in sql and not revoked:

            def revoke():
                ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
                revoked.append(True)

            transaction.on_commit(revoke)
        return result

    with connection.execute_wrapper(observe_preflight):
        Command()._launch(claimed)

    assert revoked
    k8s_boundary.batch_api.create_namespaced_job.assert_not_called()
