"""Typed, explicit tools for trusted application controllers."""

from sofia.tools.desktop import DesktopActionPreview, DesktopAutomation, DesktopConfig
from sofia.tools.desktop_backend import ControlRule, DesktopApplication, DesktopError
from sofia.tools.registry import (
    DuplicateTool,
    ToolContext,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    ToolTimeout,
    UnknownTool,
)
from sofia.tools.schemas import (
    InvalidToolDefinition,
    JSONValue,
    ToolDefinition,
    ToolParameter,
)
from sofia.tools.workspace import WorkspaceConfig, WorkspaceFiles, WorkspacePreview
from sofia.tools.workspace_paths import WorkspaceError

__all__ = [
    "ControlRule",
    "DesktopActionPreview",
    "DesktopApplication",
    "DesktopAutomation",
    "DesktopConfig",
    "DesktopError",
    "DuplicateTool",
    "InvalidToolDefinition",
    "JSONValue",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
    "ToolParameter",
    "ToolRegistry",
    "ToolResult",
    "ToolTimeout",
    "UnknownTool",
    "WorkspaceConfig",
    "WorkspaceError",
    "WorkspaceFiles",
    "WorkspacePreview",
]
