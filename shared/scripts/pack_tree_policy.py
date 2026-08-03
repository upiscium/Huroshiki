from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable, Literal
from uuid import uuid4

import packctl
from portable_paths import PortablePathError, portable_relative_path_key


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_STREAM_CHUNK_SIZE = 1024 * 1024


class PackTreePolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackMigrationTreeEntry:
    relative_path: Path
    kind: Literal["file", "directory", "invalid"]
    size: int
    mode: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    digest: str | None
    portable_key: str | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackTreeScan:
    root: Path
    root_identity: tuple[int, int]
    entries: tuple[PackMigrationTreeEntry, ...]
    snapshot_digest: str
    content_digest: str


@dataclass(frozen=True)
class PackTreeCopyResult:
    scan: PackTreeScan
    copied_files: int
    copied_directories: int
    copied_bytes: int


def _entry_payload(
    entries: tuple[PackMigrationTreeEntry, ...],
    *,
    include_identity: bool,
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for entry in entries:
        value: dict[str, object] = {
            "path": entry.relative_path.as_posix(),
            "kind": entry.kind,
            "size": entry.size,
            "mode": entry.mode,
            "digest": entry.digest,
            "portable": entry.portable_key,
            "errors": list(entry.errors),
        }
        if include_identity:
            value.update(
                {
                    "device": entry.device,
                    "inode": entry.inode,
                    "mtime_ns": entry.mtime_ns,
                    "ctime_ns": entry.ctime_ns,
                }
            )
        payload.append(value)
    return payload


def _entries_digest(
    entries: tuple[PackMigrationTreeEntry, ...],
    *,
    include_identity: bool,
) -> str:
    return hashlib.sha256(
        json.dumps(
            _entry_payload(entries, include_identity=include_identity),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _invalid_entry(
    relative: Path,
    metadata: os.stat_result,
    message: str,
) -> PackMigrationTreeEntry:
    return PackMigrationTreeEntry(
        relative,
        "invalid",
        metadata.st_size,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        None,
        None,
        (message,),
    )


def _inspect_file(
    parent_fd: int,
    name: str,
    relative: Path,
    listed: os.stat_result,
    checkpoint: Callable[[], None],
) -> PackMigrationTreeEntry:
    try:
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        return _invalid_entry(relative, listed, f"cannot open file safely: {error}")
    try:
        opened = os.fstat(file_fd)
        errors: list[str] = []
        if not stat.S_ISREG(opened.st_mode):
            errors.append("special filesystem entry is not allowed")
        if (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino):
            errors.append("file changed while opening")
        if opened.st_nlink != 1:
            errors.append("hard-linked files are not allowed")
        digest = hashlib.sha256()
        while True:
            checkpoint()
            chunk = os.read(file_fd, _STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(file_fd)
        try:
            bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            bound = None
        if (
            bound is None
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (opened.st_dev, opened.st_ino) != (bound.st_dev, bound.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            errors.append("file changed while hashing")
        return PackMigrationTreeEntry(
            relative,
            "file" if not errors else "invalid",
            opened.st_size,
            stat.S_IMODE(opened.st_mode),
            opened.st_dev,
            opened.st_ino,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            digest.hexdigest(),
            None,
            tuple(errors),
        )
    finally:
        os.close(file_fd)


def _scan_directory(
    directory_fd: int,
    relative: Path,
    entries: list[PackMigrationTreeEntry],
    checkpoint: Callable[[], None],
    excluded_roots: frozenset[str],
) -> None:
    checkpoint()
    opened = os.fstat(directory_fd)
    entry_index = len(entries)
    entries.append(
        PackMigrationTreeEntry(
            relative,
            "directory",
            0,
            stat.S_IMODE(opened.st_mode),
            opened.st_dev,
            opened.st_ino,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            None,
            None,
        )
    )
    try:
        children: list[os.DirEntry[str]] = []
        with os.scandir(directory_fd) as iterator:
            for child in iterator:
                checkpoint()
                children.append(child)
        children.sort(key=lambda child: child.name)
    except OSError as error:
        entries[entry_index] = replace(
            entries[entry_index],
            kind="invalid",
            errors=(f"cannot list directory: {error}",),
        )
        return

    for child in children:
        checkpoint()
        if relative == Path(".") and child.name in excluded_roots:
            continue
        child_relative = (
            Path(child.name) if relative == Path(".") else relative / child.name
        )
        try:
            listed = child.stat(follow_symlinks=False)
        except OSError as error:
            entries.append(
                PackMigrationTreeEntry(
                    child_relative,
                    "invalid",
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    None,
                    None,
                    (f"cannot inspect entry: {error}",),
                )
            )
            continue
        if stat.S_ISLNK(listed.st_mode):
            entries.append(_invalid_entry(child_relative, listed, "symlink is not allowed"))
            continue
        if stat.S_ISDIR(listed.st_mode):
            try:
                child_fd = os.open(child.name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                entries.append(
                    _invalid_entry(
                        child_relative,
                        listed,
                        f"cannot open directory safely: {error}",
                    )
                )
                continue
            try:
                current = os.fstat(child_fd)
                if (current.st_dev, current.st_ino) != (
                    listed.st_dev,
                    listed.st_ino,
                ):
                    entries.append(
                        _invalid_entry(
                            child_relative,
                            current,
                            "directory changed while opening",
                        )
                    )
                    continue
                child_index = len(entries)
                _scan_directory(
                    child_fd,
                    child_relative,
                    entries,
                    checkpoint,
                    excluded_roots,
                )
                try:
                    bound = os.stat(
                        child.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    bound = None
                after = os.fstat(child_fd)
                if (
                    bound is None
                    or (current.st_dev, current.st_ino)
                    != (after.st_dev, after.st_ino)
                    or (current.st_dev, current.st_ino)
                    != (bound.st_dev, bound.st_ino)
                    or current.st_mtime_ns != after.st_mtime_ns
                    or current.st_ctime_ns != after.st_ctime_ns
                ):
                    entry = entries[child_index]
                    entries[child_index] = replace(
                        entry,
                        kind="invalid",
                        errors=entry.errors
                        + ("directory changed while scanning",),
                    )
            finally:
                os.close(child_fd)
            continue
        if stat.S_ISREG(listed.st_mode):
            entries.append(
                _inspect_file(
                    directory_fd,
                    child.name,
                    child_relative,
                    listed,
                    checkpoint,
                )
            )
            continue
        entries.append(
            _invalid_entry(
                child_relative,
                listed,
                "special filesystem entry is not allowed",
            )
        )

    after = os.fstat(directory_fd)
    if (
        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or opened.st_mtime_ns != after.st_mtime_ns
        or opened.st_ctime_ns != after.st_ctime_ns
    ):
        entry = entries[entry_index]
        entries[entry_index] = replace(
            entry,
            kind="invalid",
            errors=entry.errors + ("directory changed while scanning",),
        )


def _apply_portable_validation(
    entries: tuple[PackMigrationTreeEntry, ...],
) -> tuple[PackMigrationTreeEntry, ...]:
    owners: dict[str, Path] = {}
    errors: dict[Path, list[str]] = {}
    portable: dict[Path, str] = {}
    for entry in entries:
        if entry.relative_path == Path("."):
            continue
        try:
            key = portable_relative_path_key(
                entry.relative_path,
                context="Pack migration path",
            )
        except PortablePathError as error:
            errors.setdefault(entry.relative_path, []).append(str(error))
            continue
        portable[entry.relative_path] = key
        previous = owners.get(key)
        if previous is not None and previous != entry.relative_path:
            message = (
                f"portable path collision: {previous} and {entry.relative_path}"
            )
            errors.setdefault(previous, []).append(message)
            errors.setdefault(entry.relative_path, []).append(message)
        else:
            owners[key] = entry.relative_path
    return tuple(
        replace(
            entry,
            kind="invalid" if errors.get(entry.relative_path) else entry.kind,
            portable_key=portable.get(entry.relative_path),
            errors=entry.errors + tuple(errors.get(entry.relative_path, ())),
        )
        for entry in entries
    )


def scan_pack_migration_source(
    pack_root: Path,
    *,
    checkpoint: Callable[[], None],
    excluded_roots: frozenset[str] = frozenset(),
) -> PackTreeScan:
    checkpoint()
    descriptor_bound_path = (
        pack_root.parent.parent == Path("/proc/self/fd")
        and pack_root.parent.name.isdecimal()
    )
    parent_fd = -1
    root_fd = -1
    try:
        if descriptor_bound_path:
            listed = os.stat(pack_root, follow_symlinks=False)
            root_fd = os.open(pack_root, _DIRECTORY_FLAGS)
        else:
            parent_fd = os.open(pack_root.parent, _DIRECTORY_FLAGS)
            listed = os.stat(pack_root.name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(pack_root.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if not stat.S_ISDIR(listed.st_mode) or stat.S_ISLNK(listed.st_mode):
            raise PackTreePolicyError("Pack migration source must be an ordinary directory")
        opened = os.fstat(root_fd)
        if (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino):
            raise PackTreePolicyError("Pack migration source changed while opening")
        entries: list[PackMigrationTreeEntry] = []
        _scan_directory(root_fd, Path("."), entries, checkpoint, excluded_roots)
        checkpoint()
        try:
            bound = (
                os.stat(pack_root, follow_symlinks=False)
                if descriptor_bound_path
                else os.stat(
                    pack_root.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            )
        except OSError as error:
            raise PackTreePolicyError(
                f"Pack migration source changed while scanning: {error}"
            ) from error
        if (opened.st_dev, opened.st_ino) != (bound.st_dev, bound.st_ino):
            raise PackTreePolicyError("Pack migration source changed while scanning")
        immutable = _apply_portable_validation(tuple(entries))
        return PackTreeScan(
            pack_root,
            (opened.st_dev, opened.st_ino),
            immutable,
            _entries_digest(immutable, include_identity=True),
            _entries_digest(immutable, include_identity=False),
        )
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _selected_entries(
    scan: PackTreeScan,
    include: tuple[Path, ...],
) -> tuple[PackMigrationTreeEntry, ...]:
    normalized: list[Path] = []
    for path in include:
        if path.is_absolute() or path == Path(".") or ".." in path.parts:
            raise PackTreePolicyError(f"Invalid Pack migration include path: {path}")
        normalized.append(path)
    selected = [scan.entries[0]]
    for entry in scan.entries[1:]:
        if any(
            entry.relative_path == root or root in entry.relative_path.parents
            for root in normalized
        ):
            selected.append(entry)
    return tuple(selected)


def _open_relative_parent(
    root_fd: int,
    relative: Path,
    expected_identities: dict[Path, tuple[int, int]] | None = None,
) -> tuple[int, list[int]]:
    current = root_fd
    opened: list[int] = []
    current_relative = Path(".")
    for part in relative.parts[:-1]:
        descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
        current_relative = (
            Path(part)
            if current_relative == Path(".")
            else current_relative / part
        )
        if expected_identities is not None:
            expected = expected_identities.get(current_relative)
            opened_metadata = os.fstat(descriptor)
            if expected is None or (opened_metadata.st_dev, opened_metadata.st_ino) != expected:
                os.close(descriptor)
                for item in reversed(opened):
                    os.close(item)
                raise PackTreePolicyError(
                    f"Pack migration directory changed: {current_relative}"
                )
        opened.append(descriptor)
        current = descriptor
    return current, opened


def _copy_file_at(
    source_root_fd: int,
    destination_root_fd: int,
    entry: PackMigrationTreeEntry,
    checkpoint: Callable[[], None],
    source_identities: dict[Path, tuple[int, int]],
    destination_identities: dict[Path, tuple[int, int]],
) -> tuple[int, int]:
    source_parent, source_opened = _open_relative_parent(
        source_root_fd, entry.relative_path, source_identities
    )
    destination_parent, destination_opened = _open_relative_parent(
        destination_root_fd, entry.relative_path, destination_identities
    )
    source_fd = temporary_fd = -1
    temporary_name = f".{entry.relative_path.name}.huroshiki-{uuid4().hex}.tmp"
    try:
        source_fd = os.open(
            entry.relative_path.name,
            _FILE_FLAGS,
            dir_fd=source_parent,
        )
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (entry.device, entry.inode)
            or opened.st_size != entry.size
            or opened.st_mtime_ns != entry.mtime_ns
            or opened.st_ctime_ns != entry.ctime_ns
            or opened.st_nlink != 1
        ):
            raise PackTreePolicyError(
                f"Pack migration source changed: {entry.relative_path}"
            )
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            entry.mode,
            dir_fd=destination_parent,
        )
        digest = hashlib.sha256()
        while True:
            checkpoint()
            chunk = os.read(source_fd, _STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                checkpoint()
                written = os.write(temporary_fd, view)
                if written == 0:
                    raise OSError(errno.EIO, "short Pack migration write")
                view = view[written:]
        os.fchmod(temporary_fd, entry.mode)
        os.fsync(temporary_fd)
        after = os.fstat(source_fd)
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
            or digest.hexdigest() != entry.digest
        ):
            raise PackTreePolicyError(
                f"Pack migration source changed while copying: {entry.relative_path}"
            )
        os.close(temporary_fd)
        temporary_fd = -1
        packctl.renameat2(
            destination_parent,
            temporary_name,
            destination_parent,
            entry.relative_path.name,
            packctl.RENAME_NOREPLACE,
        )
        installed = os.stat(
            entry.relative_path.name,
            dir_fd=destination_parent,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(installed.st_mode):
            raise PackTreePolicyError(
                f"Pack migration destination file changed: {entry.relative_path}"
            )
        return installed.st_dev, installed.st_ino
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=destination_parent)
        except FileNotFoundError:
            pass
        if source_fd >= 0:
            os.close(source_fd)
        for descriptor in reversed(destination_opened):
            os.close(descriptor)
        for descriptor in reversed(source_opened):
            os.close(descriptor)


def copy_pack_tree_snapshot(
    expected: PackTreeScan,
    destination_root: Path,
    *,
    include: tuple[Path, ...],
    checkpoint: Callable[[], None],
    destination_parent_identity: tuple[int, int] | None = None,
) -> PackTreeCopyResult:
    checkpoint()
    if any(entry.kind == "invalid" or entry.errors for entry in expected.entries):
        raise PackTreePolicyError("Unsafe Pack migration source cannot be copied")
    current = scan_pack_migration_source(expected.root, checkpoint=checkpoint)
    if current != expected:
        raise PackTreePolicyError("Pack migration source changed after snapshot")
    selected = _selected_entries(expected, include)
    source_identities = {
        entry.relative_path: (entry.device, entry.inode)
        for entry in selected
        if entry.kind == "directory"
    }
    source_fd = os.open(expected.root, _DIRECTORY_FLAGS)
    destination_parent_fd = os.open(destination_root.parent, _DIRECTORY_FLAGS)
    destination_fd = -1
    try:
        opened_source = os.fstat(source_fd)
        if (opened_source.st_dev, opened_source.st_ino) != expected.root_identity:
            raise PackTreePolicyError("Pack migration source root was replaced")
        opened_parent = os.fstat(destination_parent_fd)
        if destination_parent_identity is not None and (
            opened_parent.st_dev,
            opened_parent.st_ino,
        ) != destination_parent_identity:
            raise PackTreePolicyError(
                "Pack migration destination parent was replaced"
            )
        try:
            os.mkdir(destination_root.name, 0o700, dir_fd=destination_parent_fd)
        except FileExistsError as error:
            raise PackTreePolicyError(
                f"Pack migration destination already exists: {destination_root.name}"
            ) from error
        destination_fd = os.open(
            destination_root.name,
            _DIRECTORY_FLAGS,
            dir_fd=destination_parent_fd,
        )
        destination_identity = os.fstat(destination_fd)
        destination_identities = {
            Path("."): (destination_identity.st_dev, destination_identity.st_ino)
        }
        directories = [entry for entry in selected[1:] if entry.kind == "directory"]
        files = [entry for entry in selected[1:] if entry.kind == "file"]
        for entry in directories:
            checkpoint()
            parent_fd, opened = _open_relative_parent(
                destination_fd,
                entry.relative_path,
                destination_identities,
            )
            child_fd = -1
            try:
                os.mkdir(entry.relative_path.name, 0o700, dir_fd=parent_fd)
                child_fd = os.open(
                    entry.relative_path.name,
                    _DIRECTORY_FLAGS,
                    dir_fd=parent_fd,
                )
                child = os.fstat(child_fd)
                destination_identities[entry.relative_path] = (
                    child.st_dev,
                    child.st_ino,
                )
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
                for descriptor in reversed(opened):
                    os.close(descriptor)
        for entry in files:
            checkpoint()
            destination_identities[entry.relative_path] = _copy_file_at(
                source_fd,
                destination_fd,
                entry,
                checkpoint,
                source_identities,
                destination_identities,
            )
        for entry in reversed(directories):
            parent_fd, opened = _open_relative_parent(
                destination_fd,
                entry.relative_path,
                destination_identities,
            )
            child_fd = -1
            try:
                child_fd = os.open(
                    entry.relative_path.name,
                    _DIRECTORY_FLAGS,
                    dir_fd=parent_fd,
                )
                os.fchmod(child_fd, entry.mode)
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
                for descriptor in reversed(opened):
                    os.close(descriptor)
        os.fchmod(destination_fd, selected[0].mode)
        checkpoint()
        bound_destination = os.stat(
            destination_root.name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        if (destination_identity.st_dev, destination_identity.st_ino) != (
            bound_destination.st_dev,
            bound_destination.st_ino,
        ):
            raise PackTreePolicyError(
                "Pack migration destination was replaced while copying"
            )
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(destination_parent_fd)
        os.close(source_fd)
    after = scan_pack_migration_source(expected.root, checkpoint=checkpoint)
    if after != expected:
        raise PackTreePolicyError("Pack migration source changed while copying")
    destination_scan = scan_pack_migration_source(
        destination_root,
        checkpoint=checkpoint,
    )
    expected_content = _entries_digest(selected, include_identity=False)
    if destination_scan.content_digest != expected_content:
        raise PackTreePolicyError("Pack migration destination verification failed")
    for entry in destination_scan.entries:
        if destination_identities.get(entry.relative_path) != (
            entry.device,
            entry.inode,
        ):
            raise PackTreePolicyError(
                f"Pack migration destination entry was replaced: {entry.relative_path}"
            )
    return PackTreeCopyResult(
        destination_scan,
        len(files),
        len(directories),
        sum(entry.size for entry in files),
    )
