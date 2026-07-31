from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import codecs
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Callable, Iterator, Literal
from uuid import uuid4


OVERLAY_TARGETS = ("common", "client", "server")
PACKWIZ_ROOT_FILES = {"pack.toml", "index.toml"}


class OverlayPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class OverlayEntry:
    relative_path: Path
    kind: str
    size: int = 0
    link_target: str | None = None
    mode: int = 0
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True)
class OverlayFile:
    contents: bytes
    mode: int
    device: int
    inode: int
    digest: str


@dataclass(frozen=True)
class OverlayFileInspection:
    size: int
    mode: int
    device: int
    inode: int
    digest: str
    text_kind: Literal["utf8", "binary"]
    text_probe: bytes


@dataclass(frozen=True)
class OverlayIssue:
    relative_path: Path
    message: str


@dataclass(frozen=True)
class OverlayScan:
    entries: tuple[OverlayEntry, ...]
    issues: tuple[OverlayIssue, ...]


@dataclass(frozen=True)
class LocalImportEntry:
    relative_path: Path
    kind: Literal["file", "directory", "invalid"]
    size: int
    mode: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    digest: str | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalImportScan:
    root: Path
    kind: Literal["file", "directory", "invalid"]
    entries: tuple[LocalImportEntry, ...]


def _invalidate_local_import_entry(
    entry: LocalImportEntry,
    message: str,
) -> LocalImportEntry:
    return LocalImportEntry(
        entry.relative_path,
        "invalid",
        entry.size,
        entry.mode,
        entry.device,
        entry.inode,
        entry.mtime_ns,
        entry.ctime_ns,
        entry.digest,
        entry.errors + (message,),
    )


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_STREAM_CHUNK_SIZE = 1024 * 1024


@dataclass
class _OverlayParent:
    fd: int
    ancestors: list[tuple[int, str]]


def _open_directory(name: str, parent_fd: int) -> int:
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def _renameat2(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
    flags: int,
) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "atomic overlay rename is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "atomic overlay rename is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        flags,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), new_name)


