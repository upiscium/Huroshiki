"""Semantic verification and atomic activation of Publish generations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import time
import threading
import re
from typing import Callable
from uuid import uuid4
import tomllib

import packctl
from pack_publish import PackPublishError, PackPublishManifest, PublishFileEntry, validate_publish_manifest
from process_runner import BoundedProcessResult, process_failure_message
from publish_target import (
    PublishRemoteTarget,
    PublishTargetError,
    publish_remote_target_from_legacy_settings,
)
from publish_transfer import (
    PublishStagedFile,
    PublishStagedGeneration,
    PublishTransferError,
    build_publish_remote_header,
    compute_publish_generation_id,
    run_publish_remote_control_request,
)


class PublishSemanticVerificationError(PublishTransferError):
    """The remote generation did not satisfy the semantic contract."""


class PublishSemanticVerificationUncertainError(PublishSemanticVerificationError):
    """Verification could not establish a bounded, trustworthy result."""


class PublishActivationError(PublishTransferError):
    """The requested generation was not activated."""

    def __init__(
        self,
        message: str,
        recovery_path: PurePosixPath | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path
        self.operation_id = operation_id


class PublishActivationUncertainError(PublishActivationError):
    """Activation outcome could not be classified safely."""


class PublishActivationCleanupError(PublishActivationError):
    """Activation temporary-entry cleanup is pending."""

    def __init__(
        self,
        message: str,
        recovery_path: PurePosixPath | None = None,
        operation_id: str | None = None,
        *,
        activated: PublishActivatedGeneration | None = None,
        expected_status: str | None = None,
    ) -> None:
        super().__init__(message, recovery_path, operation_id)
        self.activated = activated
        self.expected_status = expected_status


@dataclass(frozen=True)
class PublishSemanticVerification:
    manifest_digest: str
    target_config_digest: str
    generation_id: str
    pack_toml_sha256: str
    index_toml_sha256: str
    verified_file_count: int
    manifest: PackPublishManifest | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PublishActivatedGeneration:
    manifest_digest: str
    target_config_digest: str
    generation_id: str
    generation_path: PurePosixPath
    current_path: PurePosixPath
    previous_generation_id: str | None
    reused: bool


_VERIFY_TIMEOUT_SECONDS = 600.0
_DESCRIPTOR_NAMES = frozenset({"pack.toml", "index.toml"})
_GENERATION_ID_RE = re.compile(r"^v1-[0-9a-f]{64}$")


def _deadline(value: float | None) -> float:
    return value if value is not None else time.monotonic() + _VERIFY_TIMEOUT_SECONDS


def _checkpoint(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PublishSemanticVerificationError("Publish semantic verification was cancelled")
    if time.monotonic() >= deadline:
        raise PublishSemanticVerificationError("Publish semantic verification deadline exceeded")


def _activation_checkpoint(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PublishActivationError("Publish activation was cancelled")
    if time.monotonic() >= deadline:
        raise PublishActivationError("Publish activation deadline exceeded")


def _emit(progress: Callable[[str], object] | None, phase: str) -> None:
    if progress is None:
        return
    try:
        progress(phase)
    except Exception:
        pass


def _expected_staged_files(
    manifest: PackPublishManifest,
) -> tuple[PublishStagedFile, ...]:
    return tuple(
        PublishStagedFile(
            entry.relative_path,
            entry.size,
            entry.sha256,
            entry.mode,
        )
        for entry in manifest.files
    )


def _validate_inputs(
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
) -> tuple[PublishFileEntry, ...]:
    try:
        validate_publish_manifest(manifest)
    except PackPublishError as error:
        raise PublishSemanticVerificationError(str(error)) from error
    if not isinstance(staged, PublishStagedGeneration):
        raise PublishSemanticVerificationError("semantic verification requires a staged generation")
    if not isinstance(target, PublishRemoteTarget):
        raise PublishSemanticVerificationError("semantic verification requires a PublishRemoteTarget")
    expected_generation = compute_publish_generation_id(manifest, target)
    expected_path = target.publication_root / "generations" / expected_generation
    if staged.manifest_digest != manifest.manifest_digest:
        raise PublishSemanticVerificationError("staged generation manifest digest does not match manifest")
    if staged.target_config_digest != target.config_digest:
        raise PublishSemanticVerificationError("staged generation target digest does not match target")
    if staged.generation_id != expected_generation:
        raise PublishSemanticVerificationError("staged generation ID does not match manifest and target")
    if staged.generation_path != expected_path:
        raise PublishSemanticVerificationError("staged generation path is not canonical")
    if staged.total_bytes != manifest.total_bytes:
        raise PublishSemanticVerificationError("staged generation byte total does not match manifest")
    if staged.files != _expected_staged_files(manifest):
        raise PublishSemanticVerificationError("staged generation files do not match manifest")
    descriptors: dict[str, PublishFileEntry] = {}
    for entry in manifest.files:
        if entry.relative_path.as_posix() in _DESCRIPTOR_NAMES:
            descriptors[entry.relative_path.as_posix()] = entry
    if set(descriptors) != _DESCRIPTOR_NAMES or any(
        entry.source_kind != "generated" or entry.contents is None
        for entry in descriptors.values()
    ):
        raise PublishSemanticVerificationError("manifest descriptors are not generated files")
    for name, entry in descriptors.items():
        try:
            tomllib.loads(entry.contents.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as error:
            raise PublishSemanticVerificationError(f"manifest {name} is not valid TOML") from error
    return manifest.files


def _resolve_current_publish_target(
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
        raise PublishTargetError(str(error)) from error


def _check_current_publish_target(
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    error_type: type[PublishTransferError],
) -> None:
    try:
        current = _resolve_current_publish_target(manifest, target)
    except PublishTargetError as error:
        raise error_type("current Publish target could not be resolved") from error
    if current.config_digest != target.config_digest:
        raise error_type("Publish target configuration is stale")


def _header_files(files: tuple[PublishFileEntry, ...]) -> list[dict[str, object]]:
    return [
        {
            "path": entry.relative_path.as_posix(),
            "size": entry.size,
            "sha256": entry.sha256,
            "mode": entry.mode,
            "source_kind": entry.source_kind,
        }
        for entry in files
    ]


def _verification_header(
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    operation_id: str,
    files: tuple[PublishFileEntry, ...],
    request: str = "verify",
    previous_generation_id: str | None = None,
) -> dict[str, object]:
    return build_publish_remote_header(
        request,
        operation_id=operation_id,
        manifest_digest=manifest.manifest_digest,
        source_snapshot_digest=manifest.source_snapshot_digest,
        target_config_digest=target.config_digest,
        generation_id=staged.generation_id,
        publication_root=target.publication_root,
        files=_header_files(files),
        total_bytes=manifest.total_bytes,
        semantic={
            "target_side": manifest.target_side,
            "minecraft_version": manifest.minecraft_version,
            "loader": manifest.loader,
            "loader_version": manifest.loader_version,
            "loader_names": sorted(packctl.LOADER_FLAGS),
            **(
                {"previous_generation_id": previous_generation_id}
                if previous_generation_id is not None
                else {}
            ),
        },
    )


def _lifecycle_failure(result: BoundedProcessResult) -> str | None:
    if result.termination_incomplete:
        return "Publish semantic verification process termination was incomplete"
    if result.orphaned_descendants:
        return "Publish semantic verification left background processes after completion"
    return None


def _response_error(response: dict[str, object] | None) -> str:
    if response is not None and response.get("error") is not None:
        return str(response["error"])
    return "remote semantic verification did not succeed"


def _validate_response(
    response: dict[str, object],
    *,
    operation_id: str,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    staged: PublishStagedGeneration,
    pack_digest: str,
    index_digest: str,
) -> None:
    expected = {
        "ok": True,
        "request": "verify",
        "status": "verified",
        "operation_id": operation_id,
        "manifest_digest": manifest.manifest_digest,
        "target_config_digest": target.config_digest,
        "generation_id": staged.generation_id,
        "pack_toml_sha256": pack_digest,
        "index_toml_sha256": index_digest,
    }
    for key, value in expected.items():
        if response.get(key) != value:
            raise PublishSemanticVerificationUncertainError(
                f"remote semantic verification response does not bind {key}"
            )


def verify_publish_generation(
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[str], object] | None = None,
) -> PublishSemanticVerification:
    files = _validate_inputs(staged, manifest, target)
    operation_deadline = _deadline(deadline)
    _checkpoint(cancel_event, operation_deadline)
    _check_current_publish_target(manifest, target, PublishSemanticVerificationError)
    _emit(progress, "verifying-generation")
    operation_id = uuid4().hex
    header = _verification_header(staged, manifest, target, operation_id, files)
    result, response = run_publish_remote_control_request(
        target,
        header,
        deadline=operation_deadline,
        cancel_event=cancel_event,
    )
    lifecycle_failure = _lifecycle_failure(result)
    if lifecycle_failure is not None:
        raise PublishSemanticVerificationUncertainError(lifecycle_failure)
    if not result.succeeded:
        failure = process_failure_message(result, label="Publish semantic verification")
        raise PublishSemanticVerificationError(failure or _response_error(response))
    if response is None or response.get("ok") is not True:
        raise PublishSemanticVerificationError(_response_error(response))
    pack_digest = next(entry.sha256 for entry in files if entry.relative_path.as_posix() == "pack.toml")
    index_digest = next(entry.sha256 for entry in files if entry.relative_path.as_posix() == "index.toml")
    _validate_response(
        response,
        operation_id=operation_id,
        manifest=manifest,
        target=target,
        staged=staged,
        pack_digest=pack_digest,
        index_digest=index_digest,
    )
    _emit(progress, "verified")
    return PublishSemanticVerification(
        manifest.manifest_digest,
        target.config_digest,
        staged.generation_id,
        pack_digest,
        index_digest,
        len(files),
        manifest,
    )


def _validate_activation_inputs(
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    verification: PublishSemanticVerification,
) -> tuple[tuple[PublishFileEntry, ...], str, str]:
    try:
        files = _validate_inputs(staged, manifest, target)
    except PublishSemanticVerificationError as error:
        raise PublishActivationError(str(error)) from error
    pack_digest = next(entry.sha256 for entry in files if entry.relative_path.as_posix() == "pack.toml")
    index_digest = next(entry.sha256 for entry in files if entry.relative_path.as_posix() == "index.toml")
    if not isinstance(verification, PublishSemanticVerification):
        raise PublishActivationError("activation requires semantic verification")
    expected = {
        "manifest_digest": manifest.manifest_digest,
        "target_config_digest": target.config_digest,
        "generation_id": staged.generation_id,
        "pack_toml_sha256": pack_digest,
        "index_toml_sha256": index_digest,
    }
    for key, value in expected.items():
        if getattr(verification, key) != value:
            raise PublishActivationError(f"verification token does not bind {key}")
    return files, pack_digest, index_digest


def _validate_activation_response(
    response: dict[str, object],
    *,
    request: str,
    status: str,
    operation_id: str,
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    pack_digest: str,
    index_digest: str,
) -> tuple[str | None, bool]:
    expected = {
        "ok": True,
        "request": request,
        "status": status,
        "operation_id": operation_id,
        "manifest_digest": manifest.manifest_digest,
        "target_config_digest": target.config_digest,
        "generation_id": staged.generation_id,
    }
    for key, value in expected.items():
        if response.get(key) != value:
            raise PublishActivationUncertainError(
                f"remote activation response does not bind {key}"
            )
    previous = response.get("previous_generation_id")
    if previous is not None and (
        not isinstance(previous, str) or _GENERATION_ID_RE.fullmatch(previous) is None
    ):
        raise PublishActivationUncertainError(
            "remote activation response contains an invalid previous generation"
        )
    current = response.get("current_generation_id")
    if status in {"activated", "reused"}:
        if (
            response.get("pack_toml_sha256") != pack_digest
            or response.get("index_toml_sha256") != index_digest
            or current != staged.generation_id
            or response.get("reused") is not (status == "reused")
        ):
            raise PublishActivationUncertainError(
                "remote activation response semantic binding is invalid"
            )
    elif current is not None and (
        not isinstance(current, str)
        or _GENERATION_ID_RE.fullmatch(current) is None
    ):
        raise PublishActivationUncertainError(
            "remote activation status contains an invalid current generation"
        )
    return previous, status == "reused"


def _activation_header(
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    operation_id: str,
    files: tuple[PublishFileEntry, ...],
    request: str,
    previous_generation_id: str | None = None,
) -> dict[str, object]:
    return _verification_header(
        staged,
        manifest,
        target,
        operation_id,
        files,
        request=request,
        previous_generation_id=previous_generation_id,
    )


def _activation_cleanup(
    target: PublishRemoteTarget,
    header: dict[str, object],
    *,
    deadline: float,
    finalize_receipt: bool,
    expected_status: str | None = None,
) -> None:
    try:
        result, response = run_publish_remote_control_request(
            target,
            {
                **header,
                "request": "activation-cleanup",
                "finalize_receipt": finalize_receipt,
                "expected_activation_status": expected_status,
            },
            deadline=min(deadline, time.monotonic() + 30.0),
            cancel_event=None,
        )
    except BaseException as error:
        raise PublishActivationCleanupError(
            "activation temporary cleanup could not be verified",
            target.publication_root / (".huroshiki-activation-" + str(header["operation_id"]) + ".json"),
            str(header["operation_id"]),
        ) from error
    lifecycle_failure = _lifecycle_failure(result)
    if (
        lifecycle_failure is not None
        or not result.succeeded
        or response is None
        or response.get("ok") is not True
        or response.get("request") != "activation-cleanup"
        or response.get("status") != "cleaned"
        or response.get("operation_id") != header["operation_id"]
        or response.get("manifest_digest") != header["manifest_digest"]
        or response.get("target_config_digest") != header["target_config_digest"]
        or response.get("generation_id") != header["generation_id"]
        or response.get("finalize_receipt") is not finalize_receipt
        or response.get("expected_activation_status") != expected_status
    ):
        raise PublishActivationCleanupError(
            lifecycle_failure or _response_error(response),
            target.publication_root / (".huroshiki-activation-" + str(header["operation_id"]) + ".json"),
            str(header["operation_id"]),
        )


def retry_publish_activation_cleanup(
    staged: PublishStagedGeneration,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    operation_id: str,
    *,
    deadline: float | None = None,
    finalize_receipt: bool = False,
    expected_status: str | None = None,
) -> None:
    """Retry cleanup after an activation outcome retained a recovery path.

    This operation removes only the operation-owned temporary symlink.  It
    never removes a generation or changes ``current``.  Receipt finalization
    is opt-in and must only be requested after a separately bound outcome is
    known.
    """
    try:
        files = _validate_inputs(staged, manifest, target)
    except PublishSemanticVerificationError as error:
        raise PublishActivationError(str(error)) from error
    if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise PublishActivationError("activation cleanup requires a valid operation ID")
    if finalize_receipt and expected_status not in {"activated", "reused", "not_activated"}:
        raise PublishActivationError("finalized activation cleanup requires a bound outcome")
    if not finalize_receipt and expected_status is not None:
        raise PublishActivationError("unfinalized activation cleanup cannot bind an outcome")
    operation_deadline = _deadline(deadline)
    if time.monotonic() >= operation_deadline:
        raise PublishActivationCleanupError(
            "activation cleanup deadline exceeded",
            target.publication_root / (".huroshiki-activation-" + operation_id + ".json"),
            operation_id,
        )
    header = _activation_header(
        staged,
        manifest,
        target,
        operation_id,
        files,
        "activation-cleanup",
    )
    _activation_cleanup(
        target,
        header,
        deadline=operation_deadline,
        finalize_receipt=finalize_receipt,
        expected_status=expected_status,
    )


def activate_publish_generation(
    staged: PublishStagedGeneration,
    verification: PublishSemanticVerification,
    target: PublishRemoteTarget,
    *,
    manifest: PackPublishManifest | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[str], object] | None = None,
) -> PublishActivatedGeneration:
    if manifest is None and isinstance(verification, PublishSemanticVerification):
        manifest = verification.manifest
    if manifest is None:
        raise PublishActivationError(
            "activation requires the manifest used for semantic verification"
        )
    files, pack_digest, index_digest = _validate_activation_inputs(
        staged,
        manifest,
        target,
        verification,
    )
    operation_deadline = _deadline(deadline)
    _activation_checkpoint(cancel_event, operation_deadline)
    _check_current_publish_target(manifest, target, PublishActivationError)
    _emit(progress, "activating-generation")
    operation_id = uuid4().hex
    header = _activation_header(
        staged,
        manifest,
        target,
        operation_id,
        files,
        "activate",
    )
    result, response = run_publish_remote_control_request(
        target,
        header,
        deadline=operation_deadline,
        cancel_event=cancel_event,
    )
    lifecycle_failure = _lifecycle_failure(result)
    if lifecycle_failure is not None:
        raise PublishActivationUncertainError(
            lifecycle_failure,
            target.publication_root / (".huroshiki-activation-" + operation_id + ".json"),
            operation_id,
        )
    if result.succeeded and response is not None and response.get("ok") is True:
        status = response.get("status")
        if status in {"activated", "reused"}:
            previous, reused = _validate_activation_response(
                response,
                request="activate",
                status=str(status),
                operation_id=operation_id,
                staged=staged,
                manifest=manifest,
                target=target,
                pack_digest=pack_digest,
                index_digest=index_digest,
            )
            activated = PublishActivatedGeneration(
                manifest.manifest_digest,
                target.config_digest,
                staged.generation_id,
                staged.generation_path,
                target.publication_root / "current",
                previous,
                reused,
            )
            try:
                _activation_cleanup(
                    target,
                    header,
                    deadline=operation_deadline,
                    finalize_receipt=True,
                    expected_status=str(status),
                )
            except PublishActivationCleanupError as error:
                raise PublishActivationCleanupError(
                    str(error),
                    error.recovery_path,
                    error.operation_id,
                    activated=activated,
                    expected_status=str(status),
                ) from error
            _emit(progress, "activated")
            return activated

    activation_failure = process_failure_message(result, label="Publish activation")
    if response is not None and response.get("error") is not None:
        activation_failure = str(response["error"])
    status_header = _activation_header(
        staged,
        manifest,
        target,
        operation_id,
        files,
        "activation-status",
    )
    status_deadline = min(operation_deadline, time.monotonic() + 30.0)
    try:
        status_result, status_response = run_publish_remote_control_request(
            target,
            status_header,
            deadline=status_deadline,
            cancel_event=None,
        )
    except BaseException as error:
        try:
            _activation_cleanup(
                target,
                status_header,
                deadline=operation_deadline,
                finalize_receipt=False,
            )
        except PublishActivationCleanupError as cleanup_error:
            raise PublishActivationUncertainError(
                "activation status is unavailable and temporary cleanup is pending",
                cleanup_error.recovery_path,
                operation_id,
            ) from cleanup_error
        raise PublishActivationUncertainError(
            f"{activation_failure or 'activation outcome is uncertain'}; activation status is unavailable",
            target.publication_root / (".huroshiki-activation-" + operation_id + ".json"),
            operation_id,
        ) from error
    status_lifecycle = _lifecycle_failure(status_result)
    if status_lifecycle is not None:
        raise PublishActivationUncertainError(
            status_lifecycle,
            target.publication_root / (".huroshiki-activation-" + operation_id + ".json"),
            operation_id,
        )
    if status_result.succeeded and status_response is not None and status_response.get("ok") is True:
        status = status_response.get("status")
        if status in {"activated", "reused"}:
            previous, reused = _validate_activation_response(
                status_response,
                request="activation-status",
                status=str(status),
                operation_id=operation_id,
                staged=staged,
                manifest=manifest,
                target=target,
                pack_digest=pack_digest,
                index_digest=index_digest,
            )
            activated = PublishActivatedGeneration(
                manifest.manifest_digest,
                target.config_digest,
                staged.generation_id,
                staged.generation_path,
                target.publication_root / "current",
                previous,
                reused,
            )
            try:
                _activation_cleanup(
                    target,
                    status_header,
                    deadline=operation_deadline,
                    finalize_receipt=True,
                    expected_status=str(status),
                )
            except PublishActivationCleanupError as error:
                raise PublishActivationCleanupError(
                    str(error),
                    error.recovery_path,
                    error.operation_id,
                    activated=activated,
                    expected_status=str(status),
                ) from error
            return activated
        if status in {"not_activated", "uncertain"}:
            if status == "not_activated":
                _validate_activation_response(
                    status_response,
                    request="activation-status",
                    status="not_activated",
                    operation_id=operation_id,
                    staged=staged,
                    manifest=manifest,
                    target=target,
                    pack_digest=pack_digest,
                    index_digest=index_digest,
                )
                _activation_cleanup(
                    target,
                    status_header,
                    deadline=operation_deadline,
                    finalize_receipt=True,
                    expected_status="not_activated",
                )
                raise PublishActivationError(
                    activation_failure or "Publish generation was not activated"
                )
            try:
                _activation_cleanup(
                    target,
                    status_header,
                    deadline=operation_deadline,
                    finalize_receipt=False,
                )
            except PublishActivationCleanupError as cleanup_error:
                raise PublishActivationUncertainError(
                    "Publish activation outcome is uncertain and temporary cleanup is pending",
                    cleanup_error.recovery_path,
                    operation_id,
                ) from cleanup_error
            raise PublishActivationUncertainError(
                activation_failure or "Publish activation outcome is uncertain",
                target.publication_root / (".huroshiki-activation-" + operation_id + ".json"),
                operation_id=operation_id,
            )
    try:
        _activation_cleanup(
            target,
            status_header,
            deadline=operation_deadline,
            finalize_receipt=False,
        )
    except PublishActivationCleanupError as cleanup_error:
        raise PublishActivationUncertainError(
            "Publish activation outcome is uncertain and temporary cleanup is pending",
            cleanup_error.recovery_path,
            operation_id,
        ) from cleanup_error
    raise PublishActivationUncertainError(
        activation_failure or "Publish activation outcome is uncertain",
        target.publication_root / (".huroshiki-activation-" + operation_id + ".json"),
        operation_id=operation_id,
    )
