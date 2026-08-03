"""Bounded Packwiz-native materialization for provider artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import tomllib
from typing import Callable, Literal
from urllib.parse import quote
from uuid import uuid4

import tomlkit

from dependency_equivalence import (
    DependencyCandidate,
    EquivalenceContext,
    MaterializedArtifact,
    parse_semantic_jar,
)
from portable_paths import portable_basename, portable_relative_path
from process_runner import BoundedProcessResult, run_bounded_process
from url_artifacts import download_url_artifact


INSTALLER_ENV = "HUROSHIKI_PACKWIZ_INSTALLER_JAR"
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
SUPPORTED_HASH_ALGORITHMS = {"sha1", "sha256", "sha512", "md5", "murmur2"}
METADATA_CURSEFORGE_MODE = "metadata:curseforge"
DownloadMode = Literal["url", "metadata:curseforge"]
MANUAL_DOWNLOAD_MARKERS = (
    "requires manual download",
    "manual download",
    "cannot be distributed automatically",
)


@dataclass(frozen=True)
class ProviderArtifactMetadata:
    relative: Path
    filename: str
    algorithm: str
    expected_hash: str
    mode: DownloadMode
    download_url: str | None
    contents: bytes
    curseforge_project_id: int | None
    curseforge_file_id: int | None


class ProviderArtifactError(RuntimeError):
    pass


def _process_ok(
    result: BoundedProcessResult, label: str, *, supports_manual_download: bool = False
) -> None:
    if result.termination_incomplete:
        raise ProviderArtifactError(f"{label} process termination was incomplete")
    if result.orphaned_descendants:
        raise ProviderArtifactError(f"{label} left background processes")
    if result.cancelled:
        raise ProviderArtifactError(f"{label} was cancelled")
    if result.timed_out:
        raise ProviderArtifactError(f"{label} deadline exceeded")
    if result.returncode != 0:
        if supports_manual_download and _requires_manual_download(
            result.stdout, result.stderr
        ):
            raise ProviderArtifactError(
                "CurseForge artifact requires manual download and cannot be automatically verified"
            )
        raise ProviderArtifactError(f"{label} failed with exit code {result.returncode}")


def _artifact_mode(raw_mode: object) -> DownloadMode:
    if raw_mode is None:
        return "url"
    mode = str(raw_mode).strip().lower()
    return "url" if mode in {"", "url"} else mode


def _parse_provider_identity(identity: str) -> tuple[str, int]:
    provider, _, project_id = identity.partition(":")
    if not project_id:
        raise ProviderArtifactError("provider identity must be provider:project-id")
    if provider.strip().lower() != "curseforge":
        raise ProviderArtifactError("provider artifact is not CurseForge")
    return "curseforge", _parse_positive_integer(
        project_id,
        "provider artifact has no positive numeric CurseForge project ID",
    )


def _parse_positive_integer(value: object, message: str) -> int:
    text = str(value).strip()
    if not text.isdecimal():
        raise ProviderArtifactError(message)
    number = int(text)
    if number <= 0:
        raise ProviderArtifactError(message)
    return number


def _requires_manual_download(stdout: str, stderr: str) -> bool:
    message = f"{stdout}\n{stderr}".lower()
    return any(marker in message for marker in MANUAL_DOWNLOAD_MARKERS)


def _validate_curseforge_metadata(document: object) -> tuple[int, int]:
    if not isinstance(document, dict):
        raise ProviderArtifactError("provider metadata has no update data")
    update = document.get("update")
    if not isinstance(update, dict):
        raise ProviderArtifactError("provider metadata has no update data")
    curseforge = update.get("curseforge")
    if not isinstance(curseforge, dict):
        raise ProviderArtifactError("provider metadata has no curseforge update section")
    project_id = _parse_positive_integer(
        curseforge.get("project-id", ""),
        "provider metadata has no positive numeric CurseForge project ID",
    )
    file_id = _parse_positive_integer(
        curseforge.get("file-id", curseforge.get("fileId", "")),
        "provider metadata has no positive numeric CurseForge file ID",
    )
    return project_id, file_id


def _metadata(candidate: DependencyCandidate) -> ProviderArtifactMetadata:
    try:
        relative = portable_relative_path(Path(candidate.relative_metadata_path))
        filename = portable_basename(
            candidate.filename, context="Provider artifact filename"
        )
        document = tomllib.loads(candidate.contents.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        raise ProviderArtifactError("provider metadata is invalid") from error
    download = document.get("download")
    if not isinstance(download, dict):
        raise ProviderArtifactError("provider metadata has no download mapping")
    algorithm = str(download.get("hash-format", "")).strip().lower()
    expected = str(download.get("hash", "")).strip().lower()
    mode = _artifact_mode(download.get("mode"))
    url = str(download.get("url", "")).strip()
    if algorithm not in SUPPORTED_HASH_ALGORITHMS:
        raise ProviderArtifactError("provider metadata uses an unsupported hash")
    if not expected:
        raise ProviderArtifactError("provider metadata has no declared hash")
    parsed = tomlkit.parse(candidate.contents.decode("utf-8"))
    url_value: str | None
    metadata_project_id: int | None = None
    metadata_file_id: int | None = None

    if mode == "url":
        if not url:
            raise ProviderArtifactError(
                "provider artifact URL mode requires an HTTP(S) URL"
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ProviderArtifactError(
                "provider artifact URL mode requires an HTTP(S) URL"
            )
        url_value = url
    elif mode == METADATA_CURSEFORGE_MODE:
        if url:
            raise ProviderArtifactError(
                "metadata:curseforge mode must not include download.url"
            )
        provider = candidate.provider_identity.split(":", 1)[0].strip().lower()
        if provider != "curseforge":
            raise ProviderArtifactError(
                "metadata:curseforge mode is unsupported for non-CurseForge metadata"
            )
        _, candidate_project_id = _parse_provider_identity(candidate.provider_identity)
        metadata_project_id, metadata_file_id = _validate_curseforge_metadata(document)
        if candidate_project_id != metadata_project_id:
            raise ProviderArtifactError(
                "provider identity project ID does not match metadata project-id"
            )
        url_value = None
    else:
        raise ProviderArtifactError(f"provider artifact mode {mode!r} is unsupported")

    parsed["side"] = "both"

    return ProviderArtifactMetadata(
        relative,
        filename,
        algorithm,
        expected,
        mode,
        url_value,
        tomlkit.dumps(parsed).encode("utf-8"),
        metadata_project_id,
        metadata_file_id,
    )


def _check(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderArtifactError("provider artifact materialization was cancelled")
    if time.monotonic() >= deadline:
        raise ProviderArtifactError("provider artifact materialization deadline exceeded")


def _murmur2_file(
    path: Path, cancel_event: threading.Event | None, deadline: float
) -> str:
    normalized_length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _check(cancel_event, deadline)
            normalized_length += sum(value not in {9, 10, 13, 32} for value in chunk)
    remaining = normalized_length
    value = 1 ^ remaining
    pending = bytearray()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _check(cancel_event, deadline)
            pending.extend(item for item in chunk if item not in {9, 10, 13, 32})
            offset = 0
            while len(pending) - offset >= 4:
                part = int.from_bytes(pending[offset : offset + 4], "little")
                part = (part * 0x5BD1E995) & 0xFFFFFFFF
                part ^= part >> 24
                part = (part * 0x5BD1E995) & 0xFFFFFFFF
                value = ((value * 0x5BD1E995) ^ part) & 0xFFFFFFFF
                offset += 4
                remaining -= 4
            if offset:
                del pending[:offset]
    if remaining == 3:
        value ^= pending[2] << 16
    if remaining >= 2:
        value ^= pending[1] << 8
    if remaining >= 1:
        value ^= pending[0]
        value = (value * 0x5BD1E995) & 0xFFFFFFFF
    value ^= value >> 13
    value = (value * 0x5BD1E995) & 0xFFFFFFFF
    value ^= value >> 15
    return str(value & 0xFFFFFFFF)


def _artifact_hash(
    path: Path,
    algorithm: str,
    cancel_event: threading.Event | None,
    deadline: float,
) -> tuple[str, str]:
    declared = None if algorithm == "murmur2" else hashlib.new(algorithm)
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _check(cancel_event, deadline)
            sha256.update(chunk)
            if declared is not None:
                declared.update(chunk)
    declared_value = (
        _murmur2_file(path, cancel_event, deadline)
        if declared is None
        else declared.hexdigest().lower()
    )
    return declared_value, sha256.hexdigest()


def materialize_provider_artifact(
    candidate: DependencyCandidate,
    context: EquivalenceContext,
    *,
    workspace: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
) -> MaterializedArtifact:
    """Materialize one fixed candidate below an operation-owned workspace."""
    workspace = Path(workspace)
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.is_symlink():
        raise ProviderArtifactError("transaction workspace must be an existing directory")
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderArtifactError("provider artifact materialization was cancelled")
    effective_deadline = deadline if deadline is not None else time.monotonic() + 120
    if time.monotonic() >= effective_deadline:
        raise ProviderArtifactError("provider artifact materialization deadline exceeded")
    metadata = _metadata(candidate)
    relative = metadata.relative
    filename = metadata.filename
    algorithm = metadata.algorithm
    expected_hash = metadata.expected_hash
    contents = metadata.contents

    root = Path(tempfile.mkdtemp(prefix="provider-artifact-", dir=workspace))
    project = root / "project"
    output = root / "output"
    project.mkdir()
    output.mkdir()
    remaining = effective_deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderArtifactError("provider artifact materialization deadline exceeded")

    downloaded = None
    downloaded_sha256: str | None = None
    server_started = False
    server: HTTPServer | None = None
    server_thread: threading.Thread | None = None
    if metadata.download_url is not None:
        downloaded = root / "downloaded.jar"
        try:
            download_url_artifact(
                metadata.download_url,
                cancel_event or threading.Event(),
                root / "logs",
                context.target_loader,
                MAX_ARTIFACT_BYTES,
                total_timeout_seconds=remaining,
                retained_path=downloaded,
            )
        except Exception as error:
            raise ProviderArtifactError(f"provider artifact download failed: {error}") from error

        declared_hash, downloaded_sha256 = _artifact_hash(
            downloaded, algorithm, cancel_event, effective_deadline
        )
        if declared_hash.lower() != expected_hash.lower():
            raise ProviderArtifactError("downloaded artifact hash does not match metadata")

    installer = os.environ.get(INSTALLER_ENV, "")
    installer_path = Path(installer)
    if not installer or not installer_path.is_file() or installer_path.is_symlink():
        raise ProviderArtifactError("Packwiz Installer is unavailable")

    try:
        local_url = None
        if downloaded is not None:
            artifact_route = f"/{uuid4().hex}/artifact.jar"

            class ArtifactHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                    if self.path != artifact_route:
                        self.send_error(404)
                        return
                    _check(cancel_event, effective_deadline)
                    self.connection.settimeout(
                        max(0.001, min(1.0, effective_deadline - time.monotonic()))
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/java-archive")
                    self.send_header("Content-Length", str(downloaded.stat().st_size))
                    self.end_headers()
                    with downloaded.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            _check(cancel_event, effective_deadline)
                            self.wfile.write(chunk)

                def log_message(self, _format: str, *_args: object) -> None:
                    return

            server = HTTPServer(("127.0.0.1", 0), ArtifactHandler)
            server_thread = threading.Thread(
                target=lambda: server.serve_forever(poll_interval=0.05),
                name="huroshiki-provider-artifact-server",
                daemon=False,
            )
            server_thread.start()
            server_started = True
            local_url = f"http://127.0.0.1:{server.server_port}{artifact_route}"

        document = tomlkit.parse(contents.decode("utf-8"))
        download = document["download"]
        if local_url is not None:
            download["url"] = local_url
            download["hash-format"] = "sha256"
            assert downloaded_sha256 is not None
            download["hash"] = downloaded_sha256
        contents = tomlkit.dumps(document).encode("utf-8")
        metadata = project / relative
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_bytes(contents)
        (project / "pack.toml").write_text(
            'name = "Artifact verification"\nauthor = "huroshiki"\nversion = "0"\n'
            'pack-format = "packwiz:1.1.0"\n\n[index]\nfile = "index.toml"\n'
            'hash-format = "sha256"\nhash = ""\n\n[versions]\n'
            f'minecraft = "{context.minecraft}"\n'
            f'{context.loader} = "{context.loader_version}"\n',
            encoding="utf-8",
        )
        (project / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        process_kwargs: dict[str, object] = {
            "cwd": project,
            "cancel_event": cancel_event,
            "deadline": effective_deadline,
        }
        if process_result_callback is not None:
            process_kwargs["result_callback"] = process_result_callback
        refresh = run_bounded_process(["packwiz", "refresh"], **process_kwargs)
        _process_ok(refresh, "packwiz refresh")
        pack_toml = project / "pack.toml"
        if not pack_toml.is_file() or pack_toml.is_symlink():
            raise ProviderArtifactError("packwiz refresh did not retain pack.toml")
        pack_url = "file://" + quote(str(pack_toml), safe="/:@")
        install = run_bounded_process(
            [
                "java",
                "-jar",
                str(installer_path),
                "--no-gui",
                "--side",
                "client",
                "--pack-folder",
                str(output),
                "--meta-file",
                "packwiz.json",
                pack_url,
            ],
            **process_kwargs,
        )
        _process_ok(
            install,
            "Packwiz Installer",
            supports_manual_download=True,
        )
    finally:
        if server is not None:
            try:
                if server_started:
                    server.shutdown()
            finally:
                server.server_close()
        if server_thread is not None and server is not None and server_started:
            server_thread.join(
                timeout=max(
                    0.0, min(5.0, effective_deadline - time.monotonic())
                )
            )
        if server_thread is not None and server_started and server_thread.is_alive():
            raise ProviderArtifactError("provider artifact server cleanup was incomplete")

    artifact = output / relative.parent / filename
    try:
        artifact.relative_to(output)
        artifact_stat = artifact.lstat()
    except (ValueError, OSError) as error:
        raise ProviderArtifactError(
            "Packwiz Installer did not produce the expected artifact"
        ) from error
    if not stat.S_ISREG(artifact_stat.st_mode):
        raise ProviderArtifactError("materialized artifact is not an ordinary file")
    if artifact_stat.st_size > MAX_ARTIFACT_BYTES:
        raise ProviderArtifactError("materialized artifact exceeds the size limit")
    declared_hash, artifact_sha256 = _artifact_hash(
        artifact, algorithm, cancel_event, effective_deadline
    )
    if declared_hash.lower() != expected_hash.lower():
        raise ProviderArtifactError("materialized artifact hash does not match metadata")
    try:
        semantic = parse_semantic_jar(artifact, context.target_loader)
    except Exception:
        semantic = None
    return MaterializedArtifact(artifact_sha256, semantic)


materialize_artifact = materialize_provider_artifact
