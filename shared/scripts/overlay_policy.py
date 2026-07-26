from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import shutil
import stat
from typing import Iterator
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


@dataclass(frozen=True)
class OverlayIssue:
    relative_path: Path
    message: str


@dataclass(frozen=True)
class OverlayScan:
    entries: tuple[OverlayEntry, ...]
    issues: tuple[OverlayIssue, ...]


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass
class _OverlayParent:
    fd: int
    ancestors: list[tuple[int, str]]


def _open_directory(name: str, parent_fd: int) -> int:
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


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


def read_overlay_text(content_root: Path, target: str, relative_path: str | Path) -> str:
    relative = normalize_overlay_relative_path(relative_path)
    with _open_overlay_parent(content_root, target, relative, create=False) as parent:
        try:
            fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent.fd,
            )
        except FileNotFoundError as error:
            raise OverlayPolicyError(f"Overlay file does not exist: {target}/{relative}") from error
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OverlayPolicyError(f"Overlay file does not exist: {target}/{relative}")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd >= 0:
                os.close(fd)


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
    if relative.name == ".gitkeep":
        raise OverlayPolicyError(".gitkeep is managed internally")
    if is_packwiz_owned_name(relative.name):
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
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        issues.append(OverlayIssue(relative, f"cannot inspect entry: {error}"))
        return

    if stat.S_ISLNK(metadata.st_mode):
        target = _link_target(path)
        entries.append(OverlayEntry(relative, "symlink", link_target=target))
        issues.append(OverlayIssue(relative, f"symlink is not allowed -> {target}"))
        if is_packwiz_owned_name(path.name):
            issues.append(OverlayIssue(relative, "Packwiz-owned path is not allowed"))
        return

    if not include_entry and not stat.S_ISDIR(metadata.st_mode):
        issues.append(OverlayIssue(relative, "overlay target must be a directory"))
        return

    if include_entry and stat.S_ISREG(metadata.st_mode):
        entries.append(OverlayEntry(relative, "file", metadata.st_size))
    elif include_entry and not stat.S_ISDIR(metadata.st_mode):
        entries.append(OverlayEntry(relative, "special"))
        issues.append(OverlayIssue(relative, "special filesystem entry is not allowed"))

    if include_entry and is_packwiz_owned_name(path.name):
        issues.append(OverlayIssue(relative, "Packwiz-owned path is not allowed"))

    if not stat.S_ISDIR(metadata.st_mode):
        return
    try:
        children = sorted(os.scandir(path), key=lambda item: item.name)
    except OSError as error:
        issues.append(OverlayIssue(relative, f"cannot list directory: {error}"))
        return
    for child in children:
        child_relative = relative / child.name
        _scan_path(Path(child.path), child_relative, entries, issues)


def scan_content_overlays(content_root: Path) -> OverlayScan:
    entries: list[OverlayEntry] = []
    issues: list[OverlayIssue] = []
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
        _scan_path(
            content_root / target,
            Path(target),
            entries,
            issues,
            include_entry=False,
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
