"""Semantic verification and atomic activation of Publish generations."""

from __future__ import annotations

from dataclasses import dataclass
import time
import threading
from typing import Callable
from uuid import uuid4
import tomllib

import packctl
from pack_publish import PackPublishError, PackPublishManifest, PublishFileEntry, validate_publish_manifest
from process_runner import BoundedProcessResult, process_failure_message
from publish_target import PublishRemoteTarget
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


@dataclass(frozen=True)
class PublishSemanticVerification:
    manifest_digest: str
    target_config_digest: str
    generation_id: str
    pack_toml_sha256: str
    index_toml_sha256: str
    verified_file_count: int


_VERIFY_TIMEOUT_SECONDS = 600.0
_DESCRIPTOR_NAMES = frozenset({"pack.toml", "index.toml"})


def _deadline(value: float | None) -> float:
    return value if value is not None else time.monotonic() + _VERIFY_TIMEOUT_SECONDS


def _checkpoint(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PublishSemanticVerificationError("Publish semantic verification was cancelled")
    if time.monotonic() >= deadline:
        raise PublishSemanticVerificationError("Publish semantic verification deadline exceeded")


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
) -> dict[str, object]:
    return build_publish_remote_header(
        "verify",
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
    )
