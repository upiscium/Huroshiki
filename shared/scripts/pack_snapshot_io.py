"""Shared helpers for securely reading files from bounded snapshots.

These utilities are intentionally small and process-free so they can be reused
across publish- and transfer-oriented workflows that need the same "open-by-FD,
identity-bound" file-reading guarantees.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import stat
from typing import Callable


_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_STREAM_CHUNK = 1024 * 1024


class PackSnapshotReadError(RuntimeError):
    """Raised when a snapshot-bound read is rejected for safety reasons."""


def read_snapshot_file(
    root_fd: int,
    relative: Path,
    expected,
    *,
    checkpoint: Callable[[], None],
    directories: dict[Path, object] | None = None,
    max_bytes: int | None = None,
    retain_bytes: bool = True,
) -> bytes:
    """Read a file that has already been validated in a snapshot scan.

    The read is revalidated against the scanned identity for both the target file
    and every opened directory component. This provides the same safety envelope as
    the previous private publication helper.
    """
    current = root_fd
    opened: list[int] = []
    fd = -1
    try:
        for part in relative.parts[:-1]:
            checkpoint()
            child = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current)
            ancestor = Path(*relative.parts[: len(opened) + 1])
            expected_ancestor = directories.get(ancestor) if directories is not None else None
            if expected_ancestor is not None:
                opened_metadata = os.fstat(child)
                if (opened_metadata.st_dev, opened_metadata.st_ino) != (expected_ancestor.device, expected_ancestor.inode):
                    os.close(child)
                    raise PackSnapshotReadError(f"directory changed while opening: {ancestor}")
            opened.append(child)
            current = child

        checkpoint()
        fd = os.open(relative.name, _FILE_OPEN_FLAGS, dir_fd=current)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PackSnapshotReadError(f"unsafe publication file: {relative}")
        if (metadata.st_dev, metadata.st_ino) != (expected.device, expected.inode):
            raise PackSnapshotReadError(f"file changed before reading: {relative}")

        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            checkpoint()
            chunk = os.read(fd, _STREAM_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise PackSnapshotReadError(f"descriptor is too large: {relative}")
            digest.update(chunk)
            if retain_bytes:
                chunks.append(chunk)

        after = os.fstat(fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            expected.device,
            expected.inode,
            expected.size,
            expected.mtime_ns,
            expected.ctime_ns,
        ) or digest.hexdigest() != expected.digest:
            raise PackSnapshotReadError(f"file changed while reading: {relative}")

        for index, ancestor_fd in enumerate(opened):
            checkpoint()
            ancestor_path = Path(*relative.parts[: index + 1])
            expected_ancestor = directories.get(ancestor_path) if directories is not None else None
            if expected_ancestor is not None:
                bound = os.fstat(ancestor_fd)
                if (
                    bound.st_dev,
                    bound.st_ino,
                    bound.st_mtime_ns,
                    bound.st_ctime_ns,
                ) != (
                    expected_ancestor.device,
                    expected_ancestor.inode,
                    expected_ancestor.mtime_ns,
                    expected_ancestor.ctime_ns,
                ):
                    raise PackSnapshotReadError(
                        f"directory changed while reading: {ancestor_path}"
                    )

        return b"".join(chunks) if retain_bytes else b""
    except OSError as error:
        raise PackSnapshotReadError(f"cannot read descriptor-bound file {relative}: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        for item in reversed(opened):
            os.close(item)
