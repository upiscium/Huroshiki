from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from typing import Callable, Literal
import tomllib

import packctl
from pack_migration import (
    PACK_MIGRATION_TIMEOUT_SECONDS,
    PackMigrationError,
    PackMigrationPlan,
    PackMigrationTarget,
    PackMigrationStale,
    _identity,
    _record_plan_diagnostic,
    _retire_resolution_pending_warnings,
    _same_snapshot,
    _validate_plan_detached_state,
    snapshot_pack_migration_source_at,
)
from pack_migration_roots import (
    PackMigrationRoot,
    PackMigrationRootCandidate,
    PackMigrationRootError,
    PackMigrationRootManifestMissing,
    PackMigrationRootSelection,
    PackRootRecord,
    ensure_pack_root_manifest_ignored,
    extract_pack_migration_root_candidates,
    extract_pack_migration_roots,
    set_url_metadata_project_id,
    write_pack_root_manifest,
    _read_relative_file,
)
from pack_tree_policy import (
    PackTreeScan,
    copy_pack_tree_snapshot,
    scan_pack_migration_source,
)
from provider_identity import (
    ProviderIdentityError,
    canonical_identity,
    parse_provider_metadata,
)
from pack_migration_version_intent import (
    PackMigrationVersionIntentError,
    PackMigrationVersionIntentFacts,
    PackMigrationVersionIntentIssue,
    DetachedVersionIntentMetadata,
    intent_by_identity,
    read_detached_version_intent,
    validate_detached_version_intent,
)
from mod_version_overrides import (
    ensure_mod_version_overrides_ignored,
    serialize_mod_version_overrides,
    write_mod_version_overrides,
)


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class PackMigrationResolutionError(PackMigrationError):
    pass


class PackMigrationResolutionCancelled(PackMigrationResolutionError):
    pass


class PackMigrationResolutionDeadlineExceeded(PackMigrationResolutionError):
    pass


UnresolvedReason = Literal[
    "provider-project-missing",
    "no-compatible-file",
    "provider-response-invalid",
    "provider-identity-ambiguous",
    "url-compatible-unknown",
    "url-incompatible-loader",
    "url-incompatible-minecraft",
    "url-invalid-archive",
    "metadata-invalid",
    "side-conflict",
    "path-collision",
    "filename-collision",
    "identity-collision",
    "root-provenance-required",
    "version-intent-blocked",
]


@dataclass(frozen=True)
class PackMigrationResolvedRoot:
    source_root: PackMigrationRoot
    target_identity: str
    target_file_id: str | None
    target_version: str | None
    target_side: Literal["client", "server", "both"]
    target_metadata_path: Path
    target_filename: str
    classification: Literal["unchanged", "updated", "identity-change"]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackMigrationUnresolvedRoot:
    source_root: PackMigrationRoot | PackMigrationRootCandidate
    reason_code: UnresolvedReason
    message: str
    retryable: bool
    replacement_supported: bool


@dataclass(frozen=True)
class PackMigrationDependencyEntry:
    canonical_identity: str
    provider: str
    project_id: str
    file_id: str | None
    version: str | None
    side: str
    metadata_path: Path
    filename: str
    root: bool


@dataclass(frozen=True)
class PackMigrationDependencyDelta:
    added: tuple[PackMigrationDependencyEntry, ...] = ()
    removed: tuple[PackMigrationDependencyEntry, ...] = ()
    updated: tuple[
        tuple[PackMigrationDependencyEntry, PackMigrationDependencyEntry], ...
    ] = ()
    unchanged: tuple[PackMigrationDependencyEntry, ...] = ()
    side_changed: tuple[
        tuple[PackMigrationDependencyEntry, PackMigrationDependencyEntry], ...
    ] = ()
    identity_changed: tuple[
        tuple[PackMigrationDependencyEntry, PackMigrationDependencyEntry], ...
    ] = ()
    path_changed: tuple[tuple[Path, Path], ...] = ()
    filename_changed: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PackMigrationProgress:
    phase: Literal[
        "validating",
        "extracting-roots",
        "initializing-target",
        "resolving-roots",
        "building-closure",
        "refreshing",
        "validating-target",
        "validating-resolutions",
        "applying-resolutions",
        "validating-publication",
        "ready",
        "publishing",
        "verifying",
        "cleaning-up",
        "classifying",
        "committing",
    ]
    completed: int
    total: int
    current_root: str | None
    message: str


@dataclass(frozen=True)
class UrlMigrationCompatibility:
    status: Literal["compatible", "incompatible", "unknown"]
    loader_status: Literal["compatible", "incompatible", "unknown"]
    minecraft_status: Literal["compatible", "incompatible", "unknown"]
    detected_loaders: tuple[str, ...]
    detected_minecraft_versions: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackMigrationResolutionPlan:
    source_snapshot_digest: str
    target: PackMigrationTarget
    roots: tuple[PackMigrationRoot, ...]
    root_candidates: tuple[PackMigrationRootCandidate, ...]
    resolved_roots: tuple[PackMigrationResolvedRoot, ...]
    unresolved_roots: tuple[PackMigrationUnresolvedRoot, ...]
    dependency_delta: PackMigrationDependencyDelta
    side_changes: tuple[tuple[str, str, str], ...]
    identity_changes: tuple[tuple[str, str], ...]
    path_collisions: tuple[str, ...]
    filename_collisions: tuple[str, ...]
    provider_warnings: tuple[str, ...]
    url_compatibility: tuple[tuple[str, UrlMigrationCompatibility], ...]
    target_source_snapshot: PackTreeScan | None
    state: Literal["resolved", "resolution-required"]
    provenance_required: bool = False
    resolution_attempt: int = 0
    version_intent_facts: PackMigrationVersionIntentFacts = PackMigrationVersionIntentFacts()
    version_intent_issues: tuple[PackMigrationVersionIntentIssue, ...] = ()
    version_intent_digest: str = ""

    def diagnostic_summary(self) -> dict[str, object]:
        delta = self.dependency_delta
        return {
            "roots": len(self.roots),
            "root_candidates": len(self.root_candidates),
            "provenance_required": self.provenance_required,
            "resolved": len(self.resolved_roots),
            "unresolved": len(self.unresolved_roots),
            "identity_changes": len(self.identity_changes),
            "path_collisions": len(self.path_collisions),
            "filename_collisions": len(self.filename_collisions),
            "resolution_attempt": self.resolution_attempt,
            "version_intent_digest": self.version_intent_digest,
            "version_intent_issues": len(self.version_intent_issues),
            "dependency_delta": {
                "added": len(delta.added),
                "removed": len(delta.removed),
                "updated": len(delta.updated),
                "unchanged": len(delta.unchanged),
            },
        }


@dataclass(frozen=True)
class PackMigrationRootSelectionAuthority:
    """Exact migration-local provenance selected from one fixed candidate set."""

    source_snapshot_digest: str
    target: PackMigrationTarget
    resolution_attempt: int
    candidate_digest: str
    selections: tuple[PackMigrationRootSelection, ...]
    selected_candidates: tuple[PackMigrationRootCandidate, ...]
    roots: tuple[PackRootRecord, ...]
    legacy_url_identities: tuple[tuple[Path, str], ...]
    selection_digest: str

    def validates(self, source_snapshot_digest: str, target: PackMigrationTarget) -> bool:
        return (
            self.source_snapshot_digest == source_snapshot_digest
            and self.target == target
            and self.selection_digest == _root_selection_digest(
                self.source_snapshot_digest,
                self.candidate_digest,
                self.selections,
                self.roots,
            )
        )


def _checkpoint(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PackMigrationResolutionCancelled("Pack migration resolution was cancelled")
    if time.monotonic() >= deadline:
        raise PackMigrationResolutionDeadlineExceeded(
            "Pack migration resolution deadline exceeded"
        )


def _progress(
    callback: Callable[[PackMigrationProgress], None] | None,
    value: PackMigrationProgress,
) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:
        pass


def _create_workspace(plan: PackMigrationPlan) -> Path:
    transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
    try:
        opened = os.fstat(transaction_fd)
        if (opened.st_dev, opened.st_ino) != plan._transaction_identity:
            raise PackMigrationStale("Pack migration transaction root was replaced")
        attempt = int(getattr(plan, "_resolution_attempt", 0))
        name = f"resolver-work-attempt-{attempt:04d}"
        os.mkdir(name, 0o700, dir_fd=transaction_fd)
        workspace_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=transaction_fd)
        try:
            workspace = os.fstat(workspace_fd)
            plan._resolver_work_identity = (workspace.st_dev, workspace.st_ino)
        finally:
            os.close(workspace_fd)
    finally:
        os.close(transaction_fd)
    plan._resolver_work_root = plan.transaction_root / name
    return plan._resolver_work_root


def _scan_workspace_source(
    plan: PackMigrationPlan,
    workspace: Path,
    workspace_fd: int,
    checkpoint: Callable[[], None],
) -> PackTreeScan:
    workspace_metadata = os.fstat(workspace_fd)
    if (workspace_metadata.st_dev, workspace_metadata.st_ino) != plan._resolver_work_identity:
        raise PackMigrationStale("Resolver workspace descriptor changed")
    if _identity(workspace) != plan._resolver_work_identity:
        raise PackMigrationStale("Resolver workspace path was replaced")
    source_fd = os.open("source", _DIRECTORY_FLAGS, dir_fd=workspace_fd)
    try:
        source_metadata = os.fstat(source_fd)
        result = scan_pack_migration_source(
            workspace / "source", checkpoint=checkpoint
        )
        if result.root_identity != (source_metadata.st_dev, source_metadata.st_ino):
            raise PackMigrationStale("Resolver source path was replaced")
        return result
    finally:
        os.close(source_fd)


