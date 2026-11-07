from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
from threading import Barrier
from typing import cast

import pytest

from sofia.security import (
    AccountingBlocked,
    Cancellation,
    InvalidGuardrailInput,
    InvalidReservation,
    OperationCancelled,
    Reservation,
    RunBudget,
    SpendingBudgetExceeded,
    StepLimitExceeded,
    TokenBudgetExceeded,
)


def budget(
    steps: int = 10, tokens: int = 100, usd: Decimal = Decimal("1")
) -> RunBudget:
    return RunBudget(
        max_steps=steps, max_tokens=tokens, max_usd=usd, cancellation=Cancellation()
    )


@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None])
def test_invalid_counts_in_limits_estimates_and_settlements(value: object) -> None:
    invalid = cast(int, value)
    with pytest.raises(InvalidGuardrailInput):
        budget(steps=invalid)
    with pytest.raises(InvalidGuardrailInput):
        budget(tokens=invalid)
    ledger = budget()
    before = ledger.snapshot
    with pytest.raises(InvalidGuardrailInput):
        ledger.reserve(estimated_tokens=invalid, estimated_usd=Decimal(0))
    assert ledger.snapshot == before
    handle = ledger.reserve(estimated_tokens=1, estimated_usd=Decimal(0))
    before = ledger.snapshot
    if value is not None:  # None is explicitly unknown usage at settlement only.
        with pytest.raises(InvalidGuardrailInput):
            ledger.settle(handle, actual_tokens=invalid, actual_usd=Decimal(0))
        assert ledger.snapshot == before
    ledger.settle(handle, actual_tokens=0, actual_usd=Decimal(0))


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        0.1,
        "0.1",
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("0.0000001"),
    ],
)
def test_invalid_money_is_rejected_without_accounting_changes(value: object) -> None:
    invalid = cast(Decimal, value)
    with pytest.raises(InvalidGuardrailInput):
        budget(usd=invalid)
    ledger = budget()
    before = ledger.snapshot
    with pytest.raises(InvalidGuardrailInput):
        ledger.reserve(estimated_tokens=1, estimated_usd=invalid)
    assert ledger.snapshot == before
    handle = ledger.reserve(estimated_tokens=1, estimated_usd=Decimal("0.1"))
    before = ledger.snapshot
    if value is not None:
        with pytest.raises(InvalidGuardrailInput):
            ledger.settle(handle, actual_tokens=1, actual_usd=invalid)
        assert ledger.snapshot == before
    ledger.settle(handle, actual_tokens=1, actual_usd=Decimal("0.1"))


def test_step_boundary_and_runs_are_independent() -> None:
    first, second = budget(steps=2), budget(steps=2)
    first.consume_step()
    first.consume_step()
    with pytest.raises(StepLimitExceeded):
        first.consume_step()
    with pytest.raises(StepLimitExceeded):
        first.reserve(estimated_tokens=0, estimated_usd=Decimal(0))
    assert first.snapshot.steps == 2
    assert first.snapshot.remaining_steps == 0
    assert second.snapshot.steps == 0
    with pytest.raises(StepLimitExceeded):
        budget(steps=0).consume_step()


def test_exact_money_independent_of_decimal_context_and_token_boundary() -> None:
    with localcontext() as context:
        context.prec = 1
        ledger = budget(tokens=3, usd=Decimal("0.300001"))
        first = ledger.reserve(estimated_tokens=1, estimated_usd=Decimal("0.1"))
        second = ledger.reserve(estimated_tokens=2, estimated_usd=Decimal("0.200001"))
        assert ledger.snapshot.remaining_tokens == 0
        assert ledger.snapshot.remaining_usd == 0
        assert ledger.snapshot.reserved_usd == Decimal("0.300001")
        with pytest.raises(TokenBudgetExceeded):
            ledger.reserve(estimated_tokens=1, estimated_usd=Decimal(0))
        with pytest.raises(SpendingBudgetExceeded):
            ledger.reserve(estimated_tokens=0, estimated_usd=Decimal("0.000001"))
        ledger.settle(first, actual_tokens=1, actual_usd=Decimal("0.1"))
        ledger.settle(second, actual_tokens=1, actual_usd=Decimal("0.100001"))
        assert ledger.snapshot.committed_tokens == 2
        assert ledger.snapshot.remaining_tokens == 1
        assert ledger.snapshot.committed_usd == Decimal("0.200001")
        assert ledger.snapshot.remaining_usd == Decimal("0.1")
        assert ledger.snapshot.reserved_tokens == ledger.snapshot.reserved_usd == 0
        assert ledger.snapshot.steps == 2


