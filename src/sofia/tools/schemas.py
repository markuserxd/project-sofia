"""Small bounded JSON subset, without coercion or user-defined Python hooks."""

import math
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
ParameterKind = Literal[
    "string", "integer", "number", "boolean", "null", "array", "object"
]
MAX_DEPTH = 8
MAX_COLLECTION = 128
MAX_STRING = 8192
MAX_NODES = 4096
MAX_TEXT = 65536
_KINDS = ("string", "integer", "number", "boolean", "null", "array", "object")


class InvalidToolDefinition(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_tool_definition")


class InvalidArguments(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_arguments")


def valid_name(value: object) -> bool:
    return (
        type(value) is str
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) is not None
    )


@dataclass(frozen=True, slots=True)
class ToolParameter:
    name: str
    kind: ParameterKind
    required: bool = True


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Public metadata only. Security requirements and handlers stay private."""

    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()
    timeout: float = 5.0


def validate_definition(definition: ToolDefinition) -> None:
    if type(definition) is not ToolDefinition:
        raise InvalidToolDefinition()
    if (
        not valid_name(definition.name)
        or type(definition.description) is not str
        or not definition.description.strip()
        or len(definition.description) > 1024
        or type(definition.parameters) is not tuple
        or len(definition.parameters) > MAX_COLLECTION
        or type(definition.timeout) not in (int, float)
    ):
        raise InvalidToolDefinition()
    try:
        if not math.isfinite(definition.timeout) or definition.timeout <= 0:
            raise InvalidToolDefinition()
    except OverflowError:
        raise InvalidToolDefinition() from None
    names: set[str] = set()
    for parameter in definition.parameters:
        if (
            type(parameter) is not ToolParameter
            or not valid_name(parameter.name)
            or type(parameter.kind) is not str
            or parameter.kind not in _KINDS
            or type(parameter.required) is not bool
            or parameter.name in names
        ):
            raise InvalidToolDefinition()
        names.add(parameter.name)


def copy_json(value: object) -> JSONValue:
    """Validate and isolate exact builtins; bound total work as well as depth.

    Arrays/objects contain arbitrary values in this same bounded JSON subset.
    Cycles fail at the depth limit. No copy/deepcopy/serialization hooks run.
    """
    nodes = 0
    text = 0

    def visit(item: object, depth: int) -> JSONValue:
        nonlocal nodes, text
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise InvalidArguments()
        if item is None or type(item) is bool:
            return item
        if type(item) is str:
            text += len(item)
            if len(item) > MAX_STRING or text > MAX_TEXT:
                raise InvalidArguments()
            return item
        if type(item) is int:
            if not -(2**63) <= item < 2**63:
                raise InvalidArguments()
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidArguments()
            return item
        if type(item) is list:
            if len(item) > MAX_COLLECTION:
                raise InvalidArguments()
            return [visit(child, depth + 1) for child in item]
        if type(item) is dict:
            if len(item) > MAX_COLLECTION:
                raise InvalidArguments()
            result: dict[str, JSONValue] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise InvalidArguments()
                visit(key, depth + 1)
                result[key] = visit(child, depth + 1)
            return result
        raise InvalidArguments()

    return visit(value, 0)


def validate_arguments(
    definition: ToolDefinition, arguments: object
) -> dict[str, JSONValue]:
    if type(arguments) is not dict:
        raise InvalidArguments()
    copied = copy_json(arguments)
    assert isinstance(copied, dict)
    parameters = {parameter.name: parameter for parameter in definition.parameters}
    if copied.keys() - parameters.keys():
        raise InvalidArguments()
    types: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "null": (type(None),),
        "array": (list,),
        "object": (dict,),
    }
    for name, parameter in parameters.items():
        if name not in copied:
            if parameter.required:
                raise InvalidArguments()
        elif type(copied[name]) not in types[parameter.kind]:
            raise InvalidArguments()
    return copied
