"""Native result application and retirement use the same real ledger lock order."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection

from tests.engine.services.test_native_warm_retirement import (
    _apply_result,
    _ready_payload,
)
from tests.engine.services.test_native_warm_retirement import (
    warm_runtime as warm_runtime,
)

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_warm_ready_result_and_retirement_serialize_on_the_real_ledger(warm_runtime):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL lock contention")
    from engine.models import CapacityDraw, ProvisionerLaunchIntent, Range, WarmRangeGeneration
    from engine.services import dispatch_created_raes_range, retire_generation
    from shared.operation_results import ResultStep

    case = warm_runtime
    dispatch_created_raes_range(case.request_id, credential=case.credential)
    provision = ProvisionerLaunchIntent.objects.get(payload__operation="provision")
    payload = _ready_payload(provision)
    result_holds_ledger = threading.Event()
    retirement_attempts_ledger = threading.Event()
    release_result = threading.Event()
    result_target_locks = []
    range_table = connection.ops.quote_name(Range._meta.db_table)
    ledger_table = connection.ops.quote_name(WarmRangeGeneration._meta.db_table)

    def trace_result(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "FOR UPDATE" in sql and any(name in sql for name in (range_table, ledger_table)):
            result_target_locks.append(sql)
            if ledger_table in sql and not result_holds_ledger.is_set():
                result_holds_ledger.set()
                assert release_result.wait(5)
        return result

    def apply_ready():
        try:
            with connection.execute_wrapper(trace_result):
                _apply_result(provision, ResultStep.RAES_TERMINAL_READY, payload)
        finally:
            connection.close()

    def trace_retirement(execute, sql, params, many, context):
        if "FOR UPDATE" in sql and ledger_table in sql:
            retirement_attempts_ledger.set()
        return execute(sql, params, many, context)

    def retire():
        assert result_holds_ledger.wait(5)
        try:
            with connection.execute_wrapper(trace_retirement):
                return retire_generation(case.generation)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result_future, retirement_future = pool.submit(apply_ready), pool.submit(retire)
        try:
            assert retirement_attempts_ledger.wait(5)
            assert ledger_table in result_target_locks[0]
            assert not any(range_table in sql for sql in result_target_locks)
        finally:
            release_result.set()
        result_future.result(timeout=12)
        assert not retirement_future.result(timeout=12)

    case.generation.refresh_from_db()
    assert case.generation.state == "ready"
    assert not ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").exists()
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None