def initialize_target_packwiz_source(
    work_root: Path,
    target: PackMigrationTarget,
    *,
    cancel_event: threading.Event | None,
    deadline: float,
    progress: Callable[[PackMigrationProgress], None] | None,
    operation_root: Path | None = None,
) -> PackTreeScan:
    bound_root = operation_root or work_root
    _checkpoint(cancel_event, deadline)
    _progress(
        progress,
        PackMigrationProgress(
            "initializing-target", 0, 1, None, "Initializing target Packwiz source"
        ),
    )
    packctl.init_packwiz_project(
        bound_root,
        display_name=target.display_name,
        minecraft=target.minecraft_version,
        loader=target.loader,
        loader_version=target.loader_version,
        cancel_event=cancel_event,
        deadline=deadline,
    )
    if packctl.project_versions(bound_root / "source") != (
        target.minecraft_version,
        target.loader,
        target.loader_version,
    ):
        raise PackMigrationResolutionError(
            "Initialized Packwiz source does not match the migration target"
        )
    source_fd = os.open(bound_root / "source", _DIRECTORY_FLAGS)
    try:
        source_metadata = os.fstat(source_fd)
        result = scan_pack_migration_source(
            work_root / "source",
            checkpoint=lambda: _checkpoint(cancel_event, deadline),
        )
        if result.root_identity != (source_metadata.st_dev, source_metadata.st_ino):
            raise PackMigrationStale("Initialized Packwiz source path was replaced")
    finally:
        os.close(source_fd)
    _progress(
        progress,
        PackMigrationProgress(
            "initializing-target", 1, 1, None, "Initialized target Packwiz source"
        ),
    )
    return result


def _metadata_entries(
    source: Path,
    root_identities: set[str],
    checkpoint: Callable[[], None],
    *,
    scan: PackTreeScan | None = None,
) -> tuple[PackMigrationDependencyEntry, ...]:
    scan = scan or scan_pack_migration_source(source, checkpoint=checkpoint)
    unsafe = [
        entry.relative_path
        for entry in scan.entries
        if entry.kind == "invalid" or entry.errors
    ]
    if unsafe:
        raise PackMigrationResolutionError(
            f"Packwiz source contains unsafe entry: {unsafe[0]}"
        )
    entries: list[PackMigrationDependencyEntry] = []
    identities: set[str] = set()
    for entry in scan.entries:
        checkpoint()
        if entry.kind != "file" or not entry.relative_path.name.endswith(".pw.toml"):
            continue
        contents = _read_relative_file(
            source,
            scan,
            entry.relative_path,
            max_bytes=2 * 1024 * 1024,
        )
        metadata = parse_provider_metadata(entry.relative_path, contents)
        if metadata.canonical_identity in identities:
            raise PackMigrationResolutionError(
                f"Duplicate dependency identity: {metadata.canonical_identity}"
            )
        identities.add(metadata.canonical_identity)
        entries.append(
            PackMigrationDependencyEntry(
                metadata.canonical_identity,
                metadata.provider,
                metadata.project_id,
                metadata.file_id,
                metadata.version,
                metadata.side,
                metadata.metadata_path,
                metadata.filename,
                metadata.canonical_identity in root_identities,
            )
        )
    return tuple(sorted(entries, key=lambda item: item.canonical_identity))


def _dependency_delta(
    before: tuple[PackMigrationDependencyEntry, ...],
    after: tuple[PackMigrationDependencyEntry, ...],
) -> PackMigrationDependencyDelta:
    old = {entry.canonical_identity: entry for entry in before}
    new = {entry.canonical_identity: entry for entry in after}
    added = tuple(new[key] for key in sorted(new.keys() - old.keys()))
    removed = tuple(old[key] for key in sorted(old.keys() - new.keys()))
    updated: list[tuple[PackMigrationDependencyEntry, PackMigrationDependencyEntry]] = []
    unchanged: list[PackMigrationDependencyEntry] = []
    side_changed = []
    identity_changed = []
    path_changed = []
    filename_changed = []
    for key in sorted(old.keys() & new.keys()):
        left, right = old[key], new[key]
        if left.side != right.side:
            side_changed.append((left, right))
        if left.metadata_path != right.metadata_path:
            path_changed.append((left.metadata_path, right.metadata_path))
        if left.filename != right.filename:
            filename_changed.append((left.filename, right.filename))
        if (
            left.file_id,
            left.version,
            left.side,
            left.metadata_path,
            left.filename,
            left.root,
        ) == (
            right.file_id,
            right.version,
            right.side,
            right.metadata_path,
            right.filename,
            right.root,
        ):
            unchanged.append(right)
        else:
            updated.append((left, right))
    return PackMigrationDependencyDelta(
        added,
        removed,
        tuple(updated),
        tuple(unchanged),
        tuple(side_changed),
        tuple(identity_changed),
        tuple(path_changed),
        tuple(filename_changed),
    )


def _operation_failure(error: BaseException) -> bool:
    text = str(error).lower()
    return any(
        value in text
        for value in (
            "cancel",
            "deadline",
            "timed out",
            "termination",
            "orphan",
            "background process",
            "protocol",
            "invalid json",
        )
    )


def _validate_detached_snapshot(
    plan: PackMigrationPlan,
    checkpoint: Callable[[], None],
) -> PackTreeScan:
    return _validate_plan_detached_state(plan, checkpoint)


def _validate_live_source(
    plan: PackMigrationPlan,
    repository_root: Path,
    cancel_event: threading.Event | None,
    deadline: float,
) -> None:
    current = snapshot_pack_migration_source_at(
        plan.source_key,
        plan.source_root,
        repository_root,
        cancel_event=cancel_event,
        deadline=deadline,
    )
    if not _same_snapshot(current, plan.source_snapshot):
        raise PackMigrationStale("Source Pack changed during resolution")


def _classify_collision(message: str) -> UnresolvedReason | None:
    lowered = message.lower()
    if "filename collision" in lowered:
        return "filename-collision"
    if "path collision" in lowered:
        return "path-collision"
    if "identity" in lowered and any(
        value in lowered for value in ("duplicat", "mismatch", "disagreement")
    ):
        return "identity-collision"
    if "side" in lowered and any(
        value in lowered for value in ("invalid", "conflict", "disagreement")
    ):
        return "side-conflict"
    return None


def _url_compatibility(
    contents: bytes,
    target: PackMigrationTarget,
) -> UrlMigrationCompatibility:
    try:
        document = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        return UrlMigrationCompatibility(
            "unknown", "unknown", "unknown", (), (), ("URL metadata is invalid",)
        )
    huroshiki = document.get("huroshiki", {})
    if not isinstance(huroshiki, dict):
        huroshiki = {}
    raw_loaders = huroshiki.get("loaders", [])
    loaders = (
        tuple(sorted(str(item) for item in raw_loaders))
        if isinstance(raw_loaders, list)
        else ()
    )
    raw_versions = huroshiki.get("minecraft-versions", [])
    versions = (
        tuple(sorted(str(item) for item in raw_versions))
        if isinstance(raw_versions, list)
        else ()
    )
    loader_status: Literal["compatible", "incompatible", "unknown"] = (
        "compatible"
        if target.loader in loaders
        else "incompatible" if loaders else "unknown"
    )
    exact_versions = {
        value[1:-1] if value.startswith("[") and value.endswith("]") else value
        for value in versions
        if value.replace(".", "").isdigit()
        or (
            value.startswith("[")
            and value.endswith("]")
            and value[1:-1].replace(".", "").isdigit()
        )
    }
    if target.minecraft_version in exact_versions or "*" in versions:
        minecraft_status: Literal["compatible", "incompatible", "unknown"] = "compatible"
    elif versions and len(exact_versions) == len(versions):
        minecraft_status = "incompatible"
    else:
        minecraft_status = "unknown"
    status: Literal["compatible", "incompatible", "unknown"]
    if "incompatible" in {loader_status, minecraft_status}:
        status = "incompatible"
    elif loader_status == minecraft_status == "compatible":
        status = "compatible"
    else:
        status = "unknown"
    warnings = () if status == "compatible" else ("URL compatibility requires resolution",)
    return UrlMigrationCompatibility(
        status, loader_status, minecraft_status, loaders, versions, warnings
    )


