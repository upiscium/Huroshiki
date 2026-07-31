from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import threading
import time
import tomllib
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

import yaml

import packctl
from url_artifacts import DEFAULT_URL_MAX_JAR_SIZE_BYTES
from overlay_policy import scan_content_overlays
from pack_tree_policy import (
    PackMigrationTreeEntry,
    PackTreePolicyError,
    PackTreeScan,
    copy_pack_tree_snapshot,
    scan_pack_migration_source,
)


PACK_MIGRATION_TIMEOUT_SECONDS = 600.0
PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS = 10.0
PACK_MIGRATION_INCLUDE = (
    Path("pack.yaml"),
    Path("profiles.yaml"),
    Path("source"),
    Path("content"),
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CONFIG_MAX_BYTES = 2 * 1024 * 1024


class PackMigrationError(RuntimeError):
    pass


class PackMigrationResolutionDiagnostic(Protocol):
    def diagnostic_summary(self) -> dict[str, object]: ...


class PackMigrationCancelled(PackMigrationError):
    pass


class PackMigrationDeadlineExceeded(PackMigrationError):
    pass


class PackMigrationStale(PackMigrationError):
    pass


class PackMigrationCleanupError(PackMigrationError):
    pass


class PackMigrationPublicationError(PackMigrationError):
    pass


class PackMigrationPlanningError(PackMigrationError):
    def __init__(self, message: str, plan: "PackMigrationPlan") -> None:
        super().__init__(message)
        self.plan = plan


@dataclass(frozen=True)
class PackMigrationTarget:
    target_id: str
    display_name: str
    minecraft_version: str
    loader: Literal["neoforge", "forge", "fabric", "quilt"]
    loader_version: str
    mode: Literal["copy"] = "copy"

    def __post_init__(self) -> None:
        try:
            packctl.validate_pack_id(self.target_id)
            packctl.validate_project_creation_fields(
                display_name=self.display_name,
                minecraft=self.minecraft_version,
                loader_version=self.loader_version,
            )
        except packctl.ConfigError as error:
            raise PackMigrationError(str(error)) from error
        normalized_loader = self.loader.strip().lower()
        if normalized_loader not in {"neoforge", "forge", "fabric", "quilt"}:
            raise PackMigrationError("Unsupported target loader")
        if self.mode != "copy":
            raise PackMigrationError("Only copy Pack migration is supported")
        object.__setattr__(self, "loader", normalized_loader)
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "minecraft_version", self.minecraft_version.strip())
        object.__setattr__(self, "loader_version", self.loader_version.strip())


@dataclass(frozen=True)
class PackMigrationWarning:
    code: str
    message: str
    relative_path: Path | None = None
    acknowledgement_required: bool = False


@dataclass(frozen=True)
class PackMigrationChange:
    category: Literal["copy", "skip", "target-config", "warning"]
    relative_path: Path | None
    detail: str


@dataclass(frozen=True)
class PackMigrationSourceSnapshot:
    project_key: str
    project_identity: tuple[int, int]
    entries: tuple[PackMigrationTreeEntry, ...]
    pack_yaml_digest: str
    pack_toml_digest: str | None
    source_tree_digest: str
    content_tree_digest: str
    distribution_config_digest: str | None
    provider_metadata_digest: str
    url_max_jar_size_bytes: int
    url_allow_private_networks: bool
    total_files: int
    total_directories: int
    total_bytes: int
    snapshot_digest: str
    validation_errors: tuple[str, ...]
    _tree_scan: PackTreeScan = field(repr=False, compare=False)


@dataclass(frozen=True)
class PackMigrationValidationToken:
    plan_identity: int
    staging_content_digest: str
    staging_snapshot_digest: str
    target_snapshot: PackMigrationSourceSnapshot