def test_failed_paired_reservation_does_not_consume_step_or_tokens() -> None:
    ledger = budget()
    before = ledger.snapshot
    with pytest.raises(SpendingBudgetExceeded):
        ledger.reserve(estimated_tokens=100, estimated_usd=Decimal("2"))
    assert ledger.snapshot == before


def test_foreign_fabricated_copied_and_closed_reservations_rejected() -> None:
    ledger, other = budget(), budget()
    first = ledger.reserve(estimated_tokens=10, estimated_usd=Decimal("0.2"))
    second = ledger.reserve(estimated_tokens=20, estimated_usd=Decimal("0.3"))
    foreign = other.reserve(estimated_tokens=1, estimated_usd=Decimal("0.1"))
    before = ledger.snapshot
    for invalid in (foreign, Reservation(), copy(first), None, {}, "private-input"):
        with pytest.raises(InvalidReservation):
            ledger.settle(
                cast(Reservation, invalid), actual_tokens=0, actual_usd=Decimal(0)
            )
        assert ledger.snapshot == before
    ledger.settle(first, actual_tokens=10, actual_usd=Decimal("0.2"))
    with pytest.raises(InvalidReservation):
        ledger.settle(first, actual_tokens=0, actual_usd=Decimal(0))
    assert ledger.snapshot.committed_tokens == 10
    assert ledger.snapshot.reserved_tokens == 20
    assert ledger.snapshot.committed_usd == Decimal("0.2")
    assert ledger.snapshot.reserved_usd == Decimal("0.3")
    ledger.settle(second, actual_tokens=0, actual_usd=Decimal(0))


@pytest.mark.parametrize(
    ("tokens", "cost", "committed_tokens", "committed_usd"),
    [
        (None, None, 10, "0.1"),
        (None, Decimal(0), 10, "0"),
        (0, None, 0, "0.1"),
        (11, Decimal("0.1"), 11, "0.1"),
        (10, Decimal("0.2"), 10, "0.2"),
        (101, Decimal("2"), 101, "2"),
    ],
)
def test_unknown_or_overrun_records_charge_then_blocks(
    tokens: int | None, cost: Decimal | None, committed_tokens: int, committed_usd: str
) -> None:
    ledger = budget()
    handle = ledger.reserve(estimated_tokens=10, estimated_usd=Decimal("0.1"))
    other = ledger.reserve(estimated_tokens=1, estimated_usd=Decimal("0.1"))
    with pytest.raises(AccountingBlocked):
        ledger.settle(handle, actual_tokens=tokens, actual_usd=cost)
    snapshot = ledger.snapshot
    assert snapshot.committed_tokens == committed_tokens
    assert snapshot.committed_usd == Decimal(committed_usd)
    assert snapshot.remaining_tokens >= 0
    assert snapshot.remaining_usd >= 0
    assert snapshot.blocked
    with pytest.raises(AccountingBlocked):
        ledger.reserve(estimated_tokens=0, estimated_usd=Decimal(0))
    with pytest.raises(AccountingBlocked):
        ledger.consume_step()
    with pytest.raises(InvalidReservation):
        ledger.settle(handle, actual_tokens=0, actual_usd=Decimal(0))
    ledger.settle(other, actual_tokens=0, actual_usd=Decimal(0))
    assert ledger.snapshot.blocked


def test_cancellation_blocks_new_work_but_allows_accounting_cleanup() -> None:
    cancellation = Cancellation()
    ledger = RunBudget(
        max_steps=1, max_tokens=10, max_usd=Decimal(1), cancellation=cancellation
    )
    handle = ledger.reserve(estimated_tokens=10, estimated_usd=Decimal(1))
    cancellation.cancel()
    with pytest.raises(OperationCancelled):
        ledger.consume_step()
    with pytest.raises(OperationCancelled):
        ledger.reserve(estimated_tokens=-1, estimated_usd=Decimal(-1))
    with pytest.raises(AccountingBlocked):
        ledger.settle(handle, actual_tokens=None, actual_usd=None)
    assert ledger.snapshot.committed_tokens == 10
    assert ledger.snapshot.committed_usd == 1
    with pytest.raises(OperationCancelled):
        ledger.consume_step()


