"""Trusted, single-use approval for a harmless in-memory fixture only.

These objects are never provider tools. Real file/desktop/browser adapters are
deliberately not registered by the GUI and need their own exact scope workflows.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Condition, Lock
from time import monotonic
from uuid import uuid4

from sofia.core import AgentTask
from sofia.security import Action, PermissionDenied
from sofia.tools import (
    JSONValue,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)


@dataclass(frozen=True, slots=True, repr=False)
class Review:
    task_id: str
    adapter_id: str
    preview_id: str
    action: Action
    target: str
    payload: str
    expires: float
    scope: str = field(repr=False)


class MemoryFixture:
    """One exact proposal and one disposable cell, with no external effects."""

    def __init__(self, task: AgentTask, *, lifetime: float = 60.0) -> None:
        if not 0 < lifetime <= 120:
            raise ValueError("invalid_approval_lifetime")
        self.task = task
        self._lock = Lock()
        adapter_id, preview_id = uuid4().hex, uuid4().hex
        payload = "SOFIA synthetic note — สวัสดี"
        scope = ":".join(
            (
                "memory",
                task.task_id,
                adapter_id,
                preview_id,
                sha256(payload.encode("utf-8")).hexdigest(),
            )
        )
        self.review = Review(
            task.task_id,
            adapter_id,
            preview_id,
            Action.FILE_WRITE,
            "Disposable in-memory note",
            payload,
            monotonic() + lifetime,
            scope,
        )
        self._available = True
        self._value: str | None = None

    def _validate(self, review: Review) -> None:
        self.task.run.cancellation.checkpoint()
        if (
            review is not self.review
            or not self._available
            or monotonic() >= self.review.expires
        ):
            raise PermissionDenied()

    def validate(self, review: Review) -> None:
        with self._lock:
            self._validate(review)

    def grant(self, review: Review) -> None:
        with self._lock:
            self._validate(review)
            self.task.run.permissions.approve(review.action, review.scope)

    def discard(self) -> None:
        with self._lock:
            self._available = False
            self.task.run.permissions.revoke(self.review.action, self.review.scope)

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._value == self.review.payload

    def register(self, registry: ToolRegistry, progress: Callable[[], None]) -> None:
        if registry.run_guardrails is not self.task.run:
            raise ValueError("run_mismatch")

        def commit(
            arguments: Mapping[str, JSONValue], context: ToolContext
        ) -> JSONValue:
            progress()
            with self._lock:
                context.checkpoint()
                self._validate(self.review)
                if arguments["preview_id"] != self.review.preview_id:
                    raise PermissionDenied()
                self.task.run.permissions.check(self.review.action, self.review.scope)
                self._available = False
                try:
                    context.checkpoint()
                    self._value = self.review.payload
                finally:
                    self.task.run.permissions.revoke(
                        self.review.action, self.review.scope
                    )
                return {"committed": True}

        registry.register(
            ToolDefinition(
                "memory_commit",
                "Commit one synthetic note in memory",
                (ToolParameter("preview_id", "string"),),
            ),
            commit,
            action=self.review.action,
            target=self.review.scope,
        )


class ApprovalCoordinator:
    """A bounded worker wait; decisions never need the worker's Qt event loop.

    Only the trusted GUI controller calls decide, following the review dialog's
    explicit Approve/Deny click. Decision delivery grants nothing by itself.
    The worker revalidates proposal identity/expiry/run before granting scopes.
    """

    def __init__(self, fixture: MemoryFixture) -> None:
        self.fixture = fixture
        self._condition = Condition()
        self._pending = False
        self._closed = False
        self._decision: bool | None = None

    def decide(self, review: Review, approved: bool) -> bool:
        with self._condition:
            if (
                not self._pending
                or self._closed
                or self._decision is not None
                or review is not self.fixture.review
                or type(approved) is not bool
            ):
                return False
            try:
                self.fixture.validate(review)
            except Exception:
                approved = False
            self._decision = approved
            self._condition.notify_all()
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._decision = False
            self.fixture.discard()
            self._condition.notify_all()

    def wait(self, emit: Callable[[Review], None]) -> None:
        review = self.fixture.review
        with self._condition:
            if self._closed or self._pending:
                raise PermissionDenied()
            self.fixture.validate(review)
            self._pending = True
        try:
            emit(review)
            with self._condition:
                while self._decision is None and not self._closed:
                    self.fixture.validate(review)
                    self._condition.wait(
                        timeout=min(0.05, max(0, review.expires - monotonic()))
                    )
                self.fixture.validate(review)
                if self._closed or self._decision is not True:
                    raise PermissionDenied()
                self.fixture.grant(review)
        except BaseException:
            self.fixture.discard()
            raise
        finally:
            with self._condition:
                self._pending = False
                self._closed = True
