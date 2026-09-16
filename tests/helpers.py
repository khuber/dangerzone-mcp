"""Shared fixtures for the test modules."""

import os
import sys
from typing import Any

from mcp import StdioServerParameters
from mcp.types import CallToolResult, TextContent

from dangerzone_mcp.registry import ToolDefinition

BASE: dict[str, Any] = {
    "name": "greet",
    "description": "Greet a person",
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "source": 'def main(arguments):\n    return "Hello, " + arguments["name"]',
}

# Parses within the character limits but exhausts the parser's recursion limit.
COMPLEX_SOURCE = "def main(a): return " + "+".join(["1"] * 10000)


def definition(name: str = "greet", source: str = "def main(a): return 1") -> ToolDefinition:
    """Create the smallest valid tool definition."""
    return ToolDefinition(
        name=name, description="Example", input_schema={"type": "object"}, source=source
    )


def stdio_target(
    *args: str, cwd: str | None = None, env: dict[str, str] | None = None
) -> StdioServerParameters:
    """Launch the installed server as a subprocess with extra command-line arguments."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "dangerzone_mcp", *args],
        cwd=cwd,
        env=dict(os.environ) if env is None else env,
    )


def error_text(result: CallToolResult) -> str:
    """Return the text of a tool error result."""
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text
