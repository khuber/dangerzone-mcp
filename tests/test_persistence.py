import json
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest
from mcp import Client
from mcp.client.subscriptions import ToolsListChanged

from dangerzone_mcp.persistence import FILENAME, JsonStore, lock_path, project_storage_path
from dangerzone_mcp.registry import PROTECTED_NAMES, ToolRegistry
from dangerzone_mcp.server import create_server
from tests.helpers import COMPLEX_SOURCE, definition, error_text, stdio_target


def git(directory: Path, *args: str) -> str:
    """Run Git only inside a temporary test repository."""
    return subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_project_discovery(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "test")
    nested = tmp_path / "src" / "nested"
    nested.mkdir(parents=True)
    assert project_storage_path(nested) == tmp_path / FILENAME
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "Test fixture",
    )
    worktree = tmp_path / "linked"
    git(tmp_path, "worktree", "add", "-b", "linked", str(worktree))
    assert (worktree / ".git").is_file()
    assert project_storage_path(worktree) == worktree / FILENAME


def test_project_discovery_outside_git(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "nested"
    nested.mkdir(parents=True)
    assert project_storage_path(nested) == nested / FILENAME


@pytest.mark.parametrize("target_exists", [False, True])
def test_symlinked_catalog_rejected(tmp_path: Path, target_exists: bool) -> None:
    target = tmp_path / "outside" / FILENAME
    if target_exists:
        target.parent.mkdir()
        target.write_text('{"version": 1, "tools": []}\n')
    project = tmp_path / "project"
    project.mkdir()
    (project / FILENAME).symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        JsonStore(project / FILENAME)
    with pytest.raises(ValueError, match="symlink"):
        ToolRegistry(project / FILENAME)
    assert target.exists() == target_exists
    assert not (target.parent / f"{FILENAME}.lock").exists()


def test_nonexistent_project_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="directory"):
        project_storage_path(tmp_path / "missing")


def test_startup_and_reads_create_nothing_in_project(tmp_path: Path, cache_home: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = project / FILENAME
    registry = ToolRegistry(path)
    assert registry.list_tools() == []
    with pytest.raises(KeyError):
        registry.get("greet")
    assert list(project.iterdir()) == []
    lock = lock_path(path)
    assert lock.is_relative_to(cache_home)
    assert lock.exists()
    assert lock_path(tmp_path / "other" / FILENAME) != lock
    registry.add(definition())
    assert sorted(project.iterdir()) == [path]
    assert [tool.name for tool in ToolRegistry(path).list_tools()] == ["greet"]


def test_persistence_lifecycle_and_shared_writers(tmp_path: Path) -> None:
    path = tmp_path / FILENAME
    first = ToolRegistry(path)
    assert not path.exists()
    second = ToolRegistry(path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(first.add, definition("a")),
            executor.submit(second.add, definition("b")),
        ]
        for future in futures:
            future.result()
    assert [tool.name for tool in ToolRegistry(path).list_tools()] == ["a", "b"]
    second.edit(definition("a", "def main(a): return 2"))
    assert first.get("a").source.endswith("return 2")
    first.remove("b")
    assert [tool.name for tool in second.list_tools()] == ["a"]
    assert not list(tmp_path.glob("*.tmp"))
    assert not PROTECTED_NAMES.intersection(
        t["name"] for t in json.loads(path.read_text())["tools"]
    )


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o664])
def test_catalog_mutations_preserve_permissions(tmp_path: Path, mode: int) -> None:
    path = tmp_path / FILENAME
    registry = ToolRegistry(path)
    registry.add(definition())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(mode)
    registry.edit(definition(source="def main(a): return 2"))
    assert stat.S_IMODE(path.stat().st_mode) == mode
    registry.remove("greet")
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("operation", ["add", "edit", "remove"])
def test_failed_save_preserves_catalog(tmp_path: Path, operation: str) -> None:
    path = tmp_path / FILENAME
    registry = ToolRegistry(path)
    registry.add(definition())
    before = path.read_bytes()
    with patch("dangerzone_mcp.persistence.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            if operation == "add":
                registry.add(definition("new"))
            elif operation == "edit":
                registry.edit(definition(source="def main(a): return 9"))
            else:
                registry.remove("greet")
    assert path.read_bytes() == before
    assert ToolRegistry(path).get("greet") == definition()
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_flush_closes_and_cleans_temporary_file(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / FILENAME)
    original_unlink = Path.unlink
    with tempfile.NamedTemporaryFile(mode="w", dir=tmp_path, suffix=".tmp", delete=False) as stream:

        def unlink_closed(path: Path, missing_ok: bool = False) -> None:
            assert stream.closed
            original_unlink(path, missing_ok=missing_ok)

        with (
            patch("dangerzone_mcp.persistence.tempfile.NamedTemporaryFile", return_value=stream),
            patch("dangerzone_mcp.persistence.os.fsync", side_effect=OSError("flush failed")),
            patch.object(Path, "unlink", unlink_closed),
            pytest.raises(OSError, match="flush failed"),
        ):
            store.write("{}")
    assert not list(tmp_path.glob("*.tmp"))
    assert not store.path.exists()


@pytest.mark.parametrize(
    "data",
    [
        "{bad json",
        '{"version":2,"tools":[]}',
        json.dumps({"version": 1, "tools": [definition().model_dump()] * 2}),
        *[
            json.dumps({"version": 1, "tools": [definition(name).model_dump()]})
            for name in PROTECTED_NAMES
        ],
        json.dumps({"version": 1, "tools": [definition(source="invalid python!").model_dump()]}),
        json.dumps({"version": 1, "tools": [definition(source=COMPLEX_SOURCE).model_dump()]}),
    ],
)
def test_invalid_file_is_never_overwritten(tmp_path: Path, data: str) -> None:
    path = tmp_path / FILENAME
    path.write_text(data)
    with pytest.raises(ValueError, match="Invalid tool catalog"):
        ToolRegistry(path)
    assert path.read_text() == data


@pytest.mark.anyio
async def test_stdio_restart_restores_edited_tool_and_removal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "test")
    nested = project / "src"
    nested.mkdir()
    target = stdio_target("--project-dir", str(nested), cwd=str(tmp_path))
    async with Client(target) as client:
        assert not (await client.call_tool("add_tool", definition().model_dump())).is_error
    assert (project / FILENAME).exists()
    assert not (tmp_path / FILENAME).exists()
    assert not (nested / FILENAME).exists()
    # Later restarts exercise the same catalog in process.
    async with Client(create_server(storage_path=project / FILENAME)) as client:
        assert (await client.call_tool("greet", {})).structured_content == {"result": 1}
        assert not (
            await client.call_tool(
                "edit_tool", definition(source="def main(a): return 2").model_dump()
            )
        ).is_error
    async with Client(create_server(storage_path=project / FILENAME)) as client:
        assert (await client.call_tool("greet", {})).structured_content == {"result": 2}
        assert not (await client.call_tool("remove_tool", {"name": "greet"})).is_error
    async with Client(target) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == PROTECTED_NAMES


