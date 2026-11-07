from decimal import Decimal

from sofia.security import Action, RunGuardrails
from sofia.tools import ToolRegistry, WorkspaceConfig, WorkspaceFiles


def setup(root, *, exclusive=True, steps=100, **limits):
    run = RunGuardrails(max_steps=steps, max_tokens=10, max_usd=Decimal(0))
    registry = ToolRegistry(run=run)
    files = WorkspaceFiles(
        WorkspaceConfig(root, exclusive_access=exclusive, **limits), run=run
    )
    files.register(registry)
    return files, registry, run


def allow(files, run, path, action=Action.SENSITIVE_READ, *, directory=False):
    run.permissions.approve(action, files.permission_scope)
    run.permissions.approve(action, files.path_scope(path, directory=directory))


def call(registry, tool, /, **arguments):
    result = registry.execute("workspace_" + tool, arguments)
    assert result.succeeded, result
    assert isinstance(result.output, dict)
    return result.output


def preview(files, registry, run, path="note.txt", operation="create", content="hello"):
    allow(files, run, path)
    args = {"path": path, "operation": operation}
    if operation != "delete":
        args["content"] = content
    result = call(registry, "preview", **args)
    assert result["status"] == "ok", result
    return result["preview_id"]


def approve(files, run, preview_id):
    proposal = files.proposal(preview_id)
    action = Action.FILE_DELETE if proposal.operation == "delete" else Action.FILE_WRITE
    allow(files, run, proposal.path, action)
    run.permissions.approve(action, files.approval_scope(preview_id))


def mutate(files, registry, run, **kwargs):
    proposal_id = preview(files, registry, run, **kwargs)
    approve(files, run, proposal_id)
    return call(registry, kwargs.get("operation", "create"), preview_id=proposal_id)
