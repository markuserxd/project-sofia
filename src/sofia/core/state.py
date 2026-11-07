"""Explicit single-execution task lifecycle, with immutable terminal states."""

from enum import Enum


class TaskStatus(Enum):
    CREATED = "created"
    PLANNING = "planning"
    VALIDATING = "validating"
    RUNNING = "running"
    OBSERVING = "observing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"

    @property
    def terminal(self) -> bool:
        return self in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.LIMIT_EXCEEDED,
        )


class TaskState:
    def __init__(self) -> None:
        self._status = TaskStatus.CREATED

    @property
    def status(self) -> TaskStatus:
        return self._status

    def transition(self, target: TaskStatus) -> None:
        allowed = {
            TaskStatus.CREATED: (TaskStatus.PLANNING,),
            TaskStatus.PLANNING: (TaskStatus.VALIDATING,),
            TaskStatus.VALIDATING: (TaskStatus.RUNNING,),
            TaskStatus.RUNNING: (TaskStatus.OBSERVING,),
            TaskStatus.OBSERVING: (TaskStatus.VERIFYING,),
            TaskStatus.VERIFYING: (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        }
        if (
            type(target) is not TaskStatus
            or self.status.terminal
            or (
                target not in allowed[self.status]
                and target
                not in (
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.LIMIT_EXCEEDED,
                )
            )
        ):
            raise ValueError("invalid_agent_transition")
        self._status = target
