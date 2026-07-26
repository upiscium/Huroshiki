from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat


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
