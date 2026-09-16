import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

from dangerzone_mcp.persistence import FILENAME
from dangerzone_mcp.registry import PROTECTED_NAMES
from tests.helpers import COMPLEX_SOURCE, definition, stdio_target


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", "-1"])
def test_bad_cli_timeout_has_no_traceback(tmp_path: Path, timeout: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dangerzone_mcp",
            "--project-dir",
            str(tmp_path),
            "--timeout",
            timeout,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "timeout must be finite and positive" in result.stderr
    assert "Traceback" not in result.stderr


def test_complex_catalog_source_has_no_startup_traceback(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "test", str(tmp_path)], check=True, capture_output=True)
    data = json.dumps({"version": 1, "tools": [definition(source=COMPLEX_SOURCE).model_dump()]})
    (tmp_path / FILENAME).write_text(data)
    result = subprocess.run(
        [sys.executable, "-m", "dangerzone_mcp", "--project-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "too complex" in result.stderr
    assert "Traceback" not in result.stderr
    assert (tmp_path / FILENAME).read_text() == data


@pytest.mark.anyio
@pytest.mark.parametrize("existing_catalog", [None, "{corrupt"])
async def test_no_persist_ignores_catalog_and_does_not_save(
    tmp_path: Path, existing_catalog: str | None
) -> None:
    path = tmp_path / FILENAME
    if existing_catalog is not None:
        path.write_text(existing_catalog)
    target = stdio_target("--no-persist", "--project-dir", str(tmp_path))
    async with Client(target) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == PROTECTED_NAMES
        assert not (
            await client.call_tool("add_tool", definition("temporary").model_dump())
        ).is_error
        assert (await client.call_tool("temporary", {})).structured_content == {"result": 1}
    async with Client(target) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == PROTECTED_NAMES
    if existing_catalog is None:
        assert not path.exists()
    else:
        assert path.read_text() == existing_catalog
    assert not path.with_suffix(".json.lock").exists()
