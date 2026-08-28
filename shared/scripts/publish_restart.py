"""Bounded restart phase for an already activated Publish generation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import threading
import time
from typing import Callable, Literal
from uuid import uuid4

import packctl
from pack_publish import PackPublishError, PackPublishManifest, validate_publish_manifest
from process_runner import BoundedProcessResult, run_bounded_process
from publish_activation import PublishActivatedGeneration
from publish_target import (
    PublishRemoteTarget,
    PublishRestartTarget,
    PublishSshEndpoint,
    PublishTargetError,
    publish_remote_target_from_legacy_settings,
    validate_publish_remote_path,
)
from publish_transfer import compute_publish_generation_id


class PublishRestartError(RuntimeError):
    """The bounded Publish restart could not complete as requested."""


class PublishRestartCancelled(PublishRestartError):
    """Restart was cancelled before the remote operation was launched."""


class PublishRestartDeadlineExceeded(PublishRestartError):
    """Restart deadline expired before the remote operation was launched."""


class PublishRestartIntegrityError(PublishRestartError):
    """Restart may have occurred, but its outcome is not trustworthy."""

    def __init__(self, message: str, result: "PublishRestartResult") -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class PublishRestartResult:
    manifest_digest: str
    target_config_digest: str
    generation_id: str
    attempted: bool
    succeeded: bool
    status: Literal["succeeded", "failed", "uncertain"]
    remote_returncode: int | None = None

    def __post_init__(self) -> None:
        if self.status == "succeeded":
            if not self.attempted or not self.succeeded:
                raise ValueError("succeeded restart result must be attempted and successful")
        elif self.status in {"failed", "uncertain"}:
            if not self.attempted or self.succeeded:
                raise ValueError("failed/uncertain restart result must be attempted and unsuccessful")
        else:
            raise ValueError("invalid Publish restart status")
        if self.remote_returncode is not None and (
            not isinstance(self.remote_returncode, int)
            or isinstance(self.remote_returncode, bool)
            or not -2147483648 <= self.remote_returncode <= 2147483647
        ):
            raise ValueError("invalid bounded remote return code")


_PROTOCOL_SCHEMA = "huroshiki-publish-restart-v1"
_PROTOCOL_VERSION = 1
_RESTART_TIMEOUT_SECONDS = 180.0
_REMOTE_DOCKER_TIMEOUT_SECONDS = 120
_SSH_CONNECT_TIMEOUT_SECONDS = 10
_MAX_REQUEST_BYTES = 65536
_MAX_REMOTE_OUTPUT_BYTES = 65536
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_ID_RE = re.compile(r"^v1-[0-9a-f]{64}$")
_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _remote_helper_source() -> str:
    return rf'''import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import unicodedata

SCHEMA = {_PROTOCOL_SCHEMA!r}
VERSION = {_PROTOCOL_VERSION}
MAX_REQUEST = {_MAX_REQUEST_BYTES}
DOCKER_TIMEOUT = {_REMOTE_DOCKER_TIMEOUT_SECONDS}
HEX = re.compile(r"^[0-9a-f]{{64}}$")
GENERATION = re.compile(r"^v1-[0-9a-f]{{64}}$")
OPERATION = re.compile(r"^[0-9a-f]{{32}}$")
SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
EXPECTED = {{
    "schema", "version", "operation_id", "manifest_digest",
    "target_config_digest", "generation_id", "stack_dir", "service", "mode",
}}

def strict_object(pairs):
    value = {{}}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate request field")
        value[key] = item
    return value

def send(request, status, returncode):
    response = {{
        "schema": SCHEMA,
        "version": VERSION,
        "request": "restart",
        "operation_id": request["operation_id"],
        "manifest_digest": request["manifest_digest"],
        "target_config_digest": request["target_config_digest"],
        "generation_id": request["generation_id"],
        "status": status,
        "returncode": returncode,
    }}
    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def valid_stack(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if len(value.encode("utf-8")) > 4096 or value == "/" or not value.startswith("/"):
        return False
    if value.startswith("//") or "//" in value or value.endswith("/") or "\\" in value:
        return False
    if not unicodedata.is_normalized("NFC", value):
        return False
    if any(unicodedata.category(ch) in {{"Cc", "Zl", "Zp"}} or ch.isspace() for ch in value):
        return False
    parts = [part for part in value.split("/") if part]
    if any(part in {{".", ".."}} or len(part.encode("utf-8")) > 255 for part in parts):
        return False
    return PurePosixPath(value).as_posix() == value

def valid_service(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value.encode("utf-8")) <= 255
        and not any(unicodedata.category(ch) in {{"Cc", "Zl", "Zp"}} for ch in value)
        and SERVICE.fullmatch(value) is not None
    )

def main():
    payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(payload) > MAX_REQUEST:
        return 2
    try:
        request = json.loads(payload, object_pairs_hook=strict_object)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return 2
    if not isinstance(request, dict) or set(request) != EXPECTED:
        return 2
    bindings_valid = (
        request.get("schema") == SCHEMA
        and request.get("version") == VERSION
        and isinstance(request.get("operation_id"), str)
        and OPERATION.fullmatch(request["operation_id"]) is not None
        and isinstance(request.get("manifest_digest"), str)
        and HEX.fullmatch(request["manifest_digest"]) is not None
        and isinstance(request.get("target_config_digest"), str)
        and HEX.fullmatch(request["target_config_digest"]) is not None
        and isinstance(request.get("generation_id"), str)
        and GENERATION.fullmatch(request["generation_id"]) is not None
    )
    if not bindings_valid:
        return 2
    if (
        request.get("mode") != "compose"
        or not valid_stack(request.get("stack_dir"))
        or not valid_service(request.get("service"))
    ):
        send(request, "failed", 2)
        return 0
    try:
        os.chdir(request["stack_dir"])
        completed = subprocess.run(
            ["docker", "compose", "restart", request["service"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_TIMEOUT,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        send(request, "uncertain", -1)
        return 0
    except OSError:
        send(request, "failed", 127)
        return 0
    if completed.returncode == 0:
        send(request, "succeeded", 0)
    else:
        returncode = completed.returncode
        if not isinstance(returncode, int) or not -2147483648 <= returncode <= 2147483647:
            returncode = 1
        send(request, "failed", returncode)
    return 0

raise SystemExit(main())
'''


_REMOTE_HELPER_SCRIPT = _remote_helper_source()
_REMOTE_HELPER_PAYLOAD = base64.b64encode(
    _REMOTE_HELPER_SCRIPT.encode("utf-8")
).decode("ascii")
_REMOTE_COMMAND = "python3 -c " + shlex.quote(
    f'import base64;exec(base64.b64decode("{_REMOTE_HELPER_PAYLOAD}"))'
)


def _emit(progress: Callable[[str], object] | None, phase: str) -> None:
    if progress is None:
        return
    try:
        progress(phase)
    except Exception:
        pass


def _checkpoint(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PublishRestartCancelled("Publish restart was cancelled")
    if time.monotonic() >= deadline:
        raise PublishRestartDeadlineExceeded("Publish restart deadline exceeded")


def _validate_target(target: PublishRemoteTarget) -> PublishRemoteTarget:
    if type(target) is not PublishRemoteTarget:
        raise PublishRestartError("Publish restart requires a PublishRemoteTarget")
    try:
        restart = PublishRestartTarget(
            mode=target.restart.mode,
            endpoint=PublishSshEndpoint(
                target.restart.endpoint.host,
                target.restart.endpoint.port,
                target.restart.endpoint.user,
            ),
            stack_dir=target.restart.stack_dir,
            service=target.restart.service,
            enabled=target.restart.enabled,
        )
        validated = PublishRemoteTarget(
            server_id=target.server_id,
            publication_endpoint=PublishSshEndpoint(
                target.publication_endpoint.host,
                target.publication_endpoint.port,
                target.publication_endpoint.user,
            ),
            publication_root=target.publication_root,
            restart=restart,
            config_digest=target.config_digest,
        )
        if len(validated.restart.service.encode("utf-8")) > 255:
            raise PublishTargetError("Compose service exceeds the supported length")
        validate_publish_remote_path(
            validated.restart.stack_dir.as_posix(), field="restart.stack_dir"
        )
    except (AttributeError, PublishTargetError) as error:
        raise PublishRestartError("Publish restart target is invalid") from error
    return validated


def _validate_binding(
    activated: PublishActivatedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
) -> str:
    if type(activated) is not PublishActivatedGeneration:
        raise PublishRestartError(
            "Publish restart requires a PublishActivatedGeneration"
        )
    try:
        validate_publish_manifest(manifest)
    except PackPublishError as error:
        raise PublishRestartError(str(error)) from error
    expected_generation_id = compute_publish_generation_id(manifest, target)
    expected_generation_path = (
        target.publication_root / "generations" / expected_generation_id
    )
    expected_current_path = target.publication_root / "current"
    if activated.manifest_digest != manifest.manifest_digest:
        raise PublishRestartError("activated generation manifest digest is invalid")
    if activated.target_config_digest != target.config_digest:
        raise PublishRestartError("activated generation target digest is invalid")
    if activated.generation_id != expected_generation_id:
        raise PublishRestartError("activated generation ID is invalid")
    if activated.generation_path != expected_generation_path:
        raise PublishRestartError("activated generation path is not canonical")
    if activated.current_path != expected_current_path:
        raise PublishRestartError("activated current path is not canonical")
    return expected_generation_id


def _resolve_current_target(
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
) -> PublishRemoteTarget:
    try:
        settings = packctl.deployment_settings(manifest.pack_id)
        return publish_remote_target_from_legacy_settings(
            rsync_target=settings.rsync_target,
            ssh_host=settings.ssh_host,
            stack_dir=settings.stack_dir,
            service=settings.service,
            server_id=target.server_id,
            remote_path=target.publication_root.as_posix(),
        )
    except (packctl.ConfigError, PublishTargetError) as error:
        raise PublishRestartError(
            "current Publish target could not be resolved"
        ) from error


def _endpoint_destination(endpoint: PublishSshEndpoint) -> str:
    host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    return f"{endpoint.user}@{host}" if endpoint.user else host


def _ssh_command(endpoint: PublishSshEndpoint) -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_SECONDS}",
        "-p",
        str(endpoint.port),
        "--",
        _endpoint_destination(endpoint),
        _REMOTE_COMMAND,
    ]


def _request(
    operation_id: str,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    generation_id: str,
) -> dict[str, object]:
    return {
        "schema": _PROTOCOL_SCHEMA,
        "version": _PROTOCOL_VERSION,
        "operation_id": operation_id,
        "manifest_digest": manifest.manifest_digest,
        "target_config_digest": target.config_digest,
        "generation_id": generation_id,
        "stack_dir": target.restart.stack_dir.as_posix(),
        "service": target.restart.service,
        "mode": target.restart.mode,
    }


def _parse_response(stdout: str) -> dict[str, object] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        response = json.loads(lines[0])
    except (TypeError, json.JSONDecodeError):
        return None
    return response if isinstance(response, dict) else None


def _validate_response(
    response: dict[str, object],
    *,
    operation_id: str,
    manifest_digest: str,
    target_config_digest: str,
    generation_id: str,
) -> tuple[str, int]:
    expected_keys = {
        "schema",
        "version",
        "request",
        "operation_id",
        "manifest_digest",
        "target_config_digest",
        "generation_id",
        "status",
        "returncode",
    }
    expected_values = {
        "schema": _PROTOCOL_SCHEMA,
        "version": _PROTOCOL_VERSION,
        "request": "restart",
        "operation_id": operation_id,
        "manifest_digest": manifest_digest,
        "target_config_digest": target_config_digest,
        "generation_id": generation_id,
    }
    if set(response) != expected_keys or any(
        response.get(key) != value for key, value in expected_values.items()
    ):
        raise ValueError("Publish restart response binding is invalid")
    status = response.get("status")
    returncode = response.get("returncode")
    if status not in {"succeeded", "failed", "uncertain"}:
        raise ValueError("Publish restart response status is invalid")
    if (
        not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or not -2147483648 <= returncode <= 2147483647
    ):
        raise ValueError("Publish restart response return code is invalid")
    if status == "succeeded" and returncode != 0:
        raise ValueError("Publish restart success return code is invalid")
    if status == "failed" and returncode == 0:
        raise ValueError("Publish restart failure return code is invalid")
    return status, returncode


def _result(
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    generation_id: str,
    status: Literal["succeeded", "failed", "uncertain"],
    returncode: int | None = None,
) -> PublishRestartResult:
    return PublishRestartResult(
        manifest.manifest_digest,
        target.config_digest,
        generation_id,
        True,
        status == "succeeded",
        status,
        returncode,
    )


def restart_activated_publish(
    activated: PublishActivatedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[str], object] | None = None,
) -> PublishRestartResult:
    """Restart the configured service after immutable Publish activation evidence."""

    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + _RESTART_TIMEOUT_SECONDS
    )
    _emit(progress, "validating")
    _checkpoint(cancel_event, effective_deadline)
    validated_target = _validate_target(target)
    generation_id = _validate_binding(activated, manifest, validated_target)
    _checkpoint(cancel_event, effective_deadline)
    current_target = _resolve_current_target(manifest, validated_target)
    if current_target.config_digest != validated_target.config_digest:
        raise PublishRestartError("Publish target configuration is stale")
    _checkpoint(cancel_event, effective_deadline)

    operation_id = uuid4().hex
    request = _request(
        operation_id, manifest, validated_target, generation_id
    )
    payload = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > _MAX_REQUEST_BYTES:
        raise PublishRestartError("Publish restart request is too large")

    _emit(progress, "restarting")
    process = run_bounded_process(
        _ssh_command(validated_target.restart.endpoint),
        cwd=Path(packctl.PACKS).parent,
        stdin=payload,
        cancel_event=cancel_event,
        deadline=effective_deadline,
        max_output_bytes=_MAX_REMOTE_OUTPUT_BYTES,
    )
    uncertain = _result(
        manifest, validated_target, generation_id, "uncertain", process.returncode
    )
    if process.termination_incomplete:
        _emit(progress, "uncertain")
        raise PublishRestartIntegrityError(
            "Publish restart process termination was incomplete", uncertain
        )
    if process.orphaned_descendants:
        _emit(progress, "uncertain")
        raise PublishRestartIntegrityError(
            "Publish restart left background processes after completion", uncertain
        )
    if (
        process.returncode is None
        and process.process_group is None
        and process.parent_process is None
    ):
        if process.cancelled:
            raise PublishRestartCancelled(
                "Publish restart was cancelled before SSH launch"
            )
        if process.timed_out:
            raise PublishRestartDeadlineExceeded(
                "Publish restart deadline exceeded before SSH launch"
            )

    response = _parse_response(process.stdout)
    try:
        if response is None:
            raise ValueError("Publish restart response is missing or malformed")
        status, returncode = _validate_response(
            response,
            operation_id=operation_id,
            manifest_digest=manifest.manifest_digest,
            target_config_digest=validated_target.config_digest,
            generation_id=generation_id,
        )
    except ValueError as error:
        _emit(progress, "uncertain")
        raise PublishRestartIntegrityError(str(error), uncertain) from error

    if (
        process.cancelled
        or process.timed_out
        or process.output_limit_exceeded
        or process.returncode != 0
        or status == "uncertain"
    ):
        _emit(progress, "uncertain")
        raise PublishRestartIntegrityError(
            "Publish restart outcome is uncertain",
            _result(
                manifest,
                validated_target,
                generation_id,
                "uncertain",
                returncode,
            ),
        )
    if status == "failed":
        _emit(progress, "failed")
        return _result(
            manifest,
            validated_target,
            generation_id,
            "failed",
            returncode,
        )
    _emit(progress, "succeeded")
    return _result(
        manifest,
        validated_target,
        generation_id,
        "succeeded",
        returncode,
    )
