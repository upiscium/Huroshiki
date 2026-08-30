from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import tomllib
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

import yaml

import packctl
from process_runner import BoundedProcessResult, stop_process_group
from url_artifacts import DEFAULT_URL_MAX_JAR_SIZE_BYTES
from overlay_policy import scan_content_overlays
from pack_tree_policy import (
    PackMigrationTreeEntry,
    PackTreePolicyError,
    PackTreeScan,
    copy_pack_tree_snapshot,
    scan_pack_migration_source,
)
from pack_migration_version_intent import (
    DetachedVersionIntentMetadata,
    PackMigrationVersionIntentError,
    validate_detached_version_intent,
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
_PUBLICATION_SECRET = object()


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
    minecraft_version: str | None
    loader: str | None
    loader_version: str | None
    _tree_scan: PackTreeScan = field(repr=False, compare=False)


@dataclass(frozen=True)
class _PackMigrationValidationToken:
    plan_identity: int
    resolution_attempt: int
    source_snapshot_digest: str
    staging_content_digest: str
    staging_snapshot_digest: str
    staging_identity: tuple[int, int]
    resolution_digest: str
    acknowledged_warning_digest: str
    target_snapshot: PackMigrationSourceSnapshot


class PackMigrationPublicationPlan:
    """Opaque, digest-bound handoff from resolution to publication.

    The constructor is intentionally private-by-convention; callers obtain one
    only from ``prepare_pack_migration_publication``.
    """

    __slots__ = ("_plan", "_token")

    def __init__(self, plan: "PackMigrationPlan", token: _PackMigrationValidationToken, *, _secret: object) -> None:
        if _secret is not _PUBLICATION_SECRET:
            raise TypeError("Pack migration publication handoffs are issued by the resolver")
        self._plan = plan
        self._token = token


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
    source_versions: tuple[str, str, str] | None = None
    try:
        source_versions = _pack_versions_from_bytes(
            _read_scanned_file(scan, Path("source/pack.toml")),
            "source/pack.toml",
        )
    except PackMigrationError as error:
        validation_errors.append(str(error))
    try:
        validate_detached_version_intent(
            root / "source",
            metadata=tuple(
                DetachedVersionIntentMetadata(
                    entry.relative_path.relative_to("source"),
                    _read_scanned_file(scan, entry.relative_path) or b"",
                )
                for entry in scan.entries
                if entry.kind == "file"
                and entry.relative_path.parts[:1] == ("source",)
                and entry.relative_path.name.endswith(".pw.toml")
            ),
            checkpoint=checkpoint,
        )
    except (PackMigrationError, PackMigrationVersionIntentError) as error:
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
        source_versions[0] if source_versions is not None else None,
        source_versions[1] if source_versions is not None else None,
        source_versions[2] if source_versions is not None else None,
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
        self._resolved_source_snapshot_digest: str | None = None
        self._validation_token: _PackMigrationValidationToken | None = None
        self._acknowledged_warning_codes: tuple[str, ...] = ()
        self._publication_committed = False
        self._publication_state: Literal[
            "not-published", "published", "uncertain"
        ] = "not-published"
        self._published_identity: tuple[int, int] | None = None
        self.operation_id = uuid4().hex
        self.resolution: PackMigrationResolutionDiagnostic | None = None
        self._resolver_work_root: Path | None = None
        self._resolver_work_identity: tuple[int, int] | None = None
        self._resolved_staging_digest: str | None = None
        self._provenance_committed = False
        self._resolution_attempt = 0
        self._resolution_input_digest: str | None = None
        self._active_resolution_request: object | None = None
        self._previous_resolution: PackMigrationResolutionDiagnostic | None = None
        self._explicit_removed_roots: tuple[str, ...] = ()
        self._explicit_replaced_roots: tuple[tuple[str, str], ...] = ()
        self._conflict_removed_roots: tuple[object, ...] = ()
        self._conflict_replaced_roots: tuple[object, ...] = ()
        self._resolver_process_results: list[BoundedProcessResult] = []
        self._lock = threading.RLock()

    def _record_resolver_process_result(self, result: BoundedProcessResult) -> None:
        if result.termination_incomplete:
            self._resolver_process_results.append(result)

    def _retry_resolver_process_cleanup(self, deadline: float) -> None:
        remaining: list[BoundedProcessResult] = []
        for result in self._resolver_process_results:
            if result.process_group is None:
                remaining.append(result)
                continue
            cleanup = stop_process_group(
                result.process_group,
                parent=result.parent_process,
                cleanup_deadline=deadline,
            )
            if not (cleanup.group_drained and cleanup.parent_reaped):
                remaining.append(result)
        self._resolver_process_results = remaining
        if remaining:
            raise PackMigrationCleanupError(
                "Pack migration resolver process-group cleanup was incomplete"
            )

    @property
    def state(self) -> str:
        return self._state

    @property
    def publication_lifecycle(self) -> Literal["precommit", "uncertain", "committed", "discarded"]:
        """A read-only lifecycle classification for UI/coordinator callers."""
        if self._publication_committed:
            return "committed"
        if self._publication_state == "uncertain":
            return "uncertain"
        if self._state == "discarded":
            return "discarded"
        return "precommit"


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
        )
        )
    warnings.append(
        PackMigrationWarning(
            "resolver-pending",
            "MOD resolution and target Packwiz initialization are pending",
        )
    )
    return tuple(warnings), skipped


