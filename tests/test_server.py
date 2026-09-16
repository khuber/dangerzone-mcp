import asyncio
import errno
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest
from mcp import Client
from mcp.client.subscriptions import ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from dangerzone_mcp.registry import PROTECTED_NAMES
from dangerzone_mcp.server import create_server
from tests.helpers import BASE, COMPLEX_SOURCE, definition, error_text, stdio_target


@pytest.mark.anyio
@pytest.mark.parametrize("stdio", [False, True])
async def test_lifecycle_and_notifications(stdio: bool, tmp_path: Path) -> None:
    target = stdio_target("--project-dir", str(tmp_path)) if stdio else create_server()
    async with Client(target) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_capabilities.tools is not None
        assert client.server_capabilities.tools.list_changed
        initial = await client.list_tools()
        assert [t.name for t in initial.tools] == sorted(PROTECTED_NAMES)
        assert initial.ttl_ms == 0
        assert initial.cache_scope == "private"
        async with client.listen(tools_list_changed=True) as subscription:
            events = aiter(subscription)
            result = await client.call_tool("add_tool", BASE)
            assert not result.is_error
            with anyio.fail_after(5):
                assert isinstance(await anext(events), ToolsListChanged)
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == sorted(PROTECTED_NAMES | {"greet"})
            assert (
                next(t for t in listed.tools if t.name == "greet").input_schema
                == BASE["input_schema"]
            )
            called = await client.call_tool("greet", {"name": "Ada"})
            assert called.structured_content == {"result": "Hello, Ada"}
            edited = {
                **BASE,
                "description": "Double a number",
                "input_schema": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": ["n"],
                    "additionalProperties": False,
                },
                "source": 'async def main(arguments):\n    return arguments["n"] * 2',
            }
            assert not (await client.call_tool("edit_tool", edited)).is_error
            with anyio.fail_after(5):
                assert isinstance(await anext(events), ToolsListChanged)
            assert (await client.call_tool("greet", {"n": 3})).structured_content == {"result": 6}
            assert (await client.call_tool("greet", {"name": "Ada"})).is_error
            updated = next(t for t in (await client.list_tools()).tools if t.name == "greet")
            assert updated.description == "Double a number"
            assert updated.input_schema == edited["input_schema"]
            assert not (await client.call_tool("remove_tool", {"name": "greet"})).is_error
            with anyio.fail_after(5):
                assert isinstance(await anext(events), ToolsListChanged)
            assert (await client.list_tools()).tools == initial.tools
            with pytest.raises(MCPError, match="Unknown tool"):
                await client.call_tool("greet", {"n": 3})


@pytest.mark.anyio
async def test_management_tools_are_immutable_and_failures_do_not_notify() -> None:
    async with Client(create_server()) as client:
        initial = (await client.list_tools()).tools
        async with client.listen(tools_list_changed=True) as subscription:
            for name in sorted(PROTECTED_NAMES):
                for operation in sorted(PROTECTED_NAMES):
                    args = {"name": name} if operation == "remove_tool" else {**BASE, "name": name}
                    result = await client.call_tool(operation, args)
                    assert "Protected built-in" in error_text(result)
            assert (await client.list_tools()).tools == initial
            with anyio.move_on_after(0.1) as scope:
                await anext(aiter(subscription))
            assert scope.cancel_called
        assert not (await client.call_tool("add_tool", BASE)).is_error


