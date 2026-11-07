"""Application-owned approvals; requests cannot carry approval claims."""

from enum import Enum
from threading import Lock

from sofia.security.cancellation import Cancellation
from sofia.security.errors import InvalidGuardrailInput, PermissionDenied, UnknownAction


class Action(Enum):
    """Explicit capabilities; all require approval, including paid requests."""

    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    SHELL_EXECUTE = "shell_execute"
    SOFTWARE_INSTALL = "software_install"
    MESSAGE_SEND = "message_send"
    FORM_SUBMIT = "form_submit"
    SETTINGS_CHANGE = "settings_change"
    SENSITIVE_READ = "sensitive_read"
    API_REQUEST = "api_request"


def _validate_scope(action: Action, target: str) -> None:
    if type(action) is not Action:
        raise UnknownAction()
    if type(target) is not str or not target.strip() or target == "*":
        raise InvalidGuardrailInput()


class PermissionPolicy:
    def __init__(self, cancellation: Cancellation) -> None:
        self._cancellation = cancellation
        self._lock = Lock()
        # Membership is the explicit approval status, owned only by this policy.
        self._approved: set[tuple[Action, str]] = set()

    def approve(self, action: Action, target: str) -> None:
        """Trusted application controller only; never expose as a model tool."""
        with self._lock:
            self._cancellation.checkpoint()
            _validate_scope(action, target)
            self._approved.add((action, target))

    def revoke(self, action: Action, target: str) -> None:
        with self._lock:
            _validate_scope(action, target)
            self._approved.discard((action, target))

    def check(self, action: Action, target: str) -> None:
        with self._lock:
            self._cancellation.checkpoint()
            _validate_scope(action, target)
            if (action, target) not in self._approved:
                raise PermissionDenied()