def _exchange_target_source(
    plan: PackMigrationPlan,
    workspace: Path,
    expected_resolver_scan: PackTreeScan,
    checkpoint: Callable[[], None],
) -> PackTreeScan:
    checkpoint()
    current_staging = scan_pack_migration_source(
        plan.target_staging_root, checkpoint=checkpoint
    )
    if current_staging.snapshot_digest != plan._staging_snapshot_digest:
        raise PackMigrationStale("Target staging changed before resolver handoff")
    staging_fd = os.open(plan.target_staging_root, _DIRECTORY_FLAGS)
    workspace_fd = os.open(workspace, _DIRECTORY_FLAGS)
    exchange_attempted = False
    try:
        opened_staging = os.fstat(staging_fd)
        if (opened_staging.st_dev, opened_staging.st_ino) != plan._staging_identity:
            raise PackMigrationStale("Target staging was replaced before handoff")
        opened_workspace = os.fstat(workspace_fd)
        if (opened_workspace.st_dev, opened_workspace.st_ino) != plan._resolver_work_identity:
            raise PackMigrationStale("Resolver workspace was replaced before handoff")
        old_source = os.stat("source", dir_fd=staging_fd, follow_symlinks=False)
        new_source = os.stat("source", dir_fd=workspace_fd, follow_symlinks=False)
        if (new_source.st_dev, new_source.st_ino) != expected_resolver_scan.root_identity:
            raise PackMigrationStale("Resolver source changed before handoff")
        exchange_attempted = True
        packctl.renameat2(
            staging_fd,
            "source",
            workspace_fd,
            "source",
            packctl.RENAME_EXCHANGE,
        )
        installed = os.stat("source", dir_fd=staging_fd, follow_symlinks=False)
        displaced = os.stat("source", dir_fd=workspace_fd, follow_symlinks=False)
        if (installed.st_dev, installed.st_ino) != (new_source.st_dev, new_source.st_ino) or (
            displaced.st_dev,
            displaced.st_ino,
        ) != (old_source.st_dev, old_source.st_ino):
            raise PackMigrationStale("Resolver source exchange verification failed")
        result = scan_pack_migration_source(
            plan.target_staging_root / "source", checkpoint=checkpoint
        )
        expected_identities = {
            entry.relative_path: (entry.device, entry.inode)
            for entry in expected_resolver_scan.entries
        }
        installed_identities = {
            entry.relative_path: (entry.device, entry.inode)
            for entry in result.entries
        }
        if (
            result.root_identity != expected_resolver_scan.root_identity
            or result.content_digest != expected_resolver_scan.content_digest
            or installed_identities != expected_identities
        ):
            raise PackMigrationStale("Installed resolver source verification failed")
        installed_versions = packctl.project_versions(
            plan.target_staging_root / "source"
        )
        if installed_versions != (
            plan.target.minecraft_version,
            plan.target.loader,
            plan.target.loader_version,
        ):
            raise PackMigrationStale(
                "Installed resolver source target versions changed during handoff"
            )
        packctl.renameat2(
            workspace_fd,
            "source",
            workspace_fd,
            "original-source",
            packctl.RENAME_NOREPLACE,
        )
        exchange_attempted = False
        return result
    except BaseException:
        if exchange_attempted:
            try:
                installed = os.stat("source", dir_fd=staging_fd, follow_symlinks=False)
                displaced = os.stat("source", dir_fd=workspace_fd, follow_symlinks=False)
                installed_identity = (installed.st_dev, installed.st_ino)
                displaced_identity = (displaced.st_dev, displaced.st_ino)
                new_identity = (new_source.st_dev, new_source.st_ino)
                old_identity = (old_source.st_dev, old_source.st_ino)
                if installed_identity == old_identity and displaced_identity == new_identity:
                    pass
                elif installed_identity == new_identity and displaced_identity == old_identity:
                    packctl.renameat2(
                        staging_fd,
                        "source",
                        workspace_fd,
                        "source",
                        packctl.RENAME_EXCHANGE,
                    )
                else:
                    raise PackMigrationResolutionError(
                        "Resolver source handoff rollback identity is uncertain"
                    )
            except BaseException as rollback_error:
                raise PackMigrationResolutionError(
                    f"Resolver source handoff rollback failed: {rollback_error}"
                ) from rollback_error
        raise
    finally:
        os.close(workspace_fd)
        os.close(staging_fd)


def _commit_root_provenance_source(
    plan: PackMigrationPlan,
    provenance_source: Path,
    expected_provenance_scan: PackTreeScan,
    checkpoint: Callable[[], None],
) -> None:
    source_entry = next(
        (
            entry
            for entry in plan.source_snapshot.entries
            if entry.relative_path == Path("source")
        ),
        None,
    )
    if source_entry is None or source_entry.kind != "directory":
        raise PackMigrationResolutionError("Source Packwiz directory is missing")
    pack_fd = os.open(plan.source_root, _DIRECTORY_FLAGS)
    transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
    exchange_attempted = False
    try:
        opened_pack = os.fstat(pack_fd)
        opened_transaction = os.fstat(transaction_fd)
        if (opened_pack.st_dev, opened_pack.st_ino) != plan.source_snapshot.project_identity:
            raise PackMigrationStale("Source Pack root was replaced before provenance commit")
        if (opened_transaction.st_dev, opened_transaction.st_ino) != plan._transaction_identity:
            raise PackMigrationStale(
                "Pack migration transaction was replaced before provenance commit"
            )
        live = os.stat("source", dir_fd=pack_fd, follow_symlinks=False)
        staged = os.stat(
            provenance_source.name,
            dir_fd=transaction_fd,
            follow_symlinks=False,
        )
        if (live.st_dev, live.st_ino) != (source_entry.device, source_entry.inode):
            raise PackMigrationStale("Source Packwiz directory changed before provenance commit")
        if (staged.st_dev, staged.st_ino) != expected_provenance_scan.root_identity:
            raise PackMigrationStale("Provenance staging changed before commit")
        exchange_attempted = True
        packctl.renameat2(
            pack_fd,
            "source",
            transaction_fd,
            provenance_source.name,
            packctl.RENAME_EXCHANGE,
        )
        installed = os.stat("source", dir_fd=pack_fd, follow_symlinks=False)
        displaced = os.stat(
            provenance_source.name,
            dir_fd=transaction_fd,
            follow_symlinks=False,
        )
        if (installed.st_dev, installed.st_ino) != (staged.st_dev, staged.st_ino) or (
            displaced.st_dev,
            displaced.st_ino,
        ) != (live.st_dev, live.st_ino):
            raise PackMigrationStale("Provenance source exchange verification failed")
        installed_scan = scan_pack_migration_source(
            plan.source_root / "source", checkpoint=checkpoint
        )
        expected_identities = {
            entry.relative_path: (entry.device, entry.inode)
            for entry in expected_provenance_scan.entries
        }
        installed_identities = {
            entry.relative_path: (entry.device, entry.inode)
            for entry in installed_scan.entries
        }
        if (
            installed_scan.root_identity != expected_provenance_scan.root_identity
            or installed_scan.content_digest != expected_provenance_scan.content_digest
            or installed_identities != expected_identities
        ):
            raise PackMigrationStale("Committed provenance source verification failed")
        exchange_attempted = False
    except BaseException:
        if exchange_attempted:
            try:
                installed = os.stat("source", dir_fd=pack_fd, follow_symlinks=False)
                displaced = os.stat(
                    provenance_source.name,
                    dir_fd=transaction_fd,
                    follow_symlinks=False,
                )
                installed_identity = (installed.st_dev, installed.st_ino)
                displaced_identity = (displaced.st_dev, displaced.st_ino)
                staged_identity = (staged.st_dev, staged.st_ino)
                live_identity = (live.st_dev, live.st_ino)
                if installed_identity == live_identity and displaced_identity == staged_identity:
                    pass
                elif installed_identity == staged_identity and displaced_identity == live_identity:
                    packctl.renameat2(
                        pack_fd,
                        "source",
                        transaction_fd,
                        provenance_source.name,
                        packctl.RENAME_EXCHANGE,
                    )
                else:
                    raise PackMigrationResolutionError(
                        "Provenance commit rollback identity is uncertain"
                    )
            except BaseException as rollback_error:
                raise PackMigrationResolutionError(
                    f"Provenance commit rollback failed: {rollback_error}"
                ) from rollback_error
        raise
    finally:
        os.close(transaction_fd)
        os.close(pack_fd)