@pytest.mark.anyio
async def test_failed_edits_are_atomic_and_duplicate_adds_rejected() -> None:
    async with Client(create_server()) as client:
        assert (await client.call_tool("edit_tool", BASE)).is_error
        assert not (await client.call_tool("add_tool", BASE)).is_error
        assert (await client.call_tool("add_tool", BASE)).is_error
        bad = {**BASE, "source": "def main(:"}
        assert (await client.call_tool("edit_tool", bad)).is_error
        assert (await client.call_tool("greet", {"name": "Ada"})).structured_content == {
            "result": "Hello, Ada"
        }
        assert (await client.call_tool("remove_tool", {"name": "missing"})).is_error
        assert (await client.call_tool("add_tool", {**BASE, "replace": True})).is_error


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source, expected",
    [
        ('def main(a):\n    raise RuntimeError("broken")', "RuntimeError: broken"),
        ("def main(a):\n    return {1, 2}", "not JSON serializable"),
        ('def main(a):\n    return float("nan")', "Out of range"),
        ("def main(a):\n    raise SystemExit(4)", "SystemExit: 4"),
        ("def main(a):\n    raise ValueError(chr(0xD800))", r"ValueError: \ud800"),
        ("import os\ndef main(a):\n    os._exit(7)", "status 7"),
        ("def main(a):\n    while True: pass", "exceeded"),
        ('raise RuntimeError("load failed")\ndef main(a):\n    return 1', "load failed"),
    ],
)
async def test_execution_errors(source: str, expected: str) -> None:
    async with Client(create_server(timeout=0.5 if "while True" in source else 30)) as client:
        assert not (await client.call_tool("add_tool", {**BASE, "source": source})).is_error
        assert expected in error_text(await client.call_tool("greet", {"name": "Ada"}))
        assert len((await client.list_tools()).tools) == 4


@pytest.mark.anyio
async def test_stdout_and_worker_globals_do_not_change_server() -> None:
    source = """
def main(arguments):
    print("diagnostic output")
    import dangerzone_mcp.registry as registry
    registry.PROTECTED_NAMES = frozenset()
    return arguments
"""
    async with Client(create_server()) as client:
        assert not (await client.call_tool("add_tool", {**BASE, "source": source})).is_error
        assert (await client.call_tool("greet", {"name": "Ada"})).structured_content == {
            "result": {"name": "Ada"}
        }
        assert (await client.call_tool("remove_tool", {"name": "add_tool"})).is_error


@pytest.mark.anyio
async def test_multiple_subscribers_and_concurrent_add() -> None:
    server = create_server()
    async with Client(server) as first, Client(server) as second:
        async with (
            first.listen(tools_list_changed=True) as one,
            second.listen(tools_list_changed=True) as two,
        ):
            results = await asyncio.gather(
                first.call_tool("add_tool", BASE), second.call_tool("add_tool", BASE)
            )
            assert sum(not result.is_error for result in results) == 1
            with anyio.fail_after(5):
                assert isinstance(await anext(aiter(one)), ToolsListChanged)
                assert isinstance(await anext(aiter(two)), ToolsListChanged)
            assert (await first.list_tools()).tools == (await second.list_tools()).tools


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        create_server(timeout=timeout)


@pytest.mark.anyio
async def test_local_refs_and_data_named_ref() -> None:
    async with Client(create_server()) as client:
        tool = {
            **definition("test", "def main(a): return a").model_dump(),
            "input_schema": {
                "type": "object",
                "$defs": {"n": {"type": "integer"}},
                "properties": {"n": {"$ref": "#/$defs/n"}},
                "examples": [{"$ref": "this is data, not a schema"}],
            },
        }
        assert not (await client.call_tool("add_tool", tool)).is_error
        assert not (await client.call_tool("test", {"n": 4})).is_error
        assert "Failed validating" not in error_text(await client.call_tool("test", {"n": "bad"}))


@pytest.mark.anyio
async def test_cursor_and_unknown_tool_are_protocol_errors() -> None:
    async with Client(create_server()) as client:
        with pytest.raises(MCPError) as error:
            await client.list_tools(cursor="bogus")
        assert error.value.code == INVALID_PARAMS
        with pytest.raises(MCPError) as error:
            await client.call_tool("missing", {})
        assert error.value.code == INVALID_PARAMS