@pytest.mark.parametrize(
    ("tokens", "cost", "error"),
    [
        (100, Decimal(0), TokenBudgetExceeded),
        (0, Decimal(1), SpendingBudgetExceeded),
    ],
)
def test_concurrent_final_capacity_reservations(
    tokens: int, cost: Decimal, error: type[Exception]
) -> None:
    ledger = budget()
    barrier = Barrier(8, timeout=5)

    def reserve() -> Reservation | None:
        barrier.wait()
        try:
            return ledger.reserve(estimated_tokens=tokens, estimated_usd=cost)
        except error:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reserve) for _ in range(8)]
        results = [future.result(timeout=5) for future in futures]
    assert sum(result is not None for result in results) == 1
    assert ledger.snapshot.steps == 1
    assert ledger.snapshot.reserved_tokens == tokens
    assert ledger.snapshot.reserved_usd == cost


def test_concurrent_settlement_consumes_reservation_once() -> None:
    ledger = budget()
    handle = ledger.reserve(estimated_tokens=10, estimated_usd=Decimal(1))
    barrier = Barrier(2, timeout=5)

    def settle() -> bool:
        barrier.wait()
        try:
            ledger.settle(handle, actual_tokens=10, actual_usd=Decimal(1))
            return True
        except InvalidReservation:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(settle) for _ in range(2)]
        assert sorted(future.result(timeout=5) for future in futures) == [False, True]
    assert ledger.snapshot.committed_tokens == 10
    assert ledger.snapshot.committed_usd == 1
    assert ledger.snapshot.reserved_tokens == ledger.snapshot.reserved_usd == 0


def test_zero_limits_snapshot_immutability_and_large_exact_money() -> None:
    ledger = budget(tokens=0, usd=Decimal(0))
    handle = ledger.reserve(estimated_tokens=0, estimated_usd=Decimal(0))
    ledger.settle(handle, actual_tokens=0, actual_usd=Decimal(0))
    with pytest.raises(TokenBudgetExceeded):
        ledger.reserve(estimated_tokens=1, estimated_usd=Decimal(0))
    with pytest.raises(SpendingBudgetExceeded):
        ledger.reserve(estimated_tokens=0, estimated_usd=Decimal("0.000001"))
    snapshot = ledger.snapshot
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, "steps", 0)
    assert ledger.snapshot.steps == 1
    large = budget(usd=Decimal("1E5000"))
    assert large.snapshot.remaining_usd == Decimal("1E5000")


def test_concurrent_step_limit_includes_requests() -> None:
    ledger = budget(steps=1)
    barrier = Barrier(2, timeout=5)

    def start(request: bool) -> bool:
        barrier.wait()
        try:
            if request:
                ledger.reserve(estimated_tokens=1, estimated_usd=Decimal("0.1"))
            else:
                ledger.consume_step()
            return True
        except StepLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, request) for request in (True, False)]
        assert sorted(future.result(timeout=5) for future in futures) == [False, True]
    assert ledger.snapshot.steps == 1


def test_known_zero_usage_after_stop_releases_only_unused_capacity() -> None:
    cancellation = Cancellation()
    ledger = RunBudget(
        max_steps=2, max_tokens=10, max_usd=Decimal(1), cancellation=cancellation
    )
    completed = ledger.reserve(estimated_tokens=5, estimated_usd=Decimal("0.5"))
    ledger.settle(completed, actual_tokens=3, actual_usd=Decimal("0.3"))
    undispatched = ledger.reserve(estimated_tokens=5, estimated_usd=Decimal("0.5"))
    cancellation.cancel()
    ledger.settle(undispatched, actual_tokens=0, actual_usd=Decimal(0))
    assert ledger.snapshot.committed_tokens == 3
    assert ledger.snapshot.committed_usd == Decimal("0.3")
    assert ledger.snapshot.reserved_tokens == ledger.snapshot.reserved_usd == 0
    assert ledger.snapshot.steps == 2
