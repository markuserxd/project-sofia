from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from sofia.security import Action, RunGuardrails
from sofia.tools import (
    DuplicateTool,
    InvalidToolDefinition,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    UnknownTool,
)


def registry():
    run = RunGuardrails(max_steps=10, max_tokens=0, max_usd=Decimal(0))
    return ToolRegistry(run=run), run


def test_registration_discovery_is_immutable_public_metadata_only():
    tools, run = registry()
    handler = Mock(return_value=None)
    definition = ToolDefinition("add", "Add integers", (ToolParameter("a", "integer"),))
    tools.register(definition, handler, action=Action.SENSITIVE_READ, target="private")
    assert tools.list_tools() == (definition,)
    assert tools.describe("add") == definition
    assert tools.contains("add") and not tools.contains("ADD")
    assert not tools.contains([])
    public = asdict(tools.describe("add"))
    assert set(public) == {"name", "description", "parameters", "timeout"}
    assert "private" not in repr(public)
    with pytest.raises(FrozenInstanceError):
        definition.name = "other"
    with pytest.raises(FrozenInstanceError):
        definition.parameters[0].required = False
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 0
    with pytest.raises(UnknownTool, match="^unknown_tool$"):
        tools.describe("private-unknown")
    with pytest.raises(DuplicateTool, match="^duplicate_tool$"):
        tools.register(definition, Mock())
    assert tools.describe("add") is definition


@pytest.mark.parametrize(
    "name", ["", "1abc", "a.b", "../x", "a b", "a\n", "é", "a" * 65, [], None]
)
def test_invalid_names(name):
    tools, _ = registry()
    with pytest.raises(InvalidToolDefinition, match="^invalid_tool_definition$"):
        tools.register(ToolDefinition(name, "Description"), Mock())
    with pytest.raises(UnknownTool):
        tools.describe(name)


@pytest.mark.parametrize("name", ["A", "tool_name-2", "a" * 64])
def test_valid_names(name):
    tools, _ = registry()
    tools.register(ToolDefinition(name, "Description"), Mock())
    assert tools.contains(name)


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), -float("inf"), True, "1", None, 10**1000],
)
def test_invalid_timeouts(timeout):
    tools, _ = registry()
    with pytest.raises(InvalidToolDefinition):
        tools.register(ToolDefinition("tool", "Description", timeout=timeout), Mock())


@pytest.mark.parametrize(
    "changes",
    [
        {"description": ""},
        {"description": " "},
        {"description": "x" * 1025},
        {"description": object()},
        {"parameters": []},
        {"parameters": (object(),)},
        {"parameters": (ToolParameter("a", "unsupported"),)},
        {"parameters": (ToolParameter("a", []),)},
        {"parameters": (ToolParameter("a", "string", required=1),)},
        {"parameters": (ToolParameter("../a", "string"),)},
        {"parameters": (ToolParameter("a", "string"),) * 2},
        {"parameters": tuple(ToolParameter(f"p{i}", "integer") for i in range(129))},
    ],
)
def test_invalid_schema(changes):
    tools, _ = registry()
    with pytest.raises(InvalidToolDefinition):
        tools.register(
            replace(ToolDefinition("tool", "Description"), **changes), Mock()
        )


@pytest.mark.parametrize(
    "definition,handler",
    [(None, Mock()), ({}, Mock()), (ToolDefinition("tool", "Description"), None)],
)
def test_incomplete_registration(definition, handler):
    tools, _ = registry()
    with pytest.raises(InvalidToolDefinition):
        tools.register(definition, handler)


@pytest.mark.parametrize(
    "metadata",
    [
        {"action": "file_write", "target": "resource"},
        {"action": {"approved": True}, "target": "resource"},
        {"action": Action.API_REQUEST, "target": "resource"},
        {"action": Action.FILE_WRITE},
        {"action": Action.FILE_WRITE, "target": ""},
        {"action": Action.FILE_WRITE, "target": " "},
        {"action": Action.FILE_WRITE, "target": "*"},
        {"action": Action.FILE_WRITE, "target": object()},
        {"target": "resource"},
    ],
)
def test_invalid_security_metadata(metadata):
    tools, _ = registry()
    with pytest.raises(InvalidToolDefinition):
        tools.register(ToolDefinition("tool", "Description"), Mock(), **metadata)
    assert tools.list_tools() == ()
