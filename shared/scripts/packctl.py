#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import ipaddress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Iterable, Literal, Sequence
import unicodedata
from urllib.parse import unquote, urlparse
from uuid import uuid4

import tomlkit
import yaml

from deploy_support import (
    DeployPreview,
    RsyncChange,
    RsyncTargetParts,
    distribution_digest,
    join_rsync_target,
    parse_rsync_changes,
    rsync_deploy_command,
    split_rsync_target,
    validate_rsync_target,
)
from packctl_errors import ConfigError
from huroshiki_paths import import_root_argument, resolve_root
from huroshiki_version import VERSION
from overlay_policy import copy_content_overlays, scan_content_overlays
from portable_paths import PortablePathError, portable_basename_key
from process_runner import (
    BoundedProcessResult,
    PACKWIZ_OPERATION_TIMEOUT_SECONDS,
    PACKWIZ_PROCESS_TIMEOUT_SECONDS,
    process_failure_message,
    run_bounded_process,
)
import project_locks
from project_locks import (
    ProjectLockBusy,
    ProjectLockMetadata,
    ProjectLockSet,
    ProjectLockSetError,
    process_start_identity,
)
from url_artifacts import DEFAULT_URL_MAX_JAR_SIZE_BYTES

ROOT = resolve_root(import_root_argument(sys.argv[1:]))
PACKS = ROOT / "packs"
TEMPLATES = ROOT / "templates"
SHARED = ROOT / "shared"
PACKAGE_DATA = Path(
    os.environ.get("HUROSHIKI_DATA_DIR", Path(__file__).resolve().parents[1])
)
STATE_ROOT = ROOT / ".huroshiki"
TRANSACTION_ROOT = STATE_ROOT / "transactions"
LOG_ROOT = STATE_ROOT / "logs"
TRASH_ROOT = STATE_ROOT / "trash"
DEPLOY_SNAPSHOT_ROOT = STATE_ROOT / "deploy-snapshots"
RSYNC_PROCESS_TIMEOUT_SECONDS = 120
RSYNC_PREVIEW_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
RSYNC_OUTPUT_MAX_BYTES = RSYNC_PREVIEW_OUTPUT_MAX_BYTES
RSYNC_DIAGNOSTIC_MAX_CHARS = 4096
VALID_SIDES = {"client", "server", "both"}
SIDE_ALIASES = {
    "b": "both",
    "both": "both",
    "c": "client",
    "client": "client",
    "s": "server",
    "server": "server",
}
TARGET_SIDES = {
    "client": {"client", "both"},
    "server": {"server", "both"},
}
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SSH_USER_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")
SSH_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
COMPOSE_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LOADER_FLAGS = {
    "neoforge": "--neoforge-version",
    "forge": "--forge-version",
    "fabric": "--fabric-version",
    "quilt": "--quilt-version",
}
TEMPLATE_COMMITTED_KEYS = frozenset(
    {
        "id",
        "display_name",
        "enabled",
        "minecraft",
        "loader",
        "reference_loader_version",
        "mods",
    }
)
TEMPLATE_LOCAL_KEYS = frozenset(
    {"url_max_jar_size_bytes", "url_allow_private_networks"}
)
PACK_LOCAL_KEYS = {
    "distribution": frozenset({"rsync_target", "public_pack_url"}),
    "minecraft_server": frozenset({"ssh_host", "stack_dir", "service"}),
    "url_max_jar_size_bytes": None,
    "url_allow_private_networks": None,
}


CONFIG_WRITE_RACE_ERROR = "changed while applying command; retry the operation"
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
_CONFIG_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
)
_CONFIG_FILE_FLAGS = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
)
_CONFIG_TEMP_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)


@dataclass(frozen=True)
class ConfigFileSnapshot:
    name: str
    exists: bool
    mode: int | None
    device: int | None
    inode: int | None
    bytes: bytes | None
    digest: str | None


@dataclass(frozen=True)
class ExchangeState:
    target: ConfigFileSnapshot
    temporary: ConfigFileSnapshot


@dataclass
class ConfigDirectory:
    path: Path
    fd: int
    device: int
    inode: int
    parent_path: Path
    parent_fd: int
    parent_device: int
    parent_inode: int
    name: str

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1

    def __enter__(self) -> "ConfigDirectory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class ProjectConfigSnapshot:
    committed: ConfigFileSnapshot
    local: ConfigFileSnapshot


@dataclass(frozen=True)
class EffectiveUrlPolicy:
    max_size: int
    max_size_source: str
    allow_private: bool
    allow_private_source: str


@dataclass(frozen=True)
class DeploymentSettings:
    rsync_target: str
    ssh_host: str
    stack_dir: str
    service: str


@dataclass(frozen=True)
class DeploymentSettingsSources:
    rsync_target: str
    ssh_host: str
    stack_dir: str
    service: str


@dataclass(frozen=True)
class DeploymentSettingsBaseline:
    settings: DeploymentSettings
    snapshot: ProjectConfigSnapshot


@dataclass(frozen=True)
class PublicPackUrlInfo:
    value: str | None
    source: Literal["local", "committed", "unset"]
    installer_command: str | None


@dataclass(frozen=True)
class PublicPackUrlBaseline:
    info: PublicPackUrlInfo
    committed_value: str | None
    snapshot: ProjectConfigSnapshot


class Unset:
    __slots__ = ()


UNSET = Unset()