def _root_candidate_digest(resolution: PackMigrationResolutionPlan) -> str:
    payload = {
        "source_snapshot_digest": resolution.source_snapshot_digest,
        "target": {
            "id": resolution.target.target_id,
            "display_name": resolution.target.display_name,
            "minecraft": resolution.target.minecraft_version,
            "loader": resolution.target.loader,
            "loader_version": resolution.target.loader_version,
            "mode": resolution.target.mode,
        },
        "resolution_attempt": resolution.resolution_attempt,
        "version_intent_digest": resolution.version_intent_digest,
        "candidates": [
            {
                "canonical_identity": candidate.canonical_identity,
                "provider": candidate.provider,
                "project_id": candidate.project_id,
                "source_file_id": candidate.source_file_id,
                "source_version": candidate.source_version,
                "source_side": candidate.source_side,
                "source_metadata_path": candidate.source_metadata_path.as_posix(),
                "source_filename": candidate.source_filename,
                "source_download_url": candidate.source_download_url,
            }
            for candidate in sorted(
                resolution.root_candidates,
                key=lambda item: item.source_metadata_path.as_posix(),
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _root_selection_digest(
    source_snapshot_digest: str,
    candidate_digest: str,
    selections: tuple[PackMigrationRootSelection, ...],
    roots: tuple[PackRootRecord, ...],
) -> str:
    payload = {
        "source_snapshot_digest": source_snapshot_digest,
        "candidate_digest": candidate_digest,
        "selections": [
            {
                "path": item.source_metadata_path.as_posix(),
                "provider": item.provider,
                "project_id": item.project_id,
            }
            for item in selections
        ],
        "roots": [
            {
                "provider": item.provider,
                "project_id": item.project_id,
                "side": item.side,
            }
            for item in roots
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _create_root_selection_authority(
    plan: PackMigrationPlan,
    selections: tuple[PackMigrationRootSelection, ...],
    checkpoint: Callable[[], None],
) -> PackMigrationRootSelectionAuthority:
    resolution = plan.resolution
    if (
        plan.state != "resolution-required"
        or not isinstance(resolution, PackMigrationResolutionPlan)
        or not resolution.provenance_required
        or resolution.source_snapshot_digest != plan.source_snapshot.snapshot_digest
        or resolution.target != plan.target
        or resolution.resolution_attempt != plan._resolution_attempt
    ):
        raise PackMigrationResolutionError(
            "Root selection requires the current provenance resolution result"
        )
    if plan._migration_root_authority is not None:
        raise PackMigrationResolutionError("Migration-local root provenance was already selected")
    candidates = {
        candidate.source_metadata_path: candidate
        for candidate in resolution.root_candidates
    }
    if len(candidates) != len(resolution.root_candidates):
        raise PackMigrationResolutionError("Root candidate paths are ambiguous")
    selected_paths: set[Path] = set()
    records: list[PackRootRecord] = []
    normalized_selections: list[PackMigrationRootSelection] = []
    legacy_url_selections: list[tuple[Path, str]] = []
    identities: set[str] = set()
    for selection in selections:
        checkpoint()
        if not isinstance(selection, PackMigrationRootSelection):
            raise PackMigrationResolutionError(
                "Root selections must use PackMigrationRootSelection"
            )
        if selection.source_metadata_path in selected_paths:
            raise PackMigrationResolutionError(
                f"Duplicate root selection: {selection.source_metadata_path}"
            )
        candidate = candidates.get(selection.source_metadata_path)
        if candidate is None:
            raise PackMigrationResolutionError(
                f"Unknown root candidate: {selection.source_metadata_path}"
            )
        if selection.provider != candidate.provider:
            raise PackMigrationResolutionError(
                f"Root provider disagrees with metadata: {selection.source_metadata_path}"
            )
        try:
            identity = canonical_identity(selection.provider, selection.project_id)
        except ProviderIdentityError as error:
            raise PackMigrationResolutionError(str(error)) from error
        if candidate.canonical_identity is not None and identity != candidate.canonical_identity:
            raise PackMigrationResolutionError(
                f"Root identity disagrees with metadata: {selection.source_metadata_path}"
            )
        if identity in identities:
            raise PackMigrationResolutionError(f"Duplicate selected root identity: {identity}")
        selected_paths.add(selection.source_metadata_path)
        identities.add(identity)
        normalized_project_id = identity.partition(":")[2]
        normalized_selections.append(
            PackMigrationRootSelection(
                selection.source_metadata_path,
                selection.provider,
                normalized_project_id,
            )
        )
        records.append(
            PackRootRecord(
                selection.provider,
                normalized_project_id,
                candidate.source_side,
            )
        )
        if candidate.provider == "url" and candidate.canonical_identity is None:
            legacy_url_selections.append(
                (candidate.source_metadata_path, normalized_project_id)
            )
    missing_legacy_urls = [
        candidate.source_metadata_path
        for candidate in resolution.root_candidates
        if candidate.provider == "url"
        and candidate.canonical_identity is None
        and candidate.source_metadata_path not in selected_paths
    ]
    if missing_legacy_urls:
        raise PackMigrationResolutionError(
            "Legacy URL metadata requires an explicit root identity: "
            f"{missing_legacy_urls[0]}"
        )
    candidate_digest = _root_candidate_digest(resolution)
    ordered_selections = tuple(
        sorted(normalized_selections, key=lambda item: item.source_metadata_path.as_posix())
    )
    ordered_candidates = tuple(
        candidates[item.source_metadata_path] for item in ordered_selections
    )
    ordered_records = tuple(sorted(records, key=lambda item: item.canonical_identity))
    ordered_urls = tuple(
        sorted(legacy_url_selections, key=lambda item: item[0].as_posix())
    )
    selection_digest = _root_selection_digest(
        plan.source_snapshot.snapshot_digest,
        candidate_digest,
        ordered_selections,
        ordered_records,
    )
    return PackMigrationRootSelectionAuthority(
        plan.source_snapshot.snapshot_digest,
        plan.target,
        plan._resolution_attempt,
        candidate_digest,
        ordered_selections,
        ordered_candidates,
        ordered_records,
        ordered_urls,
        selection_digest,
    )


def select_pack_migration_roots_at(
    plan: PackMigrationPlan,
    selections: tuple[PackMigrationRootSelection, ...],
    *,
    repository_root: Path,
    state_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PackMigrationProgress], None] | None = None,
) -> PackMigrationResolutionPlan:
    """Materialize explicit provenance in owned state and continue one plan."""
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    operation_event = cancel_event if cancel_event is not None else threading.Event()
    checkpoint = lambda: _checkpoint(operation_event, effective_deadline)
    with plan._lock:
        if set(plan._lock_set.owned_keys) != {
            plan.source_key,
            f"pack:{plan.target.target_id}",
        }:
            raise PackMigrationResolutionError("Pack migration locks are not fully owned")
        authority = _create_root_selection_authority(plan, tuple(selections), checkpoint)
        try:
            checkpoint()
            _validate_detached_snapshot(plan, checkpoint)
            _validate_live_source(
                plan, repository_root, operation_event, effective_deadline
            )
            detached_source = plan.source_snapshot_root / "source"
            detached_scan = scan_pack_migration_source(
                detached_source, checkpoint=checkpoint
            )
            provenance_source = plan.transaction_root / "migration-provenance-source"
            top_level = tuple(
                sorted(
                    {
                        Path(entry.relative_path.parts[0])
                        for entry in detached_scan.entries[1:]
                    },
                    key=lambda path: path.as_posix(),
                )
            )
            copy_pack_tree_snapshot(
                detached_scan,
                provenance_source,
                include=top_level,
                checkpoint=checkpoint,
                destination_parent_identity=plan._transaction_identity,
            )
            ensure_pack_root_manifest_ignored(
                provenance_source, checkpoint=checkpoint
            )
            for relative_path, project_id in authority.legacy_url_identities:
                checkpoint()
                set_url_metadata_project_id(
                    provenance_source, relative_path, project_id
                )
            write_pack_root_manifest(provenance_source, authority.roots)
            metadata_before_refresh: dict[Path, bytes] = {}
            if authority.legacy_url_identities:
                before_refresh_scan = scan_pack_migration_source(
                    provenance_source, checkpoint=checkpoint
                )
                metadata_before_refresh = {
                    entry.relative_path: _read_relative_file(
                        provenance_source,
                        before_refresh_scan,
                        entry.relative_path,
                        max_bytes=2 * 1024 * 1024,
                        checkpoint=checkpoint,
                    )
                    for entry in before_refresh_scan.entries
                    if entry.kind == "file"
                    and entry.relative_path.name.endswith(".pw.toml")
                }
                packctl.run_packwiz(
                    ["packwiz", "refresh"],
                    cwd=provenance_source,
                    cancel_event=operation_event,
                    deadline=effective_deadline,
                    project_id=plan.target.target_id,
                    operation="migration-local-provenance-refresh",
                )
            provenance_scan = scan_pack_migration_source(
                provenance_source, checkpoint=checkpoint
            )
            if metadata_before_refresh:
                metadata_after_refresh = {
                    entry.relative_path: _read_relative_file(
                        provenance_source,
                        provenance_scan,
                        entry.relative_path,
                        max_bytes=2 * 1024 * 1024,
                        checkpoint=checkpoint,
                    )
                    for entry in provenance_scan.entries
                    if entry.kind == "file"
                    and entry.relative_path.name.endswith(".pw.toml")
                }
                if metadata_after_refresh != metadata_before_refresh:
                    raise PackMigrationResolutionError(
                        "Migration-local provenance refresh changed Packwiz metadata"
                    )
            extracted = extract_pack_migration_roots(
                provenance_source,
                expected_identity=provenance_scan.root_identity,
                expected_snapshot_digest=provenance_scan.snapshot_digest,
                checkpoint=checkpoint,
            )
            extracted_records = tuple(
                sorted(
                    (
                        PackRootRecord(root.provider, root.project_id, root.source_side)
                        for root in extracted
                    ),
                    key=lambda item: item.canonical_identity,
                )
            )
            if extracted_records != authority.roots:
                raise PackMigrationResolutionError(
                    "Migration-local root manifest does not match the selected roots"
                )
            extracted_by_path = {root.source_metadata_path: root for root in extracted}
            for selection, candidate in zip(
                authority.selections, authority.selected_candidates, strict=True
            ):
                root = extracted_by_path.get(selection.source_metadata_path)
                if root is None or (
                    root.canonical_identity
                    != canonical_identity(selection.provider, selection.project_id)
                    or root.source_file_id != candidate.source_file_id
                    or root.source_version != candidate.source_version
                    or root.source_side != candidate.source_side
                    or root.source_metadata_path != candidate.source_metadata_path
                    or root.source_filename != candidate.source_filename
                    or root.source_download_url != candidate.source_download_url
                ):
                    raise PackMigrationResolutionError(
                        "Migration-local selected root changed during materialization: "
                        f"{selection.source_metadata_path}"
                    )
            _validate_detached_snapshot(plan, checkpoint)
            _validate_live_source(
                plan, repository_root, operation_event, effective_deadline
            )
            plan._provenance_source_root = provenance_source
            plan._provenance_source_identity = provenance_scan.root_identity
            plan._provenance_source_content_digest = provenance_scan.content_digest
            plan._provenance_source_snapshot_digest = provenance_scan.snapshot_digest
            plan._migration_root_authority = authority
            plan._previous_resolution = plan.resolution
            plan.resolution = None
            plan._state = "staged"
            _record_plan_diagnostic(plan)
        except BaseException as error:
            plan._state = "failed"
            plan.cleanup_error = error
            _record_plan_diagnostic(plan)
            raise
    return _resolve_effective_root_set(
        plan,
        None,
        repository_root=repository_root,
        state_root=state_root,
        cancel_event=operation_event,
        deadline=effective_deadline,
        progress=progress,
    )


def commit_pack_migration_root_selection_at(
    plan: PackMigrationPlan,
    selections: tuple[PackMigrationRootSelection, ...],
    *,
    repository_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> tuple[PackRootRecord, ...]:
    """Atomically persist explicit roots, then discard the stale migration plan."""
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    operation_event = cancel_event if cancel_event is not None else threading.Event()
    checkpoint = lambda: _checkpoint(operation_event, effective_deadline)
    with plan._lock:
        resolution = plan.resolution
        if (
            plan.state != "resolution-required"
            or not isinstance(resolution, PackMigrationResolutionPlan)
            or not resolution.provenance_required
        ):
            raise PackMigrationResolutionError(
                "Root selection requires a provenance resolution result"
            )
        if plan._provenance_committed:
            raise PackMigrationResolutionError("Root provenance was already committed")
        if set(plan._lock_set.owned_keys) != {
            plan.source_key,
            f"pack:{plan.target.target_id}",
        }:
            raise PackMigrationResolutionError("Pack migration locks are not fully owned")
        candidates = {
            candidate.source_metadata_path: candidate
            for candidate in resolution.root_candidates
        }
        if len(candidates) != len(resolution.root_candidates):
            raise PackMigrationResolutionError("Root candidate paths are ambiguous")
        selected_paths: set[Path] = set()
        records: list[PackRootRecord] = []
        legacy_url_selections: list[tuple[Path, str]] = []
        identities: set[str] = set()
        for selection in selections:
            checkpoint()
            if selection.source_metadata_path in selected_paths:
                raise PackMigrationResolutionError(
                    f"Duplicate root selection: {selection.source_metadata_path}"
                )
            candidate = candidates.get(selection.source_metadata_path)
            if candidate is None:
                raise PackMigrationResolutionError(
                    f"Unknown root candidate: {selection.source_metadata_path}"
                )
            if selection.provider != candidate.provider:
                raise PackMigrationResolutionError(
                    f"Root provider disagrees with metadata: {selection.source_metadata_path}"
                )
            try:
                identity = canonical_identity(selection.provider, selection.project_id)
            except ProviderIdentityError as error:
                raise PackMigrationResolutionError(str(error)) from error
            if (
                candidate.canonical_identity is not None
                and identity != candidate.canonical_identity
            ):
                raise PackMigrationResolutionError(
                    f"Root identity disagrees with metadata: {selection.source_metadata_path}"
                )
            if identity in identities:
                raise PackMigrationResolutionError(f"Duplicate selected root identity: {identity}")
            selected_paths.add(selection.source_metadata_path)
            identities.add(identity)
            normalized_project_id = identity.partition(":")[2]
            records.append(
                PackRootRecord(
                    selection.provider,
                    normalized_project_id,
                    candidate.source_side,
                )
            )
            if candidate.provider == "url" and candidate.canonical_identity is None:
                legacy_url_selections.append(
                    (candidate.source_metadata_path, normalized_project_id)
                )
        missing_legacy_urls = [
            candidate.source_metadata_path
            for candidate in resolution.root_candidates
            if candidate.provider == "url"
            and candidate.canonical_identity is None
            and candidate.source_metadata_path not in selected_paths
        ]
        if missing_legacy_urls:
            raise PackMigrationResolutionError(
                "Legacy URL metadata requires an explicit root identity: "
                f"{missing_legacy_urls[0]}"
            )
        checkpoint()
        _validate_detached_snapshot(plan, checkpoint)
        _validate_live_source(
            plan,
            repository_root,
            operation_event,
            effective_deadline,
        )
        detached_source = plan.source_snapshot_root / "source"
        detached_scan = scan_pack_migration_source(
            detached_source, checkpoint=checkpoint
        )
        provenance_source = plan.transaction_root / "provenance-staging"
        top_level = tuple(
            sorted(
                {
                    Path(entry.relative_path.parts[0])
                    for entry in detached_scan.entries[1:]
                },
                key=lambda path: path.as_posix(),
            )
        )
        try:
            copy_pack_tree_snapshot(
                detached_scan,
                provenance_source,
                include=top_level,
                checkpoint=checkpoint,
                destination_parent_identity=plan._transaction_identity,
            )
            ensure_pack_root_manifest_ignored(provenance_source)
            for relative_path, project_id in legacy_url_selections:
                checkpoint()
                set_url_metadata_project_id(
                    provenance_source,
                    relative_path,
                    project_id,
                )
            write_pack_root_manifest(provenance_source, tuple(records))
            if legacy_url_selections:
                packctl.run_packwiz(
                    ["packwiz", "refresh"],
                    cwd=provenance_source,
                    cancel_event=operation_event,
                    deadline=effective_deadline,
                    project_id=plan.target.target_id,
                    operation="migration-provenance-refresh",
                )
            provenance_scan = scan_pack_migration_source(
                provenance_source, checkpoint=checkpoint
            )
            extracted = extract_pack_migration_roots(
                provenance_source,
                expected_identity=provenance_scan.root_identity,
                expected_snapshot_digest=provenance_scan.snapshot_digest,
                checkpoint=checkpoint,
            )
            if {root.canonical_identity for root in extracted} != identities:
                raise PackMigrationResolutionError(
                    "Committed root manifest does not match the selected identities"
                )
            _validate_detached_snapshot(plan, checkpoint)
            _validate_live_source(
                plan,
                repository_root,
                operation_event,
                effective_deadline,
            )
            _commit_root_provenance_source(
                plan,
                provenance_source,
                provenance_scan,
                checkpoint,
            )
            plan._provenance_committed = True
        except BaseException as error:
            plan._state = "failed"
            plan.cleanup_error = error
            _record_plan_diagnostic(plan)
            raise
        from pack_migration import discard_pack_migration_plan

        discard_pack_migration_plan(plan, deadline=effective_deadline)
        return tuple(sorted(records, key=lambda record: record.canonical_identity))


def _resolve_effective_root_set(
    plan: PackMigrationPlan,
    roots: tuple[PackMigrationRoot, ...] | None = None,
    *,
    repository_root: Path,
    state_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PackMigrationProgress], None] | None = None,
) -> PackMigrationResolutionPlan:
    del state_root
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
    )
    operation_event = cancel_event if cancel_event is not None else threading.Event()
    checkpoint = lambda: _checkpoint(operation_event, effective_deadline)
    with plan._lock:
        conflict_retry = roots is not None and getattr(plan, "_active_resolution_request", None) is not None
        if plan.state != "staged" and not (conflict_retry and plan.state == "resolving"):
            raise PackMigrationResolutionError(
                f"Pack migration resolution requires staged state, not {plan.state}"
            )
        if set(plan._lock_set.owned_keys) != {
            plan.source_key,
            f"pack:{plan.target.target_id}",
        }:
            raise PackMigrationResolutionError("Pack migration locks are not fully owned")
        if not hasattr(plan, "_resolution_attempt"):
            plan._resolution_attempt = 0
        plan._state = "resolving"
        _progress(progress, PackMigrationProgress("validating", 0, 1, None, "Validating plan"))
        resolver_workspace_fd = -1
        try:
            checkpoint()
            if _identity(plan.transaction_root) != plan._transaction_identity:
                raise PackMigrationStale("Pack migration transaction root was replaced")
            live_source = snapshot_pack_migration_source_at(
                plan.source_key,
                plan.source_root,
                repository_root,
                cancel_event=operation_event,
                deadline=effective_deadline,
            )
            if not _same_snapshot(live_source, plan.source_snapshot):
                raise PackMigrationStale("Source Pack changed before resolution")
            detached = _validate_detached_snapshot(plan, checkpoint)
            _progress(
                progress,
                PackMigrationProgress("extracting-roots", 0, 1, None, "Extracting roots"),
            )
            detached_source = (
                plan._provenance_source_root
                if plan._migration_root_authority is not None
                else plan.source_snapshot_root / "source"
            )
            if detached_source is None:
                raise PackMigrationStale(
                    "Migration-local root provenance source is unavailable"
                )
            detached_source_scan = scan_pack_migration_source(
                detached_source, checkpoint=checkpoint
            )
            if plan._migration_root_authority is None:
                detached_source_entry = next(
                    (
                        entry
                        for entry in detached.entries
                        if entry.relative_path == Path("source")
                    ),
                    None,
                )
                if (
                    detached_source_entry is None
                    or detached_source_entry.kind != "directory"
                    or (
                        detached_source_entry.device,
                        detached_source_entry.inode,
                    )
                    != detached_source_scan.root_identity
                ):
                    raise PackMigrationStale("Detached Packwiz source was replaced")
            elif (
                detached_source_scan.root_identity
                != plan._provenance_source_identity
                or detached_source_scan.content_digest
                != plan._provenance_source_content_digest
                or detached_source_scan.snapshot_digest
                != plan._provenance_source_snapshot_digest
            ):
                raise PackMigrationStale(
                    "Migration-local provenance source changed before resolution"
                )
            try:
                detached_overrides = read_detached_version_intent(
                    detached_source, checkpoint=checkpoint
                )
                detached_metadata = (
                    tuple(
                        DetachedVersionIntentMetadata(
                            entry.relative_path,
                            _read_relative_file(
                                detached_source,
                                detached_source_scan,
                                entry.relative_path,
                                max_bytes=2 * 1024 * 1024,
                            ),
                        )
                        for entry in detached_source_scan.entries
                        if entry.kind == "file"
                        and entry.relative_path.name.endswith(".pw.toml")
                    )
                    if detached_overrides
                    else ()
                )
                intent_facts = validate_detached_version_intent(
                    detached_source,
                    metadata=detached_metadata,
                    checkpoint=checkpoint,
                    overrides=detached_overrides,
                )
            except PackMigrationVersionIntentError as error:
                raise PackMigrationResolutionError(str(error)) from error
            try:
                original_roots = extract_pack_migration_roots(
                    detached_source,
                    expected_identity=detached_source_scan.root_identity,
                    expected_snapshot_digest=detached_source_scan.snapshot_digest,
                    checkpoint=checkpoint,
                )
            except PackMigrationRootManifestMissing:
                original_roots = None
            if roots is None and original_roots is not None:
                roots = original_roots
            if original_roots is None:
                candidates = extract_pack_migration_root_candidates(
                    detached_source,
                    expected_identity=detached_source_scan.root_identity,
                    expected_snapshot_digest=detached_source_scan.snapshot_digest,
                    checkpoint=checkpoint,
                )
                _validate_detached_snapshot(plan, checkpoint)
                _validate_live_source(
                    plan,
                    repository_root,
                    operation_event,
                    effective_deadline,
                )
                result = PackMigrationResolutionPlan(
                    plan.source_snapshot.snapshot_digest,
                    plan.target,
                    (),
                    candidates,
                    (),
                    tuple(
                        PackMigrationUnresolvedRoot(
                            candidate,
                            "root-provenance-required",
                            "Select whether this installed MOD is an explicit root",
                            False,
                            True,
                        )
                        for candidate in candidates
                    ),
                    PackMigrationDependencyDelta(),
                    (),
                    (),
                    (),
                    (),
                    (),
                    (),
                    None,
                    "resolution-required",
                    True,
                    int(getattr(plan, "_resolution_attempt", 0)),
                    intent_facts,
                    (),
                    intent_facts.digest,
                )
                plan.resolution = result
                plan._state = "resolution-required"
                _record_plan_diagnostic(plan)
                return result
            roots = tuple(roots)
            root_ids = {root.canonical_identity for root in roots}
            retired_root_ids = (
                {root.canonical_identity for root in (original_roots or ())} - root_ids
                if getattr(plan, "_active_resolution_request", None) is not None
                else set()
            )
            overrides = intent_by_identity(intent_facts)
            workspace = _create_workspace(plan)
            resolver_workspace_fd = os.open(workspace, _DIRECTORY_FLAGS)
            opened_workspace = os.fstat(resolver_workspace_fd)
            if (opened_workspace.st_dev, opened_workspace.st_ino) != plan._resolver_work_identity:
                raise PackMigrationStale("Resolver workspace was replaced after creation")
            bound_workspace = Path(f"/proc/self/fd/{resolver_workspace_fd}")
            (bound_workspace / "roots").mkdir()
            (bound_workspace / "logs").mkdir()
            initialize_target_packwiz_source(
                workspace,
                plan.target,
                cancel_event=operation_event,
                deadline=effective_deadline,
                progress=progress,
                operation_root=bound_workspace,
            )
            import huroshiki_core as core

            resolved: list[PackMigrationResolvedRoot] = []
            provisional_resolved: dict[str, PackMigrationResolvedRoot] = {}
            unresolved: list[PackMigrationUnresolvedRoot] = []
            intent_issues: list[PackMigrationVersionIntentIssue] = []
            root_closures: list[tuple[PackMigrationRoot, object]] = []
            path_collisions: list[str] = []
            filename_collisions: list[str] = []
            identity_collisions: list[tuple[str, str]] = []
            url_compatibility: list[tuple[str, UrlMigrationCompatibility]] = []
            for index, root in enumerate(
                sorted(roots, key=lambda item: item.canonical_identity), 1
            ):
                override = overrides.get(root.canonical_identity)
                checkpoint()
                _progress(
                    progress,
                    PackMigrationProgress(
                        "resolving-roots",
                        index - 1,
                        len(roots),
                        root.canonical_identity,
                        f"Resolving {root.canonical_identity}",
                    ),
                )
                try:
                    if root.provider == "modrinth":
                        selector = core.resolve_project_selector(
                            "modrinth",
                            root.project_id,
                            cancel_event=operation_event,
                            deadline=effective_deadline,
                        )
                        if selector.canonical_project_id != root.project_id:
                            unresolved.append(
                                PackMigrationUnresolvedRoot(
                                    root,
                                    "provider-identity-ambiguous",
                                    "Provider project identity changed",
                                    False,
                                    True,
                                )
                            )
                            continue
                    if override is not None:
                        selection = core.exact_mod_artifact_selection(
                            override.provider,
                            override.project_id,
                            override.artifact_id,
                        )
                        exact_source = bound_workspace / "roots" / f"exact-{index}"
                        core.create_resolver_source(
                            exact_source,
                            display_name=f"Resolve {root.canonical_identity}",
                            minecraft=plan.target.minecraft_version,
                            loader=plan.target.loader,
                            loader_version=plan.target.loader_version,
                        )
                        closure = core.resolve_exact_mod_closure(
                            selection,
                            source=exact_source,
                            cancel_event=operation_event,
                            deadline=effective_deadline,
                            checkpoint=checkpoint,
                            process_result_callback=plan._record_resolver_process_result,
                            diagnostic_project_id=plan.target.target_id,
                        )
                        core.verify_exact_mod_metadata(selection, closure.metadata)
                    else:
                        closure = core.resolve_mod_closure(
                            provider=root.provider,
                            selector=root.source_download_url or root.project_id,
                            minecraft=plan.target.minecraft_version,
                            loader=plan.target.loader,
                            loader_version=plan.target.loader_version,
                            canonical_project_id=(
                                None if root.provider == "url" else root.project_id
                            ),
                            cancel_event=operation_event,
                            deadline=effective_deadline,
                            resolver_root=bound_workspace / "roots" / f"root-{index}",
                            url_max_jar_size_bytes=plan.source_snapshot.url_max_jar_size_bytes,
                            url_allow_private_networks=plan.source_snapshot.url_allow_private_networks,
                            process_result_callback=plan._record_resolver_process_result,
                            diagnostic_project_id=plan.target.target_id,
                        )
                except Exception as error:
                    if _operation_failure(error):
                        raise PackMigrationResolutionError(str(error)) from error
                    message = str(error)
                    lowered = message.lower()
                    if override is not None:
                        reason = "version-intent-blocked"
                        intent_issues.append(
                            PackMigrationVersionIntentIssue(
                                root.canonical_identity,
                                root.canonical_identity,
                                override.artifact_id,
                                "version-intent-blocked",
                                message[:240],
                            )
                        )
                    elif root.provider == "url":
                        if "does not declare support for loader" in lowered:
                            reason: UnresolvedReason = "url-incompatible-loader"
                        elif "archive" in lowered or "metadata" in lowered:
                            reason = "url-invalid-archive"
                        else:
                            reason = "url-compatible-unknown"
                    elif "project" in lowered and (
                        "missing" in lowered or "not found" in lowered
                    ):
                        reason = "provider-project-missing"
                    else:
                        reason = "no-compatible-file"
                    unresolved.append(
                        PackMigrationUnresolvedRoot(
                            root,
                            reason,
                            message[:240],
                            True,
                            True,
                        )
                    )
                    continue
                root_records = [
                    item for item in closure.metadata if f"{item.provider}:{item.project_id}" == root.canonical_identity
                ]
                if len(root_records) != 1:
                    unresolved.append(
                        PackMigrationUnresolvedRoot(
                            root,
                            "provider-identity-ambiguous",
                            "Resolver did not return exactly one canonical root",
                            False,
                            True,
                        )
                    )
                    continue
                try:
                    target_metadata = parse_provider_metadata(
                        root_records[0].relative_path,
                        root_records[0].contents,
                    )
                except ProviderIdentityError as error:
                    raise PackMigrationResolutionError(str(error)) from error
                if root.provider == "url":
                    compatibility = _url_compatibility(
                        root_records[0].contents,
                        plan.target,
                    )
                    url_compatibility.append((root.canonical_identity, compatibility))
                    if compatibility.status != "compatible":
                        reason: UnresolvedReason
                        if compatibility.loader_status == "incompatible":
                            reason = "url-incompatible-loader"
                        elif compatibility.minecraft_status == "incompatible":
                            reason = "url-incompatible-minecraft"
                        else:
                            reason = "url-compatible-unknown"
                        unresolved.append(
                            PackMigrationUnresolvedRoot(
                                root,
                                reason,
                                compatibility.warnings[0],
                                False,
                                True,
                            )
                        )
                        continue
                classification: Literal["unchanged", "updated", "identity-change"]
                if target_metadata.canonical_identity != root.canonical_identity:
                    classification = "identity-change"
                elif (
                    target_metadata.file_id == root.source_file_id
                    and target_metadata.metadata_path == root.source_metadata_path
                    and target_metadata.filename == root.source_filename
                    and target_metadata.side == root.source_side
                ):
                    classification = "unchanged"
                else:
                    classification = "updated"
                if classification == "identity-change":
                    unresolved.append(
                        PackMigrationUnresolvedRoot(
                            root,
                            "provider-identity-ambiguous",
                            "Resolved root identity changed",
                            False,
                            True,
                        )
                    )
                    continue
                resolved_root = PackMigrationResolvedRoot(
                    root,
                    target_metadata.canonical_identity,
                    target_metadata.file_id,
                    target_metadata.version,
                    root.source_side,
                    target_metadata.metadata_path,
                    target_metadata.filename,
                    classification,
                )
                root_closures.append((root, closure))
                provisional_resolved[root.canonical_identity] = resolved_root
            closure_by_root = {
                root.canonical_identity: closure for root, closure in root_closures
            }
            dependency_overrides = {
                identity: override
                for identity, override in overrides.items()
            }
            for owner, initial_closure in root_closures:
                owner_identity = owner.canonical_identity
                current = initial_closure
                root_item = next(
                    (
                        item
                        for item in current.metadata
                        if f"{item.provider}:{item.project_id}" == owner_identity
                    ),
                    None,
                )
                root_artifact = (
                    None
                    if root_item is None
                    else parse_provider_metadata(
                        root_item.relative_path, root_item.contents
                    ).file_id
                )
                constrained_identities: set[str] = set()
                for constraint_attempt in range(len(dependency_overrides) + 1):
                    current_identities = {
                        f"{item.provider}:{item.project_id}"
                        for item in current.metadata
                    }
                    required_identities = {
                        identity
                        for identity in dependency_overrides
                        if identity != owner_identity
                        and identity in current_identities
                    }
                    if required_identities <= constrained_identities:
                        break
                    constrained_identities.update(required_identities)
                    selections = tuple(
                        core.exact_mod_artifact_selection(
                            dependency_overrides[identity].provider,
                            dependency_overrides[identity].project_id,
                            dependency_overrides[identity].artifact_id,
                        )
                        for identity in sorted(constrained_identities)
                    )
                    if root_artifact is None or owner.provider == "url":
                        message = (
                            f"Exact dependency intent cannot constrain {owner_identity}"
                        )
                        intent_issues.extend(
                            PackMigrationVersionIntentIssue(
                                selection.identity_label,
                                owner_identity,
                                str(selection.artifact_id),
                                "version-intent-blocked",
                                message,
                            )
                            for selection in selections
                        )
                        break
                    try:
                        exact_source = bound_workspace / "roots" / (
                            "constrained-"
                            + owner_identity.replace(":", "-")
                            + f"-{constraint_attempt:04d}"
                        )
                        core.create_resolver_source(
                            exact_source,
                            display_name=f"Resolve {owner_identity}",
                            minecraft=plan.target.minecraft_version,
                            loader=plan.target.loader,
                            loader_version=plan.target.loader_version,
                        )
                        constrained = core.resolve_exact_mod_closure(
                            core.exact_mod_artifact_selection(
                                owner.provider,
                                owner.project_id,
                                root_artifact,
                            ),
                            source=exact_source,
                            cancel_event=operation_event,
                            deadline=effective_deadline,
                            checkpoint=checkpoint,
                            preseed_selections=selections,
                            process_result_callback=plan._record_resolver_process_result,
                            diagnostic_project_id=plan.target.target_id,
                        )
                        for selection in selections:
                            core.verify_exact_mod_metadata(
                                selection, constrained.metadata
                            )
                        current = constrained
                        closure_by_root[owner_identity] = constrained
                    except Exception as error:
                        if _operation_failure(error):
                            raise PackMigrationResolutionError(str(error)) from error
                        intent_issues.extend(
                            PackMigrationVersionIntentIssue(
                                selection.identity_label,
                                owner_identity,
                                str(selection.artifact_id),
                                "version-intent-blocked",
                                str(error)[:240],
                            )
                            for selection in selections
                        )
                        break
                else:
                    raise PackMigrationResolutionError(
                        "Exact dependency constraints did not converge for "
                        f"{owner_identity}"
                    )
            issue_identities = {issue.identity for issue in intent_issues}
            final_graph_identities = {
                f"{item.provider}:{item.project_id}"
                for root, _ in root_closures
                for item in closure_by_root[root.canonical_identity].metadata
            }
            for identity, override in sorted(dependency_overrides.items()):
                owners = tuple(
                    root.canonical_identity
                    for root, _ in root_closures
                    if any(
                        f"{item.provider}:{item.project_id}" == identity
                        for item in closure_by_root[root.canonical_identity].metadata
                    )
                )
                if (
                    not owners
                    and identity not in issue_identities
                    and identity not in retired_root_ids
                ):
                    intent_issues.append(
                        PackMigrationVersionIntentIssue(
                            identity,
                            None,
                            override.artifact_id,
                            "version-intent-blocked",
                            f"Version override {identity} is not required by any "
                            "resolved root",
                        )
                    )
            retained_intent_identities = final_graph_identities | root_ids
            retained_overrides = tuple(
                item
                for item in intent_facts.overrides
                if item.canonical_identity in retained_intent_identities
            )
            intent_facts = replace(
                intent_facts,
                overrides=retained_overrides,
                automatic_identities=tuple(
                    identity
                    for identity in sorted(final_graph_identities)
                    if identity
                    not in {item.canonical_identity for item in retained_overrides}
                ),
                digest=hashlib.sha256(
                    serialize_mod_version_overrides(retained_overrides)
                ).hexdigest(),
            )
            if intent_issues:
                existing_intent_failures = {
                    (item.source_root.canonical_identity, item.message)
                    for item in unresolved
                    if item.reason_code == "version-intent-blocked"
                    and isinstance(item.source_root, PackMigrationRoot)
                }
                unresolved.extend(
                    PackMigrationUnresolvedRoot(
                        next(
                            (root for root in roots if root.canonical_identity == issue.owner_identity),
                            roots[0],
                        ),
                        "version-intent-blocked", issue.message, False,
                        issue.identity == issue.owner_identity,
                    ) for issue in intent_issues
                    if roots
                    and (issue.owner_identity or roots[0].canonical_identity, issue.message)
                    not in existing_intent_failures
                )
            _progress(
                progress,
                PackMigrationProgress(
                    "building-closure",
                    len(resolved),
                    len(roots),
                    None,
                    "Built resolved root closures",
                ),
            )
            source_roots = {
                root.canonical_identity for root in (original_roots or roots)
            }
            before = _metadata_entries(detached_source, source_roots, checkpoint)
            unresolved_identities = {
                item.source_root.canonical_identity
                for item in unresolved
                if isinstance(item.source_root, PackMigrationRoot)
            }
            for root, _ in root_closures:
                checkpoint()
                if root.canonical_identity in unresolved_identities:
                    continue
                try:
                    core.merge_metadata_closure(
                        bound_workspace / "source",
                        closure_by_root[root.canonical_identity],
                        requested_side=root.source_side,
                        cancel_event=operation_event,
                        deadline=effective_deadline,
                        equivalence_workspace=bound_workspace / "equivalence",
                        process_result_callback=plan._record_resolver_process_result,
                    )
                except Exception as error:
                    collision_reason = _classify_collision(str(error))
                    if collision_reason is None:
                        raise
                    message = str(error)[:240]
                    if collision_reason == "path-collision":
                        path_collisions.append(message)
                    elif collision_reason == "filename-collision":
                        filename_collisions.append(message)
                    elif collision_reason == "identity-collision":
                        identity_collisions.append(
                            (root.canonical_identity, "collision")
                        )
                    unresolved.append(
                        PackMigrationUnresolvedRoot(
                            root, collision_reason, message, False, True
                        )
                    )
                    unresolved_identities.add(root.canonical_identity)
                    continue
                resolved.append(provisional_resolved[root.canonical_identity])
            if unresolved:
                _validate_detached_snapshot(plan, checkpoint)
                _validate_live_source(
                    plan, repository_root, operation_event, effective_deadline
                )
                result = PackMigrationResolutionPlan(
                    plan.source_snapshot.snapshot_digest, plan.target, roots, (),
                    tuple(resolved), tuple(unresolved), PackMigrationDependencyDelta(),
                    (), tuple(identity_collisions), tuple(path_collisions),
                    tuple(filename_collisions), (), tuple(url_compatibility), None,
                    "resolution-required", False,
                    int(getattr(plan, "_resolution_attempt", 0)), intent_facts,
                    tuple(intent_issues), intent_facts.digest,
                )
                plan.resolution = result
                plan._state = "resolution-required"
                _record_plan_diagnostic(plan)
                return result
            target_root_identities = {item.target_identity for item in resolved}
            merged_target_entries = {
                item.canonical_identity: item
                for item in _metadata_entries(
                    bound_workspace / "source", target_root_identities, checkpoint
                )
            }
            finalized_resolved: list[PackMigrationResolvedRoot] = []
            target_root_records: list[PackRootRecord] = []
            for item in resolved:
                provider, separator, project_id = item.target_identity.partition(":")
                if not separator or canonical_identity(provider, project_id) != item.target_identity:
                    raise PackMigrationResolutionError(
                        f"Resolved target root identity is invalid: {item.target_identity}"
                    )
                target_entry = merged_target_entries.get(item.target_identity)
                if target_entry is None or target_entry.side not in {
                    "client", "server", "both"
                }:
                    raise PackMigrationResolutionError(
                        f"Resolved target root metadata is unavailable: {item.target_identity}"
                    )
                final_classification = item.classification
                if (
                    final_classification == "unchanged"
                    and target_entry.side != item.source_root.source_side
                ):
                    final_classification = "updated"
                finalized_resolved.append(
                    replace(
                        item,
                        target_side=target_entry.side,
                        classification=final_classification,
                    )
                )
                target_root_records.append(
                    PackRootRecord(provider, project_id, target_entry.side)  # type: ignore[arg-type]
                )
            resolved = finalized_resolved
            expected_target_roots = tuple(
                sorted(target_root_records, key=lambda item: item.canonical_identity)
            )
            ensure_pack_root_manifest_ignored(
                bound_workspace / "source", checkpoint=checkpoint
            )
            write_pack_root_manifest(
                bound_workspace / "source", expected_target_roots
            )
            if intent_facts.overrides:
                ensure_mod_version_overrides_ignored(
                    bound_workspace / "source", checkpoint=checkpoint
                )
                write_mod_version_overrides(
                    bound_workspace / "source",
                    intent_facts.overrides,
                    checkpoint=checkpoint,
                )
            _progress(
                progress,
                PackMigrationProgress("refreshing", len(roots), len(roots), None, "Refreshing target"),
            )
            core.run_noninteractive_packwiz(
                ["packwiz", "refresh"],
                cwd=bound_workspace / "source",
                cancel_event=operation_event,
                deadline=effective_deadline,
                label="Pack migration target refresh",
                process_result_callback=plan._record_resolver_process_result,
                project_id=plan.target.target_id,
                operation="migration-refresh",
            )
            _progress(
                progress,
                PackMigrationProgress(
                    "validating-target",
                    len(roots),
                    len(roots),
                    None,
                    "Validating target Packwiz source",
                ),
            )
            target_versions = packctl.project_versions(bound_workspace / "source")
            if target_versions != (
                plan.target.minecraft_version,
                plan.target.loader,
                plan.target.loader_version,
            ):
                raise PackMigrationResolutionError(
                    "Resolver source target versions do not match the migration target"
                )
            resolver_scan = _scan_workspace_source(
                plan,
                workspace,
                resolver_workspace_fd,
                checkpoint,
            )
            installed_target_roots = extract_pack_migration_roots(
                workspace / "source",
                expected_identity=resolver_scan.root_identity,
                expected_snapshot_digest=resolver_scan.snapshot_digest,
                checkpoint=checkpoint,
            )
            installed_target_records = tuple(
                sorted(
                    (
                        PackRootRecord(root.provider, root.project_id, root.source_side)
                        for root in installed_target_roots
                    ),
                    key=lambda item: item.canonical_identity,
                )
            )
            if installed_target_records != expected_target_roots:
                raise PackMigrationResolutionError(
                    "Resolved target root provenance does not match the effective root set"
                )
            target_roots = {root.target_identity for root in resolved}
            after = _metadata_entries(
                workspace / "source",
                target_roots,
                checkpoint,
                scan=resolver_scan,
            )
            _progress(
                progress,
                PackMigrationProgress(
                    "classifying",
                    len(roots),
                    len(roots),
                    None,
                    "Classifying dependency changes",
                ),
            )
            delta = _dependency_delta(before, after)
            _validate_detached_snapshot(plan, checkpoint)
            _validate_live_source(
                plan,
                repository_root,
                operation_event,
                effective_deadline,
            )
            _progress(
                progress,
                PackMigrationProgress("committing", len(roots), len(roots), None, "Committing target source"),
            )
            installed_scan = _exchange_target_source(
                plan, workspace, resolver_scan, checkpoint
            )
            plan._resolved_source_snapshot_digest = installed_scan.snapshot_digest
            result = PackMigrationResolutionPlan(
                plan.source_snapshot.snapshot_digest,
                plan.target,
                roots,
                (),
                tuple(resolved),
                (),
                delta,
                tuple(
                    (old.canonical_identity, old.side, new.side)
                    for old, new in delta.side_changed
                ),
                tuple(
                    (old.canonical_identity, new.canonical_identity)
                    for old, new in delta.identity_changed
                ),
                (),
                (),
                (),
                tuple(url_compatibility),
                installed_scan,
                "resolved",
                False,
                int(getattr(plan, "_resolution_attempt", 0)),
                intent_facts,
                (),
                intent_facts.digest,
            )
            plan.resolution = result
            plan._state = "resolved"
            _retire_resolution_pending_warnings(plan)
            _record_plan_diagnostic(plan)
            return result
        except BaseException as error:
            plan._state = "failed"
            plan.cleanup_error = error
            _record_plan_diagnostic(plan)
            raise
        finally:
            if resolver_workspace_fd >= 0:
                os.close(resolver_workspace_fd)


def resolve_pack_migration_plan_at(
    plan: PackMigrationPlan,
    *,
    repository_root: Path,
    state_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PackMigrationProgress], None] | None = None,
) -> PackMigrationResolutionPlan:
    """Run the initial, provenance-aware migration resolution."""
    return _resolve_effective_root_set(
        plan,
        None,
        repository_root=repository_root,
        state_root=state_root,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def resolve_pack_migration_conflicts_at(
    plan: PackMigrationPlan,
    request: "PackMigrationResolutionRequest",
    *,
    repository_root: Path,
    state_root: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PackMigrationProgress], None] | None = None,
) -> "PackMigrationConflictResolutionResult":
    """Apply a pure conflict choice by resolving its effective roots afresh."""
    from pack_migration_conflicts import (
        PackMigrationConflictResolutionError,
        PackMigrationConflictResolutionResult,
        validate_resolution_request,
    )

    # Validation is deliberately the first operation under the plan lock.  In
    # particular, it must not emit progress or inspect a path before rejecting
    # a stale/user-forged request.
    with plan._lock:
        validated = validate_resolution_request(plan, request)
        effective_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + PACK_MIGRATION_TIMEOUT_SECONDS
        )
        operation_event = (
            cancel_event if cancel_event is not None else threading.Event()
        )
        checkpoint = lambda: _checkpoint(operation_event, effective_deadline)
        _progress(
            progress,
            PackMigrationProgress(
                "validating-resolutions",
                0,
                1,
                None,
                "Validating resolutions",
            ),
        )
        try:
            checkpoint()
            import huroshiki_core as core

            for replacement in validated.replaced_roots:
                if replacement.replacement_root.provider != "modrinth":
                    continue
                try:
                    selector = core.resolve_project_selector(
                        "modrinth",
                        replacement.replacement_root.project_id,
                        cancel_event=operation_event,
                        deadline=effective_deadline,
                    )
                except Exception as error:
                    if _operation_failure(error):
                        raise
                    # Missing or incompatible projects remain user-level
                    # unresolved outcomes in the fresh resolver attempt.
                    continue
                if (
                    selector.canonical_project_id
                    != replacement.replacement_root.project_id
                ):
                    raise PackMigrationConflictResolutionError(
                        "Modrinth replacement must use its canonical project ID"
                    )
        except PackMigrationConflictResolutionError:
            raise
        except BaseException as error:
            plan._state = "failed"
            plan.cleanup_error = error
            _record_plan_diagnostic(plan)
            raise
        previous = plan.resolution
        plan._active_resolution_request = request
        plan._previous_resolution = previous
        plan._resolution_input_digest = request.resolution_snapshot_digest
        plan._resolution_attempt = int(getattr(plan, "_resolution_attempt", 0)) + 1
        plan._state = "resolving"
        try:
            checkpoint()
            if _identity(plan.transaction_root) != plan._transaction_identity:
                raise PackMigrationStale("Pack migration transaction root was replaced")
            _validate_detached_snapshot(plan, checkpoint)
            _validate_live_source(
                plan, repository_root, operation_event, effective_deadline
            )
            staging = scan_pack_migration_source(plan.target_staging_root, checkpoint=checkpoint)
            if (
                staging.snapshot_digest != plan._staging_snapshot_digest
                or staging.root_identity != plan._staging_identity
            ):
                raise PackMigrationStale("Target staging changed before conflict resolution")
            _progress(
                progress,
                PackMigrationProgress(
                    "applying-resolutions",
                    0,
                    len(validated.effective_roots),
                    None,
                    "Applying resolutions",
                ),
            )
            result = _resolve_effective_root_set(
                plan,
                tuple(validated.effective_roots),
                repository_root=repository_root,
                state_root=state_root,
                cancel_event=operation_event,
                deadline=effective_deadline,
                progress=progress,
            )
            cumulative_removed = tuple(
                {
                    item.source_root.canonical_identity: item
                    for item in (
                        plan._conflict_removed_roots + validated.removed_roots
                    )
                }.values()
            )
            cumulative_replaced = tuple(
                {
                    item.old_identity: item
                    for item in (
                        plan._conflict_replaced_roots + validated.replaced_roots
                    )
                }.values()
            )
            replacement_changes = tuple(
                (item.old_identity, item.new_identity)
                for item in cumulative_replaced
            )
            result = replace(
                result,
                identity_changes=tuple(
                    dict.fromkeys(result.identity_changes + replacement_changes)
                ),
            )
            plan.resolution = result
            plan._state = result.state
            if result.state == "resolved":
                _retire_resolution_pending_warnings(plan)
            explicit_identities = {
                item.source_root.canonical_identity
                for item in cumulative_removed
            } | {item.old_identity for item in cumulative_replaced}
            removed_dependencies = tuple(
                entry.canonical_identity
                for entry in result.dependency_delta.removed
                if entry.canonical_identity not in explicit_identities
            )
            dependencies_are_attributable = (
                result.state == "resolved"
                and len(cumulative_removed) == 1
                and not cumulative_replaced
            )
            removed = tuple(
                replace(
                    item,
                    removed_dependencies=(
                        removed_dependencies
                        if dependencies_are_attributable
                        else item.removed_dependencies
                    ),
                )
                for item in cumulative_removed
            )
            plan._conflict_removed_roots = removed
            plan._conflict_replaced_roots = cumulative_replaced
            plan._explicit_removed_roots = tuple(
                dict.fromkeys(
                    item.source_root.canonical_identity for item in removed
                )
            )
            plan._explicit_replaced_roots = tuple(
                dict.fromkeys(
                    (item.old_identity, item.new_identity)
                    for item in cumulative_replaced
                )
            )
            _record_plan_diagnostic(plan)
            return PackMigrationConflictResolutionResult(
                resolution_plan=result,
                removed_roots=removed,
                replaced_roots=cumulative_replaced,
                remaining_unresolved=result.unresolved_roots,
                attempt_number=plan._resolution_attempt,
                state=result.state,
            )
        except BaseException as error:
            plan._state = "failed"
            plan.cleanup_error = error
            _record_plan_diagnostic(plan)
            raise
        finally:
            plan._active_resolution_request = None


__all__ = [
    "PackMigrationDependencyDelta",
    "PackMigrationDependencyEntry",
    "PackMigrationProgress",
    "PackMigrationResolutionPlan",
    "PackMigrationRootSelectionAuthority",
    "PackMigrationResolvedRoot",
    "PackMigrationUnresolvedRoot",
    "UrlMigrationCompatibility",
    "commit_pack_migration_root_selection_at",
    "initialize_target_packwiz_source",
    "resolve_pack_migration_plan_at",
    "resolve_pack_migration_conflicts_at",
    "select_pack_migration_roots_at",
]