@pytest.mark.anyio
@pytest.mark.parametrize("stdio", [False, True])
async def test_legacy_tool_changes(tmp_path: Path, stdio: bool) -> None:
    target = stdio_target("--no-persist") if stdio else create_server()
    async with Client(target, mode="legacy") as client:
        assert client.server_capabilities.tools is not None
        assert client.server_capabilities.tools.list_changed
        assert not (await client.call_tool("add_tool", definition("test").model_dump())).is_error
        assert "test" in [tool.name for tool in (await client.list_tools()).tools]


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["add_tool", "edit_tool", "remove_tool"])
async def test_persistence_timeout_is_not_worker_timeout(tmp_path: Path, operation: str) -> None:
    async with Client(create_server(storage_path=tmp_path / "tools.json")) as client:
        assert not (await client.call_tool("add_tool", BASE)).is_error
        arguments = (
            {"name": "greet"}
            if operation == "remove_tool"
            else {**BASE, "name": "another" if operation == "add_tool" else "greet"}
        )
        with patch(
            "dangerzone_mcp.persistence.os.replace",
            side_effect=OSError(errno.ETIMEDOUT, "catalog write timed out"),
        ):
            message = error_text(await client.call_tool(operation, arguments))
        assert "catalog write timed out" in message
        assert "execution exceeded" not in message
        assert (await client.call_tool("greet", {"name": "Ada"})).structured_content == {
            "result": "Hello, Ada"
        }


@pytest.mark.anyio
async def test_surrogate_results_do_not_crash_stdio() -> None:
    async with Client(stdio_target("--no-persist")) as client:
        assert not (await client.call_tool("add_tool", BASE)).is_error
        for expression in ["chr(0xD800)", "[chr(0xDFFF)]", "{chr(0xD800): 1}"]:
            assert not (
                await client.call_tool(
                    "edit_tool", {**BASE, "source": f"def main(a): return {expression}"}
                )
            ).is_error
            message = error_text(await client.call_tool("greet", {"name": "Ada"}))
            assert "surrogate" in message.lower()
            assert len((await client.list_tools()).tools) == 4
        assert not (await client.call_tool("edit_tool", BASE)).is_error
        assert (await client.call_tool("greet", {"name": "Ada"})).structured_content == {
            "result": "Hello, Ada"
        }


@pytest.mark.anyio
async def test_expensive_argument_validation_times_out_without_blocking_other_sessions() -> None:
    server = create_server(timeout=1)
    async with Client(server) as first, Client(server) as second:
        assert not (
            await first.call_tool(
                "add_tool",
                {
                    **BASE,
                    "input_schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "pattern": "^(a+)+$"}},
                    },
                },
            )
        ).is_error
        started = anyio.current_time()
        pending = asyncio.create_task(first.call_tool("greet", {"name": "a" * 28 + "!"}))
        try:
            await anyio.sleep(0.1)
            assert len((await second.list_tools()).tools) == 4
            assert anyio.current_time() - started < 0.8
            with anyio.fail_after(3):
                assert "exceeded 1 seconds" in error_text(await pending)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.anyio
async def test_invalid_arguments_do_not_execute_source(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    async with Client(create_server()) as client:
        assert not (
            await client.call_tool(
                "add_tool",
                {
                    **BASE,
                    "source": f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                    "def main(a): return a",
                },
            )
        ).is_error
        assert (await client.call_tool("greet", {"name": 1})).is_error
        assert not marker.exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes, message",
    [
        ({"source": COMPLEX_SOURCE}, "too complex"),
        ({"input_schema": {"type": "object", "$ref": "#"}}, "Cyclic schema references"),
        (
            {
                "input_schema": {
                    "type": "object",
                    "$ref": "#/examples/0",
                    "examples": [{"$ref": "https://example.com/schema"}],
                }
            },
            "Only local schema references",
        ),
    ],
)
async def test_definition_failures_are_tool_errors(changes: dict[str, Any], message: str) -> None:
    async with Client(create_server()) as client:
        assert not (await client.call_tool("add_tool", BASE)).is_error
        for operation, name in [("add_tool", "invalid"), ("edit_tool", "greet")]:
            result = await client.call_tool(operation, {**BASE, **changes, "name": name})
            assert message in error_text(result)
        assert (await client.call_tool("greet", {"name": "Ada"})).structured_content == {
            "result": "Hello, Ada"
        }
