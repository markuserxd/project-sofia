"""Offline, synthetic temporary workspace; no APIs or persistent user-file edits."""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from sofia.core import (
    AgentLoop,
    AgentPlan,
    AgentTask,
    PlannedStep,
    TaskStatus,
    VerificationResult,
)
from sofia.security import Action, PermissionDenied, RunGuardrails
from sofia.tools import JSONValue, ToolRegistry, WorkspaceConfig, WorkspaceFiles


def demonstration(root: Path) -> None:
    """The caller owns this otherwise-empty temporary directory exclusively."""
    run = RunGuardrails(max_steps=20, max_tokens=0, max_usd=Decimal(0))
    registry = ToolRegistry(run=run)
    with WorkspaceFiles(WorkspaceConfig(root, exclusive_access=True), run=run) as files:
        files.register(registry)
        _demonstrate(root, run, registry, files)


def _demonstrate(
    root: Path, run: RunGuardrails, registry: ToolRegistry, files: WorkspaceFiles
) -> None:

    def execute(tool: str, **arguments: JSONValue) -> dict[str, JSONValue]:
        result = registry.execute("workspace_" + tool, arguments)
        assert result.succeeded and isinstance(result.output, dict)
        return result.output

    # Trusted fixture decisions only: in an application these follow user review.
    for action in (Action.SENSITIVE_READ, Action.FILE_WRITE):
        run.permissions.approve(action, files.permission_scope)
        run.permissions.approve(action, files.path_scope("example.txt"))
    run.permissions.approve(
        Action.SENSITIVE_READ, files.path_scope(".", directory=True)
    )

    proposed = execute(
        "preview",
        path="example.txt",
        operation="create",
        content="Synthetic first version.\n",
    )
    pid = proposed["preview_id"]
    assert isinstance(pid, str)
    proposal = files.proposal(pid)
    assert proposal.content == "Synthetic first version.\n"
    run.permissions.approve(Action.FILE_WRITE, files.approval_scope(pid))
    # The actual mutation passes through the Agent Loop and registry. Verification
    # is local, deterministic, controller-owned, and reads only this temp fixture.
    loop = AgentLoop(
        registry=registry,
        planner=lambda instruction: AgentPlan(
            (PlannedStep("workspace_create", {"preview_id": pid}),)
        ),
        verifier=lambda step, observation: VerificationResult(
            isinstance(observation.output, dict)
            and observation.output.get("committed") is True
            and (root / "example.txt").read_text(encoding="utf-8") == proposal.content
        ),
        completion_verifier=lambda records: VerificationResult(
            len(records) == 1
            and records[0].tool == "workspace_create"
            and (root / "example.txt").read_text(encoding="utf-8") == proposal.content
        ),
    )
    assert (
        loop.execute(AgentTask("Create the approved synthetic example.", run)).status
        is TaskStatus.COMPLETED
    )
    assert execute("list", path=".")["count"] == 1
    assert execute("read", path="example.txt")["content"] == proposal.content

    replacement = execute(
        "preview",
        path="example.txt",
        operation="replace",
        content="Synthetic replacement.\n",
    )
    replacement_id = replacement["preview_id"]
    assert isinstance(replacement_id, str)
    try:
        execute("replace", preview_id=replacement_id)
    except PermissionDenied:
        pass
    else:
        raise AssertionError("unapproved_replace_succeeded")
    run.permissions.approve(Action.FILE_WRITE, files.approval_scope(replacement_id))
    assert execute("replace", preview_id=replacement_id)["committed"]
    assert execute("read", path="example.txt")["content"] == "Synthetic replacement.\n"
    assert execute("read", path="../outside.txt")["code"] == "invalid_path"

    deletion = execute("preview", path="example.txt", operation="delete")
    try:
        execute("delete", preview_id=deletion["preview_id"])
    except PermissionDenied:
        pass
    else:
        raise AssertionError("unauthorized_delete_succeeded")
    assert (root / "example.txt").is_file()
    print(
        "workspace: create/list/read/approved replacement verified; unsafe operations denied"
    )


def main() -> None:
    with TemporaryDirectory(prefix="sofia-workspace-") as temporary:
        # Windows TEMP can use an 8.3 spelling. Canonicalize this controller-owned
        # temporary root; untrusted tool paths still reject aliases outright.
        demonstration(Path(temporary).resolve(strict=True))


if __name__ == "__main__":
    main()