def _checkpoint(
    cancel_event: threading.Event | None,
    deadline: float | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PackMigrationCancelled("Pack migration was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise PackMigrationDeadlineExceeded("Pack migration deadline exceeded")


def _semantic_tree_digest(
    entries: tuple[PackMigrationTreeEntry, ...],
    root: Path,
) -> str:
    selected = []
    for entry in entries:
        if entry.relative_path != root and root not in entry.relative_path.parents:
            continue
        selected.append(
            {
                "path": entry.relative_path.as_posix(),
                "kind": entry.kind,
                "mode": entry.mode,
                "size": entry.size,
                "digest": entry.digest,
                "errors": list(entry.errors),
            }
        )
    return hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _entry_map(scan: PackTreeScan) -> dict[Path, PackMigrationTreeEntry]:
    return {entry.relative_path: entry for entry in scan.entries}


def _open_relative_file(scan: PackTreeScan, relative: Path) -> tuple[int, list[int]]:
    root_fd = os.open(scan.root, _DIRECTORY_FLAGS)
    opened = [root_fd]
    current = root_fd
    try:
        for part in relative.parts[:-1]:
            current = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            opened.append(current)
        descriptor = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=current,
        )
        return descriptor, opened
    except BaseException:
        for item in reversed(opened):
            os.close(item)
        raise


def _read_scanned_file(scan: PackTreeScan, relative: Path) -> bytes | None:
    entry = _entry_map(scan).get(relative)
    if entry is None:
        return None
    if entry.kind != "file" or entry.errors or entry.size > _CONFIG_MAX_BYTES:
        raise PackMigrationError(f"Cannot read migration configuration: {relative}")
    descriptor, opened = _open_relative_file(scan, relative)
    try:
        metadata = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino) != (entry.device, entry.inode)
            or metadata.st_size != entry.size
            or metadata.st_mtime_ns != entry.mtime_ns
            or metadata.st_ctime_ns != entry.ctime_ns
        ):
            raise PackMigrationStale(f"Pack migration file changed: {relative}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _CONFIG_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _CONFIG_MAX_BYTES:
                raise PackMigrationError(f"Migration configuration is too large: {relative}")
        contents = b"".join(chunks)
        if hashlib.sha256(contents).hexdigest() != entry.digest:
            raise PackMigrationStale(f"Pack migration file changed: {relative}")
        return contents
    finally:
        os.close(descriptor)
        for item in reversed(opened):
            os.close(item)


def _yaml_mapping(contents: bytes | None, name: str) -> dict[str, Any]:
    if contents is None:
        return {}
    try:
        value = yaml.safe_load(contents.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise PackMigrationError(f"{name}: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PackMigrationError(f"{name} must contain a YAML mapping")
    return value


def _pack_versions_from_bytes(
    contents: bytes | None,
    name: str,
) -> tuple[str, str, str]:
    if contents is None:
        raise PackMigrationError(f"{name}: missing required file")
    try:
        document = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackMigrationError(f"{name}: {error}") from error
    versions = document.get("versions")
    if not isinstance(versions, dict):
        raise PackMigrationError(f"{name}: versions must be a mapping")
    minecraft = versions.get("minecraft")
    if not isinstance(minecraft, str) or not minecraft.strip():
        raise PackMigrationError(
            f"{name}: versions.minecraft must be a non-empty string"
        )
    loaders = [
        loader
        for loader in ("neoforge", "forge", "fabric", "quilt")
        if loader in versions
    ]
    if len(loaders) != 1:
        raise PackMigrationError(
            f"{name}: exactly one supported loader must be configured"
        )
    loader = loaders[0]
    loader_version = versions[loader]
    if not isinstance(loader_version, str) or not loader_version.strip():
        raise PackMigrationError(
            f"{name}: versions.{loader} must be a non-empty string"
        )
    return minecraft.strip(), loader, loader_version.strip()


def _snapshot_digest_payload(
    *,
    project_key: str,
    project_identity: tuple[int, int],
    scan: PackTreeScan,
    pack_yaml_digest: str,
    pack_toml_digest: str | None,
    source_tree_digest: str,
    content_tree_digest: str,
    distribution_config_digest: str | None,
    provider_metadata_digest: str,
    url_max_jar_size_bytes: int,
    url_allow_private_networks: bool,
    validation_errors: tuple[str, ...],
) -> str:
    payload = {
        "project_key": project_key,
        "project_identity": list(project_identity),
        "tree": scan.snapshot_digest,
        "pack_yaml": pack_yaml_digest,
        "pack_toml": pack_toml_digest,
        "source_tree": source_tree_digest,
        "content_tree": content_tree_digest,
        "distribution": distribution_config_digest,
        "providers": provider_metadata_digest,
        "url_max_jar_size_bytes": url_max_jar_size_bytes,
        "url_allow_private_networks": url_allow_private_networks,
        "validation_errors": list(validation_errors),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def snapshot_pack_migration_source_at(
    project_key: str,
    project_root: Path,
    repository_root: Path,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationSourceSnapshot:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    checkpoint = lambda: _checkpoint(cancel_event, effective_deadline)
    checkpoint()
    kind, separator, project_id = project_key.partition(":")
    if not separator or kind != "pack":
        raise PackMigrationError("Pack migration snapshots require pack:<id>")
    try:
        packctl.validate_pack_id(project_id)
    except packctl.ConfigError as error:
        raise PackMigrationError(str(error)) from error
    repository = Path(os.path.abspath(repository_root))
    root = Path(os.path.abspath(project_root))
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise PackMigrationError("Pack migration source escaped repository") from error
    try:
        scan = scan_pack_migration_source(root, checkpoint=checkpoint)
    except (OSError, PackTreePolicyError) as error:
        raise PackMigrationError(str(error)) from error
    entries = _entry_map(scan)
    validation_errors = [
        f"{entry.relative_path}: {message}"
        for entry in scan.entries
        for message in entry.errors
    ]
    pack_yaml = entries.get(Path("pack.yaml"))
    pack_toml = entries.get(Path("source/pack.toml"))
    index_toml = entries.get(Path("source/index.toml"))
    if pack_yaml is None or pack_yaml.kind != "file" or pack_yaml.digest is None:
        validation_errors.append("pack.yaml: missing required regular file")
        pack_yaml_digest = "missing"
    else:
        pack_yaml_digest = pack_yaml.digest
    if pack_toml is None or pack_toml.kind != "file" or pack_toml.digest is None:
        validation_errors.append("source/pack.toml: missing required regular file")
        pack_toml_digest = None
    else:
        pack_toml_digest = pack_toml.digest
    if index_toml is None or index_toml.kind != "file":
        validation_errors.append("source/index.toml: missing required regular file")
    try:
        committed = _yaml_mapping(_read_scanned_file(scan, Path("pack.yaml")), "pack.yaml")
        local = _yaml_mapping(
            _read_scanned_file(scan, Path("pack.local.yaml")),
            "pack.local.yaml",
        )
        if committed.get("id") != project_id:
            validation_errors.append(
                f"pack.yaml id must match source Pack ID {project_id}"
            )
        effective = packctl.prospective_pack_config(project_id, committed, local)
        distribution_payload = {
            "distribution": effective.get("distribution"),
            "minecraft_server": effective.get("minecraft_server"),
        }
        distribution_config_digest = hashlib.sha256(
            json.dumps(
                distribution_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        raw_url_limit = effective.get(
            "url_max_jar_size_bytes", DEFAULT_URL_MAX_JAR_SIZE_BYTES
        )
        if (
            isinstance(raw_url_limit, bool)
            or not isinstance(raw_url_limit, int)
            or raw_url_limit <= 0
        ):
            raise PackMigrationError("url_max_jar_size_bytes must be a positive integer")
        url_max_jar_size_bytes = raw_url_limit
        raw_private = effective.get("url_allow_private_networks", False)
        if not isinstance(raw_private, bool):
            raise PackMigrationError("url_allow_private_networks must be a boolean")
        url_allow_private_networks = raw_private
    except (packctl.ConfigError, PackMigrationError, TypeError, ValueError) as error:
        validation_errors.append(str(error))
        distribution_config_digest = None
        url_max_jar_size_bytes = DEFAULT_URL_MAX_JAR_SIZE_BYTES
        url_allow_private_networks = False
    try:
        _pack_versions_from_bytes(
            _read_scanned_file(scan, Path("source/pack.toml")),
            "source/pack.toml",
        )
    except PackMigrationError as error:
        validation_errors.append(str(error))
    content_scan = scan_content_overlays(root / "content", checkpoint=checkpoint)
    validation_errors.extend(
        f"content/{issue.relative_path}: {issue.message}"
        for issue in content_scan.issues
    )
    source_tree_digest = _semantic_tree_digest(scan.entries, Path("source"))
    content_tree_digest = _semantic_tree_digest(scan.entries, Path("content"))
    provider_payload = [
        {
            "path": entry.relative_path.as_posix(),
            "mode": entry.mode,
            "size": entry.size,
            "digest": entry.digest,
        }
        for entry in scan.entries
        if entry.relative_path.parts[:1] == ("source",)
        and entry.relative_path.name.endswith(".pw.toml")
    ]
    provider_metadata_digest = hashlib.sha256(
        json.dumps(provider_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    immutable_errors = tuple(validation_errors)
    snapshot_digest = _snapshot_digest_payload(
        project_key=project_key,
        project_identity=scan.root_identity,
        scan=scan,
        pack_yaml_digest=pack_yaml_digest,
        pack_toml_digest=pack_toml_digest,
        source_tree_digest=source_tree_digest,
        content_tree_digest=content_tree_digest,
        distribution_config_digest=distribution_config_digest,
        provider_metadata_digest=provider_metadata_digest,
        url_max_jar_size_bytes=url_max_jar_size_bytes,
        url_allow_private_networks=url_allow_private_networks,
        validation_errors=immutable_errors,
    )
    checkpoint()
    final_scan = scan_pack_migration_source(root, checkpoint=checkpoint)
    if final_scan != scan:
        raise PackMigrationStale("Pack changed while creating migration snapshot")
    return PackMigrationSourceSnapshot(
        project_key,
        scan.root_identity,
        scan.entries,
        pack_yaml_digest,
        pack_toml_digest,
        source_tree_digest,
        content_tree_digest,
        distribution_config_digest,
        provider_metadata_digest,
        url_max_jar_size_bytes,
        url_allow_private_networks,
        sum(entry.kind == "file" for entry in scan.entries),
        sum(entry.kind == "directory" for entry in scan.entries) - 1,
        sum(entry.size for entry in scan.entries if entry.kind == "file"),
        snapshot_digest,
        immutable_errors,
        scan,
    )


def _same_snapshot(
    left: PackMigrationSourceSnapshot,
    right: PackMigrationSourceSnapshot,
) -> bool:
    return (
        left.project_key == right.project_key
        and left.project_identity == right.project_identity
        and left.snapshot_digest == right.snapshot_digest
        and left.entries == right.entries
    )


class PackMigrationPlan:
    def __init__(
        self,
        *,
        source_key: str,
        source_root: Path,
        target_root: Path,
        target: PackMigrationTarget,
        source_snapshot: PackMigrationSourceSnapshot,
        transaction_root: Path,
        source_snapshot_root: Path,
        target_staging_root: Path,
        changes: tuple[PackMigrationChange, ...],
        warnings: tuple[PackMigrationWarning, ...],
        copied_files: int,
        copied_directories: int,
        copied_bytes: int,
        skipped_paths: tuple[Path, ...],
        lock_set: packctl.ProjectLockSet,
        transaction_identity: tuple[int, int],
        target_parent_identity: tuple[int, int],
        state: Literal["planning", "staged", "failed"] = "planning",
    ) -> None:
        self.source_key = source_key
        self.source_root = source_root
        self.target_root = target_root
        self.target = target
        self.source_snapshot = source_snapshot
        self.transaction_root = transaction_root
        self.source_snapshot_root = source_snapshot_root
        self.target_staging_root = target_staging_root
        self.changes = changes
        self.warnings = warnings
        self.copied_files = copied_files
        self.copied_directories = copied_directories
        self.copied_bytes = copied_bytes
        self.skipped_paths = skipped_paths
        self.cleanup_error: BaseException | None = None
        self._state = state
        self._lock_set = lock_set
        self._transaction_identity = transaction_identity
        self._target_parent_identity = target_parent_identity
        self._source_copy_identity: tuple[int, int] | None = None
        self._source_copy_content_digest: str | None = None
        self._source_copy_snapshot_digest: str | None = None
        self._staging_identity: tuple[int, int] | None = None
        self._staging_snapshot_digest: str | None = None
        self._validation_token: PackMigrationValidationToken | None = None
        self._publication_committed = False
        self._publication_state: Literal[
            "not-published", "published", "uncertain"
        ] = "not-published"
        self._published_identity: tuple[int, int] | None = None
        self.resolution: PackMigrationResolutionDiagnostic | None = None
        self._resolver_work_root: Path | None = None
        self._resolver_work_identity: tuple[int, int] | None = None
        self._resolved_staging_digest: str | None = None
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        return self._state


def _identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PackMigrationError(f"Expected ordinary directory: {path}")
    return metadata.st_dev, metadata.st_ino


def _target_missing(
    target_root: Path,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bool:
    parent_fd = os.open(target_root.parent, _DIRECTORY_FLAGS)
    try:
        parent = os.fstat(parent_fd)
        if expected_parent_identity is not None and (
            parent.st_dev,
            parent.st_ino,
        ) != expected_parent_identity:
            raise PackMigrationStale("Target Pack parent was replaced")
        try:
            os.stat(target_root.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(parent_fd)


def _warnings_and_skips(
    snapshot: PackMigrationSourceSnapshot,
) -> tuple[tuple[PackMigrationWarning, ...], tuple[Path, ...]]:
    roots = {entry.relative_path.parts[0] for entry in snapshot.entries[1:]}
    allowed = {path.parts[0] for path in PACK_MIGRATION_INCLUDE}
    skipped = tuple(Path(name) for name in sorted(roots - allowed))
    warnings: list[PackMigrationWarning] = []
    for path in skipped:
        if path == Path("dist"):
            code = "dist-skipped"
            message = "Generated dist/ content is not copied"
        elif path == Path("pack.local.yaml"):
            code = "machine-local-config-skipped"
            message = "Machine-local Pack configuration is not copied"
        else:
            code = "unknown-entry-skipped"
            message = f"Pack root entry is not in the migration allowlist: {path}"
        warnings.append(PackMigrationWarning(code, message, path))
    content_files = [
        entry
        for entry in snapshot.entries
        if entry.kind == "file" and entry.relative_path.parts[:1] == ("content",)
    ]
    if content_files:
        warnings.append(
            PackMigrationWarning(
                "content-compatibility-unknown",
                "Content compatibility with the requested target is not verified",
                Path("content"),
                True,
            )
        )
    if any("kubejs" in (part.casefold() for part in entry.relative_path.parts) for entry in content_files):
        warnings.append(
            PackMigrationWarning(
                "kubejs-compatibility-unknown",
                "KubeJS scripts may require target-version changes",
                Path("content"),
                True,
            )
        )
    if any(
        any(part.casefold() in {"config", "defaultconfigs"} for part in entry.relative_path.parts)
        for entry in content_files
    ):
        warnings.append(
            PackMigrationWarning(
                "loader-specific-config",
                "Configuration content may contain loader-specific settings",
                Path("content"),
                True,
            )
        )
    if any(
        entry.relative_path.name.endswith(".pw.toml")
        and entry.digest is not None
        for entry in snapshot.entries
    ):
        warnings.append(
            PackMigrationWarning(
                "url-provider-compatibility-pending",
                "Provider and URL compatibility will be checked by a later resolver",
                Path("source"),
                True,
            )
        )
    warnings.append(
        PackMigrationWarning(
            "resolver-pending",
            "MOD resolution and target Packwiz initialization are not implemented yet",
            acknowledgement_required=True,
        )
    )
    return tuple(warnings), skipped


def _source_versions(snapshot: PackMigrationSourceSnapshot) -> tuple[str, str, str]:
    contents = _read_scanned_file(snapshot._tree_scan, Path("source/pack.toml"))
    try:
        return _pack_versions_from_bytes(contents, "source/pack.toml")
    except PackMigrationError:
        return "", "", ""


def _write_plan_file(plan: PackMigrationPlan, repository_root: Path) -> None:
    transaction_relative = plan.transaction_root.relative_to(repository_root)
    payload = {
        "schema": 2 if plan.resolution is not None else 1,
        "source": plan.source_key,
        "target": {
            "id": plan.target.target_id,
            "minecraft": plan.target.minecraft_version,
            "loader": plan.target.loader,
            "loader_version": plan.target.loader_version,
        },
        "source_snapshot": plan.source_snapshot.snapshot_digest,
        "transaction": transaction_relative.as_posix(),
        "state": plan.state,
        "cleanup_error": (
            type(plan.cleanup_error).__name__
            if plan.cleanup_error is not None
            else None
        ),
        "publication_committed": plan._publication_committed,
        "owned_locks": list(plan._lock_set.owned_keys),
        "copied": {
            "files": plan.copied_files,
            "directories": plan.copied_directories,
            "bytes": plan.copied_bytes,
        },
        "skipped": [path.as_posix() for path in plan.skipped_paths],
        "warnings": [
            {
                "code": warning.code,
                "message": warning.message,
                "path": (
                    warning.relative_path.as_posix()
                    if warning.relative_path is not None
                    else None
                ),
                "acknowledgement_required": warning.acknowledgement_required,
            }
            for warning in plan.warnings
        ],
        "resolution": (
            plan.resolution.diagnostic_summary()
            if plan.resolution is not None
            else None
        ),
        "cleanup_incomplete": plan.cleanup_error is not None,
    }
    contents = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
    temporary_name = f".plan.json.huroshiki-{uuid4().hex}.tmp"
    descriptor = -1
    try:
        opened = os.fstat(transaction_fd)
        if (opened.st_dev, opened.st_ino) != plan._transaction_identity:
            raise PackMigrationStale("Pack migration transaction root was replaced")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=transaction_fd,
        )
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short plan diagnostic write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            current = os.stat("plan.json", dir_fd=transaction_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is None:
            packctl.renameat2(
                transaction_fd,
                temporary_name,
                transaction_fd,
                "plan.json",
                packctl.RENAME_NOREPLACE,
            )
        elif stat.S_ISREG(current.st_mode):
            packctl.renameat2(
                transaction_fd,
                temporary_name,
                transaction_fd,
                "plan.json",
                packctl.RENAME_EXCHANGE,
            )
            os.unlink(temporary_name, dir_fd=transaction_fd)
        else:
            raise PackMigrationStale("Pack migration plan diagnostic was replaced")
        os.fsync(transaction_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=transaction_fd)
        except FileNotFoundError:
            pass
        os.close(transaction_fd)


def _record_plan_diagnostic(plan: PackMigrationPlan) -> None:
    try:
        _write_plan_file(plan, plan.target_root.parent.parent)
    except BaseException:
        pass


def _make_transaction_root(
    transaction_parent: Path,
    *,
    prefix: str,
) -> tuple[Path, tuple[int, int]]:
    parent_fd = os.open(transaction_parent, _DIRECTORY_FLAGS)
    try:
        for _ in range(100):
            name = f"{prefix}{uuid4().hex}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                metadata = os.fstat(descriptor)
                bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != (
                    bound.st_dev,
                    bound.st_ino,
                ):
                    raise PackMigrationStale(
                        "Pack migration transaction changed while being created"
                    )
                return transaction_parent / name, (metadata.st_dev, metadata.st_ino)
            finally:
                os.close(descriptor)
        raise PackMigrationError("Could not allocate Pack migration transaction")
    finally:
        os.close(parent_fd)


def _cleanup_transaction(
    plan: PackMigrationPlan,
    deadline: float,
    *,
    preserve_diagnostic: bool = False,
) -> None:
    try:
        os.stat(plan.transaction_root, follow_symlinks=False)
    except FileNotFoundError:
        return
    if time.monotonic() >= deadline:
        raise PackMigrationCleanupError("Pack migration cleanup deadline exceeded")
    if _identity(plan.transaction_root) != plan._transaction_identity:
        raise PackMigrationCleanupError("Pack migration transaction root was replaced")
    checkpoint = lambda: _checkpoint(None, deadline)
    scan = scan_pack_migration_source(plan.transaction_root, checkpoint=checkpoint)
    if any(entry.kind == "invalid" or entry.errors for entry in scan.entries):
        raise PackMigrationCleanupError("Pack migration transaction contains unsafe entries")
    parent_fd = os.open(plan.transaction_root.parent, _DIRECTORY_FLAGS)
    root_fd = -1
    try:
        root_fd = os.open(
            plan.transaction_root.name,
            _DIRECTORY_FLAGS,
            dir_fd=parent_fd,
        )
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != plan._transaction_identity:
            raise PackMigrationCleanupError(
                "Pack migration transaction root was replaced"
            )
        _remove_directory_contents(
            root_fd,
            checkpoint,
            preserve={"plan.json"} if preserve_diagnostic else frozenset(),
        )
        bound = os.stat(
            plan.transaction_root.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (bound.st_dev, bound.st_ino) != plan._transaction_identity:
            raise PackMigrationCleanupError(
                "Pack migration transaction root changed during cleanup"
            )
        if preserve_diagnostic:
            return
        os.close(root_fd)
        root_fd = -1
        os.rmdir(plan.transaction_root.name, dir_fd=parent_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)
    try:
        os.stat(plan.transaction_root, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise PackMigrationCleanupError("Pack migration transaction cleanup was incomplete")


def _remove_directory_contents(
    directory_fd: int,
    checkpoint: Callable[[], None],
    *,
    preserve: set[str] | frozenset[str] = frozenset(),
) -> None:
    checkpoint()
    with os.scandir(directory_fd) as iterator:
        names = sorted(entry.name for entry in iterator)
    for name in names:
        checkpoint()
        if name in preserve:
            continue
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise PackMigrationCleanupError(
                f"Unsafe entry appeared during Pack migration cleanup: {name}"
            )
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise PackMigrationCleanupError(
                    f"Directory changed during Pack migration cleanup: {name}"
                )
            _remove_directory_contents(child_fd, checkpoint)
            bound = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (bound.st_dev, bound.st_ino):
                raise PackMigrationCleanupError(
                    f"Directory changed during Pack migration cleanup: {name}"
                )
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=directory_fd)


def plan_pack_copy_migration_at(
    source_key: str,
    source_root: Path,
    target_root: Path,
    transaction_parent: Path,
    target: PackMigrationTarget,
    *,
    expected_snapshot: PackMigrationSourceSnapshot,
    repository_root: Path,
    state_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationPlan:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    checkpoint = lambda: _checkpoint(cancel_event, effective_deadline)
    checkpoint()
    source_id = source_key.partition(":")[2]
    if source_key != f"pack:{source_id}" or source_id == target.target_id:
        raise PackMigrationError("Source and target Pack IDs must be different")
    expected_target = Path(os.path.abspath(repository_root / "packs" / target.target_id))
    if Path(os.path.abspath(target_root)) != expected_target:
        raise PackMigrationError("Target Pack path was not derived from target ID")
    target_parent_identity = _identity(target_root.parent)
    if not _target_missing(target_root, target_parent_identity):
        raise PackMigrationError(f"Target Pack already exists: {target.target_id}")
    if expected_snapshot.project_key != source_key or expected_snapshot.validation_errors:
        raise PackMigrationError("Expected Pack migration snapshot is invalid")
    lock_set = packctl.acquire_project_locks(
        (source_key, f"pack:{target.target_id}"),
        deadline=effective_deadline,
        cancel_event=cancel_event,
        operation="Pack copy migration",
    )
    transaction_root: Path | None = None
    plan: PackMigrationPlan | None = None
    try:
        current = snapshot_pack_migration_source_at(
            source_key,
            source_root,
            repository_root,
            cancel_event=cancel_event,
            deadline=effective_deadline,
        )
        if not _same_snapshot(current, expected_snapshot):
            raise PackMigrationStale("Source Pack changed after migration snapshot")
        if not _target_missing(target_root, target_parent_identity):
            raise PackMigrationError(f"Target Pack already exists: {target.target_id}")
        packctl.make_state_directory(
            transaction_parent,
            state_root=state_root,
            repository_root=repository_root,
        )
        transaction_root, transaction_identity = _make_transaction_root(
            transaction_parent,
            prefix=f"pack-{source_id}-to-{target.target_id}-",
        )
        source_copy = transaction_root / "source-snapshot"
        staging = transaction_root / "target-staging"
        warnings, skipped = _warnings_and_skips(current)
        plan = PackMigrationPlan(
            source_key=source_key,
            source_root=source_root,
            target_root=target_root,
            target=target,
            source_snapshot=current,
            transaction_root=transaction_root,
            source_snapshot_root=source_copy,
            target_staging_root=staging,
            changes=(),
            warnings=warnings,
            copied_files=0,
            copied_directories=0,
            copied_bytes=0,
            skipped_paths=skipped,
            lock_set=lock_set,
            transaction_identity=transaction_identity,
            target_parent_identity=target_parent_identity,
        )
        detached = copy_pack_tree_snapshot(
            current._tree_scan,
            source_copy,
            include=PACK_MIGRATION_INCLUDE,
            checkpoint=checkpoint,
            destination_parent_identity=plan._transaction_identity,
        )
        plan._source_copy_identity = detached.scan.root_identity
        plan._source_copy_content_digest = detached.scan.content_digest
        plan._source_copy_snapshot_digest = detached.scan.snapshot_digest
        staged = copy_pack_tree_snapshot(
            detached.scan,
            staging,
            include=PACK_MIGRATION_INCLUDE,
            checkpoint=checkpoint,
            destination_parent_identity=plan._transaction_identity,
        )
        plan._staging_identity = staged.scan.root_identity
        plan._staging_snapshot_digest = staged.scan.snapshot_digest
        old_minecraft, old_loader, old_loader_version = _source_versions(current)
        try:
            committed = _yaml_mapping(
                _read_scanned_file(current._tree_scan, Path("pack.yaml")),
                "pack.yaml",
            )
            old_display = str(committed.get("display_name", source_id))
        except PackMigrationError:
            old_display = source_id
        changes = [
            PackMigrationChange("copy", entry.relative_path, "Copy unchanged")
            for entry in current.entries[1:]
            if any(
                entry.relative_path == root or root in entry.relative_path.parents
                for root in PACK_MIGRATION_INCLUDE
            )
        ]
        changes.extend(
            PackMigrationChange("skip", path, "Excluded from migration staging")
            for path in skipped
        )
        for detail in (
            f"Pack ID: {source_id} -> {target.target_id}",
            f"Display name: {old_display} -> {target.display_name}",
            f"Minecraft: {old_minecraft} -> {target.minecraft_version}",
            f"Loader: {old_loader} -> {target.loader}",
            f"Loader version: {old_loader_version} -> {target.loader_version}",
        ):
            changes.append(PackMigrationChange("target-config", None, detail))
        changes.extend(
            PackMigrationChange("warning", warning.relative_path, warning.message)
            for warning in warnings
        )
        plan.changes = tuple(changes)
        plan.copied_files = staged.copied_files
        plan.copied_directories = staged.copied_directories
        plan.copied_bytes = staged.copied_bytes
        plan._state = "staged"
        _write_plan_file(plan, repository_root)
        return plan
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if plan is not None:
            plan._state = "failed"
            try:
                _cleanup_transaction(
                    plan,
                    time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS,
                )
            except BaseException as caught:
                cleanup_error = caught
                plan.cleanup_error = caught
                _record_plan_diagnostic(plan)
        elif transaction_root is not None:
            shutil.rmtree(transaction_root, ignore_errors=True)
        if cleanup_error is None:
            lock_set.release()
            raise
        raise PackMigrationPlanningError(
            f"{error}; Pack migration cleanup failed: {cleanup_error}",
            plan,
        ) from error


def _issue_pack_migration_validation_token(
    plan: PackMigrationPlan,
    *,
    repository_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationValidationToken:
    """Internal handoff for a future resolver and publication tests."""
    if plan.state != "staged":
        raise PackMigrationError("Only a staged Pack migration can be validated")
    scan = scan_pack_migration_source(
        plan.target_staging_root,
        checkpoint=lambda: _checkpoint(cancel_event, deadline),
    )
    snapshot = snapshot_pack_migration_source_at(
        f"pack:{plan.target.target_id}",
        plan.target_staging_root,
        repository_root,
        cancel_event=cancel_event,
        deadline=deadline,
    )
    if snapshot.validation_errors:
        raise PackMigrationError("Validated target staging is not a valid Pack")
    return PackMigrationValidationToken(
        id(plan),
        scan.content_digest,
        scan.snapshot_digest,
        snapshot,
    )


def _mark_pack_migration_plan_ready(
    plan: PackMigrationPlan,
    *,
    validation_token: PackMigrationValidationToken,
) -> None:
    with plan._lock:
        if plan.state != "staged" or validation_token.plan_identity != id(plan):
            raise PackMigrationError("Invalid Pack migration validation token")
        plan._validation_token = validation_token
        plan._state = "ready"


def _release_plan_locks(plan: PackMigrationPlan) -> None:
    try:
        plan._lock_set.release()
    except BaseException as error:
        plan.cleanup_error = error
        raise PackMigrationCleanupError(
            f"Could not release Pack migration locks: {error}"
        ) from error


def _finish_committed_publication(plan: PackMigrationPlan, deadline: float) -> None:
    _cleanup_transaction(plan, deadline, preserve_diagnostic=True)
    _release_plan_locks(plan)
    plan._state = "applied"
    try:
        _cleanup_transaction(
            plan,
            time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS,
        )
    except BaseException as error:
        plan.cleanup_error = error
        return
    plan.cleanup_error = None


def _matches_validated_target(
    snapshot: PackMigrationSourceSnapshot,
    token: PackMigrationValidationToken,
) -> bool:
    return (
        not snapshot.validation_errors
        and snapshot._tree_scan.content_digest == token.staging_content_digest
        and snapshot.pack_yaml_digest == token.target_snapshot.pack_yaml_digest
        and snapshot.pack_toml_digest == token.target_snapshot.pack_toml_digest
        and snapshot.source_tree_digest == token.target_snapshot.source_tree_digest
        and snapshot.content_tree_digest == token.target_snapshot.content_tree_digest
        and snapshot.provider_metadata_digest
        == token.target_snapshot.provider_metadata_digest
    )


def apply_pack_copy_migration_at(
    plan: PackMigrationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationSourceSnapshot:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    checkpoint = lambda: _checkpoint(cancel_event, effective_deadline)
    with plan._lock:
        if plan._publication_committed:
            try:
                published = snapshot_pack_migration_source_at(
                    f"pack:{plan.target.target_id}",
                    plan.target_root,
                    plan.target_root.parent.parent,
                    cancel_event=cancel_event,
                    deadline=effective_deadline,
                )
                token = plan._validation_token
                if token is None or not _matches_validated_target(published, token):
                    raise PackMigrationPublicationError(
                        "Published target changed before cleanup retry"
                    )
                _finish_committed_publication(plan, effective_deadline)
                return published
            except BaseException as error:
                plan._state = "failed"
                plan.cleanup_error = error
                _record_plan_diagnostic(plan)
                raise
        token = plan._validation_token
        if plan.state != "ready" or token is None or token.plan_identity != id(plan):
            raise PackMigrationError(
                f"Pack migration plan cannot be applied from state {plan.state}"
            )
        if set(plan._lock_set.owned_keys) != {
            plan.source_key,
            f"pack:{plan.target.target_id}",
        }:
            raise PackMigrationError("Pack migration locks are not fully owned")
        checkpoint()
        if _identity(plan.transaction_root) != plan._transaction_identity:
            raise PackMigrationStale("Pack migration transaction root was replaced")
        if _identity(plan.target_staging_root) != plan._staging_identity:
            raise PackMigrationStale("Pack migration staging root was replaced")
        if _identity(plan.source_snapshot_root) != plan._source_copy_identity:
            raise PackMigrationStale("Detached source snapshot root was replaced")
        detached_scan = scan_pack_migration_source(
            plan.source_snapshot_root,
            checkpoint=checkpoint,
        )
        if detached_scan.content_digest != plan._source_copy_content_digest:
            raise PackMigrationStale("Detached source snapshot changed after planning")
        if detached_scan.snapshot_digest != plan._source_copy_snapshot_digest:
            raise PackMigrationStale("Detached source snapshot identity changed after planning")
        current_source = snapshot_pack_migration_source_at(
            plan.source_key,
            plan.source_root,
            plan.source_root.parent.parent,
            cancel_event=cancel_event,
            deadline=effective_deadline,
        )
        if not _same_snapshot(current_source, plan.source_snapshot):
            raise PackMigrationStale("Source Pack changed after migration planning")
        staged_scan = scan_pack_migration_source(
            plan.target_staging_root,
            checkpoint=checkpoint,
        )
        if staged_scan.content_digest != token.staging_content_digest:
            raise PackMigrationStale("Target staging changed after validation")
        if staged_scan.snapshot_digest != token.staging_snapshot_digest:
            raise PackMigrationStale("Target staging identity changed after validation")
        if not _target_missing(plan.target_root, plan._target_parent_identity):
            raise PackMigrationPublicationError("Target Pack appeared before publication")
        plan._state = "applying"
        transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
        target_parent_fd = os.open(plan.target_root.parent, _DIRECTORY_FLAGS)
        expected_identity = plan._staging_identity
        try:
            opened_transaction = os.fstat(transaction_fd)
            opened_target_parent = os.fstat(target_parent_fd)
            if (opened_transaction.st_dev, opened_transaction.st_ino) != (
                plan._transaction_identity
            ):
                raise PackMigrationStale(
                    "Pack migration transaction root was replaced before publication"
                )
            if (opened_target_parent.st_dev, opened_target_parent.st_ino) != (
                plan._target_parent_identity
            ):
                raise PackMigrationStale(
                    "Target Pack parent was replaced before publication"
                )
            try:
                plan._publication_state = "uncertain"
                packctl.renameat2(
                    transaction_fd,
                    plan.target_staging_root.name,
                    target_parent_fd,
                    plan.target_root.name,
                    packctl.RENAME_NOREPLACE,
                )
            except BaseException as rename_error:
                try:
                    target_metadata = os.stat(
                        plan.target_root.name,
                        dir_fd=target_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    target_metadata = None
                try:
                    staging_metadata = os.stat(
                        plan.target_staging_root.name,
                        dir_fd=transaction_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    staging_metadata = None
                target_identity = (
                    None
                    if target_metadata is None
                    else (target_metadata.st_dev, target_metadata.st_ino)
                )
                staging_identity = (
                    None
                    if staging_metadata is None
                    else (staging_metadata.st_dev, staging_metadata.st_ino)
                )
                if target_identity != expected_identity or staging_identity is not None:
                    if target_identity is None and staging_identity == expected_identity:
                        plan._publication_state = "not-published"
                    raise PackMigrationPublicationError(
                        "Pack migration publication result is ambiguous or raced"
                    ) from rename_error
                plan._publication_state = "published"
            published_metadata = os.stat(
                plan.target_root.name,
                dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
            if (published_metadata.st_dev, published_metadata.st_ino) != expected_identity:
                raise PackMigrationPublicationError(
                    "Published target identity does not match validated staging"
                )
            plan._publication_committed = True
            plan._publication_state = "published"
            plan._published_identity = expected_identity
        except BaseException:
            plan._state = "failed"
            _record_plan_diagnostic(plan)
            raise
        finally:
            os.close(target_parent_fd)
            os.close(transaction_fd)
        published = snapshot_pack_migration_source_at(
            f"pack:{plan.target.target_id}",
            plan.target_root,
            plan.target_root.parent.parent,
            cancel_event=cancel_event,
            deadline=effective_deadline,
        )
        if not _matches_validated_target(published, token):
            plan._state = "failed"
            _record_plan_diagnostic(plan)
            raise PackMigrationPublicationError("Published target snapshot verification failed")
        try:
            _finish_committed_publication(plan, effective_deadline)
        except BaseException as error:
            plan.cleanup_error = error
            plan._state = "failed"
            _record_plan_diagnostic(plan)
            raise
        return published


def discard_pack_migration_plan(
    plan: PackMigrationPlan,
    *,
    deadline: float | None = None,
) -> None:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS
    )
    with plan._lock:
        if (
            plan._publication_state != "not-published"
            or plan._publication_committed
            or plan.state in {"applied", "applying"}
        ):
            raise PackMigrationError("A published Pack migration cannot be discarded")
        if plan.state == "discarded":
            return
        plan._state = "discarding"
        try:
            _cleanup_transaction(
                plan,
                effective_deadline,
                preserve_diagnostic=True,
            )
            _release_plan_locks(plan)
        except BaseException as error:
            plan.cleanup_error = error
            plan._state = "failed"
            _record_plan_diagnostic(plan)
            raise
        plan._state = "discarded"
        try:
            _cleanup_transaction(
                plan,
                time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS,
            )
        except BaseException as error:
            plan.cleanup_error = error
            return
        plan.cleanup_error = None


__all__ = [
    "PackMigrationChange",
    "PackMigrationPlan",
    "PackMigrationSourceSnapshot",
    "PackMigrationTarget",
    "PackMigrationTreeEntry",
    "PackMigrationWarning",
    "apply_pack_copy_migration_at",
    "discard_pack_migration_plan",
    "plan_pack_copy_migration_at",
    "snapshot_pack_migration_source_at",
]
