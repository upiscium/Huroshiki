from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


@dataclass(frozen=True)
class RsyncChange:
    category: str
    path: str
    raw: str


@dataclass(frozen=True)
class DeployPreview:
    target: str
    dist_digest: str
    changes: tuple[RsyncChange, ...]
    raw_lines: tuple[str, ...]


def distribution_digest(dist: Path) -> str:
    digest = hashlib.sha256()
    paths = (dist, *sorted(dist.rglob("*"), key=lambda item: item.as_posix()))
    for path in paths:
        relative = path.relative_to(dist).as_posix() or "."
        stat = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(
            f"\0{stat.st_mode}\0{stat.st_uid}\0{stat.st_gid}"
            f"\0{stat.st_mtime_ns}\0".encode("ascii")
        )
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def rsync_deploy_command(dist: Path, target: str, *, dry_run: bool) -> list[str]:
    command = ["rsync", "-av", "--delete"]
    if dry_run:
        command.extend(("--dry-run", "--itemize-changes"))
    command.extend((f"{dist}/", target.rstrip("/") + "/"))
    return command


def parse_rsync_changes(output: str) -> tuple[RsyncChange, ...]:
    changes: list[RsyncChange] = []
    for line in output.splitlines():
        if line.startswith("*deleting   "):
            changes.append(RsyncChange("deleted", line[12:], line))
            continue
        if len(line) < 13 or line[11] != " ":
            continue
        itemized = line[:11]
        if itemized[0] not in "<>ch.":
            continue
        category = "added" if itemized[2:] == "+++++++++" else "updated"
        changes.append(RsyncChange(category, line[12:], line))
    return tuple(changes)
