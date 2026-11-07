"""One shared enforcement context for all operations in a run."""

from decimal import Decimal

from sofia.security.budgets import Reservation, RunBudget
from sofia.security.cancellation import Cancellation
from sofia.security.errors import InvalidGuardrailInput
from sofia.security.permissions import Action, PermissionPolicy


class RunGuardrails:
    """Construct once in trusted code; pass the same context to child operations."""

    def __init__(self, *, max_steps: int, max_tokens: int, max_usd: Decimal) -> None:
        self._cancellation = Cancellation()
        self._permissions = PermissionPolicy(self._cancellation)
        self._budget = RunBudget(
            max_steps=max_steps,
            max_tokens=max_tokens,
            max_usd=max_usd,
            cancellation=self._cancellation,
        )

    @property
    def cancellation(self) -> Cancellation:
        return self._cancellation

    @property
    def permissions(self) -> PermissionPolicy:
        return self._permissions

    @property
    def budget(self) -> RunBudget:
        return self._budget

    def begin_step(self, action: Action, target: str) -> None:
        """Authorize and consume a non-API step before performing any effect."""
        self.permissions.check(action, target)
        if action is Action.API_REQUEST:
            raise InvalidGuardrailInput()
        self.budget.consume_step()

    def begin_request(
        self, target: str, *, estimated_tokens: int, estimated_usd: Decimal
    ) -> Reservation:
        """Authorize an API request and reserve its step, tokens and cost."""
        self.permissions.check(Action.API_REQUEST, target)
        return self.budget.reserve(
            estimated_tokens=estimated_tokens, estimated_usd=estimated_usd
        )
