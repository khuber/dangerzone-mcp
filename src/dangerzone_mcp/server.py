"""Self-modifying MCP server example using SDK subscriptions and dynamic Python code."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
from contextlib import suppress
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ToolsListChanged,
)
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from dangerzone_mcp.persistence import project_storage_path
from dangerzone_mcp.registry import (
    PROTECTED_NAMES,
    RemoveToolInput,
    ToolDefinition,
    ToolRegistry,
)

logger = logging.getLogger(__name__)


class ToolExecutionTimeout(TimeoutError):
    """The worker exceeded the configured execution deadline."""


class DynamicToolServer(Server):
    """Advertise tool-list change notifications to legacy clients by default."""

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        """Enable tools_changed unless the caller chose its own notification options."""
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True),
            experimental_capabilities,
            extensions,
        )


MANAGEMENT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "status": {"type": "string"}},
    "required": ["name", "status"],
    "additionalProperties": False,
}
CUSTOM_RESULT_SCHEMA = {
    "type": "object",
    "properties": {"result": {}},
    "required": ["result"],
    "additionalProperties": False,
}
BUILTINS = [
    Tool(
        name="add_tool",
        description=(
            "Add a new Python tool. Use edit_tool to change an existing tool. "
            "source must define main(arguments), sync or async, returning JSON. "
            "input_schema describes the arguments object using JSON Schema 2020-12. "
            "The add_tool, edit_tool and remove_tool built-ins are permanent. "
            "Code executes with the server user's OS permissions."
        ),
        input_schema=ToolDefinition.model_json_schema(),
        output_schema=MANAGEMENT_RESULT_SCHEMA,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
    ),
    Tool(
        name="edit_tool",
        description=(
            "Replace an existing custom tool's description, input_schema, and "
            "Python source. Supply all fields; the name identifies the tool. "
            "Invalid edits leave the old tool intact. "
            "The add_tool, edit_tool and remove_tool built-ins cannot be edited."
        ),
        input_schema=ToolDefinition.model_json_schema(),
        output_schema=MANAGEMENT_RESULT_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    ),
    Tool(
        name="remove_tool",
        description=(
            "Remove a custom tool. The add_tool, edit_tool and remove_tool built-ins "
            "can never be removed."
        ),
        input_schema=RemoveToolInput.model_json_schema(),
        output_schema=MANAGEMENT_RESULT_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=False
        ),
    ),
]


async def execute(
    definition: ToolDefinition, arguments: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """Validate and execute a call in a worker, cleaning up its process group."""
    worker = Path(__file__).with_name("worker.py")
    # Regular files let us reap the worker without waiting for inherited pipe EOF.
    with (
        tempfile.TemporaryFile() as incoming,
        tempfile.TemporaryFile() as outgoing,
        tempfile.TemporaryFile() as errors,
    ):
        incoming.write(
            json.dumps(
                {
                    "source": definition.source,
                    "input_schema": definition.input_schema,
                    "arguments": arguments,
                }
            ).encode()
        )
        incoming.seek(0)
        try:
            with anyio.fail_after(timeout) as deadline:
                with anyio.CancelScope(shield=True):  # Never leave a spawned worker unreaped.
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        str(worker),
                        stdin=incoming,
                        stdout=outgoing,
                        stderr=errors,
                        start_new_session=os.name == "posix",
                    )
                try:
                    await process.wait()
                finally:
                    with suppress(ProcessLookupError):
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        elif process.returncode is None:
                            process.kill()
                    with anyio.CancelScope(shield=True):
                        await process.wait()
        except TimeoutError as exc:
            if deadline.cancel_called:
                raise ToolExecutionTimeout from exc
            raise
        outgoing.seek(0)
        errors.seek(0)
        output, diagnostics = outgoing.read(), errors.read()
    diagnostic_text = diagnostics.decode(errors="replace").strip()
    if diagnostic_text:
        logger.warning("Tool %s stderr:\n%s", definition.name, diagnostic_text)
    if process.returncode:
        raise ValueError(
            f"Tool worker exited with status {process.returncode}"
            + (f"\n{diagnostic_text}" if diagnostic_text else "")
        )
    try:
        result = json.loads(output)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(
            "Tool worker returned invalid JSON; direct writes to stdout "
            "(fd 1 or sys.__stdout__) can corrupt the result channel. Use print() or stderr."
        ) from exc
    if not isinstance(result, dict) or not ({"result", "error"} & result.keys()):
        raise ValueError("Tool worker returned an invalid response")
    if "error" in result:
        raise ValueError(str(result["error"]))
    return {"result": result["result"]}


def create_server(*, timeout: float = 30.0, storage_path: Path | None = None) -> Server:
    """Create a server, optionally sharing a persistent catalog at storage_path."""
    if not 0 < timeout < float("inf"):
        raise ValueError("timeout must be finite and positive")
    registry = ToolRegistry(storage_path)
    bus = InMemorySubscriptionBus()
    lock = anyio.Lock()

    async def list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        if params is not None and params.cursor is not None:
            raise MCPError(INVALID_PARAMS, "Invalid cursor: this server returns a single tool page")
        try:
            definitions = await anyio.to_thread.run_sync(registry.list_tools)
        except (ValueError, OSError) as exc:
            logger.error("Cannot load custom tool catalog %s: %s", storage_path, exc)
            definitions = []
        tools = BUILTINS + [
            Tool(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
                output_schema=CUSTOM_RESULT_SCHEMA,
            )
            for definition in definitions
        ]
        tools.sort(key=lambda tool: tool.name)
        return ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        arguments = params.arguments or {}
        try:
            if params.name in PROTECTED_NAMES:
                async with lock:
                    if params.name in {"add_tool", "edit_tool"}:
                        request = ToolDefinition.model_validate(arguments)
                        if params.name == "add_tool":
                            await anyio.to_thread.run_sync(registry.add, request)
                            status = "added"
                        else:
                            await anyio.to_thread.run_sync(registry.edit, request)
                            status = "edited"
                        name = request.name
                    else:
                        name = RemoveToolInput.model_validate(arguments).name
                        await anyio.to_thread.run_sync(registry.remove, name)
                        status = "removed"
                    await bus.publish(ToolsListChanged())
                    if ctx.session.protocol_version not in MODERN_PROTOCOL_VERSIONS:
                        await ctx.session.send_tool_list_changed()
                data = {"name": name, "status": status}
            else:
                try:
                    definition = await anyio.to_thread.run_sync(registry.get, params.name)
                except KeyError as exc:
                    raise MCPError(INVALID_PARAMS, f"Unknown tool: {params.name}") from exc
                data = await execute(definition, arguments, timeout)
            result = CallToolResult(
                content=[TextContent(type="text", text=json.dumps(data))],
                structured_content=data,
            )
            # Surface unserializable results (lone surrogates) as a tool error here
            # rather than as a crash in the transport writer.
            result.model_dump_json()
            return result
        except ToolExecutionTimeout:
            message = f"Tool execution exceeded {timeout:g} seconds"
        except (ValueError, OSError) as exc:
            message = str(exc)
        message = message.encode("utf-8", errors="backslashreplace").decode("utf-8")
        return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)

    return DynamicToolServer(
        "dangerzone-mcp",
        version=package_version("dangerzone-mcp"),
        instructions=(
            "Create tools with add_tool, change them with edit_tool, call them by name, "
            "and remove them with remove_tool. Management tools are permanent. "
            + (
                f"Custom tools persist in {storage_path}."
                if storage_path is not None
                else "Custom tools are in-memory."
            )
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_subscriptions_listen=ListenHandler(bus),
    )


async def serve_stdio(server: Server) -> None:
    """Serve JSON-RPC on stdin/stdout."""
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    """Run the local MCP server over stdio."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Keep tools in memory; do not read or write a project catalog",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory for Git root discovery (default: current working directory)",
    )
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        path = None if args.no_persist else project_storage_path(args.project_dir)
        server = create_server(timeout=args.timeout, storage_path=path)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    asyncio.run(serve_stdio(server))