def _retire_resolution_pending_warnings(plan: PackMigrationPlan) -> None:
    pending_codes = {
        "resolver-pending",
        "url-provider-compatibility-pending",
    }
    retired_messages = {
        warning.message for warning in plan.warnings if warning.code in pending_codes
    }
    plan.warnings = tuple(
        warning for warning in plan.warnings if warning.code not in pending_codes
    )
    plan.changes = tuple(
        change
        for change in plan.changes
        if not (change.category == "warning" and change.detail in retired_messages)
    )


def _source_versions(snapshot: PackMigrationSourceSnapshot) -> tuple[str, str, str]:
    contents = _read_scanned_file(snapshot._tree_scan, Path("source/pack.toml"))
    try:
        return _pack_versions_from_bytes(contents, "source/pack.toml")
    except PackMigrationError:
        return "", "", ""


def _write_plan_file(plan: PackMigrationPlan, repository_root: Path) -> None:
    transaction_relative = plan.transaction_root.relative_to(repository_root)
    payload = {
        "schema": (
            3
            if plan._resolution_attempt > 0
            else 2 if plan.resolution is not None else 1
        ),
        "operation_id": plan.operation_id,
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
        "publication_state": plan._publication_state,
        "source_snapshot_digest": plan.source_snapshot.snapshot_digest,
        "formal_staging_digest": plan._resolved_staging_digest or plan._staging_snapshot_digest,
        "target_id": plan.target.target_id,
        "acknowledged_warning_codes": list(plan._acknowledged_warning_codes),
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
        "resolution_attempt": plan._resolution_attempt,
        "explicit_resolutions": {
            "removed": list(plan._explicit_removed_roots),
            "replaced": [
                {"from": old, "to": new}
                for old, new in plan._explicit_replaced_roots
            ],
        },
        "remaining_unresolved": (
            len(getattr(plan.resolution, "unresolved_roots", ()))
            if plan.resolution is not None
            else 0
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


def _minimal_planning_owner(
    *,
    source_key: str,
    source_root: Path,
    target_root: Path,
    target: PackMigrationTarget,
    transaction_root: Path,
    transaction_identity: tuple[int, int],
    lock_set: packctl.ProjectLockSet,
    source_snapshot: PackMigrationSourceSnapshot,
    target_parent_identity: tuple[int, int],
) -> PackMigrationPlan:
    """Create the smallest retained owner for failures during plan setup."""
    return PackMigrationPlan(
        source_key=source_key,
        source_root=source_root,
        target_root=target_root,
        target=target,
        source_snapshot=source_snapshot,
        transaction_root=transaction_root,
        source_snapshot_root=transaction_root / "source-snapshot",
        target_staging_root=transaction_root / "target-staging",
        changes=(),
        warnings=(),
        copied_files=0,
        copied_directories=0,
        copied_bytes=0,
        skipped_paths=(),
        lock_set=lock_set,
        transaction_identity=transaction_identity,
        target_parent_identity=target_parent_identity,
        state="failed",
    )


def _release_planning_locks(
    plan: PackMigrationPlan,
    planning_error: BaseException,
) -> None:
    """Release planning locks or retain the failed owner for cleanup retry."""
    try:
        plan._lock_set.release()
    except BaseException as release_error:
        plan.cleanup_error = release_error
        plan._state = "failed"
        _record_plan_diagnostic(plan)
        raise PackMigrationPlanningError(
            f"{planning_error}; Pack migration lock release failed: {release_error}",
            plan,
        ) from planning_error


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


def _same_config_snapshot(
    left: packctl.ConfigFileSnapshot,
    right: packctl.ConfigFileSnapshot,
) -> bool:
    return (
        left.exists == right.exists
        and left.mode == right.mode
        and left.device == right.device
        and left.inode == right.inode
        and left.bytes == right.bytes
        and left.digest == right.digest
    )


def _write_target_config_bytes(
    descriptor: int,
    contents: bytes,
    checkpoint: Callable[[], None],
) -> None:
    view = memoryview(contents)
    while view:
        checkpoint()
        written = os.write(descriptor, view[: 64 * 1024])
        if written == 0:
            raise OSError("short target Pack configuration write")
        view = view[written:]


def _stage_pack_migration_target_config(
    plan: PackMigrationPlan,
    source_scan: PackTreeScan,
    *,
    checkpoint: Callable[[], None],
) -> tuple[PackTreeScan, tuple[PackMigrationChange, ...]]:
    """Transform and atomically stage target ``pack.yaml`` from a fixed scan."""

    checkpoint()
    source_contents = _read_scanned_file(source_scan, Path("pack.yaml"))
    source_config = _yaml_mapping(source_contents, "pack.yaml")
    target_config = deepcopy(source_config)
    source_id = source_config.get("id")
    source_display_name = source_config.get("display_name", source_id)
    target_config["id"] = plan.target.target_id
    target_config["display_name"] = plan.target.display_name

    cleared: list[PackMigrationChange] = []
    distribution = target_config.get("distribution")
    if isinstance(distribution, dict):
        for key in ("rsync_target", "public_pack_url"):
            if key in distribution:
                del distribution[key]
                cleared.append(
                    PackMigrationChange(
                        "target-config", None, f"Cleared distribution.{key}"
                    )
                )
        # The complete section is operational in the current Pack schema. Drop
        # it even if a source accepted an additional committed field, rather
        # than carrying a future destination setting into the copied Pack.
        del target_config["distribution"]
    if "minecraft_server" in target_config:
        del target_config["minecraft_server"]
        cleared.append(
            PackMigrationChange(
                "target-config", None, "Cleared minecraft_server configuration"
            )
        )

    try:
        packctl.prospective_pack_config(plan.target.target_id, target_config, {})
    except (packctl.ConfigError, TypeError, ValueError) as error:
        raise PackMigrationError(f"Target Pack configuration is invalid: {error}") from error
    serialized = yaml.safe_dump(
        target_config,
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")

    current_scan = scan_pack_migration_source(
        plan.target_staging_root,
        checkpoint=checkpoint,
    )
    if (
        current_scan.root_identity != plan._staging_identity
        or current_scan.snapshot_digest != plan._staging_snapshot_digest
    ):
        raise PackMigrationStale("Target staging changed before configuration staging")
    source_entry = _entry_map(source_scan).get(Path("pack.yaml"))
    staging_entry = _entry_map(current_scan).get(Path("pack.yaml"))
    if (
        source_entry is None
        or staging_entry is None
        or source_entry.kind != "file"
        or staging_entry.kind != "file"
        or source_entry.digest != staging_entry.digest
        or source_entry.mode != staging_entry.mode
    ):
        raise PackMigrationStale("Staged Pack configuration does not match fixed source")

    temporary_name: str | None = None
    staged_snapshot: packctl.ConfigFileSnapshot | None = None
    expected_snapshot: packctl.ConfigFileSnapshot | None = None
    try:
        with packctl.open_config_directory(plan.target_staging_root) as directory:
            if (directory.device, directory.inode) != plan._staging_identity:
                raise PackMigrationStale("Target staging root was replaced")
            checkpoint()
            expected_snapshot = packctl.read_config_snapshot(directory, "pack.yaml")
            if (
                not expected_snapshot.exists
                or expected_snapshot.device != staging_entry.device
                or expected_snapshot.inode != staging_entry.inode
                or expected_snapshot.mode != staging_entry.mode
                or expected_snapshot.digest != staging_entry.digest
                or expected_snapshot.bytes != source_contents
            ):
                raise PackMigrationStale("Staged Pack configuration changed before replacement")
            mode = expected_snapshot.mode if expected_snapshot.mode is not None else 0o600
            descriptor, temporary_name = packctl.create_config_temp(
                directory,
                "pack.yaml",
                mode,
            )
            try:
                _write_target_config_bytes(descriptor, serialized, checkpoint)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            checkpoint()
            staged_snapshot = packctl.read_config_snapshot(directory, temporary_name)
            if (
                not staged_snapshot.exists
                or staged_snapshot.mode != mode
                or staged_snapshot.bytes != serialized
                or staged_snapshot.digest != hashlib.sha256(serialized).hexdigest()
            ):
                raise PackMigrationStale("Temporary target Pack configuration changed")
            packctl.check_config_directory_identity(directory)
            current_config = packctl.read_config_snapshot(directory, "pack.yaml")
            if not _same_config_snapshot(current_config, expected_snapshot):
                raise PackMigrationStale("Staged Pack configuration changed before replacement")
            checkpoint()
            packctl.renameat2(
                directory.fd,
                temporary_name,
                directory.fd,
                "pack.yaml",
                packctl.RENAME_EXCHANGE,
            )
            packctl.check_config_directory_identity(directory)
            published = packctl.read_config_snapshot(directory, "pack.yaml")
            exchanged = packctl.read_config_snapshot(directory, temporary_name)
            if not _same_config_snapshot(published, staged_snapshot):
                raise PackMigrationStale("Target Pack configuration replacement changed")
            if not _same_config_snapshot(exchanged, expected_snapshot):
                raise PackMigrationStale("Original Pack configuration exchange changed")
            os.fsync(directory.fd)
            checkpoint()
            os.unlink(temporary_name, dir_fd=directory.fd)
            temporary_name = None
            os.fsync(directory.fd)
            packctl.check_config_directory_identity(directory)
    except packctl.ConfigError as error:
        raise PackMigrationStale("Target Pack configuration path changed") from error
    finally:
        if temporary_name is not None:
            try:
                with packctl.open_config_directory(plan.target_staging_root) as directory:
                    temporary = packctl.read_config_snapshot(directory, temporary_name)
                    known_identities = {
                        (snapshot.device, snapshot.inode)
                        for snapshot in (staged_snapshot, expected_snapshot)
                        if snapshot is not None and snapshot.exists
                    }
                    if temporary.exists and (temporary.device, temporary.inode) in known_identities:
                        os.unlink(temporary_name, dir_fd=directory.fd)
                        os.fsync(directory.fd)
            except BaseException:
                pass

    checkpoint()
    result = scan_pack_migration_source(plan.target_staging_root, checkpoint=checkpoint)
    if result.root_identity != plan._staging_identity:
        raise PackMigrationStale("Target staging root changed after configuration staging")
    result_entry = _entry_map(result).get(Path("pack.yaml"))
    if (
        result_entry is None
        or result_entry.kind != "file"
        or result_entry.errors
        or result_entry.digest != hashlib.sha256(serialized).hexdigest()
        or _read_scanned_file(result, Path("pack.yaml")) != serialized
    ):
        raise PackMigrationStale("Target Pack configuration verification failed")
    if any(entry.kind == "invalid" or entry.errors for entry in result.entries):
        raise PackMigrationError("Target staging contains unsafe entries")
    return result, (
        PackMigrationChange(
            "target-config", None, f"Pack ID: {source_id} -> {plan.target.target_id}"
        ),
        PackMigrationChange(
            "target-config",
            None,
            f"Display name: {source_display_name} -> {plan.target.display_name}",
        ),
        *cleared,
    )


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
        staged_config, target_config_changes = _stage_pack_migration_target_config(
            plan,
            detached.scan,
            checkpoint=checkpoint,
        )
        plan._staging_identity = staged_config.root_identity
        plan._staging_snapshot_digest = staged_config.snapshot_digest
        old_minecraft, old_loader, old_loader_version = _source_versions(current)
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
        changes.extend(target_config_changes)
        for detail in (
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
            # The transaction already has an identity, so retain a minimal
            # owner rather than falling back to unbounded, silent deletion.
            plan = _minimal_planning_owner(
                source_key=source_key,
                source_root=source_root,
                target_root=target_root,
                target=target,
                transaction_root=transaction_root,
                transaction_identity=transaction_identity,
                lock_set=lock_set,
                source_snapshot=expected_snapshot,
                target_parent_identity=target_parent_identity,
            )
            try:
                _cleanup_transaction(
                    plan,
                    time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS,
                )
            except BaseException as caught:
                plan.cleanup_error = caught
                _record_plan_diagnostic(plan)
                raise PackMigrationPlanningError(
                    f"{error}; Pack migration cleanup failed: {caught}", plan
                ) from error
            _release_planning_locks(plan, error)
            raise
        if cleanup_error is None:
            if plan is None:
                lock_set.release()
                raise
            _release_planning_locks(plan, error)
            raise
        raise PackMigrationPlanningError(
            f"{error}; Pack migration cleanup failed: {cleanup_error}",
            plan,
        ) from error


def _publication_digest(value: object) -> str:
    def canonical(item: object) -> object:
        if isinstance(item, Path):
            return {"__path__": item.as_posix()}
        if is_dataclass(item) and not isinstance(item, type):
            return {
                field.name: canonical(getattr(item, field.name))
                for field in fields(item)
            }
        if isinstance(item, dict):
            return {
                str(key): canonical(value)
                for key, value in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, (tuple, list)):
            return [canonical(value) for value in item]
        if isinstance(item, (set, frozenset)):
            return sorted((canonical(value) for value in item), key=repr)
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        # Resolution plans are dataclasses today.  Binding an additive object
        # field by its public attributes keeps this digest fail-closed as the
        # resolution API grows, without relying on unstable object reprs.
        attributes = getattr(item, "__dict__", None)
        if isinstance(attributes, dict):
            return {key: canonical(value) for key, value in sorted(attributes.items())}
        return {"__type__": f"{type(item).__module__}.{type(item).__qualname__}",
                "__value__": str(item)}

    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _required_warning_digest(warnings: tuple[PackMigrationWarning, ...]) -> str:
    return _publication_digest(
        [
            {
                "code": warning.code,
                "message": warning.message,
                "path": warning.relative_path.as_posix()
                if warning.relative_path is not None
                else None,
                "acknowledgement_required": warning.acknowledgement_required,
                "identifier": _warning_identifier(warning),
            }
            for warning in warnings
        ]
    )


def _resolution_digest(resolution: object) -> str:
    # Digest every declared field recursively, including source roots,
    # dependency deltas, URL compatibility, warnings, and the full target
    # source scan.  This intentionally does not select a hand-maintained
    # subset: additive resolution intent must be covered by the handoff too.
    return _publication_digest(resolution)


def _warning_identifier(warning: PackMigrationWarning) -> str:
    return _publication_digest(
        {
            "code": warning.code,
            "message": warning.message,
            "path": warning.relative_path.as_posix() if warning.relative_path else None,
        }
    )


def prepare_pack_migration_publication(
    plan: PackMigrationPlan,
    resolution_plan: object,
    *,
    acknowledged_warning_codes: tuple[str, ...] = (),
    acknowledged_warnings: tuple[str, ...] | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationPublicationPlan:
    """Validate a resolved migration and create its opaque publication handoff."""
    effective_deadline = deadline if deadline is not None else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    checkpoint = lambda: _checkpoint(cancel_event, effective_deadline)

    def report(value: object) -> None:
        if progress is None:
            return
        try:
            progress(value)
        except Exception:
            pass

    checkpoint()
    with plan._lock:
        from pack_migration_resolution import PackMigrationProgress
        report(PackMigrationProgress(
                "validating-publication", 0, 1, None,
                "Validating migration publication",
            ))
        from pack_migration_resolution import PackMigrationResolutionPlan
        if plan.state != "resolved" or plan.resolution is not resolution_plan or not isinstance(resolution_plan, PackMigrationResolutionPlan):
            raise PackMigrationPublicationError("Pack migration resolution is not the current resolved result")
        if resolution_plan.state != "resolved" or resolution_plan.unresolved_roots or resolution_plan.provenance_required:
            raise PackMigrationPublicationError("Pack migration resolution is incomplete")
        if resolution_plan.path_collisions or resolution_plan.filename_collisions:
            raise PackMigrationPublicationError("Pack migration resolution retains path or filename collisions")
        if resolution_plan.source_snapshot_digest != plan.source_snapshot.snapshot_digest:
            raise PackMigrationStale("Pack migration resolution source snapshot is stale")
        if resolution_plan.target != plan.target:
            raise PackMigrationPublicationError("Pack migration resolution target does not match plan")
        if getattr(resolution_plan, "resolution_attempt", plan._resolution_attempt) != plan._resolution_attempt:
            raise PackMigrationStale("Pack migration resolution attempt is stale")
        required_warnings = tuple(
            warning for warning in plan.warnings if warning.acknowledgement_required
        )
        required = {warning.code for warning in required_warnings}
        identifiers = {
            identifier
            for warning in required_warnings
            for identifier in (warning.code, _warning_identifier(warning))
        }
        if acknowledged_warnings is not None:
            if acknowledged_warning_codes:
                raise PackMigrationPublicationError("Warning acknowledgements were supplied twice")
            acknowledged_warning_codes = acknowledged_warnings
        supplied = tuple(acknowledged_warning_codes)
        if any(not isinstance(code, str) or not code.strip() for code in supplied):
            raise PackMigrationPublicationError("Warning acknowledgement codes must be non-empty strings")
        if len(set(supplied)) != len(supplied) or not set(supplied).issubset(identifiers):
            raise PackMigrationPublicationError("Warning acknowledgement set is incomplete or unknown")
        normalized_acknowledgements = {
            warning.code
            for warning in required_warnings
            if warning.code in supplied or _warning_identifier(warning) in supplied
        }
        if len(normalized_acknowledgements) != len(supplied) or normalized_acknowledgements != required:
            raise PackMigrationPublicationError("Warning acknowledgement set is incomplete or unknown")
        checkpoint()
        resolved_source = scan_pack_migration_source(
            plan.target_staging_root / "source", checkpoint=checkpoint
        )
        expected = resolution_plan.target_source_snapshot
        if (
            expected is None
            or resolved_source.root_identity != expected.root_identity
            or resolved_source.snapshot_digest != expected.snapshot_digest
            or resolved_source.content_digest != expected.content_digest
        ):
            raise PackMigrationStale(
                "Formal target staging source does not match resolved output"
            )
        staging = scan_pack_migration_source(
            plan.target_staging_root, checkpoint=checkpoint
        )
        _validate = snapshot_pack_migration_source_at(
            f"pack:{plan.target.target_id}", plan.target_staging_root,
            plan.target_root.parent.parent, cancel_event=cancel_event, deadline=effective_deadline,
        )
        if not _matches_validated_target(
            _validate,
            _PackMigrationValidationToken(
                id(plan),
                plan._resolution_attempt,
                plan.source_snapshot.snapshot_digest,
                staging.content_digest,
                staging.snapshot_digest,
                staging.root_identity,
                _resolution_digest(resolution_plan),
                _required_warning_digest(plan.warnings),
                _validate,
            ),
        ):
            raise PackMigrationStale("Formal target staging semantic validation failed")
        current_source = snapshot_pack_migration_source_at(plan.source_key, plan.source_root, plan.source_root.parent.parent, cancel_event=cancel_event, deadline=effective_deadline)
        if not _same_snapshot(current_source, plan.source_snapshot):
            raise PackMigrationStale("Source Pack changed before publication")
        detached = scan_pack_migration_source(plan.source_snapshot_root, checkpoint=checkpoint)
        if detached.snapshot_digest != plan._source_copy_snapshot_digest or detached.root_identity != plan._source_copy_identity:
            raise PackMigrationStale("Detached source snapshot changed before publication")
        if not _target_missing(plan.target_root, plan._target_parent_identity):
            raise PackMigrationPublicationError("Target Pack appeared before publication")
        if set(plan._lock_set.owned_keys) != {plan.source_key, f"pack:{plan.target.target_id}"}:
            raise PackMigrationPublicationError("Pack migration locks are not fully owned")
        token = _PackMigrationValidationToken(
            id(plan), plan._resolution_attempt, plan.source_snapshot.snapshot_digest,
            staging.content_digest, staging.snapshot_digest, staging.root_identity,
            _resolution_digest(resolution_plan),
            _required_warning_digest(plan.warnings),
            _validate,
        )
        plan._validation_token = token
        plan._resolved_source_snapshot_digest = resolved_source.snapshot_digest
        plan._resolved_staging_digest = staging.snapshot_digest
        plan._state = "ready"
        plan._acknowledged_warning_codes = tuple(sorted(required))
        report(PackMigrationProgress("ready", 1, 1, None, "Migration ready for publication"))
        _record_plan_diagnostic(plan)
        return PackMigrationPublicationPlan(plan, token, _secret=_PUBLICATION_SECRET)


def apply_pack_migration_publication(
    publication: PackMigrationPublicationPlan,
    *, cancel_event: threading.Event | None = None, deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationSourceSnapshot:
    if not isinstance(publication, PackMigrationPublicationPlan):
        raise PackMigrationPublicationError("Invalid Pack migration publication handoff")
    plan = publication._plan
    token = publication._token
    if plan._validation_token is not token:
        raise PackMigrationPublicationError("Pack migration publication handoff was replayed or replaced")
    result = apply_pack_copy_migration_at(publication, cancel_event=cancel_event, deadline=deadline, progress=progress)
    return result


def retry_pack_migration_cleanup(
    publication: PackMigrationPublicationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationSourceSnapshot:
    """Retry only cleanup after a committed publication uncertainty."""
    if not isinstance(publication, PackMigrationPublicationPlan):
        raise PackMigrationPublicationError("Invalid Pack migration publication handoff")
    plan = publication._plan
    if not plan._publication_committed or plan.cleanup_error is None:
        raise PackMigrationPublicationError("Migration has no committed cleanup requiring retry")
    return _retry_committed_publication(
        publication,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def _retry_committed_publication(
    publication: PackMigrationPublicationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationSourceSnapshot:
    plan = publication._plan
    if plan._validation_token is not publication._token:
        raise PackMigrationPublicationError("Pack migration publication handoff was replayed or replaced")
    if not plan._publication_committed or plan.cleanup_error is None:
        raise PackMigrationPublicationError("Migration has no committed cleanup requiring retry")
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    _checkpoint(cancel_event, effective_deadline)
    return _apply_pack_copy_migration_raw(
        plan,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def _report_publication_progress(progress: Callable[[object], None] | None, value: object) -> None:
    if progress is None:
        return
    try:
        progress(value)
    except Exception:
        pass


def _release_plan_locks(plan: PackMigrationPlan) -> None:
    try:
        plan._lock_set.release()
    except BaseException as error:
        plan.cleanup_error = error
        raise PackMigrationCleanupError(
            f"Could not release Pack migration locks: {error}"
        ) from error


def _retain_cleanup_diagnostic(plan: PackMigrationPlan) -> None:
    """Retain a bounded diagnostic tree without following an old path."""
    try:
        if plan.transaction_root.exists() and _identity(plan.transaction_root) == plan._transaction_identity:
            _record_plan_diagnostic(plan)
            return
    except BaseException:
        pass
    try:
        transaction_root, identity = _make_transaction_root(
            plan.transaction_root.parent, prefix="pack-publication-recovery-"
        )
        plan.transaction_root = transaction_root
        plan._transaction_identity = identity
        _record_plan_diagnostic(plan)
    except BaseException:
        pass


def _finish_committed_publication(plan: PackMigrationPlan, deadline: float) -> None:
    # Keep both locks until the transaction tree is completely gone.  A
    # post-commit cleanup failure must never make the published target or its
    # ownership look disposable.
    try:
        _cleanup_transaction(plan, deadline, preserve_diagnostic=True)
        if time.monotonic() >= deadline:
            raise PackMigrationCleanupError("Pack migration cleanup deadline exceeded")
        transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
        try:
            opened = os.fstat(transaction_fd)
            if (opened.st_dev, opened.st_ino) != plan._transaction_identity:
                raise PackMigrationCleanupError("Pack migration transaction root changed during cleanup")
            os.unlink("plan.json", dir_fd=transaction_fd)
            os.fsync(transaction_fd)
        finally:
            os.close(transaction_fd)
        parent_fd = os.open(plan.transaction_root.parent, _DIRECTORY_FLAGS)
        try:
            os.rmdir(plan.transaction_root.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException as error:
        plan.cleanup_error = error
        plan._state = "failed"
        _retain_cleanup_diagnostic(plan)
        raise
    try:
        _release_plan_locks(plan)
    except BaseException as error:
        plan.cleanup_error = error
        plan._state = "failed"
        _retain_cleanup_diagnostic(plan)
        raise
    plan._state = "applied"
    plan.cleanup_error = None


def _matches_validated_target(
    snapshot: PackMigrationSourceSnapshot,
    token: _PackMigrationValidationToken,
) -> bool:
    return (
        not snapshot.validation_errors
        and snapshot.project_identity == token.staging_identity
        and snapshot._tree_scan.content_digest == token.staging_content_digest
        and snapshot.pack_yaml_digest == token.target_snapshot.pack_yaml_digest
        and snapshot.pack_toml_digest == token.target_snapshot.pack_toml_digest
        and snapshot.source_tree_digest == token.target_snapshot.source_tree_digest
        and snapshot.content_tree_digest == token.target_snapshot.content_tree_digest
        and snapshot.provider_metadata_digest
        == token.target_snapshot.provider_metadata_digest
    )


def apply_pack_copy_migration_at(
    publication: PackMigrationPublicationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationSourceSnapshot:
    """Publish only an issued, one-shot migration publication handoff."""
    if not isinstance(publication, PackMigrationPublicationPlan):
        raise PackMigrationPublicationError("Pack migration publication requires a ready handoff")
    plan = publication._plan
    if plan._validation_token is not publication._token:
        raise PackMigrationPublicationError("Pack migration publication handoff was replayed or replaced")
    if plan.state == "applied":
        raise PackMigrationPublicationError("Pack migration publication handoff was already consumed")
    if plan._publication_committed:
        raise PackMigrationPublicationError(
            "Committed publication can only be retried through cleanup retry"
        )
    from pack_migration_resolution import PackMigrationProgress
    _report_publication_progress(
        progress,
        PackMigrationProgress("publishing", 0, 1, None, "Publishing migration target"),
    )
    try:
        result = _apply_pack_copy_migration_raw(
            plan, cancel_event=cancel_event, deadline=deadline, progress=progress,
        )
    except BaseException as error:
        if not plan._publication_committed:
            plan._state = "failed"
        plan.cleanup_error = error
        _record_plan_diagnostic(plan)
        raise
    return result


def _apply_pack_copy_migration_raw(
    plan: PackMigrationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
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
                retry_deadline = min(
                    effective_deadline,
                    time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS,
                )
                checkpoint()
                published = snapshot_pack_migration_source_at(
                    f"pack:{plan.target.target_id}",
                    plan.target_root,
                    plan.target_root.parent.parent,
                    cancel_event=cancel_event,
                    deadline=retry_deadline,
                )
                token = plan._validation_token
                if token is None or not _matches_validated_target(published, token):
                    raise PackMigrationPublicationError(
                        "Published target changed before cleanup retry"
                    )
                from pack_migration_resolution import PackMigrationProgress
                _report_publication_progress(
                    progress,
                    PackMigrationProgress("verifying", 1, 1, None, "Verified published target"),
                )
                _report_publication_progress(
                    progress,
                    PackMigrationProgress("cleaning-up", 0, 1, None, "Cleaning migration transaction"),
                )
                _finish_committed_publication(plan, min(retry_deadline, time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS))
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
        if token.resolution_attempt != int(getattr(plan, "_resolution_attempt", 0)):
            raise PackMigrationStale("Pack migration publication attempt is stale")
        if token.source_snapshot_digest != plan.source_snapshot.snapshot_digest:
            raise PackMigrationStale("Pack migration publication source snapshot is stale")
        if plan.resolution is not None and token.resolution_digest and token.resolution_digest != _resolution_digest(plan.resolution):
            raise PackMigrationStale("Pack migration resolution changed after handoff")
        required = {warning.code for warning in plan.warnings if warning.acknowledgement_required}
        if token.acknowledged_warning_digest and token.acknowledged_warning_digest != _required_warning_digest(plan.warnings):
            raise PackMigrationPublicationError("Pack migration warning acknowledgement is stale")
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
        if staged_scan.root_identity != token.staging_identity:
            raise PackMigrationStale("Target staging directory was replaced after validation")
        if not _target_missing(plan.target_root, plan._target_parent_identity):
            raise PackMigrationPublicationError("Target Pack appeared before publication")
        plan._state = "applying"
        transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
        target_parent_fd = os.open(plan.target_root.parent, _DIRECTORY_FLAGS)
        expected_identity = plan._staging_identity
        publication_outcome_deadline: float | None = None
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
                # This is the last caller-controlled checkpoint.  Once the
                # syscall starts, cancellation cannot leave publication
                # ownership or its result undetermined.
                checkpoint()
                publication_outcome_deadline = time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
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
        outcome_deadline = publication_outcome_deadline or time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
        try:
            published = snapshot_pack_migration_source_at(
                f"pack:{plan.target.target_id}",
                plan.target_root,
                plan.target_root.parent.parent,
                cancel_event=None,
                deadline=outcome_deadline,
            )
            if not _matches_validated_target(published, token):
                raise PackMigrationPublicationError("Published target snapshot verification failed")
            from pack_migration_resolution import PackMigrationProgress
            _report_publication_progress(
                progress,
                PackMigrationProgress("verifying", 1, 1, None, "Verified published target"),
            )
            _report_publication_progress(
                progress,
                PackMigrationProgress("cleaning-up", 0, 1, None, "Cleaning migration transaction"),
            )
            _finish_committed_publication(
                plan,
                min(outcome_deadline, time.monotonic() + PACK_MIGRATION_CLEANUP_TIMEOUT_SECONDS),
            )
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
            plan._retry_resolver_process_cleanup(effective_deadline)
            # Do not release either lock until the transaction has been
            # removed.  A failed cleanup therefore retains the owner, locks,
            # and diagnostics for a subsequent discard retry.
            _cleanup_transaction(plan, effective_deadline)
            _release_plan_locks(plan)
        except BaseException as error:
            plan.cleanup_error = error
            plan._state = "failed"
            _record_plan_diagnostic(plan)
            raise
        plan._state = "discarded"
        plan.cleanup_error = None


__all__ = [
    "PackMigrationChange",
    "PackMigrationPlan",
    "PackMigrationPublicationPlan",
    "PackMigrationSourceSnapshot",
    "PackMigrationTarget",
    "PackMigrationTreeEntry",
    "PackMigrationWarning",
    "apply_pack_copy_migration_at",
    "apply_pack_migration_publication",
    "discard_pack_migration_plan",
    "plan_pack_copy_migration_at",
    "prepare_pack_migration_publication",
    "retry_pack_migration_cleanup",
    "snapshot_pack_migration_source_at",
]
