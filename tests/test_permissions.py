from typing import cast

import pytest

from sofia.security import (
    Action,
    Cancellation,
    InvalidGuardrailInput,
    OperationCancelled,
    PermissionDenied,
    PermissionPolicy,
    UnknownAction,
)


@pytest.mark.parametrize("action", list(Action))
def test_explicit_exact_scope_and_revocation(action: Action) -> None:
    policy = PermissionPolicy(Cancellation())
    target = "private-resource"
    with pytest.raises(PermissionDenied):
        policy.check(action, target)
    policy.approve(action, target)
    policy.check(action, target)
    with pytest.raises(PermissionDenied):
        policy.check(action, target.upper())
    other = Action.FILE_WRITE if action != Action.FILE_WRITE else Action.FILE_DELETE
    with pytest.raises(PermissionDenied):
        policy.check(other, target)
    policy.revoke(action, target)
    policy.revoke(action, target)
    with pytest.raises(PermissionDenied):
        policy.check(action, target)


@pytest.mark.parametrize(
    "action", [None, [], "file_delete", "unknown", {"approved": True}]
)
def test_unknown_or_model_supplied_actions_cannot_authorize(action: object) -> None:
    policy = PermissionPolicy(Cancellation())
    for operation in (policy.approve, policy.check, policy.revoke):
        with pytest.raises(UnknownAction):
            operation(cast(Action, action), "resource")


@pytest.mark.parametrize("target", [None, [], "", " ", "*", {"approved": True}])
def test_missing_or_invalid_scope_is_rejected(target: object) -> None:
    policy = PermissionPolicy(Cancellation())
    for operation in (policy.approve, policy.check, policy.revoke):
        with pytest.raises(InvalidGuardrailInput):
            operation(Action.FILE_DELETE, cast(str, target))


def test_scope_is_not_normalized_or_used_as_a_pattern() -> None:
    policy = PermissionPolicy(Cancellation())
    policy.approve(Action.FILE_DELETE, "folder/*")
    with pytest.raises(PermissionDenied):
        policy.check(Action.FILE_DELETE, "folder/file")
    policy.approve(Action.FILE_DELETE, " resource ")
    with pytest.raises(PermissionDenied):
        policy.check(Action.FILE_DELETE, "resource")


def test_stop_overrides_approval_and_invalid_input() -> None:
    cancellation = Cancellation()
    policy = PermissionPolicy(cancellation)
    policy.approve(Action.FILE_DELETE, "resource")
    cancellation.cancel()
    for action in (Action.FILE_DELETE, cast(Action, None)):
        with pytest.raises(OperationCancelled):
            policy.check(action, "resource")
        with pytest.raises(OperationCancelled):
            policy.approve(action, "resource")
    policy.revoke(Action.FILE_DELETE, "resource")
