import asyncio
import io
import json
import logging
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from dangerzone_mcp.server import ToolExecutionTimeout, create_server, execute
from dangerzone_mcp.worker import main
from tests.helpers import definition, error_text


@pytest.mark.parametrize(
    "source, expected",
    [
        ('def main(a):\n    print("log")\n    return a["n"] + 1', {"result": 3}),
        ('async def main(a):\n    return a["n"] + 1', {"result": 3}),
        ('def main(a):\n    raise ValueError("bad")', {"error": "ValueError: bad"}),
        (
            "from jsonschema.exceptions import ValidationError\n"
            'def main(a): raise ValidationError("tool raised this")',
            {"error": "ValidationError: tool raised this"},
        ),
        ("def main(a):\n    raise SystemExit(0)", {"error": "SystemExit: 0"}),
    ],
)
def test_worker_json_contract(
    monkeypatch: pytest.MonkeyPatch, source: str, expected: dict[str, object]
) -> None:
    incoming = io.StringIO(
        json.dumps({"source": source, "input_schema": {"type": "object"}, "arguments": {"n": 2}})
    )
    outgoing = io.StringIO()
    monkeypatch.setattr("sys.stdin", incoming)
    monkeypatch.setattr("sys.stdout", outgoing)
    main()
    result = json.loads(outgoing.getvalue())
    if "error" in expected:
        assert result["error"].startswith(expected["error"])
        assert "Traceback" in result["error"]
    else:
        assert result == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source, expected",
    [
        (
            'def main(a):\n    import os\n    os.write(1, b"raw stdout")\n    return 1',
            "direct writes",
        ),
        (
            'import sys, os\ndef main(a):\n    sys.stderr.write("crash detail\\n")\n'
            "    sys.stderr.flush()\n    os._exit(1)",
            "crash detail",
        ),
    ],
)
async def test_worker_channel_errors(source: str, expected: str) -> None:
    async with Client(create_server()) as client:
        await client.call_tool("add_tool", definition("test", source).model_dump())
        assert expected in error_text(await client.call_tool("test", {}))


@pytest.mark.anyio
async def test_worker_diagnostics_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    async with Client(create_server()) as client:
        source = 'def main(a):\n    print("diagnostic")\n    return 1'
        await client.call_tool("add_tool", definition("test", source).model_dump())
        with caplog.at_level(logging.WARNING):
            assert not (await client.call_tool("test", {})).is_error
        assert "diagnostic" in caplog.text


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize(
    "outcome, detached",
    [("success", False), ("success", True), ("timeout", False), ("cancel", False)],
)
async def test_worker_exit_and_cleanup_with_background_children(
    tmp_path: Path, outcome: str, detached: bool
) -> None:
    marker = tmp_path / "pids.json"
    source = f"""
import json, os, subprocess, sys, time
from pathlib import Path
def main(arguments):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session={detached!r},
    )
    Path({str(marker)!r}).write_text(json.dumps([os.getpid(), child.pid]))
    {"return child.pid" if outcome == "success" else "time.sleep(60)"}
"""
    tool = definition("background", source)
    started = anyio.current_time()
    pending = asyncio.create_task(execute(tool, {}, 1 if outcome == "timeout" else 10))
    try:
        with anyio.fail_after(5):
            while not marker.exists():
                await anyio.sleep(0.01)
            worker_pid, child_pid = json.loads(marker.read_text())
            if outcome == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif outcome == "timeout":
                with pytest.raises(ToolExecutionTimeout):
                    await pending
            else:
                assert await pending == {"result": child_pid}
        assert anyio.current_time() - started < 3
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        if not detached:
            with anyio.fail_after(2):
                while True:
                    status = subprocess.run(
                        ["ps", "-o", "stat=", "-p", str(child_pid)],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip()
                    if not status or status.startswith("Z"):
                        break
                    await anyio.sleep(0.01)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        if marker.exists():
            for pid in json.loads(marker.read_text()):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@pytest.mark.anyio
async def test_argument_validation_is_cancellable(monkeypatch: pytest.MonkeyPatch) -> None:
    processes: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def track_worker(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_worker)
    tool = definition("pattern", "def main(a): return a").model_copy(
        update={
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string", "pattern": "^(a+)+$"}},
            }
        }
    )
    started = anyio.current_time()
    with anyio.move_on_after(0.5) as scope:
        await execute(tool, {"text": "a" * 28 + "!"}, timeout=30)
    assert scope.cancel_called
    assert anyio.current_time() - started < 2
    assert len(processes) == 1
    assert processes[0].returncode is not None


@pytest.mark.anyio
async def test_recursive_validation_is_a_tool_error() -> None:
    # Also contain validation failures if execute is called without registry checks.
    tool = definition("recursive").model_copy(
        update={"input_schema": {"type": "object", "$ref": "#"}}
    )
    with pytest.raises(ValueError, match="RecursionError"):
        await execute(tool, {}, timeout=10)
