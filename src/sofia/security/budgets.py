"""Atomic per-run accounting, with exact integer micro-USD storage."""

from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from sofia.security.cancellation import Cancellation
from sofia.security.errors import (
    AccountingBlocked,
    InvalidGuardrailInput,
    InvalidReservation,
    SpendingBudgetExceeded,
    StepLimitExceeded,
    TokenBudgetExceeded,
)


def _count(value: int) -> int:
    if type(value) is not int or value < 0:
        raise InvalidGuardrailInput()
    return value


def _micro_usd(value: Decimal) -> int:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise InvalidGuardrailInput()
    numerator, denominator = value.as_integer_ratio()
    units, remainder = divmod(numerator * 1_000_000, denominator)
    if remainder:
        raise InvalidGuardrailInput()
    return units


def _usd(units: int) -> Decimal:
    # Construct exactly, independently of the caller's Decimal precision/context.
    digits = Decimal(units).as_tuple().digits
    return Decimal((0, digits, -6))


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class Reservation:
    """Opaque identity handle; a fabricated/copied handle has no authority."""


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    steps: int
    remaining_steps: int
    committed_tokens: int
    reserved_tokens: int
    remaining_tokens: int
    committed_usd: Decimal
    reserved_usd: Decimal
    remaining_usd: Decimal
    blocked: bool


class RunBudget:
    """One ledger for steps and paired token/spend reservations.

    Reserve consumes one step only after all limits pass. Settlement is allowed
    after cancellation. Unknown usage commits the estimate and permanently blocks
    new work; an overrun commits actual usage and also blocks, even below the cap.
    """

    def __init__(
        self,
        *,
        max_steps: int,
        max_tokens: int,
        max_usd: Decimal,
        cancellation: Cancellation,
    ) -> None:
        self._max_steps = _count(max_steps)
        self._max_tokens = _count(max_tokens)
        self._max_usd = _micro_usd(max_usd)
        self._cancellation = cancellation
        self._lock = Lock()
        self._steps = 0
        self._tokens = 0
        self._usd = 0
        self._reserved_tokens = 0
        self._reserved_usd = 0
        self._blocked = False
        self._reservations: dict[Reservation, tuple[int, int]] = {}

    def _check_step(self) -> None:
        self._cancellation.checkpoint()
        if self._blocked:
            raise AccountingBlocked()
        if self._steps >= self._max_steps:
            raise StepLimitExceeded()

    def consume_step(self) -> None:
        with self._lock:
            self._check_step()
            self._steps += 1

    def reserve(self, *, estimated_tokens: int, estimated_usd: Decimal) -> Reservation:
        """Reserve both resources and one step atomically, or change nothing."""
        with self._lock:
            self._check_step()
            tokens = _count(estimated_tokens)
            cost = _micro_usd(estimated_usd)
            if self._tokens + self._reserved_tokens + tokens > self._max_tokens:
                raise TokenBudgetExceeded()
            if self._usd + self._reserved_usd + cost > self._max_usd:
                raise SpendingBudgetExceeded()
            reservation = Reservation()
            self._reservations[reservation] = (tokens, cost)
            self._reserved_tokens += tokens
            self._reserved_usd += cost
            self._steps += 1
            return reservation

    def settle(
        self,
        reservation: Reservation,
        *,
        actual_tokens: int | None,
        actual_usd: Decimal | None,
        bounds_exceeded: bool = False,
    ) -> None:
        """Trusted accounting only. None means unknown, never a free request.

        Invalid settlement leaves the reservation open. An overrun/uncertainty
        closes it, records the charge, then raises AccountingBlocked. Previously
        open reservations can still settle; no accounting block can be reset.
        """
        with self._lock:
            if type(bounds_exceeded) is not bool:
                raise InvalidGuardrailInput()
            if (
                type(reservation) is not Reservation
                or reservation not in self._reservations
            ):
                raise InvalidReservation()
            estimated_tokens, estimated_usd = self._reservations[reservation]
            tokens = (
                estimated_tokens if actual_tokens is None else _count(actual_tokens)
            )
            cost = estimated_usd if actual_usd is None else _micro_usd(actual_usd)
            uncertain = actual_tokens is None or actual_usd is None
            # An adapter may also detect an individual input/output cap breach
            # hidden by unused capacity in the paired aggregate reservation.
            overrun = (
                bounds_exceeded or tokens > estimated_tokens or cost > estimated_usd
            )
            self._tokens += tokens
            self._usd += cost
            self._reserved_tokens -= estimated_tokens
            self._reserved_usd -= estimated_usd
            del self._reservations[reservation]
            if uncertain or overrun:
                self._blocked = True
                raise AccountingBlocked()

    @property
    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                steps=self._steps,
                remaining_steps=self._max_steps - self._steps,
                committed_tokens=self._tokens,
                reserved_tokens=self._reserved_tokens,
                remaining_tokens=max(
                    0, self._max_tokens - self._tokens - self._reserved_tokens
                ),
                committed_usd=_usd(self._usd),
                reserved_usd=_usd(self._reserved_usd),
                remaining_usd=_usd(
                    max(0, self._max_usd - self._usd - self._reserved_usd)
                ),
                blocked=self._blocked,
            )
