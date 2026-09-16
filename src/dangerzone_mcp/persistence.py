"""Project-local JSON storage with atomic writes and interprocess locking."""

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from filelock import FileLock, Timeout

FILENAME = "dangerzone.tools.json"


def project_storage_path(directory: Path) -> Path:
    """Find the containing Git worktree root, falling back to the given directory."""
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"Project directory does not exist: {directory}")
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate / FILENAME
    return directory / FILENAME


class JsonStore:
    """Coordinate read-modify-write transactions on one catalog file."""

    def __init__(self, path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"Tool catalog must not be a symlink: {path}")
        self.path = path.absolute()
        self._lock = FileLock(str(self.path) + ".lock", timeout=5)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold an OS-backed lock shared by every writer of this catalog."""
        try:
            with self._lock:
                yield
        except Timeout as exc:
            raise OSError(f"Timed out locking tool catalog: {self.path}") from exc

    def write(self, data: str) -> None:
        """Flush a temporary file then atomically replace the catalog under its lock."""
        stream = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(stream.name)
        try:
            with stream:
                stream.write(data)
                stream.flush()
                with suppress(FileNotFoundError):  # A new catalog keeps mkstemp's 0600.
                    os.fchmod(stream.fileno(), stat.S_IMODE(self.path.stat().st_mode))
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
