"""Cooperative, irreversible cancellation for one run."""

from threading import Event

from sofia.security.errors import OperationCancelled, security_event


class Cancellation:
    def __init__(self) -> None:
        self._stopped = Event()

    @property
    def cancelled(self) -> bool:
        return self._stopped.is_set()

    def cancel(self) -> None:
        """Trusted controllers may call repeatedly; there is no reset."""
        self._stopped.set()
        security_event("cancellation_requested")

    def checkpoint(self) -> None:
        if self.cancelled:
            raise OperationCancelled()
