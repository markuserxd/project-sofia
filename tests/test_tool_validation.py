from collections import UserDict
from decimal import Decimal
from unittest.mock import Mock

import pytest

from sofia.security import RunGuardrails
from sofia.tools import ToolDefinition, ToolParameter, ToolRegistry
from sofia.tools.schemas import MAX_NODES, MAX_TEXT


def invoke(kind, value):
    run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
    tools = ToolRegistry(run=run)
    handler = Mock(return_value=None)
    tools.register(
        ToolDefinition("tool", "Validate", (ToolParameter("value", kind),)), handler
    )
    return tools.execute("tool", {"value": value}), handler, run


@pytest.mark.parametrize(
    "kind,value",
    [
        ("string", ""),
        ("integer", 0),
        ("integer", -(2**63)),
        ("integer", 2**63 - 1),
        ("number", 1),
        ("number", 1.5),
        ("boolean", True),
        ("null", None),
        ("array", [1, "text", False, None]),
        ("object", {"nested": [{"x": 1}]}),
    ],
)
def test_valid_types(kind, value):
    result, handler, run = invoke(kind, value)
    assert result.succeeded
    assert handler.call_args.args[0] == {"value": value}
    assert run.budget.snapshot.steps == 1


@pytest.mark.parametrize(
    "kind,value",
    [
        ("integer", True),
        ("number", False),
        ("integer", 1.0),
        ("integer", "1"),
        ("string", None),
        ("boolean", 1),
        ("null", "null"),
        ("array", (1,)),
        ("object", []),
        ("object", UserDict()),
        ("number", Decimal("1")),
        ("number", float("nan")),
        ("number", float("inf")),
        ("number", -float("inf")),
        ("integer", 2**63),
        ("integer", -(2**63) - 1),
        ("string", "x" * 8193),
        ("array", [None] * 129),
        ("object", {str(i): None for i in range(129)}),
        ("object", {1: "invalid key"}),
        ("array", [{"nested": object()}]),
        ("array", [float("nan")]),
        ("array", [{"a": (1,)}]),
        ("object", {"x" * 8193: 1}),
        ("string", object()),
    ],
)
def test_invalid_values_never_reach_handler(kind, value):
    result, handler, run = invoke(kind, value)
    assert not result.succeeded and result.error == "invalid_arguments"
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 0


def test_optional_arguments_and_no_defaults_or_coercion():
    run = RunGuardrails(max_steps=3, max_tokens=0, max_usd=Decimal(0))
    tools = ToolRegistry(run=run)
    handler = Mock(return_value=None)
    tools.register(
        ToolDefinition(
            "tool",
            "Validate",
            (
                ToolParameter("a", "integer"),
                ToolParameter("b", "string", required=False),
            ),
        ),
        handler,
    )
    assert tools.execute("tool", {"a": 1}).succeeded
    assert handler.call_args.args[0] == {"a": 1}
    assert tools.execute("tool", {"a": 1, "b": "text"}).succeeded
    for arguments in ({}, {"a": 1, "other": True}, {"a": "1"}, {"a": 1, "b": None}):
        assert tools.execute("tool", arguments).error == "invalid_arguments"
    assert handler.call_count == 2


def test_depth_cycles_and_total_work_are_bounded():
    nested = []
    for _ in range(9):
        nested = [nested]
    cyclic = []
    cyclic.append(cyclic)
    many_nodes = [[None] * 128 for _ in range(40)]
    assert 40 * 128 > MAX_NODES
    many_text = ["x" * 8192] * 9
    assert 8192 * 9 > MAX_TEXT
    for value in (nested, cyclic, many_nodes, many_text):
        result, handler, _ = invoke("array", value)
        assert result.error == "invalid_arguments"
        handler.assert_not_called()


def test_input_output_isolation_across_invocations():
    run = RunGuardrails(max_steps=2, max_tokens=0, max_usd=Decimal(0))
    tools = ToolRegistry(run=run)
    original = {"value": {"nested": [1]}}
    shared_output = {"nested": [1]}

    def handler(arguments, context):
        assert arguments == original
        arguments["value"]["nested"].append(2)
        return shared_output

    tools.register(
        ToolDefinition("tool", "Isolate", (ToolParameter("value", "object"),)), handler
    )
    first = tools.execute("tool", original)
    first.output["nested"].append(2)
    second = tools.execute("tool", original)
    assert original == {"value": {"nested": [1]}}
    assert shared_output == second.output == {"nested": [1]}


def test_custom_python_hooks_are_never_called():
    class Hostile(dict):
        def items(self):
            pytest.fail("Custom items hook executed")

        def __repr__(self):
            pytest.fail("Custom repr hook executed")

    result, handler, _ = invoke("object", Hostile())
    assert result.error == "invalid_arguments"
    handler.assert_not_called()
    tools = ToolRegistry(
        run=RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
    )
    tools.register(ToolDefinition("tool", "Validate"), Mock())
    for invalid in (Hostile(), UserDict(), [], None, {object(): 1}):
        assert tools.execute("tool", invalid).error == "invalid_arguments"