def open_config_directory(path: Path) -> ConfigDirectory:
    managed_path = Path(os.path.abspath(path))
    if not managed_path.name:
        raise ConfigError(f"Invalid configuration directory: {path}")
    parent_path = managed_path.parent
    parent_descriptor = -1
    descriptor = -1
    try:
        expected_parent = os.stat(parent_path, follow_symlinks=False)
        parent_descriptor = os.open(parent_path, _CONFIG_DIRECTORY_FLAGS)
        opened_parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(expected_parent.st_mode)
            or not stat.S_ISDIR(opened_parent.st_mode)
            or (expected_parent.st_dev, expected_parent.st_ino)
            != (opened_parent.st_dev, opened_parent.st_ino)
        ):
            raise ConfigError(
                f"{display_path(parent_path)} was replaced while being opened"
            )
        expected = os.stat(
            managed_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(
            managed_path.name,
            _CONFIG_DIRECTORY_FLAGS,
            dir_fd=parent_descriptor,
        )
    except BaseException as error:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if isinstance(error, OSError):
            raise ConfigError(
                f"Could not safely open {display_path(managed_path)}: {error}"
            ) from error
        raise

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(expected.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ConfigError(
                f"{display_path(managed_path)} was replaced while being opened"
            )
        return ConfigDirectory(
            path=managed_path,
            fd=descriptor,
            device=opened.st_dev,
            inode=opened.st_ino,
            parent_path=parent_path,
            parent_fd=parent_descriptor,
            parent_device=opened_parent.st_dev,
            parent_inode=opened_parent.st_ino,
            name=managed_path.name,
        )
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        raise


def check_config_directory_identity(directory: ConfigDirectory) -> None:
    try:
        opened_parent = os.fstat(directory.parent_fd)
        current_parent = os.stat(directory.parent_path, follow_symlinks=False)
        opened = os.fstat(directory.fd)
        current = os.stat(
            directory.name,
            dir_fd=directory.parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ConfigError(
            f"{display_path(directory.path)} changed while applying command; "
            "managed project directory is no longer current"
        ) from error
    if (
        not stat.S_ISDIR(opened_parent.st_mode)
        or not stat.S_ISDIR(current_parent.st_mode)
        or (opened_parent.st_dev, opened_parent.st_ino)
        != (directory.parent_device, directory.parent_inode)
        or (current_parent.st_dev, current_parent.st_ino)
        != (directory.parent_device, directory.parent_inode)
        or not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (directory.device, directory.inode)
        or (current.st_dev, current.st_ino) != (directory.device, directory.inode)
    ):
        raise ConfigError(
            f"{display_path(directory.path)} changed while applying command; "
            "managed project directory is no longer current"
        )


def read_config_snapshot(
    directory: ConfigDirectory,
    name: str,
) -> ConfigFileSnapshot:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ConfigError(f"Invalid configuration filename: {name!r}")
    try:
        descriptor = os.open(
            name,
            _CONFIG_FILE_FLAGS,
            dir_fd=directory.fd,
        )
    except FileNotFoundError:
        return ConfigFileSnapshot(name, False, None, None, None, None, None)
    except OSError as error:
        if error.errno == errno.ELOOP:
            message = "must be a regular file, not a symlink"
        else:
            message = f"could not be opened safely: {error}"
        raise ConfigError(f"{display_path(directory.path / name)} {message}") from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError(
                f"{display_path(directory.path / name)} must be a regular file"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        contents = b"".join(chunks)
        return ConfigFileSnapshot(
            name=name,
            exists=True,
            mode=stat.S_IMODE(metadata.st_mode),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            bytes=contents,
            digest=hashlib.sha256(contents).hexdigest(),
        )
    finally:
        os.close(descriptor)


def _same_config_snapshot(
    current: ConfigFileSnapshot,
    expected: ConfigFileSnapshot,
) -> bool:
    return (
        current.exists == expected.exists
        and current.mode == expected.mode
        and current.device == expected.device
        and current.inode == expected.inode
        and current.bytes == expected.bytes
        and current.digest == expected.digest
    )


def _same_config_identity(
    current: ConfigFileSnapshot,
    expected: ConfigFileSnapshot,
) -> bool:
    return (
        current.exists
        and expected.exists
        and current.device == expected.device
        and current.inode == expected.inode
    )


def classify_exchange_state(
    *,
    target: ConfigFileSnapshot,
    temporary: ConfigFileSnapshot,
    staged: ConfigFileSnapshot,
    expected: ConfigFileSnapshot,
) -> Literal["intact", "target_changed", "temporary_changed", "both_changed"]:
    target_matches = _same_config_snapshot(target, staged)
    temporary_matches = _same_config_snapshot(temporary, expected)
    if target_matches and temporary_matches:
        return "intact"
    if temporary_matches:
        return "target_changed"
    if target_matches:
        return "temporary_changed"
    return "both_changed"


def _check_config_snapshots(
    directory: ConfigDirectory,
    snapshots: tuple[ConfigFileSnapshot, ...],
) -> None:
    for expected in snapshots:
        try:
            current = read_config_snapshot(directory, expected.name)
        except (ConfigError, OSError) as error:
            raise ConfigError(
                f"{display_path(directory.path / expected.name)} "
                f"{CONFIG_WRITE_RACE_ERROR}"
            ) from error
        if not _same_config_snapshot(current, expected):
            raise ConfigError(
                f"{display_path(directory.path / expected.name)} "
                f"{CONFIG_WRITE_RACE_ERROR}"
            )


def project_config_snapshot(
    directory: ConfigDirectory,
    kind: str,
) -> ProjectConfigSnapshot:
    if kind == "pack":
        committed_name, local_name = "pack.yaml", "pack.local.yaml"
    elif kind == "template":
        committed_name, local_name = "template.yaml", "template.local.yaml"
    else:
        raise ConfigError(f"Unsupported project kind: {kind}")
    return ProjectConfigSnapshot(
        committed=read_config_snapshot(directory, committed_name),
        local=read_config_snapshot(directory, local_name),
    )


def renameat2(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
    flags: int,
) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "atomic config rename is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "atomic config rename is unavailable") from error
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


def create_config_temp(
    directory: ConfigDirectory,
    target_name: str,
    mode: int,
) -> tuple[int, str]:
    for _ in range(100):
        name = f".{target_name}.huroshiki-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                name,
                _CONFIG_TEMP_FLAGS,
                mode,
                dir_fd=directory.fd,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise ConfigError(f"Could not allocate temporary file for {target_name}")


def parse_yaml_snapshot(snapshot: ConfigFileSnapshot) -> dict[str, Any]:
    if not snapshot.exists:
        return {}
    try:
        value = yaml.safe_load((snapshot.bytes or b"").decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ConfigError(f"{snapshot.name}: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{snapshot.name} must contain a YAML mapping")
    return value


def _write_all(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError(errno.EIO, "short write while creating config temporary")
        view = view[written:]


def _config_artifact_name(target_name: str, label: str) -> str:
    return f".{target_name}.huroshiki-{uuid4().hex}.{label}"


def _create_config_recovery_link(
    directory: ConfigDirectory,
    source_name: str,
    target_name: str,
    label: str,
) -> str:
    for _ in range(100):
        recovery_name = _config_artifact_name(target_name, label)
        try:
            os.link(
                source_name,
                recovery_name,
                src_dir_fd=directory.fd,
                dst_dir_fd=directory.fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            continue
        os.fsync(directory.fd)
        return recovery_name
    raise ConfigError(f"Could not allocate recovery link for {target_name}")


def _create_config_recovery_copy(
    directory: ConfigDirectory,
    snapshot: ConfigFileSnapshot,
    target_name: str,
    label: str,
) -> str:
    mode = snapshot.mode if snapshot.mode is not None else 0o600
    for _ in range(100):
        recovery_name = _config_artifact_name(target_name, label)
        try:
            descriptor = os.open(
                recovery_name,
                _CONFIG_TEMP_FLAGS,
                mode,
                dir_fd=directory.fd,
            )
        except FileExistsError:
            continue
        try:
            _write_all(descriptor, snapshot.bytes or b"")
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            _unlink_config_artifact(directory, recovery_name)
            raise
        os.close(descriptor)
        os.fsync(directory.fd)
        return recovery_name
    raise ConfigError(f"Could not allocate recovery copy for {target_name}")


def _unlink_config_artifact(directory: ConfigDirectory, name: str | None) -> None:
    if name is None:
        return
    try:
        os.unlink(name, dir_fd=directory.fd)
    except FileNotFoundError:
        pass


def _link_config_artifact_noreplace(
    directory: ConfigDirectory,
    source_name: str,
    target_name: str,
) -> None:
    os.link(
        source_name,
        target_name,
        src_dir_fd=directory.fd,
        dst_dir_fd=directory.fd,
        follow_symlinks=False,
    )


def _cleanup_committed_config_artifacts(
    directory: ConfigDirectory,
    names: tuple[str | None, ...],
) -> None:
    errors: list[str] = []
    for name in names:
        if name is None:
            continue
        try:
            os.unlink(name, dir_fd=directory.fd)
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"{name}: {error}")
    try:
        os.fsync(directory.fd)
    except OSError as error:
        errors.append(f"directory fsync: {error}")
    if errors:
        try:
            print(
                "warning: configuration committed but recovery cleanup was incomplete: "
                + "; ".join(errors),
                file=sys.stderr,
            )
        except BaseException:
            pass


def _config_recovery_location(directory: ConfigDirectory, name: str) -> str:
    return (
        f"{name} in pinned directory dev={directory.device} inode={directory.inode}"
    )


def _config_snapshot_role(
    snapshot: ConfigFileSnapshot,
    *,
    expected: ConfigFileSnapshot,
    staged: ConfigFileSnapshot,
) -> str:
    if not snapshot.exists:
        return "missing"
    if _same_config_snapshot(snapshot, expected):
        return "original"
    if _same_config_snapshot(snapshot, staged):
        return "staged"
    return "external"


def _write_yaml_atomic(
    directory: ConfigDirectory,
    value: dict[str, Any],
    *,
    expected_snapshot: ConfigFileSnapshot,
    guard_snapshots: tuple[ConfigFileSnapshot, ...],
) -> None:
    check_config_directory_identity(directory)
    serialized = yaml.safe_dump(
        value,
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    mode = expected_snapshot.mode if expected_snapshot.mode is not None else 0o600
    descriptor, temporary_name = create_config_temp(
        directory,
        expected_snapshot.name,
        mode,
    )
    staged_recovery_name: str | None = None
    exchanged_recovery_name: str | None = None
    preserve_artifacts = False
    try:
        try:
            _write_all(descriptor, serialized)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        staged_snapshot = read_config_snapshot(directory, temporary_name)
        staged_recovery_name = _create_config_recovery_copy(
            directory,
            staged_snapshot,
            expected_snapshot.name,
            "staged",
        )

        check_config_directory_identity(directory)
        _check_config_snapshots(directory, guard_snapshots)
        companion_snapshots = tuple(
            snapshot
            for snapshot in guard_snapshots
            if snapshot.name != expected_snapshot.name
        )
        if not expected_snapshot.exists:
            try:
                check_config_directory_identity(directory)
                renameat2(
                    directory.fd,
                    temporary_name,
                    directory.fd,
                    expected_snapshot.name,
                    RENAME_NOREPLACE,
                )
                check_config_directory_identity(directory)
                published = read_config_snapshot(
                    directory,
                    expected_snapshot.name,
                )
                if not _same_config_snapshot(published, staged_snapshot):
                    raise ConfigError(
                        f"{display_path(directory.path / expected_snapshot.name)} "
                        "temporary configuration changed before publication"
                    )
                _check_config_snapshots(directory, companion_snapshots)
                check_config_directory_identity(directory)
                os.fsync(directory.fd)
                check_config_directory_identity(directory)
                preserve_artifacts = True
                _cleanup_committed_config_artifacts(
                    directory,
                    (staged_recovery_name,),
                )
                return
            except BaseException as error:
                preserve_artifacts = True
                temporary = read_config_snapshot(directory, temporary_name)
                if temporary.exists and _same_config_identity(
                    temporary,
                    staged_snapshot,
                ):
                    preserve_artifacts = False
                    if isinstance(error, FileExistsError):
                        raise ConfigError(
                            f"{display_path(directory.path / expected_snapshot.name)} "
                            f"{CONFIG_WRITE_RACE_ERROR}"
                        ) from error
                    raise
                canonical = read_config_snapshot(directory, expected_snapshot.name)
                rollback_name: str | None = None
                if _same_config_identity(canonical, staged_snapshot):
                    rollback_name = _config_artifact_name(
                        expected_snapshot.name,
                        "rollback",
                    )
                    try:
                        renameat2(
                            directory.fd,
                            expected_snapshot.name,
                            directory.fd,
                            rollback_name,
                            RENAME_NOREPLACE,
                        )
                        moved = read_config_snapshot(directory, rollback_name)
                        if _same_config_identity(moved, staged_snapshot):
                            _unlink_config_artifact(directory, rollback_name)
                            rollback_name = None
                        else:
                            try:
                                _link_config_artifact_noreplace(
                                    directory,
                                    rollback_name,
                                    expected_snapshot.name,
                                )
                            except FileExistsError:
                                pass
                        os.fsync(directory.fd)
                    except BaseException as rollback_error:
                        os.fsync(directory.fd)
                        detail = (
                            _config_recovery_location(directory, rollback_name)
                            if rollback_name is not None
                            else "the pinned configuration directory"
                        )
                        raise ConfigError(
                            f"{error}; rollback failed: {rollback_error}; recovery "
                            f"artifacts remain in {detail}"
                        ) from error
                if not isinstance(error, Exception):
                    raise
                raise ConfigError(
                    f"{error}; staged configuration retained at "
                    f"{_config_recovery_location(directory, staged_recovery_name)}"
                ) from error

        try:
            check_config_directory_identity(directory)
            renameat2(
                directory.fd,
                temporary_name,
                directory.fd,
                expected_snapshot.name,
                RENAME_EXCHANGE,
            )
            check_config_directory_identity(directory)
            published = read_config_snapshot(directory, expected_snapshot.name)
            if not _same_config_snapshot(published, staged_snapshot):
                raise ConfigError(
                    f"{display_path(directory.path / expected_snapshot.name)} "
                    "temporary configuration changed before publication"
                )
            exchanged = read_config_snapshot(directory, temporary_name)
            if not _same_config_snapshot(exchanged, expected_snapshot):
                raise ConfigError(
                    f"{display_path(directory.path / expected_snapshot.name)} "
                    f"{CONFIG_WRITE_RACE_ERROR}"
                )
            _check_config_snapshots(directory, companion_snapshots)
            check_config_directory_identity(directory)
            os.fsync(directory.fd)
            check_config_directory_identity(directory)
            preserve_artifacts = True
            _cleanup_committed_config_artifacts(
                directory,
                (temporary_name, staged_recovery_name),
            )
        except BaseException as error:
            preserve_artifacts = True
            temporary = read_config_snapshot(directory, temporary_name)
            if not temporary.exists:
                preserve_artifacts = False
                os.fsync(directory.fd)
                raise
            if _same_config_identity(temporary, staged_snapshot):
                preserve_artifacts = False
                raise
            state = ExchangeState(
                target=read_config_snapshot(directory, expected_snapshot.name),
                temporary=temporary,
            )
            exchange_state = classify_exchange_state(
                target=state.target,
                temporary=state.temporary,
                staged=staged_snapshot,
                expected=expected_snapshot,
            )
            rollback_name: str | None = None
            if exchange_state == "intact":
                rollback_name = _config_artifact_name(
                    expected_snapshot.name,
                    "rollback",
                )
                try:
                    renameat2(
                        directory.fd,
                        expected_snapshot.name,
                        directory.fd,
                        rollback_name,
                        RENAME_NOREPLACE,
                    )
                    moved = read_config_snapshot(directory, rollback_name)
                    if _same_config_identity(moved, staged_snapshot):
                        exchanged_recovery_name = _create_config_recovery_link(
                            directory,
                            temporary_name,
                            expected_snapshot.name,
                            "exchanged",
                        )
                        renameat2(
                            directory.fd,
                            temporary_name,
                            directory.fd,
                            expected_snapshot.name,
                            RENAME_NOREPLACE,
                        )
                        _unlink_config_artifact(directory, rollback_name)
                        rollback_name = None
                    else:
                        try:
                            _link_config_artifact_noreplace(
                                directory,
                                rollback_name,
                                expected_snapshot.name,
                            )
                        except FileExistsError:
                            pass
                    os.fsync(directory.fd)
                except BaseException as rollback_error:
                    os.fsync(directory.fd)
                    detail = (
                        _config_recovery_location(directory, rollback_name)
                        if rollback_name is not None
                        else "the pinned configuration directory"
                    )
                    raise ConfigError(
                        f"{error}; rollback failed: {rollback_error}; original and "
                        f"staged recovery artifacts remain in {detail}"
                    ) from error
            else:
                os.fsync(directory.fd)
            if not isinstance(error, Exception):
                raise
            canonical_after = read_config_snapshot(
                directory,
                expected_snapshot.name,
            )
            canonical_role = _config_snapshot_role(
                canonical_after,
                expected=expected_snapshot,
                staged=staged_snapshot,
            )
            temporary_after = read_config_snapshot(directory, temporary_name)
            temporary_detail = (
                "; exchanged recovery is at "
                f"{_config_recovery_location(directory, temporary_name)}"
                if temporary_after.exists
                else ""
            )
            exchanged_detail = (
                "; exchanged recovery is at "
                f"{_config_recovery_location(directory, exchanged_recovery_name)}"
                if exchanged_recovery_name is not None
                else ""
            )
            raise ConfigError(
                f"{error}; {canonical_role} configuration remains at the canonical "
                f"path{temporary_detail}{exchanged_detail}; staged recovery is at "
                f"{_config_recovery_location(directory, staged_recovery_name)}"
            ) from error
    finally:
        if not preserve_artifacts:
            _unlink_config_artifact(directory, temporary_name)
            _unlink_config_artifact(directory, staged_recovery_name)
            _unlink_config_artifact(directory, exchanged_recovery_name)


def _project_lock_root(project_key: str) -> Path:
    kind, separator, project_id = project_key.partition(":")
    if not separator:
        raise ConfigError("Project key must be pack:<id> or template:<id>")
    get_project_root(kind, project_id, must_exist=False)
    projects = PACKS if kind == "pack" else TEMPLATES
    repository = projects.parent
    state = repository / ".huroshiki"
    return ensure_safe_state_path(
        state / "locks", state_root=state, repository_root=repository
    )


def project_lock_path(project_key: str) -> Path:
    kind, _, project_id = project_key.partition(":")
    return _project_lock_root(project_key) / f"{kind}-{project_id}.lock"


_read_lock_metadata = project_locks.read_lock_metadata
_format_lock_owner = project_locks.format_lock_owner
_inspect_lock_path = project_locks.inspect_lock_path


class ProjectLock(project_locks.ProjectLock):
    def __init__(self, project_key: str, operation: str) -> None:
        super().__init__(project_key, operation, project_lock_path(project_key))


def acquire_project_locks(
    project_keys: Iterable[str],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
    operation: str = "multi-project transaction",
) -> ProjectLockSet:
    keys = tuple(sorted(set(project_keys)))
    if not keys:
        raise ConfigError("At least one project lock is required")
    locks = tuple(ProjectLock(key, operation) for key in keys)
    lock_set = ProjectLockSet(locks)
    try:
        for lock in locks:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise ConfigError("Project lock acquisition was cancelled")
                if time.monotonic() >= deadline:
                    raise ConfigError("Project lock acquisition deadline exceeded")
                try:
                    lock.acquire()
                    break
                except ProjectLockBusy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ConfigError(
                            f"Project lock acquisition deadline exceeded: {lock.project_key}"
                        )
                    if cancel_event is not None:
                        cancel_event.wait(min(0.02, remaining))
                    else:
                        time.sleep(min(0.02, remaining))
        return lock_set
    except BaseException as error:
        try:
            lock_set.release()
        except BaseException as release_error:
            raise ProjectLockSetError(
                f"{error}; acquired lock rollback failed: {release_error}",
                lock_set,
            ) from error
        raise


def active_project_lock(project_key: str) -> ProjectLockMetadata | None:
    return _inspect_lock_path(project_lock_path(project_key))[1]


def project_lock_is_active(project_key: str) -> bool:
    return _inspect_lock_path(project_lock_path(project_key))[0]


DEFAULT_RETENTION_DAYS = {
    "log": 30,
    "completed_transaction": 7,
    "transaction_leftover": 7,
    "trash": 30,
    "deploy_snapshot": 7,
}
TRASH_NAME_RE = re.compile(
    r"^(?P<timestamp>\d{8}-\d{6}-\d{6})-(?P<kind>pack|template)-(?P<id>[a-z0-9][a-z0-9._-]*)$"
)


@dataclass(frozen=True)
class TrashEntry:
    name: str
    kind: str
    project_id: str
    path: Path
    created_at: float
    bytes: int

    @property
    def project_key(self) -> str:
        return f"{self.kind}:{self.project_id}"


@dataclass(frozen=True)
class StateItem:
    category: str
    path: Path
    project_key: str | None
    modified_at: float
    bytes: int
    active: bool = False


@dataclass(frozen=True)
class StateCleanupReport:
    items: tuple[StateItem, ...]
    selected: tuple[StateItem, ...]
    removed_count: int
    removed_bytes: int
    dry_run: bool


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return value


def _normalize_project_text(field: str, value: str) -> str:
    normalized = value.strip()
    validate_project_text(field, normalized)
    return normalized


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _reject_unsafe_remote_text(field: str, value: str) -> str:
    normalized = value.strip()
    validate_project_text(field, normalized)
    if normalized != value or any(character.isspace() for character in normalized):
        raise ConfigError(f"{field} must not contain whitespace")
    return normalized


def validate_ssh_target(value: str) -> str:
    target = _reject_unsafe_remote_text("SSH target", value)
    if target.startswith("-"):
        raise ConfigError("SSH target must not begin with '-'")
    if "/" in target:
        raise ConfigError("SSH target must not contain '/'")
    if target.count("@") > 1:
        raise ConfigError("SSH target must contain at most one '@'")
    user: str | None = None
    host = target
    if "@" in target:
        user, host = target.split("@", 1)
        if not user or not SSH_USER_RE.fullmatch(user):
            raise ConfigError("SSH target user is empty or invalid")
    if not host:
        raise ConfigError("SSH target host must be non-empty")
    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]")):
            raise ConfigError("SSH target has malformed IPv6 brackets")
        address = host[1:-1]
        try:
            ipaddress.IPv6Address(address)
        except ValueError as error:
            raise ConfigError("SSH target has malformed IPv6 brackets") from error
    elif any(character in host for character in "[]:"):
        raise ConfigError("SSH target must bracket IPv6 addresses and contain no command")
    elif any(
        not label or len(label) > 63 or not SSH_HOST_LABEL_RE.fullmatch(label)
        for label in host.split(".")
    ):
        raise ConfigError("SSH target host is invalid")
    return f"{user}@{host}" if user is not None else host


def validate_remote_stack_dir(value: str) -> str:
    stack_dir = value.strip()
    validate_project_text("Stack directory", stack_dir)
    if stack_dir != value:
        raise ConfigError("Stack directory must not have surrounding whitespace")
    if not stack_dir.startswith("/") or stack_dir == "/":
        raise ConfigError("Stack directory must be a non-root POSIX absolute path")
    if any(part in {".", ".."} for part in stack_dir.split("/")):
        raise ConfigError("Stack directory must not contain '.' or '..' components")
    if PurePosixPath(stack_dir).as_posix() != stack_dir:
        raise ConfigError("Stack directory must be a normalized POSIX absolute path")
    return stack_dir


def validate_compose_service(value: str) -> str:
    service = value.strip()
    validate_project_text("Compose service", service)
    if service != value:
        raise ConfigError("Compose service must not have surrounding whitespace")
    if not COMPOSE_SERVICE_RE.fullmatch(service):
        raise ConfigError(
            "Compose service must use only letters, digits, '_', '.', or '-' and "
            "start with a letter or digit"
        )
    return service


def validate_public_pack_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("Public Pack URL must be a non-empty string")
    if value != value.strip():
        raise ConfigError("Public Pack URL must not have surrounding whitespace")
    if any(unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in value):
        raise ConfigError("Public Pack URL must not contain control characters")
    if any(character.isspace() for character in value):
        raise ConfigError("Public Pack URL must not contain whitespace")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        parsed.port
    except ValueError as error:
        raise ConfigError("Public Pack URL is malformed") from error
    if parsed.scheme != "https":
        raise ConfigError("Public Pack URL must use https")
    if not hostname:
        raise ConfigError("Public Pack URL must include a hostname")
    if username is not None or password is not None:
        raise ConfigError("Public Pack URL must not include credentials")
    if "#" in value:
        raise ConfigError("Public Pack URL must not include a fragment")
    if not parsed.path.endswith("/pack.toml"):
        raise ConfigError("Public Pack URL path must end with /pack.toml")
    return value


def validate_url_policy(config: dict[str, Any], context: str) -> None:
    value = config.get("url_max_jar_size_bytes")
    if "url_max_jar_size_bytes" in config and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ConfigError(f"{context}: url_max_jar_size_bytes must be a positive integer")
    allow_private = config.get("url_allow_private_networks")
    if "url_allow_private_networks" in config and not isinstance(allow_private, bool):
        raise ConfigError(f"{context}: url_allow_private_networks must be a boolean")


def validate_local_config(kind: str, path: Path, local: dict[str, Any]) -> None:
    context = display_path(path)
    if kind == "template":
        for key in local:
            if key in TEMPLATE_LOCAL_KEYS:
                continue
            if key in TEMPLATE_COMMITTED_KEYS:
                raise ConfigError(
                    f"{context}: {key} is committed semantic data; "
                    "edit template.yaml instead"
                )
            raise ConfigError(
                f"{context}: unsupported machine-local key {key!r}; "
                "allowed keys: url_allow_private_networks, url_max_jar_size_bytes"
            )
        validate_url_policy(local, context)
        return

    if kind != "pack":
        raise ConfigError(f"Unsupported local configuration kind: {kind}")
    allowed = ", ".join(sorted(PACK_LOCAL_KEYS))
    for key, value in local.items():
        nested_keys = PACK_LOCAL_KEYS.get(key)
        if key not in PACK_LOCAL_KEYS:
            raise ConfigError(
                f"{context}: unsupported machine-local key {key!r}; "
                f"allowed keys: {allowed}"
            )
        if nested_keys is None:
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"{context}: {key} must be a mapping")
        for nested_key, nested_value in value.items():
            if nested_key not in nested_keys:
                nested_allowed = ", ".join(sorted(nested_keys))
                raise ConfigError(
                    f"{context}: unsupported machine-local key "
                    f"{key}.{nested_key}; allowed keys: {nested_allowed}"
                )
            if not isinstance(nested_value, str) or not nested_value.strip():
                raise ConfigError(
                    f"{context}: {key}.{nested_key} must be a non-empty string"
                )
            if key == "distribution" and nested_key == "rsync_target":
                try:
                    validate_rsync_target(nested_value)
                except ValueError as error:
                    raise ConfigError(f"{context}: {error}") from error
            elif key == "distribution" and nested_key == "public_pack_url":
                try:
                    validate_public_pack_url(nested_value)
                except ConfigError as error:
                    raise ConfigError(f"{context}: {error}") from error
            elif key == "minecraft_server" and nested_key == "ssh_host":
                try:
                    validate_ssh_target(nested_value)
                except ConfigError as error:
                    raise ConfigError(f"{context}: {error}") from error
            elif key == "minecraft_server" and nested_key == "stack_dir":
                try:
                    validate_remote_stack_dir(nested_value)
                except ConfigError as error:
                    raise ConfigError(f"{context}: {error}") from error
            elif key == "minecraft_server" and nested_key == "service":
                try:
                    validate_compose_service(nested_value)
                except ConfigError as error:
                    raise ConfigError(f"{context}: {error}") from error
    validate_url_policy(local, context)


def ensure_safe_state_path(
    path: Path,
    *,
    state_root: Path | None = None,
    repository_root: Path | None = None,
) -> Path:
    repository = Path(os.path.abspath(repository_root or ROOT))
    state = Path(os.path.abspath(state_root or (repository / ".huroshiki")))
    candidate = Path(os.path.abspath(path))
    try:
        state.relative_to(repository)
        candidate.relative_to(state)
    except ValueError as error:
        raise ConfigError("State path escaped repository .huroshiki/") from error

    current = repository
    for part in candidate.relative_to(repository).parts:
        current /= part
        if current.is_symlink():
            raise ConfigError(f"Unsafe symlink in state path: {current}")
        if not current.exists():
            break

    resolved_repository = repository.resolve()
    resolved_state = state.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if (
        resolved_state == resolved_repository
        or resolved_repository not in resolved_state.parents
        or (
            resolved_candidate != resolved_state
            and resolved_state not in resolved_candidate.parents
        )
    ):
        raise ConfigError("Resolved state path escaped repository ROOT")
    return candidate


def make_state_directory(
    path: Path,
    *,
    state_root: Path | None = None,
    repository_root: Path | None = None,
) -> Path:
    safe = ensure_safe_state_path(
        path, state_root=state_root, repository_root=repository_root
    )
    safe.mkdir(parents=True, exist_ok=True)
    return ensure_safe_state_path(
        safe, state_root=state_root, repository_root=repository_root
    )


def validate_project_id(project_id: str) -> None:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ConfigError(
            "Project ID must use lowercase letters, digits, '.', '_' or '-', "
            "and must start with a letter or digit"
        )


def validate_pack_id(pack_id: str) -> None:
    validate_project_id(pack_id)


def validate_project_text(field: str, value: str) -> None:
    if not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    if any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"}
        for character in value
    ):
        raise ConfigError(f"{field} must not contain control characters or newlines")


def validate_project_creation_fields(
    *, display_name: str, minecraft: str, loader_version: str
) -> None:
    validate_project_text("Display name", display_name)
    validate_project_text("Minecraft version", minecraft)
    validate_project_text("Loader version", loader_version)


def get_pack_root(pack_id: str, *, must_exist: bool = True) -> Path:
    validate_project_id(pack_id)
    root = (PACKS / pack_id).resolve()
    if PACKS.resolve() not in root.parents:
        raise ConfigError("Pack path escaped packs/")
    if must_exist and not root.is_dir():
        raise ConfigError(f"Unknown pack: {pack_id}")
    return root


def _prospective_text(config: dict[str, Any], key: str, context: str) -> str:
    value = config.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{context}: {key} must be a non-empty string")
    normalized = value.strip()
    validate_project_text(key.replace("_", " ").title(), normalized)
    return normalized


def _validate_prospective_deployment(config: dict[str, Any], context: str) -> None:
    errors: list[str] = []
    if "distribution" in config:
        try:
            distribution = require_mapping(config, "distribution", context)
        except ConfigError as error:
            errors.append(str(error))
        else:
            try:
                target = distribution.get("rsync_target")
                if not isinstance(target, str) or not target:
                    raise ConfigError(
                        f"{context}.distribution.rsync_target must be a non-empty string"
                    )
                validate_rsync_target(target)
                public_url = distribution.get("public_pack_url")
                if public_url is not None:
                    if not isinstance(public_url, str):
                        raise ConfigError(
                            f"{context}.distribution.public_pack_url must be a string"
                        )
                    validate_public_pack_url(public_url)
            except (ConfigError, ValueError) as error:
                errors.append(str(error))
    if "minecraft_server" in config:
        try:
            server = require_mapping(config, "minecraft_server", context)
        except ConfigError as error:
            errors.append(str(error))
        else:
            validators = {
                "ssh_host": validate_ssh_target,
                "stack_dir": validate_remote_stack_dir,
                "service": validate_compose_service,
            }
            for field, validator in validators.items():
                try:
                    raw_value = server.get(field)
                    if not isinstance(raw_value, str) or not raw_value.strip():
                        raise ConfigError(
                            f"{context}.minecraft_server.{field} must be a non-empty string"
                        )
                    validator(raw_value)
                except ConfigError as error:
                    errors.append(str(error))
    if errors:
        raise ConfigError("; ".join(errors))


def prospective_pack_config(
    pack_id: str,
    committed: dict[str, Any],
    local: dict[str, Any],
) -> dict[str, Any]:
    committed_path = Path("packs") / pack_id / "pack.yaml"
    local_path = Path("packs") / pack_id / "pack.local.yaml"
    validate_local_config("pack", local_path, local)
    if "url_allow_private_networks" in committed:
        raise ConfigError(
            f"{committed_path}: url_allow_private_networks is machine-local only; "
            "move it to pack.local.yaml"
        )
    if committed.get("id") != pack_id:
        raise ConfigError(f"{committed_path} must contain id: {pack_id}")
    committed_distribution = committed.get("distribution")
    if isinstance(committed_distribution, dict) and "public_pack_url" in committed_distribution:
        committed_public_url = committed_distribution["public_pack_url"]
        if not isinstance(committed_public_url, str):
            raise ConfigError(
                f"{committed_path}.distribution.public_pack_url must be a string"
            )
        validate_public_pack_url(committed_public_url)
    config = merge(committed, local)
    _prospective_text(config, "display_name", str(committed_path))
    if not isinstance(config.get("enabled"), bool):
        raise ConfigError(f"{committed_path}: enabled must be a boolean")
    validate_url_policy(config, str(committed_path))
    _validate_prospective_deployment(config, str(committed_path))
    return config


def prospective_template_config(
    template_id: str,
    committed: dict[str, Any],
    local: dict[str, Any],
) -> dict[str, Any]:
    committed_path = Path("templates") / template_id / "template.yaml"
    local_path = Path("templates") / template_id / "template.local.yaml"
    validate_local_config("template", local_path, local)
    if "url_allow_private_networks" in committed:
        raise ConfigError(
            f"{committed_path}: url_allow_private_networks is machine-local only; "
            "move it to template.local.yaml"
        )
    if committed.get("id") != template_id:
        raise ConfigError(f"{committed_path} must contain id: {template_id}")
    for key in (
        "display_name",
        "minecraft",
        "loader",
        "reference_loader_version",
    ):
        _prospective_text(committed, key, str(committed_path))
    if not isinstance(committed.get("enabled"), bool):
        raise ConfigError(f"{committed_path}: enabled must be a boolean")
    loader = str(committed["loader"]).strip().lower()
    if loader not in LOADER_FLAGS:
        raise ConfigError(
            f"{committed_path}: loader must be one of "
            f"{', '.join(sorted(LOADER_FLAGS))}"
        )
    mods = committed.get("mods")
    if not isinstance(mods, list):
        raise ConfigError(f"{committed_path}: mods must be a list")
    for index, entry in enumerate(mods):
        normalize_template_mod(entry, f"{committed_path}: mods[{index}]")
    config = merge(committed, local)
    validate_url_policy(config, str(committed_path))
    return config


def load_pack_config(pack_id: str) -> dict[str, Any]:
    root = get_pack_root(pack_id)
    committed = load_yaml(root / "pack.yaml")
    if "url_allow_private_networks" in committed:
        raise ConfigError(
            f"{display_path(root / 'pack.yaml')}: url_allow_private_networks is "
            "machine-local only; move it to pack.local.yaml"
        )
    local_path = root / "pack.local.yaml"
    local = load_yaml(local_path)
    validate_local_config("pack", local_path, local)
    config = merge(committed, local)
    if config.get("id") != pack_id:
        raise ConfigError(f"packs/{pack_id}/pack.yaml must contain id: {pack_id}")
    return config


def get_template_root(template_id: str, *, must_exist: bool = True) -> Path:
    validate_project_id(template_id)
    root = (TEMPLATES / template_id).resolve()
    if TEMPLATES.resolve() not in root.parents:
        raise ConfigError("Template path escaped templates/")
    if must_exist and not root.is_dir():
        raise ConfigError(f"Unknown template: {template_id}")
    return root


def reject_legacy_template_source(root: Path) -> None:
    source = root / "source"
    if source.exists() or source.is_symlink():
        raise ConfigError(
            f"templates/{root.name}/source: legacy template source is not supported; "
            "remove it and define the template only in template.yaml"
        )


def load_template_config(template_id: str) -> dict[str, Any]:
    root = get_template_root(template_id)
    reject_legacy_template_source(root)
    config = load_yaml(root / "template.yaml")
    if "url_allow_private_networks" in config:
        raise ConfigError(
            f"{display_path(root / 'template.yaml')}: url_allow_private_networks is "
            "machine-local only; move it to template.local.yaml"
        )
    local_path = root / "template.local.yaml"
    local = load_yaml(local_path)
    validate_local_config("template", local_path, local)
    config = merge(config, local)
    if config.get("id") != template_id:
        raise ConfigError(
            f"templates/{template_id}/template.yaml must contain id: {template_id}"
        )
    return config


def get_project_root(kind: str, project_id: str, *, must_exist: bool = True) -> Path:
    if kind == "pack":
        return get_pack_root(project_id, must_exist=must_exist)
    if kind == "template":
        return get_template_root(project_id, must_exist=must_exist)
    raise ConfigError(f"Unsupported project kind: {kind}")


def _direct_state_child(parent: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ConfigError(f"Invalid state item name: {name!r}")
    safe_parent = make_state_directory(parent)
    return ensure_safe_state_path(safe_parent / name)


def path_bytes(path: Path) -> int:
    if path.is_symlink():
        return path.lstat().st_size
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return path.stat().st_size + sum(
        path_bytes(child) for child in path.iterdir()
    )


def parse_trash_entry(path: Path) -> TrashEntry:
    path = ensure_safe_state_path(path)
    if (
        TRASH_ROOT.is_symlink()
        or path.parent.resolve() != TRASH_ROOT.resolve()
        or path.is_symlink()
        or not path.is_dir()
    ):
        raise ConfigError(f"Unsafe trash entry: {path}")
    match = TRASH_NAME_RE.fullmatch(path.name)
    if match is None:
        raise ConfigError(f"Invalid trash entry name: {path.name}")
    project_id = match.group("id")
    validate_project_id(project_id)
    created = datetime.strptime(
        match.group("timestamp"), "%Y%m%d-%H%M%S-%f"
    ).replace(tzinfo=timezone.utc)
    return TrashEntry(
        path.name,
        match.group("kind"),
        project_id,
        path,
        created.timestamp(),
        path_bytes(path),
    )


def list_trash() -> list[TrashEntry]:
    ensure_safe_state_path(TRASH_ROOT)
    if not TRASH_ROOT.exists():
        return []
    if TRASH_ROOT.is_symlink() or not TRASH_ROOT.is_dir():
        raise ConfigError(f"Unsafe trash root: {TRASH_ROOT}")
    entries = [parse_trash_entry(path) for path in sorted(TRASH_ROOT.iterdir())]
    return sorted(entries, key=lambda item: item.created_at, reverse=True)


def _trash_project(kind: str, project_id: str) -> TrashEntry:
    source = get_project_root(kind, project_id)
    make_state_directory(TRASH_ROOT)
    if TRASH_ROOT.is_symlink() or source.stat().st_dev != TRASH_ROOT.stat().st_dev:
        raise ConfigError("Project trash must be on the same filesystem")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    destination = _direct_state_child(TRASH_ROOT, f"{timestamp}-{kind}-{project_id}")
    if destination.exists() or destination.is_symlink():
        raise ConfigError(f"Trash destination already exists: {destination.name}")
    source.rename(destination)
    return parse_trash_entry(destination)


def trash_project(kind: str, project_id: str) -> TrashEntry:
    with ProjectLock(f"{kind}:{project_id}", "delete project"):
        return _trash_project(kind, project_id)


def _restore_trash(name: str) -> Path:
    entry = parse_trash_entry(_direct_state_child(TRASH_ROOT, name))
    destination = get_project_root(entry.kind, entry.project_id, must_exist=False)
    if destination.exists() or destination.is_symlink():
        raise ConfigError(f"Project already exists: {entry.project_key}")
    if destination.parent.stat().st_dev != entry.path.stat().st_dev:
        raise ConfigError("Project restore must stay on the same filesystem")
    entry.path.rename(destination)
    return destination


def restore_trash(name: str) -> Path:
    entry = parse_trash_entry(_direct_state_child(TRASH_ROOT, name))
    with ProjectLock(entry.project_key, "restore project"):
        return _restore_trash(name)


def purge_trash(
    *,
    name: str | None = None,
    project_key: str | None = None,
    older_than_days: int | None = None,
) -> tuple[int, int]:
    if name is None and project_key is None and older_than_days is None:
        raise ConfigError("Trash purge requires an entry, --project, or --older-than")
    if older_than_days is not None and older_than_days < 0:
        raise ConfigError("--older-than must be non-negative")
    if project_key is not None:
        kind, separator, project_id = project_key.partition(":")
        if not separator:
            raise ConfigError("Project filter must be pack:<id> or template:<id>")
        get_project_root(kind, project_id, must_exist=False)
    now = datetime.now(timezone.utc).timestamp()
    selected: list[TrashEntry] = []
    for entry in list_trash():
        if name is not None and entry.name != name:
            continue
        if project_key is not None and entry.project_key != project_key:
            continue
        if (
            older_than_days is not None
            and now - entry.created_at < older_than_days * 86400
        ):
            continue
        selected.append(entry)
    if name is not None and not selected:
        raise ConfigError(f"Unknown or filtered trash entry: {name}")
    total = sum(entry.bytes for entry in selected)
    for entry in selected:
        with ProjectLock(entry.project_key, "purge trash"):
            ensure_safe_state_path(entry.path)
            shutil.rmtree(entry.path)
    return len(selected), total


def load_project_config(kind: str, project_id: str) -> dict[str, Any]:
    if kind == "pack":
        return load_pack_config(project_id)
    if kind == "template":
        return load_template_config(project_id)
    raise ConfigError(f"Unsupported project kind: {kind}")


def require_mapping(mapping: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{context}.{key} must be a mapping")
    return value


def require_text(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def pack_ids() -> list[str]:
    if not PACKS.exists():
        return []
    return sorted(
        path.name
        for path in PACKS.iterdir()
        if path.is_dir() and (path / "pack.yaml").is_file()
    )


def template_ids() -> list[str]:
    if not TEMPLATES.exists():
        return []
    return sorted(
        path.name
        for path in TEMPLATES.iterdir()
        if path.is_dir() and (path / "template.yaml").is_file()
    )


def side_validation_error(side: object) -> str | None:
    if isinstance(side, str) and side in VALID_SIDES:
        return None
    return f"side must be client, server, or both; got {side!r}"


def normalize_template_mod(
    entry: object,
    context: str,
    *,
    allow_invalid_side: bool = False,
) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ConfigError(f"{context} must be a mapping")
    provider = str(entry.get("provider", "")).strip().lower()
    if provider not in {"modrinth", "curseforge", "url"}:
        raise ConfigError(f"{context}.provider must be modrinth, curseforge, or url")
    project_id = str(entry.get("project_id", "")).strip()
    if not project_id:
        raise ConfigError(f"{context}.project_id must be a non-empty string")
    name = str(entry.get("name", project_id)).strip() or project_id
    raw_side = entry.get("side")
    side_error = side_validation_error(raw_side)
    if side_error is not None and not allow_invalid_side:
        raise ConfigError(f"{context}.{side_error}")
    side = raw_side if isinstance(raw_side, str) else ""
    result = {
        "name": name,
        "provider": provider,
        "project_id": project_id,
        "side": side,
    }
    if provider == "url":
        url = str(entry.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"{context}.url must be a public http(s) URL")
        if not unquote(Path(parsed.path).name).lower().endswith(".jar"):
            raise ConfigError(f"{context}.url must point to a .jar file")
        result["url"] = url
    return result


def template_mods(
    template_id: str,
    *,
    allow_invalid_sides: bool = False,
    deduplicate: bool = True,
) -> list[dict[str, str]]:
    config = load_template_config(template_id)
    value = config.get("mods", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"templates/{template_id}/template.yaml mods must be a list")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(value):
        normalized = normalize_template_mod(
            entry,
            f"mods[{index}]",
            allow_invalid_side=allow_invalid_sides,
        )
        key = (normalized["provider"], normalized["project_id"])
        if deduplicate and key in seen:
            continue
        result.append(normalized)
        seen.add(key)
    return result


def template_mods_indexed(
    template_id: str,
    *,
    allow_invalid_sides: bool = False,
    deduplicate: bool = True,
) -> list[tuple[int, dict[str, str]]]:
    config = load_template_config(template_id)
    value = config.get("mods", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"templates/{template_id}/template.yaml mods must be a list")
    result: list[tuple[int, dict[str, str]]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(value):
        normalized = normalize_template_mod(
            entry,
            f"mods[{index}]",
            allow_invalid_side=allow_invalid_sides,
        )
        key = (normalized["provider"], normalized["project_id"])
        if deduplicate and key in seen:
            continue
        result.append((index, normalized))
        seen.add(key)
    return result


def set_template_mod_side(
    template_id: str,
    provider: str,
    project_id: str,
    occurrence: int,
    side: str,
) -> None:
    root = get_template_root(template_id)
    config_path = root / "template.yaml"
    config = load_yaml(config_path)
    mods = config.get("mods")
    if not isinstance(mods, list):
        raise ConfigError(f"templates/{template_id}/template.yaml mods must be a list")
    matched = 0
    for index, raw_entry in enumerate(mods):
        entry = normalize_template_mod(
            raw_entry,
            f"mods[{index}]",
            allow_invalid_side=True,
        )
        if (entry["provider"], entry["project_id"]) != (provider, project_id):
            continue
        if matched == occurrence:
            set_template_mod_side_at_index(template_id, index, side)
            return
        matched += 1
    else:
        raise ConfigError(
            f"Unknown template MOD: {provider}:{project_id} occurrence {occurrence + 1}"
        )


def set_template_mod_side_at_index(
    template_id: str,
    index: int,
    side: str,
) -> None:
    root = get_template_root(template_id)
    load_template_config(template_id)
    config_path = root / "template.yaml"
    config = load_yaml(config_path)
    mods = config.get("mods")
    if not isinstance(mods, list):
        raise ConfigError(f"templates/{template_id}/template.yaml mods must be a list")
    if index < 0 or index >= len(mods):
        raise ConfigError(f"Unknown template MOD list index: {index}")
    raw_entry = mods[index]
    normalize_template_mod(
        raw_entry,
        f"mods[{index}]",
        allow_invalid_side=True,
    )
    raw_entry["side"] = normalize_side(side)
    temporary = config_path.with_name(".template.yaml.huroshiki-tmp")
    temporary.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(config_path)


def save_template_mods_raw(template_id: str, mods: list[object]) -> None:
    root = get_template_root(template_id)
    load_template_config(template_id)
    config_path = root / "template.yaml"
    config = load_yaml(config_path)
    config["mods"] = mods
    temporary = config_path.with_name(".template.yaml.huroshiki-tmp")
    temporary.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(config_path)


def save_template_mods(
    template_id: str,
    mods: list[dict[str, str]],
    *,
    allow_invalid_sides: bool = False,
) -> None:
    root = get_template_root(template_id)
    load_template_config(template_id)
    config_path = root / "template.yaml"
    config = load_yaml(config_path)
    normalized = [
        normalize_template_mod(
            entry,
            f"mods[{index}]",
            allow_invalid_side=allow_invalid_sides,
        )
        for index, entry in enumerate(mods)
    ]
    deduplicated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in normalized:
        key = (entry["provider"], entry["project_id"])
        if key in seen:
            continue
        deduplicated.append(entry)
        seen.add(key)
    config["mods"] = deduplicated
    temporary = config_path.with_name(".template.yaml.huroshiki-tmp")
    temporary.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(config_path)


def template_versions(template_id: str) -> tuple[str, str, str]:
    config = load_template_config(template_id)
    minecraft = require_text(config, "minecraft", template_id)
    loader = require_text(config, "loader", template_id).lower()
    if loader not in LOADER_FLAGS:
        raise ConfigError(f"Unsupported loader in template {template_id}: {loader}")
    loader_version = str(config.get("reference_loader_version", "")).strip()
    if not loader_version:
        raise ConfigError(
            f"templates/{template_id}/template.yaml must contain reference_loader_version"
        )
    return minecraft, loader, loader_version


def compatible_template_ids(minecraft: str, loader: str) -> list[str]:
    normalized_loader = loader.strip().lower()
    result: list[str] = []
    for template_id in template_ids():
        try:
            template_minecraft, template_loader, _ = template_versions(template_id)
        except ConfigError:
            continue
        if template_minecraft == minecraft and template_loader == normalized_loader:
            result.append(template_id)
    return result


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(shlex.quote(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True)


def run_packwiz(
    command: list[str],
    *,
    cwd: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> BoundedProcessResult:
    print("+", " ".join(shlex.quote(part) for part in command))
    process_deadline = time.monotonic() + PACKWIZ_PROCESS_TIMEOUT_SECONDS
    if deadline is not None:
        process_deadline = min(deadline, process_deadline)
    result = run_bounded_process(
        command,
        cwd=cwd,
        cancel_event=cancel_event,
        deadline=process_deadline,
    )
    failure = process_failure_message(result, label="Packwiz")
    if failure is not None:
        raise ConfigError(failure)
    return result


def bounded_diagnostic(text: str, *, limit: int = RSYNC_DIAGNOSTIC_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}... [diagnostic truncated; {omitted} characters omitted]"


def _rsync_failure_message(
    result: BoundedProcessResult,
    *,
    phase: str,
) -> str | None:
    phase_detail = f" (phase={phase})"
    if result.termination_incomplete:
        return f"Rsync process cleanup did not complete{phase_detail}"
    if result.orphaned_descendants:
        return f"Rsync left descendant processes running{phase_detail}"
    if result.cancelled:
        return f"Rsync operation was cancelled{phase_detail}"
    if result.timed_out:
        return f"Rsync operation exceeded its deadline{phase_detail}"
    if result.output_limit_exceeded:
        if phase == "rsync-preview":
            return f"Rsync preview output exceeded the supported limit{phase_detail}"
        return f"Rsync transfer output exceeded the supported limit{phase_detail}"
    if result.returncode != 0:
        diagnostic = bounded_diagnostic(result.stderr or result.stdout).strip()
        suffix = f": {diagnostic}" if diagnostic else ""
        return f"Rsync exited with status {result.returncode}{phase_detail}{suffix}"
    return None


def run_rsync_process(
    command: Sequence[str],
    *,
    cwd: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    phase: str = "rsync",
    max_output_bytes: int | None = None,
) -> BoundedProcessResult:
    print("+", " ".join(shlex.quote(part) for part in command))
    process_deadline = time.monotonic() + RSYNC_PROCESS_TIMEOUT_SECONDS
    if deadline is not None:
        process_deadline = min(deadline, process_deadline)
    try:
        result = run_bounded_process(
            command,
            cwd=cwd,
            cancel_event=cancel_event,
            deadline=process_deadline,
            max_output_bytes=(
                RSYNC_OUTPUT_MAX_BYTES
                if max_output_bytes is None
                else max_output_bytes
            ),
        )
    except OSError as error:
        raise ConfigError(f"Rsync process could not start (phase={phase}): {error}") from error
    failure = _rsync_failure_message(result, phase=phase)
    if failure is not None:
        raise ConfigError(failure)
    return result


def copy_tree(source: Path, destination: Path) -> None:
    if source.exists():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".gitkeep"),
        )


def metadata_files(source: Path) -> list[Path]:
    return sorted(source.rglob("*.pw.toml"))


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def normalize_side(side: str) -> str:
    normalized = SIDE_ALIASES.get(side.lower())
    if normalized is None:
        accepted = "both|b, client|c, server|s"
        raise ConfigError(f"Invalid side {side!r}; expected {accepted}")
    return normalized


def set_side_file(path: Path, side: str) -> None:
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    doc["side"] = normalize_side(side)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def set_side_and_refresh(
    source: Path,
    path: Path,
    side: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> None:
    snapshots = {
        item: item.read_bytes() if item.exists() else None
        for item in (path, source / "index.toml", source / "pack.toml")
    }
    try:
        set_side_file(path, side)
        run_packwiz(
            ["packwiz", "refresh"],
            cwd=source,
            cancel_event=cancel_event,
            deadline=deadline,
        )
    except BaseException as error:
        rollback_errors: list[str] = []
        for item, content in snapshots.items():
            try:
                if content is None:
                    item.unlink(missing_ok=True)
                else:
                    temporary = item.with_name(
                        f".{item.name}.huroshiki-side-rollback-{uuid4().hex}"
                    )
                    try:
                        temporary.write_bytes(content)
                        temporary.replace(item)
                    finally:
                        temporary.unlink(missing_ok=True)
            except BaseException as rollback_error:
                rollback_errors.append(f"{item}: {rollback_error}")
        if rollback_errors:
            raise ConfigError(
                f"{error}; rollback also failed: {'; '.join(rollback_errors)}"
            ) from error
        raise


def direct_project_selector(query: str) -> tuple[str, str] | None:
    lowered = query.lower()

    if lowered.startswith("mr:"):
        value = query[3:].strip()
        if not value:
            raise ConfigError("mr: requires a Modrinth project ID, slug or URL")
        return "modrinth", value

    if lowered.startswith("cf:"):
        value = query[3:].strip()
        if not value:
            raise ConfigError("cf: requires a CurseForge project ID, slug or URL")
        return "curseforge", value

    if lowered.startswith("url:"):
        value = query[4:].strip()
        if not value:
            raise ConfigError("url: requires an HTTPS JAR URL")
        return "url", value

    if "modrinth.com/" in lowered:
        return "modrinth", query

    if "curseforge.com/" in lowered:
        return "curseforge", query

    if urlparse(query).scheme.lower() in {"http", "https"}:
        return "url", query

    return None


def choose_provider() -> str | None:
    if not sys.stdin.isatty():
        raise ConfigError(
            "Provider selection requires a terminal. "
            "Use mr:<project>, cf:<project>, url:<https JAR URL>, or a provider URL."
        )

    print("Select provider:")
    print("  1. Modrinth")
    print("  2. CurseForge")
    print("  3. URL (self-hosted JAR)")
    print("  q. Cancel")

    while True:
        answer = input("Provider [1/2/3/q]: ").strip().lower()

        if answer in {"1", "m", "mr", "modrinth"}:
            return "modrinth"
        if answer in {"2", "c", "cf", "curseforge"}:
            return "curseforge"
        if answer in {"3", "u", "url"}:
            return "url"
        if answer in {"q", "quit", "cancel"}:
            return None

        print("Enter 1, 2, 3, or q.")


def cmd_add(args: argparse.Namespace) -> int:
    import huroshiki_core

    direct = direct_project_selector(args.query)

    if direct is not None:
        provider, selector = direct
    else:
        provider = choose_provider()
        if provider is None:
            print("Cancelled.")
            return 0
        selector = args.query

    if provider == "url":
        print("Using self-hosted URL download/install.")
    else:
        print(f"Using Packwiz {provider} search/install.")
    try:
        return huroshiki_core.add_mod_transactionally(
            huroshiki_core.project_key("pack", args.pack),
            provider,
            selector,
            args.side,
        )
    except huroshiki_core.HuroshikiError as error:
        raise ConfigError(str(error)) from error


def cmd_list(_: argparse.Namespace) -> int:
    ids = pack_ids()
    if not ids:
        print("No packs are configured")
        return 0
    print(f"{'PACK':30} {'ENABLED':8} DISPLAY NAME")
    print(f"{'-' * 30} {'-' * 8} {'-' * 30}")
    for pack_id in ids:
        config = load_pack_config(pack_id)
        enabled = "yes" if config.get("enabled", True) else "no"
        print(f"{pack_id:30} {enabled:8} {config.get('display_name', '')}")
    return 0


def cmd_list_templates(_: argparse.Namespace) -> int:
    ids = template_ids()
    if not ids:
        print("No templates are configured")
        return 0
    print(f"{'TEMPLATE':30} {'ENABLED':8} DISPLAY NAME")
    print(f"{'-' * 30} {'-' * 8} {'-' * 30}")
    for template_id in ids:
        config = load_template_config(template_id)
        enabled = "yes" if config.get("enabled", True) else "no"
        print(f"{template_id:30} {enabled:8} {config.get('display_name', '')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    import pprint

    root = get_pack_root(args.pack)
    print("== configuration ==")
    pprint.pp(load_pack_config(args.pack), sort_dicts=False)
    print("\n== Packwiz metadata ==")
    pprint.pp(read_toml(root / "source" / "pack.toml"), sort_dicts=False)
    return 0


def create_layout(root: Path) -> None:
    directories = [
        "source/mods",
        "content/common/config",
        "content/common/defaultconfigs",
        "content/common/kubejs/startup_scripts",
        "content/client/config",
        "content/client/kubejs/client_scripts",
        "content/client/resourcepacks",
        "content/client/shaderpacks",
        "content/server/config",
        "content/server/defaultconfigs",
        "content/server/kubejs/server_scripts",
    ]
    for relative in directories:
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch()


def init_packwiz_project(
    root: Path,
    *,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> None:
    if loader not in LOADER_FLAGS:
        raise ConfigError(f"Unsupported loader: {loader}")
    source = root / "source"
    source.mkdir(parents=True)
    command = [
        "packwiz",
        "--yes",
        "init",
        "--name",
        display_name,
        "--author",
        "upiscium",
        "--version",
        "0.1.0",
        "--mc-version",
        minecraft,
        "--modloader",
        loader,
        LOADER_FLAGS[loader],
        loader_version,
    ]
    run_packwiz(
        command,
        cwd=source,
        cancel_event=cancel_event,
        deadline=deadline,
    )
    create_layout(root)
    (source / ".packwizignore").write_text(
        "*.log\n*.gitkeep\n/.huroshiki-roots.json\n/crash-reports/\n/logs/\n"
        "/saves/\n/screenshots/\n/world/\n",
        encoding="utf-8",
    )
    from pack_migration_roots import write_pack_root_manifest

    write_pack_root_manifest(source, ())


def _new_pack(
    args: argparse.Namespace,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    validate_project_creation_fields(
        display_name=args.display_name,
        minecraft=args.minecraft,
        loader_version=args.loader_version,
    )
    root = get_pack_root(args.pack, must_exist=False)
    if root.exists():
        raise ConfigError(f"Pack already exists: {args.pack}")
    try:
        init_packwiz_project(
            root,
            display_name=args.display_name,
            minecraft=args.minecraft,
            loader=args.loader,
            loader_version=args.loader_version,
            cancel_event=cancel_event,
            deadline=deadline,
        )
        pack_yaml = {
            "id": args.pack,
            "display_name": args.display_name,
            "enabled": True,
            "distribution": {
                "rsync_target": f"dockge:/opt/stacks/packwiz-web/packs/{args.pack}"
            },
            "minecraft_server": {
                "ssh_host": "minecraft",
                "stack_dir": f"/opt/stacks/{args.pack}",
                "service": args.pack,
            },
        }
        (root / "pack.yaml").write_text(
            yaml.safe_dump(pack_yaml, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        (root / "pack.local.yaml.example").write_text(
            "# Copy to pack.local.yaml for machine-local overrides.\n",
            encoding="utf-8",
        )
        (root / "profiles.yaml").write_text("profiles: {}\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    print(f"Created packs/{args.pack}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    validate_project_creation_fields(
        display_name=args.display_name,
        minecraft=args.minecraft,
        loader_version=args.loader_version,
    )
    with ProjectLock(f"pack:{args.pack}", "create project"):
        return _new_pack(args)


def _new_template(args: argparse.Namespace) -> int:
    validate_project_creation_fields(
        display_name=args.display_name,
        minecraft=args.minecraft,
        loader_version=args.loader_version,
    )
    root = get_template_root(args.template, must_exist=False)
    if root.exists():
        raise ConfigError(f"Template already exists: {args.template}")
    try:
        root.mkdir(parents=True)
        template_yaml = {
            "id": args.template,
            "display_name": args.display_name,
            "enabled": True,
            "minecraft": args.minecraft,
            "loader": args.loader,
            "reference_loader_version": args.loader_version,
            "mods": [],
        }
        (root / "template.yaml").write_text(
            yaml.safe_dump(template_yaml, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    print(f"Created templates/{args.template}")
    return 0


def cmd_new_template(args: argparse.Namespace) -> int:
    validate_project_creation_fields(
        display_name=args.display_name,
        minecraft=args.minecraft,
        loader_version=args.loader_version,
    )
    with ProjectLock(f"template:{args.template}", "create project"):
        return _new_template(args)


def cmd_remove(args: argparse.Namespace) -> int:
    import huroshiki_core

    try:
        return huroshiki_core.remove_installed_mods(
            huroshiki_core.project_key("pack", args.pack),
            args.mods,
        )
    except huroshiki_core.HuroshikiError as error:
        raise ConfigError(str(error)) from error


def _normalize_bool_flag(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("must be one of true/false, yes/no, on/off, 1/0")


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _deployment_settings_from_config(
    pack_id: str,
    config: dict[str, Any],
) -> DeploymentSettings:
    ssh_host, stack_dir, service = minecraft_server_target_from_config(
        config,
        pack_id,
    )
    return DeploymentSettings(
        distribution_target_from_config(config, pack_id),
        ssh_host,
        stack_dir,
        service,
    )


def deployment_settings(pack_id: str) -> DeploymentSettings:
    return deployment_settings_baseline(pack_id).settings


def deployment_settings_baseline(pack_id: str) -> DeploymentSettingsBaseline:
    root = get_pack_root(pack_id)
    with open_config_directory(root) as directory:
        snapshot = project_config_snapshot(directory, "pack")
        committed = parse_yaml_snapshot(snapshot.committed)
        local = parse_yaml_snapshot(snapshot.local)
        config = prospective_pack_config(pack_id, committed, local)
        return DeploymentSettingsBaseline(
            _deployment_settings_from_config(pack_id, config),
            snapshot,
        )


def deployment_settings_sources(pack_id: str) -> DeploymentSettingsSources:
    baseline = deployment_settings_baseline(pack_id)
    local = parse_yaml_snapshot(baseline.snapshot.local)
    local_distribution = local.get("distribution", {})
    local_server = local.get("minecraft_server", {})
    if not isinstance(local_distribution, dict):
        local_distribution = {}
    if not isinstance(local_server, dict):
        local_server = {}
    return DeploymentSettingsSources(
        "local" if "rsync_target" in local_distribution else "committed",
        "local" if "ssh_host" in local_server else "committed",
        "local" if "stack_dir" in local_server else "committed",
        "local" if "service" in local_server else "committed",
    )


def _check_expected_project_config(
    snapshot: ProjectConfigSnapshot,
    expected: ProjectConfigSnapshot | None,
    operation: str,
) -> None:
    if expected is not None and (
        not _same_config_snapshot(snapshot.committed, expected.committed)
        or not _same_config_snapshot(snapshot.local, expected.local)
    ):
        raise ConfigError(
            f"{operation} configuration changed after it was loaded; retry the operation"
        )


def update_deployment_settings(
    pack_id: str,
    *,
    rsync_target: str | Unset = UNSET,
    ssh_host: str | Unset = UNSET,
    stack_dir: str | Unset = UNSET,
    service: str | Unset = UNSET,
    expected_baseline: ProjectConfigSnapshot | None = None,
) -> DeploymentSettings:
    normalized_rsync: str | Unset = rsync_target
    if isinstance(rsync_target, str):
        normalized_rsync = _normalize_project_text("Rsync target", rsync_target)
        try:
            normalized_rsync = validate_rsync_target(normalized_rsync)
        except ValueError as error:
            raise ConfigError(str(error)) from error
    normalized_ssh = (
        validate_ssh_target(ssh_host) if isinstance(ssh_host, str) else ssh_host
    )
    normalized_stack = (
        validate_remote_stack_dir(stack_dir)
        if isinstance(stack_dir, str)
        else stack_dir
    )
    normalized_service = (
        validate_compose_service(service) if isinstance(service, str) else service
    )

    with ProjectLock(f"pack:{pack_id}", "set deployment settings"):
        root = get_pack_root(pack_id)
        with open_config_directory(root) as directory:
            snapshot = project_config_snapshot(directory, "pack")
            _check_expected_project_config(
                snapshot,
                expected_baseline,
                "Deployment",
            )
            committed = parse_yaml_snapshot(snapshot.committed)
            local = parse_yaml_snapshot(snapshot.local)
            current = _deployment_settings_from_config(
                pack_id,
                prospective_pack_config(pack_id, committed, local),
            )
            requested = DeploymentSettings(
                current.rsync_target
                if isinstance(normalized_rsync, Unset)
                else normalized_rsync,
                current.ssh_host
                if isinstance(normalized_ssh, Unset)
                else normalized_ssh,
                current.stack_dir
                if isinstance(normalized_stack, Unset)
                else normalized_stack,
                current.service
                if isinstance(normalized_service, Unset)
                else normalized_service,
            )
            if requested == current:
                return current

            if not isinstance(normalized_rsync, Unset):
                local.setdefault("distribution", {})["rsync_target"] = normalized_rsync
            if any(
                not isinstance(value, Unset)
                for value in (normalized_ssh, normalized_stack, normalized_service)
            ):
                existing = local.setdefault("minecraft_server", {})
                if not isinstance(normalized_ssh, Unset):
                    existing["ssh_host"] = normalized_ssh
                if not isinstance(normalized_stack, Unset):
                    existing["stack_dir"] = normalized_stack
                if not isinstance(normalized_service, Unset):
                    existing["service"] = normalized_service

            prospective = prospective_pack_config(pack_id, committed, local)
            result = _deployment_settings_from_config(pack_id, prospective)
            _write_yaml_atomic(
                directory,
                local,
                expected_snapshot=snapshot.local,
                guard_snapshots=(snapshot.committed, snapshot.local),
            )
            return result


def _public_pack_url_info_from_configs(
    pack_id: str,
    committed: dict[str, Any],
    local: dict[str, Any],
) -> tuple[PublicPackUrlInfo, str | None]:
    config = prospective_pack_config(pack_id, committed, local)
    distribution = require_mapping(config, "distribution", pack_id)
    value = distribution.get("public_pack_url")
    if value is not None:
        if not isinstance(value, str):
            raise ConfigError(f"{pack_id}.distribution.public_pack_url must be a string")
        value = validate_public_pack_url(value)

    committed_distribution = committed.get("distribution", {})
    local_distribution = local.get("distribution", {})
    committed_value = (
        committed_distribution.get("public_pack_url")
        if isinstance(committed_distribution, dict)
        else None
    )
    if committed_value is not None and not isinstance(committed_value, str):
        raise ConfigError(f"{pack_id}.distribution.public_pack_url must be a string")
    if isinstance(local_distribution, dict) and "public_pack_url" in local_distribution:
        source: Literal["local", "committed", "unset"] = "local"
    elif value is not None:
        source = "committed"
    else:
        source = "unset"
    command = (
        f"java -jar packwiz-installer-bootstrap.jar {shlex.quote(value)}"
        if value is not None
        else None
    )
    return PublicPackUrlInfo(value, source, command), committed_value


def public_pack_url_baseline(pack_id: str) -> PublicPackUrlBaseline:
    root = get_pack_root(pack_id)
    with open_config_directory(root) as directory:
        snapshot = project_config_snapshot(directory, "pack")
        info, committed_value = _public_pack_url_info_from_configs(
            pack_id,
            parse_yaml_snapshot(snapshot.committed),
            parse_yaml_snapshot(snapshot.local),
        )
        return PublicPackUrlBaseline(info, committed_value, snapshot)


def public_pack_url_info(pack_id: str) -> PublicPackUrlInfo:
    return public_pack_url_baseline(pack_id).info


def set_public_pack_url(
    pack_id: str,
    url: str,
    *,
    expected_baseline: PublicPackUrlBaseline | None = None,
) -> PublicPackUrlInfo:
    normalized = validate_public_pack_url(url)
    with ProjectLock(f"pack:{pack_id}", "set Public Pack URL"):
        root = get_pack_root(pack_id)
        with open_config_directory(root) as directory:
            snapshot = project_config_snapshot(directory, "pack")
            _check_expected_project_config(
                snapshot,
                expected_baseline.snapshot if expected_baseline is not None else None,
                "Public Pack URL",
            )
            committed = parse_yaml_snapshot(snapshot.committed)
            local = parse_yaml_snapshot(snapshot.local)
            current, _ = _public_pack_url_info_from_configs(pack_id, committed, local)
            if current.value == normalized:
                return current
            local.setdefault("distribution", {})["public_pack_url"] = normalized
            result, _ = _public_pack_url_info_from_configs(pack_id, committed, local)
            _write_yaml_atomic(
                directory,
                local,
                expected_snapshot=snapshot.local,
                guard_snapshots=(snapshot.committed, snapshot.local),
            )
            return result


def clear_local_public_pack_url(
    pack_id: str,
    *,
    expected_baseline: PublicPackUrlBaseline | None = None,
) -> PublicPackUrlInfo:
    with ProjectLock(f"pack:{pack_id}", "clear local Public Pack URL"):
        root = get_pack_root(pack_id)
        with open_config_directory(root) as directory:
            snapshot = project_config_snapshot(directory, "pack")
            _check_expected_project_config(
                snapshot,
                expected_baseline.snapshot if expected_baseline is not None else None,
                "Public Pack URL",
            )
            committed = parse_yaml_snapshot(snapshot.committed)
            local = parse_yaml_snapshot(snapshot.local)
            distribution = local.get("distribution")
            if not isinstance(distribution, dict) or "public_pack_url" not in distribution:
                return _public_pack_url_info_from_configs(pack_id, committed, local)[0]
            del distribution["public_pack_url"]
            if not distribution:
                del local["distribution"]
            result, _ = _public_pack_url_info_from_configs(pack_id, committed, local)
            _write_yaml_atomic(
                directory,
                local,
                expected_snapshot=snapshot.local,
                guard_snapshots=(snapshot.committed, snapshot.local),
            )
            return result


def cmd_show_deployment(args: argparse.Namespace) -> int:
    settings = deployment_settings(args.pack)
    print("distribution:")
    print(f"  rsync_target: {settings.rsync_target}")
    print("minecraft_server:")
    print(f"  ssh_host: {settings.ssh_host}")
    print(f"  stack_dir: {settings.stack_dir}")
    print(f"  service: {settings.service}")
    return 0


def cmd_set_deployment(args: argparse.Namespace) -> int:
    if (
        args.rsync_target is None
        and args.ssh_host is None
        and args.stack_dir is None
        and args.service is None
    ):
        raise ConfigError(
            "set-deployment requires at least one of --rsync-target, --ssh-host, --stack-dir, or --service"
        )

    update_deployment_settings(
        args.pack,
        rsync_target=UNSET if args.rsync_target is None else args.rsync_target,
        ssh_host=UNSET if args.ssh_host is None else args.ssh_host,
        stack_dir=UNSET if args.stack_dir is None else args.stack_dir,
        service=UNSET if args.service is None else args.service,
    )
    print(f"Updated {display_path(get_pack_root(args.pack) / 'pack.local.yaml')}")
    return 0


def _print_public_pack_url_info(info: PublicPackUrlInfo) -> None:
    print(f"Public Pack URL: {info.value or 'not configured'}")
    print(f"Source: {info.source}")
    if info.installer_command is not None:
        print(f"Installer command: {info.installer_command}")


def cmd_show_pack_url(args: argparse.Namespace) -> int:
    info = public_pack_url_info(args.pack)
    if args.raw:
        if info.value is None:
            return 1
        print(info.value)
    else:
        _print_public_pack_url_info(info)
    return 0


def cmd_set_pack_url(args: argparse.Namespace) -> int:
    info = set_public_pack_url(args.pack, args.url)
    _print_public_pack_url_info(info)
    return 0


def cmd_clear_pack_url(args: argparse.Namespace) -> int:
    baseline = public_pack_url_baseline(args.pack)
    info = clear_local_public_pack_url(args.pack, expected_baseline=baseline)
    if baseline.info.source == "local" and info.source == "committed":
        print("Cleared local override; using committed Public Pack URL.")
    elif baseline.info.source == "local":
        print("Cleared local Public Pack URL override.")
    else:
        print("No local Public Pack URL override was configured.")
    _print_public_pack_url_info(info)
    return 0


def cmd_show_url_policy(args: argparse.Namespace) -> int:
    policy = effective_url_policy(args.kind, args.project)
    print(f"url_max_jar_size_bytes: {policy.max_size}")
    print(f"url_max_jar_size_source: {policy.max_size_source}")
    print(f"url_allow_private_networks: {str(policy.allow_private).lower()}")
    print(f"url_allow_private_networks_source: {policy.allow_private_source}")
    return 0


def effective_url_policy(kind: str, project: str) -> EffectiveUrlPolicy:
    root = get_project_root(kind, project)
    if kind == "template":
        reject_legacy_template_source(root)
    committed_name = "pack.yaml" if kind == "pack" else "template.yaml"
    local_name = "pack.local.yaml" if kind == "pack" else "template.local.yaml"
    committed = load_yaml(root / committed_name)
    local = load_yaml(root / local_name)
    config = (
        prospective_pack_config(project, committed, local)
        if kind == "pack"
        else prospective_template_config(project, committed, local)
    )
    max_size_source = (
        "local"
        if "url_max_jar_size_bytes" in local
        else "committed"
        if "url_max_jar_size_bytes" in committed
        else "default"
    )
    allow_private_source = (
        "local"
        if "url_allow_private_networks" in local
        else "committed"
        if "url_allow_private_networks" in committed
        else "default"
    )
    return EffectiveUrlPolicy(
        max_size=config.get(
            "url_max_jar_size_bytes",
            DEFAULT_URL_MAX_JAR_SIZE_BYTES,
        ),
        max_size_source=max_size_source,
        allow_private=config.get("url_allow_private_networks", False),
        allow_private_source=allow_private_source,
    )


def cmd_set_url_policy(args: argparse.Namespace) -> int:
    if args.max_size is None and args.allow_private_networks is None:
        raise ConfigError(
            "set-url-policy requires --max-size and/or --allow-private-networks"
        )
    if args.max_size is not None and (
        isinstance(args.max_size, bool)
        or not isinstance(args.max_size, int)
        or args.max_size <= 0
    ):
        raise ConfigError("url_max_jar_size_bytes must be a positive integer")

    kind = args.kind
    project = args.project

    with ProjectLock(f"{kind}:{project}", "set URL policy"):
        root = get_project_root(kind, project)
        local_path = root / (
            "pack.local.yaml" if kind == "pack" else "template.local.yaml"
        )
        with open_config_directory(root) as directory:
            snapshot = project_config_snapshot(directory, kind)
            committed = parse_yaml_snapshot(snapshot.committed)
            local = parse_yaml_snapshot(snapshot.local)
            if args.max_size is not None:
                local["url_max_jar_size_bytes"] = args.max_size
            if args.allow_private_networks is not None:
                local["url_allow_private_networks"] = args.allow_private_networks
            if kind == "pack":
                prospective_pack_config(project, committed, local)
            else:
                prospective_template_config(project, committed, local)
            _write_yaml_atomic(
                directory,
                local,
                expected_snapshot=snapshot.local,
                guard_snapshots=(snapshot.committed, snapshot.local),
            )

    print(f"Updated {display_path(local_path)}")
    return 0


def cmd_show_template_loader_version(args: argparse.Namespace) -> int:
    config = load_template_config(args.template)
    print(config["reference_loader_version"])
    return 0


def cmd_set_template_loader_version(args: argparse.Namespace) -> int:
    reference_loader_version = _normalize_project_text(
        "Loader version", args.loader_version
    )

    with ProjectLock(f"template:{args.template}", "set template loader version"):
        root = get_template_root(args.template)
        path = root / "template.yaml"
        with open_config_directory(root) as directory:
            snapshot = project_config_snapshot(directory, "template")
            committed = parse_yaml_snapshot(snapshot.committed)
            local = parse_yaml_snapshot(snapshot.local)
            committed["reference_loader_version"] = reference_loader_version
            prospective_template_config(args.template, committed, local)
            _write_yaml_atomic(
                directory,
                committed,
                expected_snapshot=snapshot.committed,
                guard_snapshots=(snapshot.committed, snapshot.local),
            )

    print(f"Updated {display_path(root / 'template.yaml')}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    import huroshiki_core

    try:
        result = huroshiki_core.update_all(
            huroshiki_core.project_key("pack", args.pack),
            allow_partial=args.allow_partial,
        )
        if result.partial:
            if args.build:
                print(
                    "Skipping build because only a partial update was applied.",
                    file=sys.stderr,
                )
            return 2
        if result.failures:
            return result.failures[0].error_returncode or 1
        if not args.build:
            return 0
        return build_pack(args.pack)
    except KeyboardInterrupt:
        print("Update preparation cancelled.", file=sys.stderr)
        return 130
    except huroshiki_core.HuroshikiError as error:
        raise ConfigError(str(error)) from error


def _print_loader_migration_preview(preview: Any) -> None:
    print(f"Minecraft: {preview.minecraft}")
    print(f"Loader: {preview.loader}")
    print(f"Loader version: {preview.old_version} -> {preview.new_version}")
    print("Changed files:")
    if preview.changes:
        for change in preview.changes:
            print(f"  {change.relative_path}")
    else:
        print("  (none)")
    if preview.warnings:
        print("Warnings:")
        for warning in preview.warnings:
            print(f"  {warning}")


def cmd_loader_version(args: argparse.Namespace) -> int:
    import huroshiki_core

    operation = None
    try:
        operation = huroshiki_core.prepare_loader_migration(
            huroshiki_core.project_key("pack", args.pack),
            args.version,
        )
        _print_loader_migration_preview(operation.preview)
        if args.apply:
            operation.apply()
            print("Loader migration applied.")
        else:
            operation.discard()
            print("Dry run only; no files were changed.")
        return 0
    except KeyboardInterrupt:
        if operation is not None:
            operation.cancel()
        print("Loader migration cancelled.", file=sys.stderr)
        return 130
    except huroshiki_core.HuroshikiError as error:
        raise ConfigError(str(error)) from error
    finally:
        if operation is not None:
            operation.discard()


def _exact_artifact_argument(args: argparse.Namespace, provider: str) -> str:
    values = [
        ("--artifact-id", args.artifact_id),
        ("--file-id", args.file_id),
        ("--version-id", args.version_id),
    ]
    provided = [(flag, value) for flag, value in values if value is not None]
    if len(provided) != 1:
        raise ConfigError(
            "Specify exactly one of --artifact-id, --file-id, or --version-id"
        )
    flag, value = provided[0]
    if flag == "--file-id" and provider != "curseforge":
        raise ConfigError("--file-id is available only for CurseForge")
    if flag == "--version-id" and provider != "modrinth":
        raise ConfigError("--version-id is available only for Modrinth")
    return value


def _print_exact_mod_version_preview(preview: Any) -> None:
    print(f"Identity: {preview.identity}")
    print(f"Version: {preview.old_version} -> {preview.new_version}")
    print(f"Artifact ID: {preview.old_artifact_id} -> {preview.new_artifact_id}")
    print(f"Added dependencies: {preview.added_dependencies}")
    for identity in preview.added_dependency_identities:
        print(f"  + {identity}")
    print(f"Removed dependencies: {preview.removed_dependencies}")
    for identity in preview.removed_dependency_identities:
        print(f"  - {identity}")
    print("Changed files:")
    if preview.changes:
        for change in preview.changes:
            print(f"  {change.relative_path}")
    else:
        print("  (none)")


def cmd_version(args: argparse.Namespace) -> int:
    import huroshiki_core

    transaction = None
    try:
        if ":" not in args.identity:
            raise ConfigError("MOD identity must be provider:project-id")
        provider, project_id = args.identity.split(":", 1)
        if provider not in {"curseforge", "modrinth"} or not project_id:
            raise ConfigError(
                "MOD identity must use curseforge:<project-id> or modrinth:<project-id>"
            )
        artifact_id = _exact_artifact_argument(args, provider)
        if provider == "modrinth":
            project_id = huroshiki_core.canonical_modrinth_id(
                project_id, "Modrinth project ID"
            )
            artifact_id = huroshiki_core.canonical_modrinth_id(
                artifact_id, "Modrinth version ID"
            )
        selection = huroshiki_core.ExactModArtifactSelection(
            provider, project_id, artifact_id
        )
        transaction = huroshiki_core.PackTransaction.create(
            huroshiki_core.project_key("pack", args.pack)
        )
        preview = transaction.prepare_exact_mod_version(selection)
        _print_exact_mod_version_preview(preview)
        if args.apply:
            transaction.apply()
            print("Exact MOD version applied.")
        else:
            transaction.discard()
            print("Dry run only; no files were changed.")
        return 0
    except KeyboardInterrupt:
        print("Exact MOD version selection cancelled.", file=sys.stderr)
        return 130
    except huroshiki_core.HuroshikiError as error:
        raise ConfigError(str(error)) from error
    finally:
        if transaction is not None and transaction.active:
            try:
                transaction.discard()
            except huroshiki_core.HuroshikiError as cleanup_error:
                raise ConfigError(
                    f"Exact MOD version transaction cleanup failed: {cleanup_error}"
                ) from cleanup_error


def _template_import_resolution(path: Path, plan: Any) -> Any:
    from template_import import ImportConflictResolution, resolve_template_import_plan
    from template_merge import TemplateMergeError

    data = load_yaml(path)
    if data.get("version") in {1, 2, 3}:
        raise ConfigError(
            f"Template import resolution version {data.get('version')} is no longer "
            "supported; regenerate the resolution against the current option-based plan"
        )
    if data.get("version") != 4:
        raise ConfigError("Template import resolution version must be 4")
    if data.get("plan_digest") != plan.plan_digest:
        raise ConfigError("Template import resolution has a stale plan digest")
    raw_names = data.get("name_conflicts", {})
    raw_urls = data.get("url_selector_conflicts", {})
    raw_logical = data.get("logical_identity_conflicts", {})
    raw_actual = data.get("actual_identity_conflicts", {})
    raw_sides = data.get("side_conflicts", {})
    if not all(
        isinstance(value, dict)
        for value in (raw_names, raw_urls, raw_logical, raw_actual, raw_sides)
    ):
        raise ConfigError("Template import resolution conflicts must be mappings")

    def options(raw_conflicts: dict[object, object]) -> dict[str, Any]:
        result = {}
        for key, value in raw_conflicts.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ConfigError("Invalid template import conflict resolution")
            option_keys = value.get("options")
            if not isinstance(option_keys, list) or not all(
                isinstance(item, str) for item in option_keys
            ):
                raise ConfigError("Template import conflict options must be strings")
            result[key] = ImportConflictResolution(
                tuple(option_keys),
                value.get("acknowledge_duplicate_risk") is True,
            )
        return result

    names = options(raw_names)
    urls = options(raw_urls)
    logical = options(raw_logical)
    actual = options(raw_actual)
    sides = {}
    for key, decision in raw_sides.items():
        if not isinstance(key, str) or ":" not in key or not isinstance(decision, str):
            raise ConfigError("Invalid template import side conflict resolution")
        sides[tuple(key.split(":", 1))] = decision
    try:
        return resolve_template_import_plan(
            plan,
            name_resolutions=names,
            url_selector_resolutions=urls,
            logical_identity_resolutions=logical,
            actual_identity_resolutions=actual,
            side_decisions=sides,
        )
    except TemplateMergeError as error:
        raise ConfigError(str(error)) from error


def _template_import_candidate_payload(plan: Any, candidate: Any) -> dict[str, Any]:
    verification = (
        None
        if candidate.origin_kind == "pack"
        else next(
            (
                item
                for item in plan.verifications
                if item.selector_identity == candidate.selector_identity
            ),
            None,
        )
    )
    if verification is None:
        status = "installed"
        actual_identity = candidate.actual_identity
        error = None
        fingerprint = None
    else:
        status = "verified" if verification.succeeded else "failed"
        actual_identity = verification.actual_identity
        error = verification.error
        fingerprint = verification.closure_fingerprint
    return {
        "selection_key": candidate.selection_key,
        "candidate_key": candidate.candidate_key,
        "origin_kind": candidate.origin_kind,
        "origin_id": candidate.origin_id,
        "status": status,
        "actual_identity": actual_identity,
        "closure_fingerprint": fingerprint,
        "error": error,
    }


def _template_import_conflict_payload(plan: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        label: [
            {
                "key": conflict.key,
                "options": [
                    {
                        "option_key": option.option_key,
                        "candidates": [
                            _template_import_candidate_payload(plan, candidate)
                            for candidate in option.candidates
                        ],
                    }
                    for option in conflict.options
                ],
            }
            for conflict in conflicts
        ]
        for label, conflicts in (
            ("name", plan.name_conflicts),
            ("url_selector", plan.url_selector_conflicts),
            ("logical_identity", plan.logical_identity_conflicts),
            ("actual_identity", plan.actual_identity_conflicts),
        )
    }


def cmd_apply_template(args: argparse.Namespace) -> int:
    import huroshiki_core
    from template_import import resolve_template_import_plan
    from template_merge import TemplateMergeError

    session = None
    operation = None
    try:
        session = huroshiki_core.TemplateImportSession.create(
            huroshiki_core.project_key("pack", args.pack),
            args.templates,
        )
        plan = session.plan
        if args.resolution is None:
            if plan.requires_resolution:
                conflict_payload = _template_import_conflict_payload(plan)
                if args.json:
                    print(
                        json.dumps(
                            {
                                "plan_digest": plan.plan_digest,
                                "requested_roots": [
                                    item.candidate_key
                                    for item in plan.template_candidates
                                ],
                                "resolved_roots": [],
                                "added_dependencies": [],
                                "removed": [],
                                "side_changes": [],
                                "conflicts": conflict_payload,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 2
                print("Template import conflicts require a resolution file:", file=sys.stderr)
                print("version: 4", file=sys.stderr)
                print(f'plan_digest: "{plan.plan_digest}"', file=sys.stderr)
                for label, conflicts in (
                    ("name_conflicts", plan.name_conflicts),
                    ("url_selector_conflicts", plan.url_selector_conflicts),
                    (
                        "logical_identity_conflicts",
                        plan.logical_identity_conflicts,
                    ),
                    ("actual_identity_conflicts", plan.actual_identity_conflicts),
                ):
                    print(f"{label}:", file=sys.stderr)
                    if not conflicts:
                        print("  {}", file=sys.stderr)
                    for conflict in conflicts:
                        print(f'  "{conflict.key}":', file=sys.stderr)
                        print("    options:", file=sys.stderr)
                        for option in conflict.options:
                            print("    # source option members:", file=sys.stderr)
                            for candidate in option.candidates:
                                status = _template_import_candidate_payload(plan, candidate)
                                detail = status["status"]
                                if status["actual_identity"] is not None:
                                    detail += ":" + ":".join(status["actual_identity"])
                                if status["error"] is not None:
                                    detail += f": {status['error']}"
                                print(
                                    f"    # - {candidate.selection_key}: {detail}",
                                    file=sys.stderr,
                                )
                                print(
                                    f"    #   candidate: {candidate.candidate_key}",
                                    file=sys.stderr,
                                )
                            print(f'      - "{option.option_key}"', file=sys.stderr)
                        print(
                            "    acknowledge_duplicate_risk: false",
                            file=sys.stderr,
                        )
                print("side_conflicts:", file=sys.stderr)
                if not plan.side_conflicts:
                    print("  {}", file=sys.stderr)
                for conflict in plan.side_conflicts:
                    key = f"{conflict.identity[0]}:{conflict.identity[1]}"
                    print(f'  "{key}": keep_pack', file=sys.stderr)
                return 2
            resolved = resolve_template_import_plan(plan)
        else:
            resolved = _template_import_resolution(Path(args.resolution), plan)
        operation = huroshiki_core.TemplateImportOperation(session, resolved)
        operation.run()
        if operation.error is not None:
            raise operation.error
        if operation.cancelled:
            print("Template import cancelled.", file=sys.stderr)
            return 130
        if operation.preview is None:
            raise ConfigError("Template import was cancelled")
        preview = operation.preview
        if args.json:
            print(
                json.dumps(
                    {
                        "plan_digest": plan.plan_digest,
                        "selected_options": resolved.selected_option_keys,
                        "requested_roots": [
                            item.candidate_key for item in plan.template_candidates
                        ],
                        "resolved_roots": [
                            {
                                "selection_key": item.selection_key,
                                "candidate_key": item.candidate_key,
                                "requested_identity": item.requested_identity,
                                "actual_identity": item.actual_identity,
                                "relative_path": str(item.relative_path),
                                "filename": item.filename,
                            }
                            for item in preview.added_roots
                        ],
                        "added_dependencies": [
                            f"{item.provider}:{item.project_id}"
                            for item in preview.added_dependencies
                        ],
                        "side_changes": preview.side_changes,
                        "removed": [item.candidate_key for item in preview.removed],
                        "changed_files": [
                            str(item.relative_path) for item in preview.changes
                        ],
                        "warnings": preview.warnings,
                        "conflicts": _template_import_conflict_payload(plan),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Template import plan: {plan.plan_digest}")
            print("Added roots:")
            for item in preview.added_roots:
                print(
                    f"  {item.requested_name} ({item.candidate_key}) -> "
                    f"{item.actual_identity[0]}:{item.actual_identity[1]}"
                )
            print("Added dependencies:")
            for item in preview.added_dependencies:
                print(f"  {item.name} ({item.provider}:{item.project_id})")
            print("Changed files:")
            for item in preview.changes:
                print(f"  {item.relative_path}")
        if args.apply:
            operation.apply()
            if not args.json:
                print("Template import applied.")
        else:
            operation.discard()
            if not args.json:
                print("Dry run only; no files were changed.")
        return 0
    except KeyboardInterrupt:
        if operation is not None:
            operation.cancel()
        elif session is not None:
            session.cancel_event.set()
        print("Template import cancelled.", file=sys.stderr)
        return 130
    except huroshiki_core.LoaderMigrationCancelled:
        print("Template import cancelled.", file=sys.stderr)
        return 130
    except (huroshiki_core.HuroshikiError, ConfigError, TemplateMergeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if operation is not None:
            operation.discard()
        elif session is not None:
            session.discard()


def cmd_side(args: argparse.Namespace) -> int:
    with ProjectLock(f"pack:{args.pack}", "side"):
        side = normalize_side(args.side)
        source = (get_pack_root(args.pack) / "source").resolve()
        target = (source / args.metadata_file).resolve()
        if source not in target.parents:
            raise ConfigError("Metadata path escaped source/")
        if not target.is_file() or not target.name.endswith(".pw.toml"):
            raise ConfigError(f"Metadata file not found: {target}")
        set_side_and_refresh(source, target, side)
    print(f"{args.pack}/{target.relative_to(source)}: side = {side}")
    return 0


def modrinth_project_reference(selector: str) -> str:
    value = selector.strip()
    if value.lower().startswith("mr:"):
        value = value[3:].strip()
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.hostname not in {
            "modrinth.com",
            "www.modrinth.com",
        }:
            raise ConfigError(f"Invalid Modrinth project URL: {selector!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError(f"Invalid Modrinth project URL: {selector!r}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] not in {"mod", "project"}:
            raise ConfigError(f"Invalid Modrinth project URL: {selector!r}")
        value = unquote(parts[1]).strip()
    if not value or any(character.isspace() for character in value):
        raise ConfigError(f"Invalid Modrinth project selector: {selector!r}")
    return value


def resolve_modrinth_identity(selector: str) -> str:
    project = modrinth_project_reference(selector)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().with_name("provider_lookup.py")),
                "modrinth",
                "resolve",
                project,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=35,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfigError(
            f"Could not resolve Modrinth project {project!r}: {error}"
        ) from error
    if result.returncode != 0:
        message = (result.stderr or "provider lookup failed").strip()
        raise ConfigError(f"Could not resolve Modrinth project {project!r}: {message}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ConfigError("Provider lookup returned invalid JSON") from error
    if not isinstance(data, dict):
        raise ConfigError("Provider lookup returned a non-object response")
    project_id = data.get("id")
    if project_id is None:
        project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ConfigError(f"No Modrinth project ID returned for {project!r}")
    return project_id


def resolve_modrinth(project: str) -> str:
    return resolve_modrinth_identity(project)


def find_metadata(source: Path, provider: str, project_id: str | int) -> Path | None:
    for path in metadata_files(source):
        update = read_toml(path).get("update", {})
        if provider == "modrinth":
            value = update.get("modrinth", {}).get("mod-id")
            if str(value or "") == str(project_id):
                return path
        else:
            value = update.get("curseforge", {}).get("project-id")
            if value == project_id:
                return path
    return None


def load_profiles(pack_root: Path) -> dict[str, Any]:
    profiles = load_yaml(PACKAGE_DATA / "profiles.yaml")
    managed_profiles = SHARED / "profiles.yaml"
    if managed_profiles != PACKAGE_DATA / "profiles.yaml":
        profiles = merge(profiles, load_yaml(managed_profiles))
    profiles = merge(profiles, load_yaml(pack_root / "profiles.yaml")).get(
        "profiles", {}
    )
    if not isinstance(profiles, dict):
        raise ConfigError("Merged profiles must be a mapping")
    return profiles


def cmd_profile(args: argparse.Namespace) -> int:
    import huroshiki_core

    profiles = load_profiles(get_pack_root(args.pack))
    try:
        huroshiki_core.apply_profiles(
            huroshiki_core.project_key("pack", args.pack),
            profiles,
            args.names,
            on_profile=lambda name: print(f"== Applying {name} to {args.pack} =="),
            on_entry=lambda _name, path, side: print(f"  {path} -> {side}"),
        )
    except huroshiki_core.HuroshikiError as error:
        raise ConfigError(str(error)) from error
    return 0


def project_versions(source: Path) -> tuple[str, str, str]:
    data = read_toml(source / "pack.toml")
    versions = data.get("versions", {})
    if not isinstance(versions, dict):
        raise ConfigError(f"{source}/pack.toml versions must be a mapping")
    minecraft = versions.get("minecraft")
    if not isinstance(minecraft, str) or not minecraft.strip():
        raise ConfigError(
            f"{source}/pack.toml versions.minecraft must be a non-empty string"
        )
    loaders = [loader for loader in LOADER_FLAGS if loader in versions]
    if len(loaders) != 1:
        raise ConfigError(
            f"{source}/pack.toml must define exactly one supported loader"
        )
    loader = loaders[0]
    loader_version = versions[loader]
    if not isinstance(loader_version, str) or not loader_version.strip():
        raise ConfigError(
            f"{source}/pack.toml versions.{loader} must be a non-empty string"
        )
    return minecraft.strip(), loader, loader_version.strip()


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def project_directories(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted(
        path
        for path in parent.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def validation_yaml(path: Path, errors: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"{display_path(path)}: missing required file")
        return None
    try:
        return load_yaml(path)
    except (ConfigError, OSError, UnicodeError, yaml.YAMLError) as error:
        errors.append(f"{display_path(path)}: {error}")
        return None


def validation_text(
    config: dict[str, Any], key: str, path: Path, errors: list[str]
) -> str | None:
    try:
        return require_text(config, key, display_path(path))
    except ConfigError as error:
        errors.append(f"{display_path(path)}: {error}")
        return None


def validate_manifest_identity(
    root: Path,
    config: dict[str, Any],
    manifest: Path,
    errors: list[str],
) -> None:
    project_id = validation_text(config, "id", manifest, errors)
    if project_id is None:
        return
    try:
        validate_project_id(project_id)
    except ConfigError as error:
        errors.append(f"{display_path(manifest)}: id {project_id!r}: {error}")
    if project_id != root.name:
        errors.append(
            f"{display_path(manifest)}: id {project_id!r} must match directory "
            f"name {root.name!r}"
        )


def validate_enabled(config: dict[str, Any], path: Path, errors: list[str]) -> None:
    if not isinstance(config.get("enabled"), bool):
        errors.append(f"{display_path(path)}: enabled must be a boolean")


def validate_url_size_limit(
    config: dict[str, Any], path: Path, errors: list[str]
) -> None:
    value = config.get("url_max_jar_size_bytes")
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        errors.append(
            f"{display_path(path)}: url_max_jar_size_bytes must be a positive integer"
        )


def validate_deployment_config(
    config: dict[str, Any], path: Path, errors: list[str]
) -> None:
    try:
        _validate_prospective_deployment(config, display_path(path))
    except ConfigError as error:
        errors.append(str(error))


def validate_packwiz_versions(source: Path, errors: list[str]) -> None:
    pack_toml = source / "pack.toml"
    try:
        data = read_toml(pack_toml)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{display_path(pack_toml)}: {error}")
        return
    versions = data.get("versions")
    if not isinstance(versions, dict):
        errors.append(f"{display_path(pack_toml)}: versions must be a mapping")
        return

    minecraft = versions.get("minecraft")
    if not isinstance(minecraft, str) or not minecraft.strip():
        errors.append(
            f"{display_path(pack_toml)}: versions.minecraft must be a non-empty string"
        )
    loaders = [loader for loader in LOADER_FLAGS if loader in versions]
    if len(loaders) != 1:
        errors.append(
            f"{display_path(pack_toml)}: must define exactly one supported loader"
        )
    else:
        loader = loaders[0]
        loader_version = versions[loader]
        if not isinstance(loader_version, str) or not loader_version.strip():
            errors.append(
                f"{display_path(pack_toml)}: versions.{loader} must be a "
                "non-empty string"
            )


def pack_source_entry_issues(
    source: Path,
    checkpoint: Callable[[], None] | None = None,
) -> list[tuple[Path, str]]:
    """Inspect a Packwiz source without following any filesystem links."""
    if checkpoint is not None:
        checkpoint()
    try:
        root_metadata = os.stat(source, follow_symlinks=False)
    except FileNotFoundError:
        return []
    except OSError as error:
        return [(Path("."), f"could not inspect safely: {error}")]
    if stat.S_ISLNK(root_metadata.st_mode):
        try:
            target = os.readlink(source)
        except OSError as error:
            target = f"<unreadable: {error}>"
        return [(Path("."), f"symlink is not allowed -> {target}")]
    if not stat.S_ISDIR(root_metadata.st_mode):
        return [(Path("."), "source must be an ordinary directory")]

    try:
        root_fd = os.open(
            source,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        return [(Path("."), f"could not inspect safely: {error}")]
    try:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            return [(Path("."), "source was replaced while being opened")]
        issues = (
            pack_source_fd_entry_issues(root_fd, checkpoint)
            if checkpoint is not None
            else pack_source_fd_entry_issues(root_fd)
        )
        try:
            current = os.stat(source, follow_symlinks=False)
        except OSError as error:
            issues.append((Path("."), f"source was replaced while scanning: {error}"))
        else:
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                issues.append((Path("."), "source was replaced while scanning"))
        return issues
    finally:
        os.close(root_fd)


def pack_source_fd_entry_issues(
    root_fd: int,
    checkpoint: Callable[[], None] | None = None,
) -> list[tuple[Path, str]]:
    """Inspect an already pinned Packwiz source directory."""
    issues: list[tuple[Path, str]] = []
    if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
        return [(Path("."), "source must be an ordinary directory")]

    def scan(directory_fd: int, relative: Path) -> None:
        if checkpoint is not None:
            checkpoint()
        try:
            with os.scandir(directory_fd) as iterator:
                names = []
                for entry in iterator:
                    if checkpoint is not None:
                        checkpoint()
                    names.append(entry.name)
                names.sort()
        except OSError as error:
            issues.append((relative, f"could not inspect safely: {error}"))
            return
        for name in names:
            if checkpoint is not None:
                checkpoint()
            item_relative = relative / name
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    try:
                        target = os.readlink(name, dir_fd=directory_fd)
                    except OSError as error:
                        target = f"<unreadable: {error}>"
                    issues.append(
                        (item_relative, f"symlink is not allowed -> {target}")
                    )
                elif stat.S_ISDIR(mode):
                    try:
                        child_fd = os.open(
                            name,
                            os.O_RDONLY
                            | os.O_DIRECTORY
                            | os.O_NOFOLLOW
                            | os.O_CLOEXEC,
                            dir_fd=directory_fd,
                        )
                    except OSError as error:
                        issues.append(
                            (item_relative, f"entry changed while opening: {error}")
                        )
                        continue
                    try:
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (
                            metadata.st_dev,
                            metadata.st_ino,
                        ):
                            issues.append(
                                (item_relative, "entry was replaced while opening")
                            )
                            continue
                        scan(child_fd, item_relative)
                        try:
                            current = os.stat(
                                name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        except OSError as error:
                            issues.append(
                                (
                                    item_relative,
                                    f"entry was replaced while scanning: {error}",
                                )
                            )
                        else:
                            if (
                                not stat.S_ISDIR(current.st_mode)
                                or (current.st_dev, current.st_ino)
                                != (opened.st_dev, opened.st_ino)
                            ):
                                issues.append(
                                    (item_relative, "entry was replaced while scanning")
                                )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(mode):
                    try:
                        file_fd = os.open(
                            name,
                            os.O_RDONLY
                            | os.O_NOFOLLOW
                            | os.O_CLOEXEC
                            | os.O_NONBLOCK,
                            dir_fd=directory_fd,
                        )
                    except OSError as error:
                        issues.append(
                            (item_relative, f"entry changed while opening: {error}")
                        )
                        continue
                    try:
                        opened = os.fstat(file_fd)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or (opened.st_dev, opened.st_ino)
                            != (metadata.st_dev, metadata.st_ino)
                        ):
                            issues.append(
                                (item_relative, "entry was replaced while opening")
                            )
                    finally:
                        os.close(file_fd)
                elif not stat.S_ISREG(mode):
                    issues.append(
                        (item_relative, "special filesystem entry is not allowed")
                    )
            except OSError as error:
                issues.append(
                    (item_relative, f"could not inspect safely: {error}")
                )

    scan(root_fd, Path("."))
    return issues


def validate_pack_directory(root: Path) -> list[str]:
    errors: list[str] = []
    manifest = root / "pack.yaml"
    config = validation_yaml(manifest, errors)
    local_path = root / "pack.local.yaml"
    local: dict[str, Any] | None = {}
    if local_path.exists():
        local = validation_yaml(local_path, errors)
    local_is_valid = local is not None
    if local is not None:
        try:
            validate_local_config("pack", local_path, local)
        except ConfigError as error:
            errors.append(str(error))
            local_is_valid = False

    try:
        validate_project_id(root.name)
    except ConfigError as error:
        errors.append(f"{display_path(root)}: invalid directory name: {error}")

    if config is not None:
        if "url_allow_private_networks" in config:
            errors.append(
                f"{display_path(manifest)}: url_allow_private_networks is "
                "machine-local only; move it to pack.local.yaml"
            )
        effective = merge(
            config, local if local_is_valid and local is not None else {}
        )
        validate_manifest_identity(root, effective, manifest, errors)
        validation_text(effective, "display_name", manifest, errors)
        validate_enabled(effective, manifest, errors)
        validate_url_size_limit(effective, manifest, errors)
        validate_deployment_config(effective, manifest, errors)

    source = root / "source"
    source_issues = pack_source_entry_issues(source)
    for relative, message in source_issues:
        path = source if relative == Path(".") else source / relative
        errors.append(f"{display_path(path)}: {message}")
    source_is_safe = not source_issues
    pack_toml = source / "pack.toml"
    index_toml = source / "index.toml"
    for required in (pack_toml, index_toml):
        if source.is_symlink() or required.is_symlink() or not required.is_file():
            errors.append(f"{display_path(required)}: missing required file")

    if source_is_safe and pack_toml.is_file():
        validate_packwiz_versions(source, errors)

    if source_is_safe and source.is_dir():
        filename_owners: dict[str, tuple[Path, str]] = {}
        for metadata in metadata_files(source):
            try:
                document = read_toml(metadata)
                side = document.get("side")
                if side_validation_error(side) is not None:
                    errors.append(
                        f"{display_path(metadata)}: side must be client, server, or both"
                    )
                filename = str(document.get("filename", ""))
                filename_key = portable_basename_key(
                    filename, context="Metadata filename"
                )
                previous = filename_owners.get(filename_key)
                if previous is not None:
                    previous_path, previous_filename = previous
                    errors.append(
                        f"{display_path(metadata)}: portable filename collision for "
                        f"{filename!r} with {display_path(previous_path)} "
                        f"({previous_filename!r})"
                    )
                else:
                    filename_owners[filename_key] = (metadata, filename)
            except PortablePathError as error:
                errors.append(f"{display_path(metadata)}: {error}")
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
                errors.append(f"{display_path(metadata)}: {error}")
    for issue in scan_content_overlays(root / "content").issues:
        path = root / "content"
        if issue.relative_path != Path("."):
            path /= issue.relative_path
        errors.append(f"{display_path(path)}: {issue.message}")
    return errors


def validate_template_directory(root: Path) -> list[str]:
    errors: list[str] = []
    source = root / "source"
    if source.exists() or source.is_symlink():
        errors.append(
            f"{display_path(source)}: legacy template source is not supported; "
            "remove it and define the template only in template.yaml"
        )
    manifest = root / "template.yaml"
    config = validation_yaml(manifest, errors)
    local_path = root / "template.local.yaml"
    local: dict[str, Any] | None = {}
    if local_path.exists():
        local = validation_yaml(local_path, errors)
    local_is_valid = local is not None
    if local is not None:
        try:
            validate_local_config("template", local_path, local)
        except ConfigError as error:
            errors.append(str(error))
            local_is_valid = False
    try:
        validate_project_id(root.name)
    except ConfigError as error:
        errors.append(f"{display_path(root)}: invalid directory name: {error}")
    if config is None:
        return errors
    committed = config
    if "url_allow_private_networks" in committed:
        errors.append(
            f"{display_path(manifest)}: url_allow_private_networks is machine-local "
            "only; move it to template.local.yaml"
        )
    config = merge(
        committed, local if local_is_valid and local is not None else {}
    )

    validate_manifest_identity(root, committed, manifest, errors)
    validation_text(committed, "display_name", manifest, errors)
    validate_enabled(committed, manifest, errors)
    validation_text(committed, "minecraft", manifest, errors)
    validation_text(committed, "loader", manifest, errors)
    validation_text(committed, "reference_loader_version", manifest, errors)
    if not isinstance(committed.get("mods"), list):
        errors.append(f"{display_path(manifest)}: mods must be a list")

    validate_manifest_identity(root, config, manifest, errors)
    validation_text(config, "display_name", manifest, errors)
    validate_enabled(config, manifest, errors)
    validation_text(config, "minecraft", manifest, errors)
    loader = validation_text(config, "loader", manifest, errors)
    if loader is not None and loader.lower() not in LOADER_FLAGS:
        errors.append(
            f"{display_path(manifest)}: loader must be one of "
            f"{', '.join(sorted(LOADER_FLAGS))}"
        )
    validation_text(config, "reference_loader_version", manifest, errors)
    validate_url_size_limit(config, manifest, errors)

    mods = committed.get("mods")
    if not isinstance(mods, list):
        return errors
    for index, entry in enumerate(mods):
        context = f"mods[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{display_path(manifest)}: {context} must be a mapping")
            continue
        provider = str(entry.get("provider", "")).strip().lower()
        if provider not in {"modrinth", "curseforge", "url"}:
            errors.append(
                f"{display_path(manifest)}: {context}.provider must be modrinth, "
                "curseforge, or url"
            )
        if not str(entry.get("project_id", "")).strip():
            errors.append(
                f"{display_path(manifest)}: {context}.project_id must be a "
                "non-empty string"
            )
        side = entry.get("side")
        if side_validation_error(side) is not None:
            errors.append(
                f"{display_path(manifest)}: {context}.side must be client, server, "
                "or both"
            )
        if provider == "url":
            url = str(entry.get("url", "")).strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(
                    f"{display_path(manifest)}: {context}.url must be a public "
                    "http(s) URL"
                )
            elif not unquote(Path(parsed.path).name).lower().endswith(".jar"):
                errors.append(
                    f"{display_path(manifest)}: {context}.url must point to a .jar file"
                )
    return errors


def print_validation_result(errors: list[str], subject: str) -> int:
    if errors:
        print(f"Validation failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Validated {subject}")
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    packs = project_directories(PACKS)
    templates = project_directories(TEMPLATES)
    for root in packs:
        errors.extend(validate_pack_directory(root))
    for root in templates:
        errors.extend(validate_template_directory(root))
    return print_validation_result(
        errors, f"{len(packs)} pack(s) and {len(templates)} template(s)"
    )


def cmd_validate_for(args: argparse.Namespace) -> int:
    root = get_pack_root(args.pack)
    return print_validation_result(validate_pack_directory(root), f"pack {args.pack}")


def copy_metadata(source: Path, destination: Path) -> None:
    for metadata in metadata_files(source):
        relative = metadata.relative_to(source)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata, output)


def swap_directory(staged: Path, destination: Path, backup: Path) -> None:
    had_destination = destination.exists()
    try:
        if had_destination:
            destination.replace(backup)
        staged.replace(destination)
    except BaseException as swap_error:
        if had_destination and backup.exists():
            try:
                if destination.exists():
                    shutil.rmtree(destination)
                backup.replace(destination)
            except BaseException as rollback_error:
                raise ConfigError(
                    f"Failed to replace {destination} and restore the previous build; "
                    f"it remains at {backup}: {rollback_error}"
                ) from swap_error
        raise


def build_target(
    root: Path,
    target: str,
    destination: Path | None = None,
    *,
    refresh: bool = True,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> list[str]:
    operation_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
    )
    source = root / "source"
    workspace: Path | None = None
    if destination is None:
        workspace = Path(tempfile.mkdtemp(prefix=".build-target-", dir=root))
        destination = workspace / target

    preserve_workspace = False
    try:
        destination.mkdir(parents=True)
        shutil.copy2(source / "pack.toml", destination / "pack.toml")
        (destination / "index.toml").write_text(
            'hash-format = "sha256"\n',
            encoding="utf-8",
        )
        copy_metadata(source, destination)

        errors: list[str] = []
        for metadata in metadata_files(destination):
            side = read_toml(metadata).get("side")
            if side_validation_error(side) is not None:
                errors.append(
                    f"{metadata.relative_to(destination)} has no valid side"
                )
                continue
            if side not in TARGET_SIDES[target]:
                metadata.unlink()

        overlay_scan = copy_content_overlays(
            root / "content", ("common", target), destination
        )
        overlay_errors = [
            f"content/{issue.relative_path}: {issue.message}"
            for issue in overlay_scan.issues
        ]
        if overlay_errors:
            return errors + overlay_errors

        if errors:
            return errors
        if refresh:
            run_packwiz(
                ["packwiz", "refresh"],
                cwd=destination,
                cancel_event=cancel_event,
                deadline=operation_deadline,
            )

        if workspace is not None:
            live_target = root / "dist" / target
            live_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                swap_directory(destination, live_target, workspace / "previous")
            except ConfigError:
                preserve_workspace = True
                raise
        return []
    finally:
        if workspace is not None and not preserve_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def _build_pack(
    pack_id: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    operation_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
    )
    root = get_pack_root(pack_id)
    for required in (root / "source" / "pack.toml", root / "source" / "index.toml"):
        if not required.is_file():
            raise ConfigError(f"Missing required file: {required}")
    workspace = Path(tempfile.mkdtemp(prefix=".build-dist-", dir=root))
    staged_dist = workspace / "dist"
    preserve_workspace = False
    try:
        errors = build_target(
            root,
            "client",
            staged_dist / "client",
            refresh=False,
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
        errors += build_target(
            root,
            "server",
            staged_dist / "server",
            refresh=False,
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
        errors = list(dict.fromkeys(errors))
        if errors:
            print(
                "Build stopped because side classification is incomplete:",
                file=sys.stderr,
            )
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            print(
                f"Use: packctl side {pack_id} "
                "mods/<name>.pw.toml client|server|both",
                file=sys.stderr,
            )
            return 1

        run_packwiz(
            ["packwiz", "refresh"],
            cwd=staged_dist / "client",
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
        run_packwiz(
            ["packwiz", "refresh"],
            cwd=staged_dist / "server",
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )

        try:
            swap_directory(staged_dist, root / "dist", workspace / "previous-dist")
        except ConfigError:
            preserve_workspace = True
            raise
    finally:
        if not preserve_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
    print(f"Built {pack_id}: packs/{pack_id}/dist/client and server")
    return 0


def build_pack(
    pack_id: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    with ProjectLock(f"pack:{pack_id}", "build"):
        return _build_pack(
            pack_id,
            cancel_event=cancel_event,
            deadline=deadline,
        )


def cmd_build(args: argparse.Namespace) -> int:
    return build_pack(args.pack)


def cmd_build_all(_: argparse.Namespace) -> int:
    failed: list[str] = []
    for pack_id in pack_ids():
        if not load_pack_config(pack_id).get("enabled", True):
            print(f"Skipping disabled pack: {pack_id}")
            continue
        print(f"== Building {pack_id} ==")
        if build_pack(pack_id) != 0:
            failed.append(pack_id)
    if failed:
        print("Failed packs:", ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def cmd_validate_template(args: argparse.Namespace) -> int:
    root = get_template_root(args.template)
    return print_validation_result(
        validate_template_directory(root), f"template {args.template}"
    )


def distribution_target_from_config(config: dict[str, Any], pack_id: str) -> str:
    distribution = require_mapping(config, "distribution", pack_id)
    target = distribution.get("rsync_target")
    if not isinstance(target, str) or not target:
        raise ConfigError(
            f"{pack_id}.distribution.rsync_target must be a non-empty string"
        )
    try:
        return validate_rsync_target(target)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def distribution_target(pack_id: str) -> str:
    return distribution_target_from_config(load_pack_config(pack_id), pack_id)


def distribution_root(pack_id: str) -> Path:
    dist = get_pack_root(pack_id) / "dist"
    for side in ("client", "server"):
        if not (dist / side / "pack.toml").is_file():
            raise ConfigError(f"{side} distribution is not built for {pack_id}")
    return dist


def _make_deploy_snapshot(pack_id: str, dist: Path) -> Path:
    repository = PACKS.parent
    state_root = repository / ".huroshiki"
    snapshot_root = make_state_directory(
        state_root / "deploy-snapshots",
        state_root=state_root,
        repository_root=repository,
    )
    if snapshot_root.stat().st_dev != dist.stat().st_dev:
        raise ConfigError("Deploy snapshot must be on the distribution filesystem")
    snapshot = ensure_safe_state_path(
        snapshot_root / f"pack-{pack_id}-{uuid4().hex}",
        state_root=state_root,
        repository_root=repository,
    )
    before = distribution_digest(dist)
    try:
        shutil.copytree(dist, snapshot, symlinks=False)
        if distribution_digest(dist) != before:
            raise ConfigError("Distribution changed while creating deploy snapshot")
        for path in (snapshot, *snapshot.rglob("*")):
            path.chmod(path.stat().st_mode & ~0o222)
        return snapshot
    except BaseException:
        if snapshot.exists():
            for path in (snapshot, *snapshot.rglob("*")):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
            shutil.rmtree(snapshot, ignore_errors=True)
        raise


def discard_deploy_snapshot(snapshot: Path) -> None:
    repository = PACKS.parent
    safe = ensure_safe_state_path(
        snapshot,
        state_root=repository / ".huroshiki",
        repository_root=repository,
    )
    if safe.exists():
        for path in (safe, *safe.rglob("*")):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o700)
        shutil.rmtree(safe)


def _deploy_preview(
    pack_id: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> DeployPreview:
    dist = distribution_root(pack_id)
    target = distribution_target(pack_id)
    snapshot = _make_deploy_snapshot(pack_id, dist)
    try:
        digest = distribution_digest(snapshot)
        command = rsync_deploy_command(snapshot, target, dry_run=True)
        result = run_rsync_process(
            command,
            cwd=ROOT,
            cancel_event=cancel_event,
            deadline=deadline,
            phase="rsync-preview",
            max_output_bytes=RSYNC_PREVIEW_OUTPUT_MAX_BYTES,
        )
        if len(result.stdout.encode("utf-8")) > RSYNC_PREVIEW_OUTPUT_MAX_BYTES:
            raise ConfigError("Rsync preview output exceeded the supported limit")
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            diagnostic = bounded_diagnostic(result.stderr)
            print(
                diagnostic,
                file=sys.stderr,
                end="" if diagnostic.endswith("\n") else "\n",
            )
        if distribution_target(pack_id) != target or distribution_digest(snapshot) != digest:
            raise ConfigError("Deploy target or snapshot changed during preview")
        raw_lines = tuple(line for line in result.stdout.splitlines() if line.strip())
        return DeployPreview(
            target, digest, parse_rsync_changes(result.stdout), raw_lines, snapshot
        )
    except BaseException:
        discard_deploy_snapshot(snapshot)
        raise


def deploy_preview(
    pack_id: str,
    *,
    build: bool = False,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> DeployPreview:
    with ProjectLock(f"pack:{pack_id}", "deploy preview"):
        if (
            build
            and _build_pack(
                pack_id,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            != 0
        ):
            raise ConfigError("Build failed; deploy preview was not created")
        return _deploy_preview(
            pack_id,
            cancel_event=cancel_event,
            deadline=deadline,
        )


def print_deploy_preview(pack_id: str, preview: DeployPreview) -> None:
    counts = {
        category: sum(change.category == category for change in preview.changes)
        for category in ("added", "updated", "deleted")
    }
    print(
        f"Preview for {pack_id} -> {preview.target}: "
        f"{counts['added']} added, {counts['updated']} updated, "
        f"{counts['deleted']} deleted"
    )


def minecraft_server_target_from_config(
    config: dict[str, Any], pack_id: str
) -> tuple[str, str, str]:
    server = require_mapping(config, "minecraft_server", pack_id)
    values: dict[str, str] = {}
    for field in ("ssh_host", "stack_dir", "service"):
        value = server.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"{pack_id}.minecraft_server.{field} must be a non-empty string"
            )
        values[field] = value
    return (
        validate_ssh_target(values["ssh_host"]),
        validate_remote_stack_dir(values["stack_dir"]),
        validate_compose_service(values["service"]),
    )


def minecraft_server_target(pack_id: str) -> tuple[str, str, str]:
    return minecraft_server_target_from_config(load_pack_config(pack_id), pack_id)


def _deploy_pack(
    pack_id: str,
    *,
    build: bool = False,
    expected_target: str | None = None,
    expected_dist_digest: str | None = None,
    snapshot: Path | None = None,
    confirmed_target: str | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    if (
        build
        and _build_pack(
            pack_id,
            cancel_event=cancel_event,
            deadline=deadline,
        )
        != 0
    ):
        return 1
    target = (
        confirmed_target
        if confirmed_target is not None
        else distribution_target(pack_id)
    )
    owned_snapshot = snapshot is None
    dist = (
        _make_deploy_snapshot(pack_id, distribution_root(pack_id))
        if owned_snapshot
        else ensure_safe_state_path(
            snapshot,
            state_root=PACKS.parent / ".huroshiki",
            repository_root=PACKS.parent,
        )
    )
    try:
        if expected_target is not None and target != expected_target:
            raise ConfigError("Deploy target changed after preview; deployment aborted")
        if (
            expected_dist_digest is not None
            and distribution_digest(dist) != expected_dist_digest
        ):
            raise ConfigError("Distribution changed after preview; deployment aborted")
        run_rsync_process(
            rsync_deploy_command(dist, target, dry_run=False),
            cwd=ROOT,
            cancel_event=cancel_event,
            deadline=deadline,
            phase="rsync-transfer",
            max_output_bytes=RSYNC_OUTPUT_MAX_BYTES,
        )
        print(f"Deployed {pack_id} to {target}")
        return 0
    finally:
        if owned_snapshot:
            discard_deploy_snapshot(dist)


def deploy_pack(
    pack_id: str,
    *,
    build: bool = False,
    expected_target: str | None = None,
    expected_dist_digest: str | None = None,
    snapshot: Path | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    with ProjectLock(f"pack:{pack_id}", "deploy"):
        return _deploy_pack(
            pack_id,
            build=build,
            expected_target=expected_target,
            expected_dist_digest=expected_dist_digest,
            snapshot=snapshot,
            cancel_event=cancel_event,
            deadline=deadline,
        )


def cmd_deploy(args: argparse.Namespace) -> int:
    return deploy_pack(
        args.pack,
        build=args.expected_dist_digest is None,
        expected_target=args.expected_target,
        expected_dist_digest=args.expected_dist_digest,
    )


def cmd_deploy_dry_run(args: argparse.Namespace) -> int:
    preview = deploy_preview(args.pack, build=True)
    try:
        print_deploy_preview(args.pack, preview)
    finally:
        discard_deploy_snapshot(preview.snapshot)
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    host, stack, service = minecraft_server_target(args.pack)
    remote = f"cd {shlex.quote(stack)} && docker compose restart {shlex.quote(service)}"
    run(["ssh", "--", host, remote])
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    with ProjectLock(f"pack:{args.pack}", "publish"):
        if _build_pack(args.pack) != 0:
            return 1
        config = load_pack_config(args.pack)
        deploy_target = distribution_target_from_config(config, args.pack)
        restart_target = minecraft_server_target_from_config(config, args.pack)
        snapshot = _make_deploy_snapshot(args.pack, distribution_root(args.pack))
        try:
            digest = distribution_digest(snapshot)
            result = _deploy_pack(
                args.pack,
                expected_target=deploy_target,
                expected_dist_digest=digest,
                snapshot=snapshot,
                confirmed_target=deploy_target,
            )
            if result != 0:
                return result
            host, stack, service = restart_target
            remote = (
                f"cd {shlex.quote(stack)} && docker compose restart "
                f"{shlex.quote(service)}"
            )
            run(["ssh", "--", host, remote])
            return 0
        finally:
            discard_deploy_snapshot(snapshot)


def cmd_serve(args: argparse.Namespace) -> int:
    result = build_pack(args.pack)
    if result != 0:
        return result
    directory = get_pack_root(args.pack) / "dist"
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"Serving {args.pack} at http://127.0.0.1:{args.port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_deploy_all(_: argparse.Namespace) -> int:
    failed: list[str] = []
    for pack_id in pack_ids():
        if not load_pack_config(pack_id).get("enabled", True):
            print(f"Skipping disabled pack: {pack_id}")
            continue
        print(f"== Building/deploying {pack_id} ==")
        try:
            if deploy_pack(pack_id, build=True) != 0:
                failed.append(pack_id)
        except Exception as error:
            print(f"{pack_id}: {error}", file=sys.stderr)
            failed.append(pack_id)
    if failed:
        print("Failed deployments:", ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    if args.kind == "packs":
        for pack_id in pack_ids():
            print(pack_id)
        return 0

    if args.kind == "templates":
        for template_id in template_ids():
            print(template_id)
        return 0

    if args.pack is None:
        raise ConfigError(f"Completion kind {args.kind!r} requires a pack ID")

    root = get_pack_root(args.pack)

    if args.kind == "metadata":
        source = root / "source"
        for path in metadata_files(source):
            print(path.relative_to(source))
        return 0

    if args.kind == "mods":
        source = root / "source"
        for path in metadata_files(source):
            print(path.name.removesuffix(".pw.toml"))
        return 0

    if args.kind == "profiles":
        profiles = load_profiles(root)

        for name in sorted(profiles):
            print(name)
        return 0

    raise ConfigError(f"Unsupported completion kind: {args.kind}")


def _project_key_from_state_name(name: str) -> str | None:
    for kind in ("pack", "template"):
        prefix = f"{kind}-"
        if name.startswith(prefix):
            project_id = name[len(prefix):].rsplit("-", 1)[0]
            try:
                validate_project_id(project_id)
            except ConfigError:
                return None
            return f"{kind}:{project_id}"
    return None


def _transaction_active(path: Path) -> bool:
    projects: set[str] = set()
    project = _project_key_from_state_name(path.name)
    if project is not None:
        projects.add(project)
    directory_fd = plan_fd = -1
    try:
        directory_fd = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        plan_fd = os.open(
            "plan.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(plan_fd)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= 1024 * 1024:
            contents = os.read(plan_fd, metadata.st_size + 1)
            value = json.loads(contents.decode("utf-8"))
            if isinstance(value, dict):
                source = value.get("source")
                target = value.get("target")
                target_id = target.get("id") if isinstance(target, dict) else None
                for candidate in (source, f"pack:{target_id}" if target_id else None):
                    if not isinstance(candidate, str):
                        continue
                    kind, separator, project_id = candidate.partition(":")
                    if separator and kind in {"pack", "template"}:
                        validate_project_id(project_id)
                        projects.add(candidate)
    except (ConfigError, OSError, UnicodeError, json.JSONDecodeError):
        pass
    finally:
        if plan_fd >= 0:
            os.close(plan_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
    return any(project_lock_is_active(candidate) for candidate in projects)


def classify_state() -> list[StateItem]:
    ensure_safe_state_path(STATE_ROOT)
    for root in (LOG_ROOT, TRANSACTION_ROOT, TRASH_ROOT, DEPLOY_SNAPSHOT_ROOT):
        ensure_safe_state_path(root)
    items: list[StateItem] = []
    if LOG_ROOT.is_dir() and not LOG_ROOT.is_symlink():
        for project_dir in sorted(LOG_ROOT.iterdir()):
            if project_dir.is_symlink() or not project_dir.is_dir():
                items.append(
                    StateItem(
                        "active_state",
                        project_dir,
                        None,
                        project_dir.lstat().st_mtime,
                        path_bytes(project_dir),
                        True,
                    )
                )
                continue
            project = _project_key_from_state_name(f"{project_dir.name}-session")
            for path in sorted(project_dir.iterdir()):
                active = path.is_symlink() or (
                    project is not None and project_lock_is_active(project)
                )
                items.append(
                    StateItem(
                        "active_state" if active else "log",
                        path,
                        project,
                        path.lstat().st_mtime,
                        path_bytes(path),
                        active,
                    )
                )
    if TRANSACTION_ROOT.is_dir() and not TRANSACTION_ROOT.is_symlink():
        ensure_safe_state_path(TRANSACTION_ROOT)
        for path in sorted(TRANSACTION_ROOT.iterdir()):
            active = (
                path.is_symlink()
                or not path.is_dir()
                or _transaction_active(path)
            )
            if active:
                category = "active_transaction"
            elif (path / ".completed").is_file():
                category = "completed_transaction"
            else:
                category = "transaction_leftover"
            items.append(
                StateItem(
                    category,
                    path,
                    _project_key_from_state_name(path.name),
                    path.lstat().st_mtime,
                    path_bytes(path),
                    active,
                )
            )
    if DEPLOY_SNAPSHOT_ROOT.is_dir() and not DEPLOY_SNAPSHOT_ROOT.is_symlink():
        ensure_safe_state_path(DEPLOY_SNAPSHOT_ROOT)
        for path in sorted(DEPLOY_SNAPSHOT_ROOT.iterdir()):
            active = path.is_symlink() or not path.is_dir()
            items.append(
                StateItem(
                    "active_state" if active else "deploy_snapshot",
                    path,
                    _project_key_from_state_name(path.name),
                    path.lstat().st_mtime,
                    path_bytes(path),
                    active,
                )
            )
    lock_root = STATE_ROOT / "locks"
    if lock_root.is_dir() and not lock_root.is_symlink():
        for path in sorted(lock_root.iterdir()):
            active, metadata = _inspect_lock_path(path)
            if not active:
                continue
            items.append(
                StateItem(
                    "active_lock",
                    path,
                    metadata.project_key if metadata is not None else None,
                    path.lstat().st_mtime,
                    path_bytes(path),
                    True,
                )
            )
    for entry in list_trash():
        active = project_lock_is_active(entry.project_key)
        items.append(
            StateItem(
                "trash",
                entry.path,
                entry.project_key,
                entry.created_at,
                entry.bytes,
                active,
            )
        )
    if STATE_ROOT.is_dir() and not STATE_ROOT.is_symlink():
        known = {
            LOG_ROOT.name,
            TRANSACTION_ROOT.name,
            TRASH_ROOT.name,
            DEPLOY_SNAPSHOT_ROOT.name,
            "locks",
        }
        for path in sorted(STATE_ROOT.iterdir()):
            if path.name not in known:
                items.append(
                    StateItem(
                        "active_state",
                        path,
                        None,
                        path.lstat().st_mtime,
                        path_bytes(path),
                        True,
                    )
                )
    return sorted(
        items,
        key=lambda item: (item.category, -item.modified_at, str(item.path)),
    )


def clean_state(
    *,
    apply: bool = False,
    older_than_days: int | None = None,
    keep: int = 0,
    project_key: str | None = None,
    now: float | None = None,
    expected: tuple[StateItem, ...] | None = None,
) -> StateCleanupReport:
    if older_than_days is not None and older_than_days < 0:
        raise ConfigError("--older-than must be non-negative")
    if keep < 0:
        raise ConfigError("--keep must be non-negative")
    if project_key is not None:
        kind, separator, project_id = project_key.partition(":")
        if not separator:
            raise ConfigError("Project filter must be pack:<id> or template:<id>")
        get_project_root(kind, project_id, must_exist=False)
    current_time = (
        datetime.now(timezone.utc).timestamp() if now is None else now
    )
    items = classify_state()
    candidates: list[StateItem] = []
    by_category: dict[str, list[StateItem]] = {}
    for item in items:
        if item.active or item.category not in DEFAULT_RETENTION_DAYS:
            continue
        if project_key is not None and item.project_key != project_key:
            continue
        by_category.setdefault(item.category, []).append(item)
    for category, matching in by_category.items():
        retention = (
            DEFAULT_RETENTION_DAYS[category]
            if older_than_days is None
            else older_than_days
        )
        newest = sorted(matching, key=lambda item: item.modified_at, reverse=True)
        for item in newest[keep:]:
            if current_time - item.modified_at >= retention * 86400:
                candidates.append(item)
    candidates.sort(key=lambda item: str(item.path))
    if apply and expected is not None and tuple(candidates) != expected:
        raise ConfigError("State cleanup candidates changed after preview; cleanup aborted")
    removed_count = 0
    removed_bytes = 0
    if apply:
        for item in candidates:
            cleanup_lock: ProjectLock | None = None
            try:
                if item.project_key is not None:
                    cleanup_lock = ProjectLock(
                        item.project_key, "state cleanup"
                    ).acquire()
                ensure_safe_state_path(item.path)
                if item.path.is_symlink():
                    continue
                if item.path.is_dir():
                    shutil.rmtree(item.path)
                elif item.path.is_file():
                    item.path.unlink()
                removed_count += 1
                removed_bytes += item.bytes
            except ConfigError:
                continue
            finally:
                if cleanup_lock is not None:
                    cleanup_lock.release()
    return StateCleanupReport(
        tuple(items),
        tuple(candidates),
        removed_count,
        removed_bytes,
        not apply,
    )


def format_bytes(value: int) -> str:
    return f"{value} bytes"


def cmd_trash_list(_: argparse.Namespace) -> int:
    entries = list_trash()
    for entry in entries:
        print(f"{entry.name}\t{entry.project_key}\t{format_bytes(entry.bytes)}")
    total = sum(item.bytes for item in entries)
    print(f"Trash: {len(entries)} item(s), {format_bytes(total)}")
    return 0


def cmd_trash_restore(args: argparse.Namespace) -> int:
    destination = restore_trash(args.entry)
    print(f"Restored {args.entry} to {destination.relative_to(ROOT)}")
    return 0


def cmd_trash_purge(args: argparse.Namespace) -> int:
    count, total = purge_trash(
        name=args.entry,
        project_key=args.project,
        older_than_days=args.older_than,
    )
    print(f"Purged {count} trash item(s), freed {format_bytes(total)}")
    return 0


def cmd_clean_state(args: argparse.Namespace) -> int:
    report = clean_state(
        apply=args.apply,
        older_than_days=args.older_than,
        keep=args.keep,
        project_key=args.project,
    )
    for item in report.items:
        status = (
            "protected"
            if item.active
            else ("selected" if item in report.selected else "retained")
        )
        print(
            f"{item.category}\t{status}\t{item.project_key or '-'}\t"
            f"{format_bytes(item.bytes)}\t{item.path.relative_to(STATE_ROOT)}"
        )
    selected_bytes = sum(item.bytes for item in report.selected)
    if report.dry_run:
        print(
            f"Dry run: would remove {len(report.selected)} item(s), "
            f"{format_bytes(selected_bytes)}"
        )
    else:
        print(
            f"Removed {report.removed_count} item(s), freed "
            f"{format_bytes(report.removed_bytes)}"
        )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manage multiple Packwiz projects")
    root.add_argument(
        "--root",
        metavar="PATH",
        help="managed repository root (default: HUROSHIKI_ROOT, then current directory)",
    )
    root.add_argument(
        "--version",
        action="version",
        version=f"packctl {VERSION}",
    )
    sub = root.add_subparsers(dest="command", required=True)

    item = sub.add_parser("complete")
    item.add_argument(
        "kind", choices=["packs", "templates", "profiles", "metadata", "mods"]
    )
    item.add_argument("pack", nargs="?")
    item.set_defaults(func=cmd_complete)
    item = sub.add_parser("list")
    item.set_defaults(func=cmd_list)
    item = sub.add_parser("list-templates")
    item.set_defaults(func=cmd_list_templates)
    item = sub.add_parser("show")
    item.add_argument("pack")
    item.set_defaults(func=cmd_show)
    item = sub.add_parser("show-deployment")
    item.add_argument("pack")
    item.set_defaults(func=cmd_show_deployment)
    item = sub.add_parser("set-deployment")
    item.add_argument("pack")
    item.add_argument("--rsync-target")
    item.add_argument("--ssh-host")
    item.add_argument("--stack-dir")
    item.add_argument("--service")
    item.set_defaults(func=cmd_set_deployment)
    item = sub.add_parser("show-pack-url")
    item.add_argument("pack")
    item.add_argument("--raw", action="store_true")
    item.set_defaults(func=cmd_show_pack_url)
    item = sub.add_parser("set-pack-url")
    item.add_argument("pack")
    item.add_argument("url")
    item.set_defaults(func=cmd_set_pack_url)
    item = sub.add_parser("clear-pack-url")
    item.add_argument("pack")
    item.set_defaults(func=cmd_clear_pack_url)
    item = sub.add_parser("new")
    item.add_argument("pack")
    item.add_argument("display_name")
    item.add_argument("minecraft")
    item.add_argument("loader", choices=sorted(LOADER_FLAGS))
    item.add_argument("loader_version")
    item.set_defaults(func=cmd_new)
    item = sub.add_parser("new-template")
    item.add_argument("template")
    item.add_argument("display_name")
    item.add_argument("minecraft")
    item.add_argument("loader", choices=sorted(LOADER_FLAGS))
    item.add_argument("loader_version")
    item.set_defaults(func=cmd_new_template)
    item = sub.add_parser("validate-template")
    item.add_argument("template")
    item.set_defaults(func=cmd_validate_template)
    item = sub.add_parser("validate")
    item.set_defaults(func=cmd_validate)
    item = sub.add_parser("validate-for")
    item.add_argument("pack")
    item.set_defaults(func=cmd_validate_for)
    item = sub.add_parser("add")
    item.add_argument("pack")
    item.add_argument("query")
    item.add_argument("side")
    item.set_defaults(func=cmd_add)
    item = sub.add_parser("remove")
    item.add_argument("pack")
    item.add_argument("mods", nargs="+")
    item.set_defaults(func=cmd_remove)
    item = sub.add_parser("update")
    item.add_argument("pack")
    item.add_argument("--build", action="store_true")
    item.add_argument("--allow-partial", action="store_true")
    item.set_defaults(func=cmd_update)
    item = sub.add_parser("loader-version")
    item.add_argument("pack")
    item.add_argument("version")
    item.add_argument("--apply", action="store_true")
    item.set_defaults(func=cmd_loader_version)
    item = sub.add_parser("version")
    item.add_argument("pack")
    item.add_argument("identity")
    item.add_argument("--artifact-id")
    item.add_argument("--file-id")
    item.add_argument("--version-id")
    item.add_argument("--apply", action="store_true")
    item.set_defaults(func=cmd_version)
    item = sub.add_parser("apply-template")
    item.add_argument("pack")
    item.add_argument("templates", nargs="+")
    item.add_argument("--resolution")
    item.add_argument("--apply", action="store_true")
    item.add_argument("--json", action="store_true")
    item.set_defaults(func=cmd_apply_template)
    item = sub.add_parser("side")
    item.add_argument("pack")
    item.add_argument("metadata_file")
    item.add_argument("side")
    item.set_defaults(func=cmd_side)
    item = sub.add_parser("profile")
    item.add_argument("pack")
    item.add_argument("names", nargs="+")
    item.set_defaults(func=cmd_profile)
    item = sub.add_parser("build")
    item.add_argument("pack")
    item.set_defaults(func=cmd_build)
    item = sub.add_parser("build-all")
    item.set_defaults(func=cmd_build_all)
    item = sub.add_parser("show-url-policy")
    item.add_argument("kind", choices=("pack", "template"))
    item.add_argument("project")
    item.set_defaults(func=cmd_show_url_policy)
    item = sub.add_parser("set-url-policy")
    item.add_argument("kind", choices=("pack", "template"))
    item.add_argument("project")
    item.add_argument("--max-size", type=_positive_int, dest="max_size")
    item.add_argument(
        "--allow-private-networks",
        type=_normalize_bool_flag,
        dest="allow_private_networks",
    )
    item.set_defaults(func=cmd_set_url_policy)
    item = sub.add_parser("show-template-loader-version")
    item.add_argument("template")
    item.set_defaults(func=cmd_show_template_loader_version)
    item = sub.add_parser("set-template-loader-version")
    item.add_argument("template")
    item.add_argument("loader_version")
    item.set_defaults(func=cmd_set_template_loader_version)
    item = sub.add_parser("deploy")
    item.add_argument("pack")
    item.add_argument("--expected-target")
    item.add_argument("--expected-dist-digest")
    item.set_defaults(func=cmd_deploy)
    item = sub.add_parser("deploy-dry-run")
    item.add_argument("pack")
    item.set_defaults(func=cmd_deploy_dry_run)
    item = sub.add_parser("restart")
    item.add_argument("pack")
    item.set_defaults(func=cmd_restart)
    item = sub.add_parser("publish")
    item.add_argument("pack")
    item.set_defaults(func=cmd_publish)
    item = sub.add_parser("serve")
    item.add_argument("pack")
    item.add_argument("--port", type=int, default=8080)
    item.set_defaults(func=cmd_serve)
    item = sub.add_parser("deploy-all")
    item.set_defaults(func=cmd_deploy_all)
    item = sub.add_parser("trash-list")
    item.set_defaults(func=cmd_trash_list)
    item = sub.add_parser("trash-restore")
    item.add_argument("entry")
    item.set_defaults(func=cmd_trash_restore)
    item = sub.add_parser("trash-purge")
    item.add_argument("entry", nargs="?")
    item.add_argument("--project")
    item.add_argument("--older-than", type=int)
    item.set_defaults(func=cmd_trash_purge)
    item = sub.add_parser("clean-huroshiki-state")
    item.add_argument("--apply", action="store_true")
    item.add_argument("--older-than", type=int)
    item.add_argument("--keep", type=int, default=0)
    item.add_argument("--project")
    item.set_defaults(func=cmd_clean_state)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"command failed with exit code {error.returncode}", file=sys.stderr)
        return error.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
