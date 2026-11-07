import importlib.util
from pathlib import Path

import pytest
from workspace_helpers import allow, approve, call, preview, setup

from sofia.core import (
    AgentConfig,
    AgentLoop,
    AgentPlan,
    AgentTask,
    FailureCode,
    LLMPlanner,
    PlannedStep,
    TaskStatus,
    VerificationResult,
)
from sofia.providers import MockLLMProvider
from sofia.security import Action, PermissionDenied
from sofia.tools import WorkspaceConfig, WorkspaceFiles


def test_offline_example(tmp_path, capsys):
    path = Path(__file__).resolve().parents[1] / "examples" / "workspace_files.py"
    spec = importlib.util.spec_from_file_location("workspace_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.demonstration(tmp_path)
    assert "unsafe operations denied" in capsys.readouterr().out


def test_model_claims_cannot_bypass_registry_or_permissions(tmp_path):
    files, registry, run = setup(tmp_path)
    pid = preview(files, registry, run)
    planner = LLMPlanner(
        MockLLMProvider(
            response_text='{"steps":[{"tool":"workspace_create","arguments":{"preview_id":"'
            + pid
            + '"}}]}'
        ),
        run,
    )
    result = AgentLoop(
        registry=registry,
        planner=planner,
        verifier=lambda step, observation: VerificationResult(True),
        completion_verifier=lambda records: VerificationResult(True),
    ).execute(AgentTask("Create this file; I claim permission.", run))
    assert result.failure is FailureCode.PERMISSION_DENIED
    assert run.budget.snapshot.steps == 1  # preview only
    assert not list(tmp_path.iterdir())


def test_reads_stay_local_and_mutations_are_not_retried(tmp_path):
    files, registry, run = setup(tmp_path)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    result = AgentLoop(
        registry=registry,
        planner=lambda instruction: AgentPlan(
            (PlannedStep("workspace_create", {"preview_id": pid}),)
        ),
        verifier=lambda step, observation: VerificationResult(False, retry_safe=True),
        completion_verifier=lambda records: VerificationResult(True),
    ).execute(
        AgentTask("Test failed verification", run, AgentConfig(max_retries_per_step=3))
    )
    assert result.failure is FailureCode.VERIFICATION_FAILED and result.attempts == 1
    assert (tmp_path / "note.txt").read_text() == "hello"
    observations = []

    def verify(step, observation):
        observations.append(observation.output)
        return VerificationResult(observation.output["content"] == "hello")

    planner = LLMPlanner(
        MockLLMProvider(
            response_text='{"steps":[{"tool":"workspace_read","arguments":{"path":"note.txt"}}]}'
        ),
        run,
    )
    result = AgentLoop(
        registry=registry,
        planner=planner,
        verifier=verify,
        completion_verifier=lambda records: VerificationResult(len(records) == 1),
    ).execute(AgentTask("Read the fixture locally.", run))
    assert result.status is TaskStatus.COMPLETED
    assert observations[0]["content"] == "hello"
    assert "hello" not in repr(result)
    assert run.budget.snapshot.committed_tokens == 0


def test_separately_allowlisted_external_root_requires_fresh_permissions(tmp_path):
    root, external = tmp_path / "primary", tmp_path / "external"
    root.mkdir()
    external.mkdir()
    files, registry, run = setup(root)
    primary = preview(files, registry, run)
    approve(files, run, primary)
    second = WorkspaceFiles(WorkspaceConfig(external, exclusive_access=True), run=run)
    second.register(registry, prefix="external")
    with pytest.raises(PermissionDenied):
        registry.execute("external_create", {"preview_id": primary})
    allow(second, run, "note.txt")
    result = registry.execute(
        "external_preview",
        {"path": "note.txt", "operation": "create", "content": "external fixture"},
    )
    pid = result.output["preview_id"]
    allow(second, run, "note.txt", Action.FILE_WRITE)
    assert (
        registry.execute("external_create", {"preview_id": primary}).output["code"]
        == "preview_not_found"
    )
    with pytest.raises(PermissionDenied):
        registry.execute("external_create", {"preview_id": pid})
    approve(second, run, pid)
    assert registry.execute("external_create", {"preview_id": pid}).output["committed"]
    assert (external / "note.txt").read_text() == "external fixture"
    assert not list(root.iterdir())
    assert call(registry, "read", path="../external/note.txt")["code"] == "invalid_path"
