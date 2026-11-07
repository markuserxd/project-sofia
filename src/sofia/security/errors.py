"""Fixed denial messages: never format untrusted data into errors or logs."""

import logging


def security_event(code: str) -> None:
    """Internal callers supply constant reason codes only."""
    try:
        logging.getLogger("sofia.security").info(code)
    except Exception:
        # A broken logging handler must not disable enforcement or accounting.
        pass


class GuardrailError(Exception):
    code = "guardrail_denied"

    def __init__(self) -> None:
        super().__init__(self.code)
        security_event(self.code)


class InvalidGuardrailInput(GuardrailError):
    code = "invalid_guardrail_input"


class UnknownAction(GuardrailError):
    code = "action_not_recognized"


class PermissionDenied(GuardrailError):
    code = "permission_denied"


class OperationCancelled(GuardrailError):
    code = "operation_cancelled"


class StepLimitExceeded(GuardrailError):
    code = "step_limit_exceeded"


class TokenBudgetExceeded(GuardrailError):
    code = "token_budget_exceeded"


class SpendingBudgetExceeded(GuardrailError):
    code = "spending_budget_exceeded"


class AccountingBlocked(GuardrailError):
    code = "accounting_requires_run_stop"


class InvalidReservation(GuardrailError):
    code = "reservation_not_active"
