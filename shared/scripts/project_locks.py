from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import BinaryIO

from packctl_errors import ConfigError


@dataclass(frozen=True)
class ProjectLockMetadata:
    pid: int
    process_start: str | None
    operation: str
    project_key: str
    acquired_at: str


def process_start_identity(pid: int) -> str | None:
    """Return Linux's process start tick, which remains stable across PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat[stat.rfind(")") + 2 :].split()
        return fields[19]
    except (IndexError, OSError, UnicodeError):
        return None


def read_lock_metadata(handle: BinaryIO) -> ProjectLockMetadata | None:
    try:
        handle.seek(0)
        value = json.loads(handle.read().decode("utf-8"))
        return ProjectLockMetadata(
            pid=int(value["pid"]),
            process_start=(
                str(value["process_start"])
                if value.get("process_start") is not None
                else None
            ),
            operation=str(value["operation"]),
            project_key=str(value["project_key"]),
            acquired_at=str(value["acquired_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
        return None


def format_lock_owner(metadata: ProjectLockMetadata | None) -> str:
    if metadata is None:
        return "owner metadata is unavailable"
    start = metadata.process_start or "unavailable"
    return (
        f"PID {metadata.pid}, process start {start}, operation "
        f"{metadata.operation!r}, project {metadata.project_key}, acquired "
        f"{metadata.acquired_at}"
    )


class ProjectLock:
    """One non-reentrant, process-level advisory lock for a project."""

    def __init__(self, project_key: str, operation: str, path: Path) -> None:
        self.project_key = project_key
        self.operation = operation
        self._handle: BinaryIO | None = None
        self.path = path
        self.metadata: ProjectLockMetadata | None = None

    def acquire(self) -> ProjectLock:
        if self._handle is not None:
            raise ConfigError(f"Project lock is already held: {self.project_key}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ConfigError(f"Unsafe project lock path: {self.path}")
        handle = self.path.open("a+b")
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                owner = read_lock_metadata(handle)
                raise ConfigError(
                    f"Project is locked: {self.project_key} ({format_lock_owner(owner)})"
                ) from error
            metadata = ProjectLockMetadata(
                pid=os.getpid(),
                process_start=process_start_identity(os.getpid()),
                operation=self.operation,
                project_key=self.project_key,
                acquired_at=datetime.now(timezone.utc).isoformat(),
            )
            handle.seek(0)
            handle.truncate()
            handle.write(
                (json.dumps(metadata.__dict__, sort_keys=True) + "\n").encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
            self.metadata = metadata
            self._handle = handle
            return self
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> ProjectLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except (OSError, ValueError):
            pass


def inspect_lock_path(path: Path) -> tuple[bool, ProjectLockMetadata | None]:
    if not path.is_file() or path.is_symlink():
        return False, None
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, read_lock_metadata(handle)
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
    return False, None
