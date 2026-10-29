"""Real concurrent Engine reservations enforce bucket and deployment maxima."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from django.db import connection

from tests.engine.services.test_warm_reservation import reservation_inputs

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("limit", ["bucket", "global"])
def test_concurrent_warm_reservations_share_the_real_admission_mutex(settings, limit):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL transaction mutex")
    from engine.models import CapacityDraw, WarmRangeGeneration
    from engine.services import reserve_warm_generation

    first, other_bucket = reservation_inputs(settings, global_limit=1 if limit == "global" else 10)
    second = other_bucket if limit == "global" else replace(first, request_id=uuid4(), capacity_draw_key=uuid4())
    first_has_mutex, second_attempts_mutex, release = threading.Event(), threading.Event(), threading.Event()

    def reserve(draft, *, first_worker):
        def trace(execute, sql, params, many, context):
            mutex = "pg_advisory_xact_lock" in sql
            if mutex and not first_worker:
                second_attempts_mutex.set()
            result = execute(sql, params, many, context)
            if mutex and first_worker:
                first_has_mutex.set()
                assert release.wait(10)
            return result

        try:
            with connection.execute_wrapper(trace):
                result = reserve_warm_generation(draft)
                return result.pk if result is not None else None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = pool.submit(reserve, first, first_worker=True)
        assert first_has_mutex.wait(10)
        contender = pool.submit(reserve, second, first_worker=False)
        try:
            assert second_attempts_mutex.wait(10)
            assert not contender.done()
        finally:
            release.set()
        assert admitted.result(timeout=15) is not None
        assert contender.result(timeout=15) is None
    assert WarmRangeGeneration.objects.count() == CapacityDraw.objects.count() == 1
