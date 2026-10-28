"""Real warm/cold account and workspace lock contention."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_warm_and_cold_launches_share_the_account_first_lock_order(
    django_user_model,
    settings,
    live_session_credential,
    allow_all_policy,
):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL lock contention")
    from cms.services._scoped_launch_reservation import reserve_scoped_launch
    from cms.services._scoped_launch_types import ScopedRangeLaunchOptions
    from cms.services._scoped_range_launch import _prepare_launch
    from cms.services._warm_pool_claim import attempt_warm_claim
    from shared.enums import RangeSource
    from tests.cms._warm_pool_fixtures import warm_claim_fixture
    from workspaces.models import Account
    from workspaces.services import add_account_member

    claim, generation, _, source, _workspace = warm_claim_fixture(django_user_model, settings, live_session_credential)
    cold_user = django_user_model.objects.create_user(username="cold-contender")
    cold_credential = live_session_credential(cold_user)
    account = Account.objects.get(uuid=claim.scope.account_uuid)
    add_account_member(account.uuid, cold_credential.principal)
    cold_holds_account = threading.Event()
    warm_attempts_account = threading.Event()
    release_cold = threading.Event()
    warm_lock_queries = []
    cold_lock_queries = []

    def trace_cold(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "FOR UPDATE" in sql:
            cold_lock_queries.append(sql)
            if '"workspaces_account"' in sql and not cold_holds_account.is_set():
                # Pause after the owning service acquired its own Account lock.
                # No test pre-lock may supply the order this regression proves.
                cold_holds_account.set()
                assert release_cold.wait(5)
        return result

    def cold():
        try:
            with connection.execute_wrapper(trace_cold):
                prepared = _prepare_launch(
                    cold_credential,
                    source.scenario_id,
                    ScopedRangeLaunchOptions(scope=claim.scope, range_source=RangeSource.MISSION_CONTROL),
                )
                return reserve_scoped_launch(prepared)[0].pk
        finally:
            connection.close()

    def trace(execute, sql, params, many, context):
        if "FOR UPDATE" in sql:
            warm_lock_queries.append(sql)
            if '"workspaces_account"' in sql:
                warm_attempts_account.set()
        return execute(sql, params, many, context)

    def warm():
        assert cold_holds_account.wait(5)
        try:
            with connection.execute_wrapper(trace):
                return attempt_warm_claim(claim)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cold_future, warm_future = pool.submit(cold), pool.submit(warm)
        try:
            assert warm_attempts_account.wait(5)
            assert '"workspaces_account"' in cold_lock_queries[0]
            # While the cold launch owns Account, warm must wait before taking Workspace.
            assert not any('"workspaces_workspace"' in sql for sql in warm_lock_queries)
        finally:
            release_cold.set()
        assert cold_future.result(timeout=12)
        assert warm_future.result(timeout=12) == generation.request_id
    generation.refresh_from_db()
    assert generation.state == "claimed"
