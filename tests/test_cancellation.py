from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event

import pytest

from sofia.security import Action, Cancellation, OperationCancelled, RunGuardrails


def test_concurrent_repeated_stop_is_irreversible() -> None:
    cancellation = Cancellation()
    barrier = Barrier(4, timeout=5)

    def stop() -> None:
        barrier.wait()
        cancellation.cancel()
        cancellation.cancel()

    cancellation.checkpoint()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(stop) for _ in range(4)]
        for future in futures:
            future.result(timeout=5)
    assert cancellation.cancelled
    for _ in range(2):
        with pytest.raises(OperationCancelled):
            cancellation.checkpoint()


def test_active_work_stops_after_blocking_operation_before_next_effect() -> None:
    run = RunGuardrails(max_steps=3, max_tokens=0, max_usd=Decimal(0))
    run.permissions.approve(Action.FILE_WRITE, "fake-resource")
    entered = Event()
    unblock = Event()
    effects: list[str] = []

    def worker() -> None:
        run.begin_step(Action.FILE_WRITE, "fake-resource")
        effects.append("first")
        entered.set()
        assert unblock.wait(timeout=5)
        run.cancellation.checkpoint()
        run.begin_step(Action.FILE_WRITE, "fake-resource")
        effects.append("second")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(timeout=5)
            run.cancellation.cancel()
        finally:
            unblock.set()
        with pytest.raises(OperationCancelled):
            future.result(timeout=5)
    assert effects == ["first"]
    assert run.budget.snapshot.steps == 1
    with pytest.raises(OperationCancelled):
        run.begin_step(Action.FILE_WRITE, "fake-resource")