@pytest.mark.anyio
async def test_save_failure_is_tool_error_without_notification(tmp_path: Path) -> None:
    path = tmp_path / FILENAME
    async with Client(create_server(storage_path=path)) as client:
        async with client.listen(tools_list_changed=True) as subscription:
            with patch("dangerzone_mcp.persistence.os.replace", side_effect=OSError("read-only")):
                assert (await client.call_tool("add_tool", definition().model_dump())).is_error
            with anyio.move_on_after(0.1) as scope:
                await anext(aiter(subscription))
            assert scope.cancel_called
        assert not (await client.call_tool("add_tool", definition().model_dump())).is_error
        async with client.listen(tools_list_changed=True) as subscription:
            assert not (await client.call_tool("remove_tool", {"name": "greet"})).is_error
            with anyio.fail_after(5):
                assert isinstance(await anext(aiter(subscription)), ToolsListChanged)


def test_loading_source_does_not_execute_it(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef main(a): return 1"
    path = tmp_path / FILENAME
    ToolRegistry(path).add(definition(source=source))
    assert ToolRegistry(path).get("greet").source == source
    assert not marker.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("corruption", ["json", "source"])
async def test_corrupt_running_catalog_preserves_builtins_and_recovers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, corruption: str
) -> None:
    path = tmp_path / FILENAME
    async with Client(create_server(storage_path=path)) as client:
        assert not (await client.call_tool("add_tool", definition().model_dump())).is_error
        original = path.read_bytes()
        corrupted = (
            "{corrupt"
            if corruption == "json"
            else json.dumps(
                {"version": 1, "tools": [definition(source=COMPLEX_SOURCE).model_dump()]}
            )
        )
        path.write_text(corrupted)
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == PROTECTED_NAMES
        assert "Cannot load custom tool catalog" in caplog.text
        assert str(path) in caplog.text
        for name, args in [
            ("add_tool", definition("new").model_dump()),
            ("edit_tool", definition().model_dump()),
            ("remove_tool", {"name": "greet"}),
        ]:
            assert "Invalid tool catalog" in error_text(await client.call_tool(name, args))
        assert path.read_text() == corrupted
        path.write_bytes(original)
        assert {tool.name for tool in (await client.list_tools()).tools} == PROTECTED_NAMES | {
            "greet"
        }
        assert (await client.call_tool("greet", {})).structured_content == {"result": 1}
        assert not (await client.call_tool("remove_tool", {"name": "greet"})).is_error


@pytest.mark.anyio
async def test_unreadable_running_catalog_preserves_builtins(tmp_path: Path) -> None:
    path = tmp_path / FILENAME
    async with Client(create_server(storage_path=path)) as client:
        with patch.object(Path, "read_text", side_effect=PermissionError("unreadable")):
            assert {tool.name for tool in (await client.list_tools()).tools} == PROTECTED_NAMES
