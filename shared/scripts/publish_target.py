"""Pure publish-target model and validation utilities.

This module is intentionally transport-neutral and performs no process/network
activity. It only validates user-facing values and builds deterministic
configuration fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from ipaddress import IPv6Address
from pathlib import PurePosixPath
from typing import Literal

import packctl
from deploy_support import split_rsync_target


class PublishTargetError(ValueError):
    """Raised when publish-target model validation fails."""


LEGACY_SERVER_ID = "legacy-pack-config"


@dataclass(frozen=True)
class PublishSshEndpoint:
    host: str
    port: int
    user: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "port", validate_publish_ssh_port(self.port))
        object.__setattr__(
            self,
            "host",
            _validate_publish_host(self.host),
        )
        if self.user is not None:
            if not isinstance(self.user, str) or not self.user:
                raise PublishTargetError("SSH endpoint user must be a non-empty string")
            if not self.user.strip() == self.user or any(
                ch.isspace() for ch in self.user
            ):
                raise PublishTargetError("SSH endpoint user must not contain whitespace")
            if not packctl.SSH_USER_RE.fullmatch(self.user):
                raise PublishTargetError("SSH endpoint user is invalid")


@dataclass(frozen=True)
class PublishRestartTarget:
    mode: Literal["compose"]
    endpoint: PublishSshEndpoint

    stack_dir: PurePosixPath
    service: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.enabled is not True:
            raise PublishTargetError("Restart must be enabled")
        if self.mode != "compose":
            raise PublishTargetError("Restart mode must be 'compose'")
        if not isinstance(self.endpoint, PublishSshEndpoint):
            raise PublishTargetError("Restart endpoint must be a PublishSshEndpoint")
        object.__setattr__(
            self,
            "stack_dir",
            validate_publish_remote_path(str(self.stack_dir), field="restart.stack_dir"),
        )
        try:
            object.__setattr__(self, "service", packctl.validate_compose_service(self.service))
        except Exception as error:
            raise PublishTargetError(str(error)) from error


@dataclass(frozen=True)
class PublishRemoteTarget:
    server_id: str
    publication_endpoint: PublishSshEndpoint
    publication_root: PurePosixPath
    restart: PublishRestartTarget
    config_digest: str

    def __post_init__(self) -> None:
        server_id = _validate_server_id(self.server_id)
        publication_endpoint = _coerce_publish_ssh_endpoint(self.publication_endpoint)
        publication_root = validate_publish_remote_path(str(self.publication_root))
        restart = _coerce_publish_restart_target(self.restart)

        object.__setattr__(self, "server_id", server_id)
        object.__setattr__(self, "publication_endpoint", publication_endpoint)
        object.__setattr__(self, "publication_root", publication_root)
        object.__setattr__(self, "restart", restart)

        expected_digest = compute_publish_remote_target_digest(
            server_id=server_id,
            publication_endpoint=publication_endpoint,
            publication_root=publication_root,
            restart=restart,
        )
        if not isinstance(self.config_digest, str):
            raise PublishTargetError("Publish remote config digest must be a string")
        if not _CONFIG_DIGEST_RE.fullmatch(self.config_digest):
            raise PublishTargetError("Publish remote config digest must be lowercase hex")
        if self.config_digest != expected_digest:
            raise PublishTargetError("Publish remote config digest is invalid")


PUBLISH_RESERVED_NAMES = frozenset({"generations", "current"})
PUBLISH_RESERVED_PREFIX = ".huroshiki-"


def is_publish_reserved_child(name: str) -> bool:
    """Return True when a child name is reserved for generated publish content."""

    return name in PUBLISH_RESERVED_NAMES or name.startswith(PUBLISH_RESERVED_PREFIX)


def validate_publish_ssh_port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PublishTargetError("SSH port must be an integer")
    if value < 1 or value > 65535:
        raise PublishTargetError("SSH port must be in the range 1..65535")
    return value


def validate_publish_remote_path(
    value: str,
    *,
    field: str = "publication_root",
) -> PurePosixPath:
    if not isinstance(value, str):
        raise PublishTargetError(f"{field} must be a non-empty string")
    if not value:
        raise PublishTargetError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise PublishTargetError(f"{field} must not contain surrounding whitespace")
    if any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"}
        for character in value
    ):
        raise PublishTargetError(f"{field} must not contain control characters")
    if any(character.isspace() for character in value):
        raise PublishTargetError(f"{field} must not contain whitespace")
    if value == "/":
        raise PublishTargetError(f"{field} must be a non-root absolute POSIX path")
    if not value.startswith("/"):
        raise PublishTargetError(f"{field} must be an absolute POSIX path")
    if value.startswith("//") or "//" in value:
        raise PublishTargetError(f"{field} must not contain repeated slashes")
    if value.endswith("/"):
        raise PublishTargetError(f"{field} must not end with a slash")
    if "\\" in value:
        raise PublishTargetError(f"{field} must not contain backslashes")
    if _WINDOWS_DRIVE_PATH_RE.match(value):
        raise PublishTargetError(f"{field} must not be a Windows drive path")
    if value.startswith("\\"):
        raise PublishTargetError(f"{field} must not be a Windows UNC path")
    if not unicodedata.is_normalized("NFC", value):
        raise PublishTargetError(f"{field} must be in NFC normalization form")
    if len(value.encode("utf-8")) > 4096:
        raise PublishTargetError(f"{field} must not exceed 4096 UTF-8 bytes")

    raw_parts = [part for part in value.split("/") if part != ""]
    for part in raw_parts:
        if part in {".", ".."}:
            raise PublishTargetError(f"{field} must not contain '.' or '..' components")
        if len(part.encode("utf-8")) > 255:
            raise PublishTargetError(
                f"{field} path component exceeds the supported 255-byte limit"
            )

    path = PurePosixPath(value)
    return path


def parse_publish_ssh_endpoint(value: str, *, port: int = 22) -> PublishSshEndpoint:
    if not isinstance(value, str):
        raise PublishTargetError("SSH endpoint must be a string")
    validated_port = validate_publish_ssh_port(port)
    try:
        normalized = packctl.validate_ssh_target(value)
    except Exception as error:
        raise PublishTargetError(str(error)) from error

    if "@" in normalized:
        user, host = normalized.split("@", 1)
    else:
        user = None
        host = normalized

    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    return PublishSshEndpoint(host=host, port=validated_port, user=user)


def publish_remote_target_from_legacy_settings(
    *,
    rsync_target: str,
    ssh_host: str,
    stack_dir: str,
    service: str,
    server_id: str = LEGACY_SERVER_ID,
    remote_path: str | None = None,
) -> PublishRemoteTarget:
    try:
        rsync_parts = split_rsync_target(rsync_target)
    except Exception as error:
        raise PublishTargetError(str(error)) from error

    publication_root_value = remote_path if remote_path is not None else rsync_parts.path
    publication_root = validate_publish_remote_path(
        publication_root_value,
        field="publication_root" if remote_path is None else "remote_path",
    )

    validated_server_id = _validate_server_id(server_id)

    try:
        publication_endpoint = parse_publish_ssh_endpoint(rsync_parts.host, port=22)
        restart_endpoint = parse_publish_ssh_endpoint(ssh_host, port=22)
        validated_stack_dir = validate_publish_remote_path(
            stack_dir,
            field="restart.stack_dir",
        )
    except PublishTargetError:
        raise
    except Exception as error:
        raise PublishTargetError(str(error)) from error

    try:
        validated_service = packctl.validate_compose_service(service)
    except Exception as error:
        raise PublishTargetError(str(error)) from error

    restart = PublishRestartTarget(
        mode="compose",
        endpoint=restart_endpoint,
        stack_dir=validated_stack_dir,
        service=validated_service,
    )
    return PublishRemoteTarget(
        server_id=validated_server_id,
        publication_endpoint=publication_endpoint,
        publication_root=publication_root,
        restart=restart,
        config_digest=compute_publish_remote_target_digest(
            server_id=validated_server_id,
            publication_endpoint=publication_endpoint,
            publication_root=publication_root,
            restart=restart,
        ),
    )


def _validate_publish_host(value: str) -> str:
    if not isinstance(value, str):
        raise PublishTargetError("SSH endpoint host must be a string")
    if not value:
        raise PublishTargetError("SSH endpoint host must be non-empty")
    if "@" in value:
        raise PublishTargetError("SSH endpoint host must not include a user")
    if value.startswith("[") or value.endswith("]"):
        raise PublishTargetError("SSH endpoint host must not include brackets")
    if "[" in value or "]" in value:
        raise PublishTargetError("SSH endpoint host must not include brackets")
    if value.startswith("/"):
        raise PublishTargetError("SSH endpoint host must not start with '/'")
    if value.startswith("\\") or "\\" in value:
        raise PublishTargetError("SSH endpoint host must not contain backslashes")
    if any(character.isspace() for character in value):
        raise PublishTargetError("SSH endpoint host must not contain whitespace")
    if any(unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in value):
        raise PublishTargetError("SSH endpoint host must not contain control characters")
    if ":" in value:
        try:
            IPv6Address(value)
        except ValueError as error:
            raise PublishTargetError("SSH endpoint host is not a valid IPv6 address") from error
        return value

    try:
        packctl.validate_ssh_target(value)
    except Exception as error:
        raise PublishTargetError(str(error)) from error

    return value


def _validate_server_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublishTargetError("server_id must be a non-empty string")
    if value != value.strip():
        raise PublishTargetError("server_id must not have surrounding whitespace")
    if any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"}
        for character in value
    ):
        raise PublishTargetError("server_id must not contain control characters")
    if any(character.isspace() for character in value):
        raise PublishTargetError("server_id must not contain whitespace")
    if not packctl.PROJECT_ID_RE.fullmatch(value):
        raise PublishTargetError(
            "server_id must use lowercase letters, digits, '.', '_' or '-', "
            "and must start with a letter or digit"
        )
    return value


_PUBLISH_REMOTE_TARGET_SCHEMA = "publish-remote-target"
_PUBLISH_REMOTE_TARGET_VERSION = 1
_CONFIG_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def compute_publish_remote_target_digest(
    *,
    server_id: str,
    publication_endpoint: PublishSshEndpoint,
    publication_root: PurePosixPath | str,
    restart: PublishRestartTarget,
) -> str:
    """Return the semantic digest for a validated remote target configuration."""

    validated_server_id = _validate_server_id(server_id)
    validated_endpoint = _coerce_publish_ssh_endpoint(publication_endpoint)
    validated_root = validate_publish_remote_path(str(publication_root))
    validated_restart = _coerce_publish_restart_target(restart)
    return _compute_publish_remote_target_digest(
        server_id=validated_server_id,
        publication_endpoint=validated_endpoint,
        publication_root=validated_root,
        restart=validated_restart,
    )


def _compute_publish_remote_target_digest(
    *,
    server_id: str,
    publication_endpoint: PublishSshEndpoint,
    publication_root: PurePosixPath,
    restart: PublishRestartTarget,
) -> str:
    payload = {
        "schema": _PUBLISH_REMOTE_TARGET_SCHEMA,
        "version": _PUBLISH_REMOTE_TARGET_VERSION,
        "server_id": server_id,
        "publication_endpoint": {
            "host": publication_endpoint.host,
            "port": publication_endpoint.port,
            "user": publication_endpoint.user,
        },
        "publication_root": publication_root.as_posix(),
        "restart_enabled": True,
        "restart": {
            "enabled": restart.enabled,
            "mode": restart.mode,
            "endpoint": {
                "host": restart.endpoint.host,
                "port": restart.endpoint.port,
                "user": restart.endpoint.user,
            },
            "stack_dir": restart.stack_dir.as_posix(),
            "service": restart.service,
        },
    }
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def _coerce_publish_ssh_endpoint(value: PublishSshEndpoint) -> PublishSshEndpoint:
    if not isinstance(value, PublishSshEndpoint):
        raise PublishTargetError("publication_endpoint must be a PublishSshEndpoint")
    return value


def _coerce_publish_restart_target(value: PublishRestartTarget) -> PublishRestartTarget:
    if not isinstance(value, PublishRestartTarget):
        raise PublishTargetError("restart must be a PublishRestartTarget")
    return value
