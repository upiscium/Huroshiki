from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import re


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
    snapshot: Path


@dataclass(frozen=True)
class RsyncTargetParts:
    host: str
    path: str


_RSYNC_CONNECT_TIMEOUT_SECONDS = 10


def rsync_rsh_command() -> str:
    return (
        "ssh -o BatchMode=yes -o ConnectTimeout="
        f"{_RSYNC_CONNECT_TIMEOUT_SECONDS}"
    )


_RSYNC_TARGET_RE = re.compile(
    r"(?:(?P<user>[A-Za-z0-9][A-Za-z0-9._-]*)@)?"
    r"(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])"
    r":(?P<path>/[A-Za-z0-9._/@+,:=-]*(?:/[A-Za-z0-9._/@+,:=-]*)*)"
)


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
    validate_rsync_target(target)
    command = ["rsync", "-av", "--delete", "-e", rsync_rsh_command()]
    if dry_run:
        command.extend(("--dry-run", "--itemize-changes"))
    command.extend(("--", f"{dist}/", target.rstrip("/") + "/"))
    return command


def validate_rsync_target(target: str) -> str:
    if not isinstance(target, str) or target != target.strip() or not target:
        raise ValueError("rsync_target must be a non-empty remote target")
    if any(ord(character) < 32 or ord(character) == 127 for character in target):
        raise ValueError("rsync_target must not contain control characters")
    match = _RSYNC_TARGET_RE.fullmatch(target)
    if match is None:
        raise ValueError(
            "rsync_target must be an explicit host:/absolute/path remote target"
        )
    host = match.group("host")
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as error:
            raise ValueError("rsync_target contains an invalid IPv6 address") from error
    return target


def split_rsync_target(value: str) -> RsyncTargetParts:
    target = validate_rsync_target(value)
    match = _RSYNC_TARGET_RE.fullmatch(target)
    if match is None:
        raise ValueError("rsync_target could not be parsed")
    user = match.group("user")
    host = match.group("host")
    return RsyncTargetParts(
        f"{user}@{host}" if user is not None else host,
        match.group("path"),
    )


def join_rsync_target(host: str, path: str) -> str:
    return validate_rsync_target(f"{host}:{path}")


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