@contextmanager
def _open_overlay_parent(
    content_root: Path,
    target: str,
    relative: Path,
    *,
    create: bool,
) -> Iterator[_OverlayParent]:
    if target not in OVERLAY_TARGETS:
        raise OverlayPolicyError("Overlay target must be common, client, or server")

    fds: list[int] = []
    ancestors: list[tuple[int, str]] = []
    try:
        root_parent_fd = os.open(content_root.parent, _DIRECTORY_FLAGS)
        fds.append(root_parent_fd)
        try:
            content_fd = _open_directory(content_root.name, root_parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(content_root.name, dir_fd=root_parent_fd)
            content_fd = _open_directory(content_root.name, root_parent_fd)
        fds.append(content_fd)
        try:
            target_fd = _open_directory(target, content_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(target, dir_fd=content_fd)
            target_fd = _open_directory(target, content_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise OverlayPolicyError(
                    f"Symlink is not allowed in content overlay: {target} "
                    f"-> {_readlink_at(target, content_fd)}"
                ) from error
            raise
        fds.append(target_fd)
        current_fd = target_fd
        for part in relative.parts[:-1]:
            try:
                child_fd = _open_directory(part, current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=current_fd)
                child_fd = _open_directory(part, current_fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    checked = Path(target, *relative.parts[: len(ancestors) + 1])
                    raise OverlayPolicyError(
                        f"Symlink is not allowed in content overlay: {checked} "
                        f"-> {_readlink_at(part, current_fd)}"
                    ) from error
                raise
            ancestors.append((current_fd, part))
            fds.append(child_fd)
            current_fd = child_fd
        yield _OverlayParent(current_fd, ancestors)
    except OSError as error:
        raise OverlayPolicyError(
            f"Cannot open overlay path {target}/{relative}: {error}"
        ) from error
    finally:
        for fd in reversed(fds):
            os.close(fd)


def create_overlay_file(content_root: Path, target: str, relative_path: str | Path) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    with _open_overlay_parent(content_root, target, relative, create=True) as parent:
        try:
            fd = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o666,
                dir_fd=parent.fd,
            )
        except FileExistsError as error:
            raise OverlayPolicyError(f"Overlay file already exists: {target}/{relative}") from error
        os.close(fd)


def read_overlay_bytes(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    *,
    max_bytes: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> OverlayFile:
    relative = normalize_overlay_relative_path(relative_path)
    if max_bytes is not None and max_bytes < 0:
        raise OverlayPolicyError("Overlay read limit must be non-negative")
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        try:
            fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent.fd,
            )
        except FileNotFoundError as error:
            raise OverlayPolicyError(
                f"Overlay file does not exist: {target}/{relative}"
            ) from error
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OverlayPolicyError(
                    f"Overlay file does not exist: {target}/{relative}"
                )
            if max_bytes is not None and opened.st_size > max_bytes:
                raise OverlayPolicyError(
                    f"Overlay file exceeds the {max_bytes}-byte read limit: "
                    f"{target}/{relative}"
                )
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            total = 0
            while True:
                if checkpoint is not None:
                    checkpoint()
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise OverlayPolicyError(
                        f"Overlay file exceeds the {max_bytes}-byte read limit: "
                        f"{target}/{relative}"
                    )
                chunks.append(chunk)
                digest.update(chunk)
            after = os.fstat(fd)
            current = os.stat(
                relative.name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            identity = (opened.st_dev, opened.st_ino)
            if (
                identity != (after.st_dev, after.st_ino)
                or identity != (current.st_dev, current.st_ino)
                or opened.st_size != after.st_size
                or opened.st_mtime_ns != after.st_mtime_ns
                or opened.st_ctime_ns != after.st_ctime_ns
            ):
                raise OverlayPolicyError(
                    f"Overlay file changed while reading: {target}/{relative}"
                )
            return OverlayFile(
                b"".join(chunks),
                stat.S_IMODE(opened.st_mode),
                opened.st_dev,
                opened.st_ino,
                digest.hexdigest(),
            )
        finally:
            os.close(fd)


def inspect_overlay_file(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    *,
    probe_bytes: int = 0,
    checkpoint: Callable[[], None] | None = None,
) -> OverlayFileInspection:
    relative = normalize_overlay_relative_path(relative_path)
    if probe_bytes < 0:
        raise OverlayPolicyError("Overlay text probe limit must be non-negative")
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        try:
            fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent.fd,
            )
        except FileNotFoundError as error:
            raise OverlayPolicyError(
                f"Overlay file does not exist: {target}/{relative}"
            ) from error
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OverlayPolicyError(
                    f"Overlay file does not exist: {target}/{relative}"
                )
            digest = hashlib.sha256()
            probe = bytearray()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            valid_utf8 = True
            contains_nul = False
            while True:
                if checkpoint is not None:
                    checkpoint()
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                contains_nul = contains_nul or b"\0" in chunk
                if valid_utf8 and not contains_nul:
                    try:
                        decoder.decode(chunk, final=False)
                    except UnicodeDecodeError:
                        valid_utf8 = False
                if len(probe) < probe_bytes:
                    probe.extend(chunk[: probe_bytes - len(probe)])
            if valid_utf8 and not contains_nul:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    valid_utf8 = False
            after = os.fstat(fd)
            current = os.stat(
                relative.name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            identity = (opened.st_dev, opened.st_ino)
            if (
                identity != (after.st_dev, after.st_ino)
                or identity != (current.st_dev, current.st_ino)
                or opened.st_size != after.st_size
                or opened.st_mtime_ns != after.st_mtime_ns
                or opened.st_ctime_ns != after.st_ctime_ns
            ):
                raise OverlayPolicyError(
                    f"Overlay file changed while inspecting: {target}/{relative}"
                )
            return OverlayFileInspection(
                opened.st_size,
                stat.S_IMODE(opened.st_mode),
                opened.st_dev,
                opened.st_ino,
                digest.hexdigest(),
                "utf8" if valid_utf8 and not contains_nul else "binary",
                bytes(probe),
            )
        finally:
            os.close(fd)


def read_overlay_text(content_root: Path, target: str, relative_path: str | Path) -> str:
    return read_overlay_bytes(content_root, target, relative_path).contents.decode("utf-8")


def create_overlay_directory(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    *,
    mode: int = 0o755,
) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        try:
            os.mkdir(relative.name, mode, dir_fd=parent.fd)
        except FileExistsError as error:
            raise OverlayPolicyError(
                f"Overlay entry already exists: {target}/{relative}"
            ) from error


def set_overlay_directory_mode(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    mode: int,
) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        directory_fd = _open_directory(relative.name, parent.fd)
        try:
            opened = os.fstat(directory_fd)
            os.fchmod(directory_fd, mode)
            current = os.stat(
                relative.name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise OverlayPolicyError(
                    f"Overlay directory changed while setting mode: {target}/{relative}"
                )
        finally:
            os.close(directory_fd)


def write_overlay_bytes(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    contents: bytes,
    *,
    mode: int | None = 0o644,
    create: bool,
    expected_digest: str | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    temporary_name = f".{relative.name}.huroshiki-tmp-{uuid4().hex}"
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        existing_identity: tuple[int, int] | None = None
        if create:
            try:
                os.stat(relative.name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise OverlayPolicyError(
                    f"Overlay entry already exists: {target}/{relative}"
                )
        else:
            current = inspect_overlay_file(
                content_root,
                target,
                relative,
                checkpoint=checkpoint,
            )
            existing_identity = (current.device, current.inode)
            if expected_digest is not None and current.digest != expected_digest:
                raise OverlayPolicyError(
                    f"Overlay file digest changed: {target}/{relative}"
                )
            if mode is None:
                mode = current.mode
        if mode is None:
            raise OverlayPolicyError("Overlay create mode is required")
        temporary_fd = -1
        preserve_temporary = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                mode,
                dir_fd=parent.fd,
            )
            view = memoryview(contents)
            offset = 0
            while offset < len(view):
                if checkpoint is not None:
                    checkpoint()
                chunk = view[offset : offset + 1024 * 1024]
                while chunk:
                    written = os.write(temporary_fd, chunk)
                    if written == 0:
                        raise OSError(errno.EIO, "short overlay write")
                    offset += written
                    chunk = chunk[written:]
            os.fchmod(temporary_fd, mode)
            os.close(temporary_fd)
            temporary_fd = -1
            if existing_identity is not None:
                current = os.stat(
                    relative.name,
                    dir_fd=parent.fd,
                    follow_symlinks=False,
                )
                if existing_identity != (current.st_dev, current.st_ino):
                    raise OverlayPolicyError(
                        f"Overlay file changed before replacement: {target}/{relative}"
                    )
            if create:
                if checkpoint is not None:
                    checkpoint()
                published = False
                try:
                    _renameat2(
                        parent.fd,
                        temporary_name,
                        parent.fd,
                        relative.name,
                        _RENAME_NOREPLACE,
                    )
                    published = True
                    if checkpoint is not None:
                        checkpoint()
                except FileExistsError as error:
                    raise OverlayPolicyError(
                        f"Overlay entry already exists: {target}/{relative}"
                    ) from error
                except BaseException as error:
                    if published:
                        try:
                            _renameat2(
                                parent.fd,
                                relative.name,
                                parent.fd,
                                temporary_name,
                                _RENAME_NOREPLACE,
                            )
                        except BaseException as rollback_error:
                            preserve_temporary = True
                            raise OverlayPolicyError(
                                f"Overlay create rollback failed: {target}/{relative}: "
                                f"{rollback_error}"
                            ) from error
                    raise
            else:
                if checkpoint is not None:
                    checkpoint()
                _renameat2(
                    parent.fd,
                    temporary_name,
                    parent.fd,
                    relative.name,
                    _RENAME_EXCHANGE,
                )
                try:
                    exchanged = os.stat(
                        temporary_name,
                        dir_fd=parent.fd,
                        follow_symlinks=False,
                    )
                    if existing_identity != (exchanged.st_dev, exchanged.st_ino):
                        raise OverlayPolicyError(
                            f"Overlay file changed during replacement: {target}/{relative}"
                        )
                    if checkpoint is not None:
                        checkpoint()
                except BaseException as error:
                    try:
                        _renameat2(
                            parent.fd,
                            temporary_name,
                            parent.fd,
                            relative.name,
                            _RENAME_EXCHANGE,
                        )
                    except BaseException as rollback_error:
                        preserve_temporary = True
                        raise OverlayPolicyError(
                            f"Overlay replace rollback failed: {target}/{relative}: "
                            f"{rollback_error}"
                        ) from error
                    raise
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if not preserve_temporary:
                try:
                    os.unlink(temporary_name, dir_fd=parent.fd)
                except FileNotFoundError:
                    pass


def delete_overlay_entry(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    *,
    directory: bool,
) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    retained_name = f".{relative.name}.huroshiki-delete-{uuid4().hex}"
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        try:
            metadata = os.stat(
                relative.name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise OverlayPolicyError(
                f"Overlay entry does not exist: {target}/{relative}"
            ) from error
        if directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise OverlayPolicyError(
                    f"Overlay directory does not exist: {target}/{relative}"
                )
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise OverlayPolicyError(
                    f"Overlay file does not exist: {target}/{relative}"
                )
        _renameat2(
            parent.fd,
            relative.name,
            parent.fd,
            retained_name,
            _RENAME_NOREPLACE,
        )
        moved = os.stat(
            retained_name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if (moved.st_dev, moved.st_ino) != (metadata.st_dev, metadata.st_ino):
            _renameat2(
                parent.fd,
                retained_name,
                parent.fd,
                relative.name,
                _RENAME_NOREPLACE,
            )
            raise OverlayPolicyError(
                f"Overlay entry changed during deletion: {target}/{relative}"
            )
        if directory:
            try:
                os.rmdir(retained_name, dir_fd=parent.fd)
            except OSError as error:
                try:
                    _renameat2(
                        parent.fd,
                        retained_name,
                        parent.fd,
                        relative.name,
                        _RENAME_NOREPLACE,
                    )
                except OSError as rollback_error:
                    raise OverlayPolicyError(
                        f"Overlay directory deletion rollback failed: "
                        f"{target}/{relative}: {rollback_error}"
                    ) from error
                if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise OverlayPolicyError(
                        f"Overlay directory is not empty: {target}/{relative}"
                    ) from error
                raise
        else:
            os.unlink(retained_name, dir_fd=parent.fd)


def move_overlay_entry(
    content_root: Path,
    source_target: str,
    source_path: str | Path,
    destination_target: str,
    destination_path: str | Path,
) -> None:
    source = normalize_overlay_relative_path(source_path)
    destination = normalize_overlay_relative_path(destination_path)
    if (
        source_target == destination_target
        and len(destination.parts) > len(source.parts)
        and destination.parts[: len(source.parts)] == source.parts
    ):
        raise OverlayPolicyError("Cannot move an overlay directory into itself")
    with _open_overlay_parent(
        content_root, source_target, source, create=False
    ) as source_parent, _open_overlay_parent(
        content_root, destination_target, destination, create=False
    ) as destination_parent:
        try:
            source_metadata = os.stat(
                source.name,
                dir_fd=source_parent.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise OverlayPolicyError(
                f"Overlay source does not exist: {source_target}/{source}"
            ) from error
        if not (
            stat.S_ISREG(source_metadata.st_mode)
            or stat.S_ISDIR(source_metadata.st_mode)
        ):
            raise OverlayPolicyError(
                f"Overlay source is not a file or directory: {source_target}/{source}"
            )
        try:
            os.stat(
                destination.name,
                dir_fd=destination_parent.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise OverlayPolicyError(
                f"Overlay destination already exists: "
                f"{destination_target}/{destination}"
            )
        _renameat2(
            source_parent.fd,
            source.name,
            destination_parent.fd,
            destination.name,
            _RENAME_NOREPLACE,
        )
        moved = os.stat(
            destination.name,
            dir_fd=destination_parent.fd,
            follow_symlinks=False,
        )
        if (moved.st_dev, moved.st_ino) != (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            _renameat2(
                destination_parent.fd,
                destination.name,
                source_parent.fd,
                source.name,
                _RENAME_NOREPLACE,
            )
            raise OverlayPolicyError(
                f"Overlay source changed during move: {source_target}/{source}"
            )


def write_overlay_text(
    content_root: Path,
    target: str,
    relative_path: str | Path,
    text: str,
) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    temporary_name = f".{relative.name}.huroshiki-tmp-{uuid4().hex}"
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        existing_fd = -1
        temporary_fd = -1
        try:
            existing_fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent.fd,
            )
            if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                raise OverlayPolicyError(f"Overlay file does not exist: {target}/{relative}")
            os.close(existing_fd)
            existing_fd = -1
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o666,
                dir_fd=parent.fd,
            )
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                temporary_fd = -1
                handle.write(text)
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
        finally:
            if existing_fd >= 0:
                os.close(existing_fd)
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=parent.fd)
            except FileNotFoundError:
                pass


def delete_overlay_file(content_root: Path, target: str, relative_path: str | Path) -> None:
    relative = normalize_overlay_relative_path(relative_path)
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        fd = -1
        try:
            fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent.fd,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OverlayPolicyError(f"Overlay file does not exist: {target}/{relative}")
        finally:
            if fd >= 0:
                os.close(fd)
        os.unlink(relative.name, dir_fd=parent.fd)
        for grandparent_fd, name in reversed(parent.ancestors):
            try:
                os.rmdir(name, dir_fd=grandparent_fd)
            except OSError:
                break


def is_packwiz_owned_name(name: str) -> bool:
    return name in PACKWIZ_ROOT_FILES or name.endswith(".pw.toml")


def normalize_overlay_relative_path(value: str | Path) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts:
        raise OverlayPolicyError("Overlay path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise OverlayPolicyError(
            "Overlay path cannot contain '.', '..', or empty components"
        )
    if any(part == ".gitkeep" for part in relative.parts):
        raise OverlayPolicyError(".gitkeep is managed internally")
    reserved = next((part for part in relative.parts if is_packwiz_owned_name(part)), None)
    if reserved is not None:
        raise OverlayPolicyError(
            f"Packwiz-owned path is not allowed in content overlays: {relative}"
        )
    return relative


def _link_target(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError as error:
        return f"<unreadable: {error}>"


def _scan_path(
    path: Path,
    relative: Path,
    entries: list[OverlayEntry],
    issues: list[OverlayIssue],
    *,
    include_entry: bool = True,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    if checkpoint is not None:
        checkpoint()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        issues.append(OverlayIssue(relative, f"cannot inspect entry: {error}"))
        return

    if stat.S_ISLNK(metadata.st_mode):
        target = _link_target(path)
        entries.append(OverlayEntry(relative, "symlink", link_target=target, mode=stat.S_IMODE(metadata.st_mode), device=metadata.st_dev, inode=metadata.st_ino))
        issues.append(OverlayIssue(relative, f"symlink is not allowed -> {target}"))
        if is_packwiz_owned_name(path.name):
            issues.append(OverlayIssue(relative, "Packwiz-owned path is not allowed"))
        return

    if not include_entry and not stat.S_ISDIR(metadata.st_mode):
        issues.append(OverlayIssue(relative, "overlay target must be a directory"))
        return

    if include_entry and stat.S_ISREG(metadata.st_mode):
        entries.append(OverlayEntry(relative, "file", metadata.st_size, mode=stat.S_IMODE(metadata.st_mode), device=metadata.st_dev, inode=metadata.st_ino))
    elif include_entry and stat.S_ISDIR(metadata.st_mode):
        entries.append(OverlayEntry(relative, "directory", mode=stat.S_IMODE(metadata.st_mode), device=metadata.st_dev, inode=metadata.st_ino))
    elif include_entry and not stat.S_ISDIR(metadata.st_mode):
        entries.append(OverlayEntry(relative, "special", mode=stat.S_IMODE(metadata.st_mode), device=metadata.st_dev, inode=metadata.st_ino))
        issues.append(OverlayIssue(relative, "special filesystem entry is not allowed"))

    if include_entry and is_packwiz_owned_name(path.name):
        issues.append(OverlayIssue(relative, "Packwiz-owned path is not allowed"))

    if not stat.S_ISDIR(metadata.st_mode):
        return
    if checkpoint is not None:
        checkpoint()
    try:
        children: list[tuple[str, Path]] = []
        with os.scandir(path) as iterator:
            for child in iterator:
                if checkpoint is not None:
                    checkpoint()
                children.append((child.name, Path(child.path)))
        children.sort(key=lambda item: item[0])
    except OSError as error:
        issues.append(OverlayIssue(relative, f"cannot list directory: {error}"))
        return
    for child_name, child_path in children:
        if checkpoint is not None:
            checkpoint()
        child_relative = relative / child_name
        _scan_path(
            child_path,
            child_relative,
            entries,
            issues,
            checkpoint=checkpoint,
        )


def scan_content_overlays(
    content_root: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> OverlayScan:
    entries: list[OverlayEntry] = []
    issues: list[OverlayIssue] = []
    if checkpoint is not None:
        checkpoint()
    try:
        metadata = content_root.lstat()
    except FileNotFoundError:
        return OverlayScan((), ())
    except OSError as error:
        return OverlayScan((), (OverlayIssue(Path("."), f"cannot inspect entry: {error}"),))

    if stat.S_ISLNK(metadata.st_mode):
        target = _link_target(content_root)
        return OverlayScan(
            (OverlayEntry(Path("."), "symlink", link_target=target),),
            (OverlayIssue(Path("."), f"symlink is not allowed -> {target}"),),
        )
    if not stat.S_ISDIR(metadata.st_mode):
        return OverlayScan(
            (), (OverlayIssue(Path("."), "content overlay root must be a directory"),)
        )

    for target in OVERLAY_TARGETS:
        if checkpoint is not None:
            checkpoint()
        _scan_path(
            content_root / target,
            Path(target),
            entries,
            issues,
            include_entry=False,
            checkpoint=checkpoint,
        )
    return OverlayScan(tuple(entries), tuple(issues))


def _readlink_at(name: str, directory_fd: int) -> str:
    try:
        return os.readlink(name, dir_fd=directory_fd)
    except OSError as error:
        return f"<unreadable: {error}>"


def _destination_entry_issue(
    name: str,
    destination_fd: int,
    relative: Path,
) -> OverlayIssue:
    try:
        metadata = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
    except OSError as error:
        return OverlayIssue(relative, f"cannot inspect destination entry: {error}")
    if stat.S_ISLNK(metadata.st_mode):
        return OverlayIssue(
            relative,
            f"destination symlink is not allowed -> {_readlink_at(name, destination_fd)}",
        )
    return OverlayIssue(relative, "destination special filesystem entry is not allowed")


def _open_destination_directory(
    name: str,
    destination_fd: int,
    relative: Path,
    issues: list[OverlayIssue],
) -> int | None:
    try:
        os.mkdir(name, dir_fd=destination_fd)
    except FileExistsError:
        pass
    except OSError as error:
        issues.append(OverlayIssue(relative, f"cannot create destination directory: {error}"))
        return None
    try:
        return _open_directory(name, destination_fd)
    except OSError:
        issues.append(_destination_entry_issue(name, destination_fd, relative))
        return None


def _destination_directory_replaced(
    name: str,
    destination_fd: int,
    opened_fd: int,
    relative: Path,
    issues: list[OverlayIssue],
) -> None:
    opened = os.fstat(opened_fd)
    try:
        current = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
    except OSError as error:
        issues.append(OverlayIssue(relative, f"destination directory was replaced: {error}"))
        return
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        issues.append(_destination_entry_issue(name, destination_fd, relative))


def _open_destination_file(
    name: str,
    destination_fd: int,
    relative: Path,
    issues: list[OverlayIssue],
) -> int | None:
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    for attempt in range(2):
        try:
            file_fd = os.open(name, flags, dir_fd=destination_fd)
            break
        except FileNotFoundError:
            try:
                file_fd = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o666,
                    dir_fd=destination_fd,
                )
                break
            except FileExistsError:
                if attempt == 0:
                    continue
                issues.append(
                    OverlayIssue(relative, "destination entry changed while opening")
                )
                return None
            except OSError:
                issues.append(_destination_entry_issue(name, destination_fd, relative))
                return None
        except OSError:
            issues.append(_destination_entry_issue(name, destination_fd, relative))
            return None
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        issues.append(_destination_entry_issue(name, destination_fd, relative))
        return None
    os.ftruncate(file_fd, 0)
    return file_fd


def _destination_file_replaced(
    name: str,
    destination_fd: int,
    opened_fd: int,
    relative: Path,
    issues: list[OverlayIssue],
) -> None:
    opened = os.fstat(opened_fd)
    try:
        current = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
    except OSError as error:
        issues.append(OverlayIssue(relative, f"destination file was replaced: {error}"))
        return
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        issues.append(_destination_entry_issue(name, destination_fd, relative))


def _copy_overlay_directory(
    source_fd: int,
    destination_fd: int,
    relative: Path,
    entries: list[OverlayEntry],
    issues: list[OverlayIssue],
) -> None:
    try:
        with os.scandir(source_fd) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as error:
        issues.append(OverlayIssue(relative, f"cannot list directory: {error}"))
        return

    for child in children:
        child_relative = relative / child.name
        try:
            metadata = child.stat(follow_symlinks=False)
        except OSError as error:
            issues.append(OverlayIssue(child_relative, f"cannot inspect entry: {error}"))
            continue
        if stat.S_ISLNK(metadata.st_mode):
            target = _readlink_at(child.name, source_fd)
            entries.append(OverlayEntry(child_relative, "symlink", link_target=target))
            issues.append(OverlayIssue(child_relative, f"symlink is not allowed -> {target}"))
            if is_packwiz_owned_name(child.name):
                issues.append(OverlayIssue(child_relative, "Packwiz-owned path is not allowed"))
            continue
        if is_packwiz_owned_name(child.name):
            issues.append(OverlayIssue(child_relative, "Packwiz-owned path is not allowed"))
            continue
        if child.name == ".gitkeep":
            continue
        if stat.S_ISDIR(metadata.st_mode):
            output_fd: int | None = None
            try:
                child_fd = _open_directory(child.name, source_fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    target = _readlink_at(child.name, source_fd)
                    entries.append(
                        OverlayEntry(child_relative, "symlink", link_target=target)
                    )
                    issues.append(
                        OverlayIssue(child_relative, f"symlink is not allowed -> {target}")
                    )
                else:
                    issues.append(
                        OverlayIssue(child_relative, f"cannot open directory: {error}")
                    )
                continue
            try:
                output_fd = _open_destination_directory(
                    child.name, destination_fd, child_relative, issues
                )
                if output_fd is None:
                    continue
                _copy_overlay_directory(
                    child_fd, output_fd, child_relative, entries, issues
                )
                _destination_directory_replaced(
                    child.name,
                    destination_fd,
                    output_fd,
                    child_relative,
                    issues,
                )
            finally:
                if output_fd is not None:
                    os.close(output_fd)
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            entries.append(OverlayEntry(child_relative, "special"))
            issues.append(
                OverlayIssue(child_relative, "special filesystem entry is not allowed")
            )
            continue

        output_fd = -1
        try:
            file_fd = os.open(
                child.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                target = _readlink_at(child.name, source_fd)
                entries.append(OverlayEntry(child_relative, "symlink", link_target=target))
                issues.append(
                    OverlayIssue(child_relative, f"symlink is not allowed -> {target}")
                )
            else:
                issues.append(OverlayIssue(child_relative, f"cannot open file: {error}"))
            continue
        try:
            opened_metadata = os.fstat(file_fd)
            if not stat.S_ISREG(opened_metadata.st_mode):
                entries.append(OverlayEntry(child_relative, "special"))
                issues.append(
                    OverlayIssue(child_relative, "special filesystem entry is not allowed")
                )
                continue
            entries.append(OverlayEntry(child_relative, "file", opened_metadata.st_size))
            output_fd = _open_destination_file(
                child.name, destination_fd, child_relative, issues
            )
            if output_fd is None:
                continue
            os.fchmod(output_fd, stat.S_IMODE(opened_metadata.st_mode))
            with os.fdopen(os.dup(file_fd), "rb") as source, os.fdopen(
                output_fd, "wb"
            ) as target:
                output_fd = -1
                shutil.copyfileobj(source, target)
                _destination_file_replaced(
                    child.name,
                    destination_fd,
                    target.fileno(),
                    child_relative,
                    issues,
                )
        finally:
            if output_fd >= 0:
                os.close(output_fd)
            os.close(file_fd)


def copy_content_overlays(
    content_root: Path,
    targets: tuple[str, ...],
    destination: Path,
) -> OverlayScan:
    entries: list[OverlayEntry] = []
    issues: list[OverlayIssue] = []
    destination_parent_fd = -1
    destination_fd = -1
    try:
        content_fd = os.open(content_root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return OverlayScan((), ())
    except OSError as error:
        return OverlayScan((), (OverlayIssue(Path("."), f"cannot open content root: {error}"),))
    try:
        try:
            destination_parent_fd = os.open(destination.parent, _DIRECTORY_FLAGS)
            destination_fd = _open_directory(destination.name, destination_parent_fd)
        except OSError as error:
            issues.append(
                OverlayIssue(Path("."), f"cannot open destination directory: {error}")
            )
            return OverlayScan(tuple(entries), tuple(issues))
        destination_metadata = os.fstat(destination_fd)
        for target in targets:
            if target not in OVERLAY_TARGETS:
                raise OverlayPolicyError(
                    "Overlay target must be common, client, or server"
                )
            try:
                target_fd = _open_directory(target, content_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                relative = Path(target)
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    link_target = _readlink_at(target, content_fd)
                    entries.append(
                        OverlayEntry(relative, "symlink", link_target=link_target)
                    )
                    issues.append(
                        OverlayIssue(relative, f"symlink is not allowed -> {link_target}")
                    )
                else:
                    issues.append(
                        OverlayIssue(relative, f"cannot open overlay target: {error}")
                    )
                continue
            try:
                _copy_overlay_directory(
                    target_fd, destination_fd, Path(target), entries, issues
                )
            finally:
                os.close(target_fd)
        try:
            current_destination = os.stat(
                destination.name,
                dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            issues.append(
                OverlayIssue(Path("."), f"destination directory was replaced: {error}")
            )
        else:
            if (
                not stat.S_ISDIR(current_destination.st_mode)
                or (current_destination.st_dev, current_destination.st_ino)
                != (destination_metadata.st_dev, destination_metadata.st_ino)
            ):
                issues.append(
                    _destination_entry_issue(
                        destination.name, destination_parent_fd, Path(".")
                    )
                )
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if destination_parent_fd >= 0:
            os.close(destination_parent_fd)
        os.close(content_fd)
    return OverlayScan(tuple(entries), tuple(issues))


def safe_overlay_child(
    content_root: Path,
    target: str,
    relative_path: str | Path,
) -> Path:
    if target not in OVERLAY_TARGETS:
        raise OverlayPolicyError("Overlay target must be common, client, or server")
    relative = normalize_overlay_relative_path(relative_path)
    candidate = content_root / target / relative

    current = content_root
    checked_relative = Path(".")
    for part in (target, *relative.parts):
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError as error:
            raise OverlayPolicyError(
                f"Cannot inspect overlay path {checked_relative}: {error}"
            ) from error
        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode):
                raise OverlayPolicyError(
                    f"Symlink is not allowed in content overlay: {checked_relative} "
                    f"-> {_link_target(current)}"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise OverlayPolicyError(
                    f"Overlay path component is not a directory: {checked_relative}"
                )
        current /= part
        checked_relative /= part

    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise OverlayPolicyError(f"Cannot inspect overlay path {target}/{relative}: {error}") from error
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise OverlayPolicyError(
                f"Symlink is not allowed in content overlay: {target}/{relative} "
                f"-> {_link_target(candidate)}"
            )
    return candidate


def _copy_content_directory_strict(
    source_fd: int,
    destination_fd: int,
    relative: Path,
    checkpoint: Callable[[], None] | None,
) -> None:
    if checkpoint is not None:
        checkpoint()
    with os.scandir(source_fd) as iterator:
        children = sorted(iterator, key=lambda item: item.name)
    for child in children:
        if checkpoint is not None:
            checkpoint()
        child_relative = relative / child.name
        metadata = child.stat(follow_symlinks=False)
        if child.name == ".gitkeep":
            continue
        if is_packwiz_owned_name(child.name):
            raise OverlayPolicyError(
                f"Packwiz-owned path is not allowed in content overlays: {child_relative}"
            )
        if stat.S_ISLNK(metadata.st_mode):
            raise OverlayPolicyError(
                f"Symlink is not allowed in content overlay: {child_relative} "
                f"-> {_readlink_at(child.name, source_fd)}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(child.name, source_fd)
            output_fd = -1
            try:
                opened_directory = os.fstat(child_fd)
                if (opened_directory.st_dev, opened_directory.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise OverlayPolicyError(
                        f"Content source changed while opening: {child_relative}"
                    )
                os.mkdir(
                    child.name,
                    stat.S_IMODE(metadata.st_mode),
                    dir_fd=destination_fd,
                )
                output_fd = _open_directory(child.name, destination_fd)
                _copy_content_directory_strict(
                    child_fd,
                    output_fd,
                    child_relative,
                    checkpoint,
                )
                os.fchmod(output_fd, stat.S_IMODE(metadata.st_mode))
                current = os.stat(
                    child.name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino)
                    != (opened_directory.st_dev, opened_directory.st_ino)
                ):
                    raise OverlayPolicyError(
                        f"Content source changed while copying: {child_relative}"
                    )
                current_output = os.stat(
                    child.name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
                opened_output = os.fstat(output_fd)
                if (current_output.st_dev, current_output.st_ino) != (
                    opened_output.st_dev,
                    opened_output.st_ino,
                ):
                    raise OverlayPolicyError(
                        f"Content destination changed while copying: {child_relative}"
                    )
            finally:
                if output_fd >= 0:
                    os.close(output_fd)
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise OverlayPolicyError(
                f"Special filesystem entry is not allowed: {child_relative}"
            )
        source_file_fd = os.open(
            child.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=source_fd,
        )
        destination_file_fd = -1
        try:
            opened = os.fstat(source_file_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OverlayPolicyError(
                    f"Special filesystem entry is not allowed: {child_relative}"
                )
            if (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise OverlayPolicyError(
                    f"Content source changed while opening: {child_relative}"
                )
            destination_file_fd = os.open(
                child.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                stat.S_IMODE(opened.st_mode),
                dir_fd=destination_fd,
            )
            while True:
                if checkpoint is not None:
                    checkpoint()
                chunk = os.read(source_file_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_file_fd, view)
                    if written == 0:
                        raise OSError(errno.EIO, "short content tree write")
                    view = view[written:]
            os.fchmod(destination_file_fd, stat.S_IMODE(opened.st_mode))
            after = os.fstat(source_file_fd)
            current = os.stat(
                child.name,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
            if (
                (opened.st_dev, opened.st_ino)
                != (after.st_dev, after.st_ino)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
                or opened.st_size != after.st_size
                or opened.st_mtime_ns != after.st_mtime_ns
                or opened.st_ctime_ns != after.st_ctime_ns
            ):
                raise OverlayPolicyError(
                    f"Content source changed while copying: {child_relative}"
                )
            output = os.stat(
                child.name,
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
            opened_output = os.fstat(destination_file_fd)
            if (output.st_dev, output.st_ino) != (
                opened_output.st_dev,
                opened_output.st_ino,
            ):
                raise OverlayPolicyError(
                    f"Content destination changed while copying: {child_relative}"
                )
        finally:
            if destination_file_fd >= 0:
                os.close(destination_file_fd)
            os.close(source_file_fd)


def copy_content_tree(
    source_content_root: Path,
    staging_content_root: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    if staging_content_root.exists():
        raise OverlayPolicyError(
            f"Content staging destination already exists: {staging_content_root}"
        )
    staging_content_root.mkdir(mode=0o700)
    destination_parent_fd = os.open(
        staging_content_root.parent,
        _DIRECTORY_FLAGS,
    )
    destination_fd = _open_directory(
        staging_content_root.name,
        destination_parent_fd,
    )
    source_parent_fd = source_fd = -1
    try:
        destination_identity = os.fstat(destination_fd)
        try:
            source_parent_fd = os.open(source_content_root.parent, _DIRECTORY_FLAGS)
            source_fd = _open_directory(source_content_root.name, source_parent_fd)
        except FileNotFoundError:
            for target in OVERLAY_TARGETS:
                os.mkdir(target, 0o755, dir_fd=destination_fd)
            return
        except OSError as error:
            raise OverlayPolicyError(
                f"Cannot open content root: {source_content_root}: {error}"
            ) from error
        source_identity = os.fstat(source_fd)
        for target in OVERLAY_TARGETS:
            if checkpoint is not None:
                checkpoint()
            try:
                target_fd = _open_directory(target, source_fd)
            except FileNotFoundError:
                os.mkdir(target, 0o755, dir_fd=destination_fd)
                continue
            except OSError as error:
                raise OverlayPolicyError(
                    f"Cannot open content overlay target {target}: {error}"
                ) from error
            output_fd = -1
            try:
                target_metadata = os.fstat(target_fd)
                os.mkdir(
                    target,
                    stat.S_IMODE(target_metadata.st_mode),
                    dir_fd=destination_fd,
                )
                output_fd = _open_directory(target, destination_fd)
                _copy_content_directory_strict(
                    target_fd,
                    output_fd,
                    Path(target),
                    checkpoint,
                )
                os.fchmod(output_fd, stat.S_IMODE(target_metadata.st_mode))
                current_target = os.stat(
                    target,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                if (current_target.st_dev, current_target.st_ino) != (
                    target_metadata.st_dev,
                    target_metadata.st_ino,
                ):
                    raise OverlayPolicyError(
                        f"Content overlay target changed while copying: {target}"
                    )
                current_output = os.stat(
                    target,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
                output_metadata = os.fstat(output_fd)
                if (current_output.st_dev, current_output.st_ino) != (
                    output_metadata.st_dev,
                    output_metadata.st_ino,
                ):
                    raise OverlayPolicyError(
                        f"Content staging target changed while copying: {target}"
                    )
            finally:
                if output_fd >= 0:
                    os.close(output_fd)
                os.close(target_fd)
        current_source = os.stat(source_content_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_source.st_mode)
            or (current_source.st_dev, current_source.st_ino)
            != (source_identity.st_dev, source_identity.st_ino)
        ):
            raise OverlayPolicyError("Content root changed while copying")
        current_destination = os.stat(
            staging_content_root.name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        if (current_destination.st_dev, current_destination.st_ino) != (
            destination_identity.st_dev,
            destination_identity.st_ino,
        ):
            raise OverlayPolicyError("Content staging root changed while copying")
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        os.close(destination_fd)
        os.close(destination_parent_fd)


def _inspect_local_import_file(
    file_fd: int,
    relative_path: Path,
    listed: os.stat_result,
    checkpoint: Callable[[], None] | None,
) -> LocalImportEntry:
    opened = os.fstat(file_fd)
    errors: list[str] = []
    if not stat.S_ISREG(opened.st_mode):
        return LocalImportEntry(
            relative_path, "invalid", 0, stat.S_IMODE(opened.st_mode),
            opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns,
            None, ("special filesystem entry is not allowed",),
        )
    if (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino):
        errors.append("source entry changed while opening")
    if opened.st_nlink != 1:
        errors.append("hard-linked source files are not allowed")
    digest = hashlib.sha256()
    while True:
        if checkpoint is not None:
            checkpoint()
        chunk = os.read(file_fd, _STREAM_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(file_fd)
    if (
        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
        or opened.st_ctime_ns != after.st_ctime_ns
    ):
        errors.append("source file changed while inspecting")
    return LocalImportEntry(
        relative_path,
        "file" if not errors else "invalid",
        opened.st_size,
        stat.S_IMODE(opened.st_mode),
        opened.st_dev,
        opened.st_ino,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
        digest.hexdigest(),
        tuple(errors),
    )


def _inspect_local_import_directory(
    directory_fd: int,
    relative_path: Path,
    entries: list[LocalImportEntry],
    checkpoint: Callable[[], None] | None,
) -> None:
    if checkpoint is not None:
        checkpoint()
    opened = os.fstat(directory_fd)
    entries.append(
        LocalImportEntry(
            relative_path, "directory", 0, stat.S_IMODE(opened.st_mode),
            opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns,
            None,
        )
    )
    try:
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda child: child.name)
    except OSError as error:
        entries[-1] = LocalImportEntry(
            relative_path, "invalid", 0, stat.S_IMODE(opened.st_mode),
            opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns,
            None, (f"cannot list source directory: {error}",),
        )
        return
    for child in children:
        if checkpoint is not None:
            checkpoint()
        relative = Path(child.name) if relative_path == Path(".") else relative_path / child.name
        try:
            metadata = child.stat(follow_symlinks=False)
        except OSError as error:
            entries.append(
                LocalImportEntry(relative, "invalid", 0, 0, 0, 0, 0, 0, None,
                                 (f"cannot inspect source entry: {error}",))
            )
            continue
        if stat.S_ISLNK(metadata.st_mode):
            entries.append(
                LocalImportEntry(
                    relative, "invalid", 0, stat.S_IMODE(metadata.st_mode),
                    metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns,
                    metadata.st_ctime_ns, None, ("symlink is not allowed",),
                )
            )
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = _open_directory(child.name, directory_fd)
            except OSError as error:
                entries.append(
                    LocalImportEntry(
                        relative, "invalid", 0, stat.S_IMODE(metadata.st_mode),
                        metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns,
                        metadata.st_ctime_ns, None,
                        (f"cannot open source directory: {error}",),
                    )
                )
                continue
            try:
                current = os.fstat(child_fd)
                if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                    entries.append(
                        LocalImportEntry(
                            relative, "invalid", 0, stat.S_IMODE(current.st_mode),
                            current.st_dev, current.st_ino, current.st_mtime_ns,
                            current.st_ctime_ns, None,
                            ("source directory changed while opening",),
                        )
                    )
                else:
                    entry_index = len(entries)
                    _inspect_local_import_directory(child_fd, relative, entries, checkpoint)
                    try:
                        bound = os.stat(
                            child.name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError:
                        bound = None
                    if bound is None or (bound.st_dev, bound.st_ino) != (
                        current.st_dev,
                        current.st_ino,
                    ):
                        entries[entry_index] = _invalidate_local_import_entry(
                            entries[entry_index],
                            "source directory changed while inspecting",
                        )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            try:
                file_fd = os.open(
                    child.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                entries.append(
                    LocalImportEntry(
                        relative, "invalid", metadata.st_size,
                        stat.S_IMODE(metadata.st_mode), metadata.st_dev,
                        metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns,
                        None, (f"cannot open source file: {error}",),
                    )
                )
                continue
            try:
                inspected = _inspect_local_import_file(
                    file_fd, relative, metadata, checkpoint
                )
                try:
                    bound = os.stat(
                        child.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    bound = None
                if bound is None or (bound.st_dev, bound.st_ino) != (
                    inspected.device,
                    inspected.inode,
                ):
                    inspected = _invalidate_local_import_entry(
                        inspected, "source file changed while inspecting"
                    )
                entries.append(inspected)
            finally:
                os.close(file_fd)
        else:
            entries.append(
                LocalImportEntry(
                    relative, "invalid", 0, stat.S_IMODE(metadata.st_mode),
                    metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns,
                    metadata.st_ctime_ns, None,
                    ("special filesystem entry is not allowed",),
                )
            )
    current = os.fstat(directory_fd)
    if (
        (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_mtime_ns != current.st_mtime_ns
        or opened.st_ctime_ns != current.st_ctime_ns
    ):
        index = next(
            index for index, entry in enumerate(entries)
            if entry.relative_path == relative_path
        )
        entry = entries[index]
        entries[index] = LocalImportEntry(
            entry.relative_path, "invalid", entry.size, entry.mode, entry.device,
            entry.inode, entry.mtime_ns, entry.ctime_ns, entry.digest,
            entry.errors + ("source directory changed while inspecting",),
        )


def scan_import_source(
    source: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> LocalImportScan:
    if checkpoint is not None:
        checkpoint()
    parent_fd = os.open(source.parent, _DIRECTORY_FLAGS)
    source_fd = -1
    try:
        listed = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(listed.st_mode):
            entry = LocalImportEntry(
                Path("."), "invalid", 0, stat.S_IMODE(listed.st_mode),
                listed.st_dev, listed.st_ino, listed.st_mtime_ns, listed.st_ctime_ns,
                None, ("symlink is not allowed",),
            )
            return LocalImportScan(source, "invalid", (entry,))
        if stat.S_ISDIR(listed.st_mode):
            source_fd = _open_directory(source.name, parent_fd)
            entries: list[LocalImportEntry] = []
            _inspect_local_import_directory(source_fd, Path("."), entries, checkpoint)
            try:
                bound = os.stat(
                    source.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                bound = None
            opened = os.fstat(source_fd)
            if bound is None or (bound.st_dev, bound.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                entries[0] = _invalidate_local_import_entry(
                    entries[0], "source root changed while inspecting"
                )
            kind: Literal["file", "directory", "invalid"] = (
                "invalid" if entries[0].kind == "invalid" else "directory"
            )
            return LocalImportScan(source, kind, tuple(entries))
        if not stat.S_ISREG(listed.st_mode):
            entry = LocalImportEntry(
                Path("."), "invalid", 0, stat.S_IMODE(listed.st_mode),
                listed.st_dev, listed.st_ino, listed.st_mtime_ns, listed.st_ctime_ns,
                None, ("special filesystem entry is not allowed",),
            )
            return LocalImportScan(source, "invalid", (entry,))
        source_fd = os.open(
            source.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        entry = _inspect_local_import_file(source_fd, Path("."), listed, checkpoint)
        try:
            bound = os.stat(
                source.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            bound = None
        if bound is None or (bound.st_dev, bound.st_ino) != (
            entry.device,
            entry.inode,
        ):
            entry = _invalidate_local_import_entry(
                entry, "source root changed while inspecting"
            )
        return LocalImportScan(source, "file" if entry.kind == "file" else "invalid", (entry,))
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        os.close(parent_fd)


def _open_import_relative_parent(root_fd: int, relative: Path) -> tuple[int, list[int]]:
    current = root_fd
    opened: list[int] = []
    for part in relative.parts[:-1]:
        child = _open_directory(part, current)
        opened.append(child)
        current = child
    return current, opened


def copy_import_source(
    expected: LocalImportScan,
    destination: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    if checkpoint is not None:
        checkpoint()
    if any(entry.kind == "invalid" or entry.errors for entry in expected.entries):
        raise OverlayPolicyError("Unsafe local import source cannot be copied")
    current = scan_import_source(expected.root, checkpoint=checkpoint)
    if current != expected:
        raise OverlayPolicyError("Local import source changed after inspection")
    if destination.exists():
        raise OverlayPolicyError(f"Import staging destination already exists: {destination}")

    if expected.kind == "directory":
        destination.mkdir(mode=0o700)
        directory_modes = [(destination, expected.entries[0].mode)]
        source_root_fd = os.open(expected.root, _DIRECTORY_FLAGS)
        try:
            for entry in expected.entries[1:]:
                if checkpoint is not None:
                    checkpoint()
                target = destination / entry.relative_path
                if entry.kind == "directory":
                    target.mkdir(mode=0o700)
                    directory_modes.append((target, entry.mode))
                    continue
                parent_fd, opened = _open_import_relative_parent(
                    source_root_fd, entry.relative_path
                )
                try:
                    source_fd = os.open(
                        entry.relative_path.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                        dir_fd=parent_fd,
                    )
                    try:
                        _copy_verified_import_file(source_fd, target, entry, checkpoint)
                    finally:
                        os.close(source_fd)
                finally:
                    for descriptor in reversed(opened):
                        os.close(descriptor)
            for directory, mode in reversed(directory_modes):
                directory.chmod(mode)
        finally:
            os.close(source_root_fd)
    else:
        source_fd = os.open(
            expected.root,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
        try:
            _copy_verified_import_file(source_fd, destination, expected.entries[0], checkpoint)
        finally:
            os.close(source_fd)
    after = scan_import_source(expected.root, checkpoint=checkpoint)
    if after != expected:
        raise OverlayPolicyError("Local import source changed while copying")


def _copy_verified_import_file(
    source_fd: int,
    destination: Path,
    expected: LocalImportEntry,
    checkpoint: Callable[[], None] | None,
) -> None:
    opened = os.fstat(source_fd)
    identity = (opened.st_dev, opened.st_ino)
    if (
        identity != (expected.device, expected.inode)
        or opened.st_size != expected.size
        or opened.st_mtime_ns != expected.mtime_ns
        or opened.st_ctime_ns != expected.ctime_ns
        or opened.st_nlink != 1
    ):
        raise OverlayPolicyError(f"Local import source changed: {expected.relative_path}")
    digest = hashlib.sha256()
    output_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        expected.mode,
    )
    try:
        while True:
            if checkpoint is not None:
                checkpoint()
            chunk = os.read(source_fd, _STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                if checkpoint is not None:
                    checkpoint()
                written = os.write(output_fd, view)
                if written == 0:
                    raise OSError(errno.EIO, "short local import write")
                view = view[written:]
        os.fchmod(output_fd, expected.mode)
        after = os.fstat(source_fd)
        if (
            identity != (after.st_dev, after.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
            or digest.hexdigest() != expected.digest
        ):
            raise OverlayPolicyError(
                f"Local import source changed while copying: {expected.relative_path}"
            )
    finally:
        os.close(output_fd)
