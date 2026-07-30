from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Callable, Literal, TypeAlias

import packctl
from overlay_policy import (
    OVERLAY_TARGETS,
    OverlayPolicyError,
    copy_content_tree,
    create_overlay_directory,
    delete_overlay_entry,
    inspect_overlay_file,
    move_overlay_entry,
    normalize_overlay_relative_path,
    read_overlay_bytes,
    scan_content_overlays,
    write_overlay_bytes,
)
from portable_paths import PortablePathError, portable_relative_path_key


CONTENT_TEXT_PROBE_BYTES = 64 * 1024
CONTENT_OPERATION_TIMEOUT_SECONDS = 600.0
CONTENT_DISCARD_TIMEOUT_SECONDS = 10.0
CONTENT_CLEANUP_TIMEOUT_SECONDS = 10.0
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SIDE_ORDER = {side: index for index, side in enumerate(OVERLAY_TARGETS)}


class ContentOperationError(RuntimeError):
    pass


class ContentOperationCancelled(ContentOperationError):
    pass


class ContentOperationDeadlineExceeded(ContentOperationError):
    pass


class ContentPlanStale(ContentOperationError):
    pass


class ContentCleanupError(ContentOperationError):
    pass


@dataclass(frozen=True)
class PathIdentity:
    exists: bool
    device: int | None = None
    inode: int | None = None
    mode: int | None = None


@dataclass(frozen=True)
class ContentEntry:
    side: Literal["common", "client", "server"]
    relative_path: Path
    kind: Literal["file", "directory", "invalid"]
    size: int
    mode: int
    executable: bool
    digest: str | None
    text_kind: Literal["utf8", "binary", "unknown"]
    category: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ContentFile:
    entry: ContentEntry
    contents: bytes


@dataclass(frozen=True)
class ContentSnapshotEntry:
    side: Literal["common", "client", "server"]
    relative_path: Path
    portable_identity: tuple[str, str]
    kind: Literal["file", "directory", "invalid"]
    mode: int
    size: int
    digest: str | None
    device: int | None
    inode: int | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContentSnapshot:
    project_key: str
    project_identity: PathIdentity
    content_parent_identity: PathIdentity
    content_identity: PathIdentity
    entries: tuple[ContentSnapshotEntry, ...]
    digest: str


@dataclass(frozen=True)
class ContentCreateFile:
    side: str
    relative_path: Path
    contents: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class ContentReplaceFile:
    side: str
    relative_path: Path
    contents: bytes
    expected_digest: str | None = None
    mode: int | None = None


@dataclass(frozen=True)
class ContentDeleteFile:
    side: str
    relative_path: Path


@dataclass(frozen=True)
class ContentCreateDirectory:
    side: str
    relative_path: Path
    mode: int = 0o755


@dataclass(frozen=True)
class ContentDeleteDirectory:
    side: str
    relative_path: Path


@dataclass(frozen=True)
class ContentMove:
    source_side: str
    source_path: Path
    destination_side: str
    destination_path: Path


ContentOperation: TypeAlias = (
    ContentCreateFile
    | ContentReplaceFile
    | ContentDeleteFile
    | ContentCreateDirectory
    | ContentDeleteDirectory
    | ContentMove
)


@dataclass(frozen=True)
class ContentConflict:
    kind: Literal[
        "portable_collision",
        "common_client_overlap",
        "common_server_overlap",
        "client_server_divergence",
        "cross_side_type_conflict",
    ]
    severity: Literal["error", "warning"]
    portable_path: str
    entries: tuple[tuple[str, Path], ...]
    message: str


@dataclass(frozen=True)
class ContentChange:
    action: Literal["created", "updated", "deleted", "moved", "unchanged"]
    side: str
    relative_path: Path
    source_side: str | None = None
    source_path: Path | None = None
    before_digest: str | None = None
    after_digest: str | None = None


def _checkpoint(
    cancel_event: threading.Event | None,
    deadline: float | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ContentOperationCancelled("Content operation was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise ContentOperationDeadlineExceeded("Content operation deadline exceeded")


def _identity(path: Path) -> PathIdentity:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return PathIdentity(False)
    except OSError as error:
        raise ContentOperationError(f"Cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ContentOperationError(f"Symlink is not allowed: {path}")
    return PathIdentity(
        True,
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
    )


def _same_identity(left: PathIdentity, right: PathIdentity) -> bool:
    return left == right


def _normalize_side(side: str) -> Literal["common", "client", "server"]:
    normalized = side.strip().lower()
    if normalized not in OVERLAY_TARGETS:
        raise ContentOperationError(
            "Content side must be common, client, or server"
        )
    return normalized  # type: ignore[return-value]


def _normalize_path(path: str | Path) -> Path:
    try:
        normalized = normalize_overlay_relative_path(path)
        portable_relative_path_key(normalized, context="Content path")
        return normalized
    except (OverlayPolicyError, PortablePathError) as error:
        raise ContentOperationError(str(error)) from error


def _validate_mode(mode: int) -> int:
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o777:
        raise ContentOperationError("Content mode must be between 0000 and 0777")
    return mode


def _category(path: Path) -> str:
    if not path.parts:
        return "other"
    prefix = path.parts[0].casefold()
    return {
        "kubejs": "kubejs",
        "config": "config",
        "defaultconfigs": "defaultconfigs",
        "datapacks": "datapack",
        "resourcepacks": "resourcepack",
        "serverconfig": "serverconfig",
    }.get(prefix, "other")


def _text_kind(probe: bytes) -> Literal["utf8", "binary", "unknown"]:
    if b"\0" in probe:
        return "binary"
    try:
        probe.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "utf8"


def _entry_sort_key(entry: ContentEntry) -> tuple[int, str, str]:
    try:
        portable = portable_relative_path_key(
            entry.relative_path,
            context="Content path",
        )
    except PortablePathError:
        portable = str(entry.relative_path).casefold()
    return (_SIDE_ORDER[entry.side], portable, str(entry.relative_path))


def list_content_entries_at(
    project_key: str,
    project_root: Path,
    side: str | None = None,
    *,
    checkpoint: Callable[[], None] | None = None,
    _content_root: Path | None = None,
) -> tuple[ContentEntry, ...]:
    selected_side = None if side is None else _normalize_side(side)
    content_root = _content_root or project_root / "content"
    scan = scan_content_overlays(content_root)
    issues: dict[Path, list[str]] = {}
    for issue in scan.issues:
        issues.setdefault(issue.relative_path, []).append(issue.message)
    entries: list[ContentEntry] = []
    for overlay_entry in scan.entries:
        if checkpoint is not None:
            checkpoint()
        if overlay_entry.relative_path == Path("."):
            entry_side = selected_side or "common"
            relative = Path(".")
        elif not overlay_entry.relative_path.parts:
            continue
        else:
            raw_side = overlay_entry.relative_path.parts[0]
            if raw_side not in OVERLAY_TARGETS:
                continue
            entry_side = _normalize_side(raw_side)
            relative = Path(*overlay_entry.relative_path.parts[1:])
        if selected_side is not None and entry_side != selected_side:
            continue
        entry_errors = list(issues.get(overlay_entry.relative_path, ()))
        try:
            portable_relative_path_key(relative, context="Content path")
        except PortablePathError as error:
            entry_errors.append(str(error))
        kind: Literal["file", "directory", "invalid"]
        digest: str | None = None
        text_kind: Literal["utf8", "binary", "unknown"] = "unknown"
        mode = overlay_entry.mode
        size = overlay_entry.size
        if overlay_entry.kind == "file" and not entry_errors:
            try:
                file = inspect_overlay_file(
                    content_root,
                    entry_side,
                    relative,
                    probe_bytes=CONTENT_TEXT_PROBE_BYTES,
                    checkpoint=checkpoint,
                )
            except (OverlayPolicyError, OSError) as error:
                kind = "invalid"
                entry_errors.append(str(error))
            else:
                kind = "file"
                mode = file.mode
                size = file.size
                digest = file.digest
                text_kind = file.text_kind
        elif overlay_entry.kind == "directory" and not entry_errors:
            kind = "directory"
        else:
            kind = "invalid"
        entries.append(
            ContentEntry(
                entry_side,
                relative,
                kind,
                size,
                mode,
                bool(mode & 0o111),
                digest,
                text_kind,
                _category(relative),
                tuple(entry_errors),
            )
        )
    return tuple(sorted(entries, key=_entry_sort_key))


def read_content_file_at(
    project_key: str,
    project_root: Path,
    side: str,
    relative_path: str | Path,
    *,
    max_bytes: int | None = None,
) -> ContentFile:
    normalized_side = _normalize_side(side)
    relative = _normalize_path(relative_path)
    try:
        file = read_overlay_bytes(
            project_root / "content",
            normalized_side,
            relative,
            max_bytes=max_bytes,
        )
    except (OverlayPolicyError, OSError) as error:
        raise ContentOperationError(str(error)) from error
    entry = ContentEntry(
        normalized_side,
        relative,
        "file",
        len(file.contents),
        file.mode,
        bool(file.mode & 0o111),
        file.digest,
        _text_kind(file.contents),
        _category(relative),
        (),
    )
    return ContentFile(entry, file.contents)


def _snapshot_digest(entries: tuple[ContentSnapshotEntry, ...]) -> str:
    serialized = [
        {
            "side": entry.side,
            "path": entry.relative_path.as_posix(),
            "portable": entry.portable_identity[1],
            "kind": entry.kind,
            "mode": entry.mode,
            "size": entry.size,
            "digest": entry.digest,
            "errors": list(entry.errors),
        }
        for entry in entries
    ]
    return hashlib.sha256(
        json.dumps(
            serialized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def content_snapshot_at(
    project_key: str,
    project_root: Path,
    *,
    content_root: Path | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> ContentSnapshot:
    actual_content_root = content_root or project_root / "content"
    before_project = _identity(project_root)
    before_parent = _identity(actual_content_root.parent)
    before_content = _identity(actual_content_root)
    entries = list_content_entries_at(
        project_key,
        project_root,
        checkpoint=checkpoint,
        _content_root=actual_content_root,
    )
    snapshot_entries: list[ContentSnapshotEntry] = []
    overlay_scan = scan_content_overlays(actual_content_root)
    metadata_by_path = {
        entry.relative_path: entry for entry in overlay_scan.entries
    }
    for entry in entries:
        if checkpoint is not None:
            checkpoint()
        try:
            portable = portable_relative_path_key(
                entry.relative_path,
                context="Content path",
            )
        except PortablePathError:
            portable = f"!invalid:{entry.relative_path.as_posix()}"
        metadata = metadata_by_path.get(Path(entry.side) / entry.relative_path)
        snapshot_entries.append(
            ContentSnapshotEntry(
                entry.side,
                entry.relative_path,
                (entry.side, portable),
                entry.kind,
                entry.mode,
                entry.size,
                entry.digest,
                metadata.device if metadata is not None else None,
                metadata.inode if metadata is not None else None,
                entry.errors,
            )
        )
    after_project = _identity(project_root)
    after_parent = _identity(actual_content_root.parent)
    after_content = _identity(actual_content_root)
    if (
        not _same_identity(before_project, after_project)
        or not _same_identity(before_parent, after_parent)
        or not _same_identity(before_content, after_content)
    ):
        raise ContentOperationError("Content root changed while creating snapshot")
    ordered = tuple(
        sorted(
            snapshot_entries,
            key=lambda entry: (
                _SIDE_ORDER[entry.side],
                entry.portable_identity[1],
                entry.relative_path.as_posix(),
            ),
        )
    )
    return ContentSnapshot(
        project_key,
        before_project,
        before_parent,
        before_content,
        ordered,
        _snapshot_digest(ordered),
    )


def _normalize_operations(
    operations: tuple[ContentOperation, ...],
) -> tuple[ContentOperation, ...]:
    normalized: list[ContentOperation] = []
    targets: set[tuple[str, str]] = set()
    for operation in operations:
        if isinstance(operation, ContentCreateFile):
            item: ContentOperation = ContentCreateFile(
                _normalize_side(operation.side),
                _normalize_path(operation.relative_path),
                bytes(operation.contents),
                _validate_mode(operation.mode),
            )
            target = (item.side, portable_relative_path_key(item.relative_path))
        elif isinstance(operation, ContentReplaceFile):
            item = ContentReplaceFile(
                _normalize_side(operation.side),
                _normalize_path(operation.relative_path),
                bytes(operation.contents),
                operation.expected_digest,
                None if operation.mode is None else _validate_mode(operation.mode),
            )
            target = (item.side, portable_relative_path_key(item.relative_path))
        elif isinstance(operation, ContentDeleteFile):
            item = ContentDeleteFile(
                _normalize_side(operation.side),
                _normalize_path(operation.relative_path),
            )
            target = (item.side, portable_relative_path_key(item.relative_path))
        elif isinstance(operation, ContentCreateDirectory):
            item = ContentCreateDirectory(
                _normalize_side(operation.side),
                _normalize_path(operation.relative_path),
                _validate_mode(operation.mode),
            )
            target = (item.side, portable_relative_path_key(item.relative_path))
        elif isinstance(operation, ContentDeleteDirectory):
            item = ContentDeleteDirectory(
                _normalize_side(operation.side),
                _normalize_path(operation.relative_path),
            )
            target = (item.side, portable_relative_path_key(item.relative_path))
        elif isinstance(operation, ContentMove):
            item = ContentMove(
                _normalize_side(operation.source_side),
                _normalize_path(operation.source_path),
                _normalize_side(operation.destination_side),
                _normalize_path(operation.destination_path),
            )
            target = (
                item.destination_side,
                portable_relative_path_key(item.destination_path),
            )
            claimed = (
                (
                    item.source_side,
                    portable_relative_path_key(item.source_path),
                ),
                target,
            )
        else:
            raise ContentOperationError(
                f"Unsupported Content operation: {type(operation).__name__}"
            )
        if not isinstance(operation, ContentMove):
            claimed = (target,)
        for claim in claimed:
            if claim in targets:
                raise ContentOperationError(
                    f"Duplicate Content operation target: {claim[0]}/{claim[1]}"
                )
            targets.add(claim)
        normalized.append(item)
    return tuple(normalized)


def _apply_operation(
    staging: Path,
    operation: ContentOperation,
    checkpoint: Callable[[], None],
) -> None:
    try:
        if isinstance(operation, ContentCreateFile):
            write_overlay_bytes(
                staging,
                operation.side,
                operation.relative_path,
                operation.contents,
                mode=operation.mode,
                create=True,
                checkpoint=checkpoint,
            )
        elif isinstance(operation, ContentReplaceFile):
            write_overlay_bytes(
                staging,
                operation.side,
                operation.relative_path,
                operation.contents,
                mode=operation.mode,
                create=False,
                expected_digest=operation.expected_digest,
                checkpoint=checkpoint,
            )
        elif isinstance(operation, ContentDeleteFile):
            delete_overlay_entry(
                staging,
                operation.side,
                operation.relative_path,
                directory=False,
            )
        elif isinstance(operation, ContentCreateDirectory):
            create_overlay_directory(
                staging,
                operation.side,
                operation.relative_path,
                mode=operation.mode,
            )
        elif isinstance(operation, ContentDeleteDirectory):
            delete_overlay_entry(
                staging,
                operation.side,
                operation.relative_path,
                directory=True,
            )
        elif isinstance(operation, ContentMove):
            move_overlay_entry(
                staging,
                operation.source_side,
                operation.source_path,
                operation.destination_side,
                operation.destination_path,
            )
    except (OverlayPolicyError, OSError) as error:
        raise ContentOperationError(str(error)) from error


def _conflicts(snapshot: ContentSnapshot) -> tuple[ContentConflict, ...]:
    conflicts: list[ContentConflict] = []
    by_side: dict[str, dict[str, list[ContentSnapshotEntry]]] = {
        side: {} for side in OVERLAY_TARGETS
    }
    cross_side: dict[str, list[ContentSnapshotEntry]] = {}
    for entry in snapshot.entries:
        key = entry.portable_identity[1]
        by_side[entry.side].setdefault(key, []).append(entry)
        cross_side.setdefault(key, []).append(entry)
    for side, identities in by_side.items():
        for key, entries in identities.items():
            if len(entries) < 2:
                continue
            conflicts.append(
                ContentConflict(
                    "portable_collision",
                    "error",
                    key,
                    tuple((side, entry.relative_path) for entry in entries),
                    f"Portable path collision in {side}: "
                    + ", ".join(str(entry.relative_path) for entry in entries),
                )
            )
    for key, entries in cross_side.items():
        representatives: dict[str, ContentSnapshotEntry] = {}
        for entry in entries:
            representatives.setdefault(entry.side, entry)
        sides = set(representatives)
        pairs = (
            ("common", "client", "common_client_overlap"),
            ("common", "server", "common_server_overlap"),
            ("client", "server", "client_server_divergence"),
        )
        for left, right, kind in pairs:
            if left not in sides or right not in sides:
                continue
            first = representatives[left]
            second = representatives[right]
            if first.kind != second.kind:
                conflicts.append(
                    ContentConflict(
                        "cross_side_type_conflict",
                        "error",
                        key,
                        ((left, first.relative_path), (right, second.relative_path)),
                        f"Cross-side type conflict for {key}: {left} is "
                        f"{first.kind}, {right} is {second.kind}",
                    )
                )
                continue
            if kind == "client_server_divergence" and (
                first.digest,
                first.mode,
                first.kind,
            ) == (second.digest, second.mode, second.kind):
                continue
            conflicts.append(
                ContentConflict(
                    kind,  # type: ignore[arg-type]
                    "warning",
                    key,
                    ((left, first.relative_path), (right, second.relative_path)),
                    (
                        f"{right} overrides {left} in the {right} build for {key}"
                        if left == "common"
                        else f"Client and server content diverge for {key}"
                    ),
                )
            )
    return tuple(conflicts)


def _changes(
    baseline: ContentSnapshot,
    result: ContentSnapshot,
    operations: tuple[ContentOperation, ...],
) -> tuple[ContentChange, ...]:
    before = {
        (entry.side, entry.relative_path): entry for entry in baseline.entries
    }
    after = {(entry.side, entry.relative_path): entry for entry in result.entries}
    changes: list[ContentChange] = []
    handled: set[tuple[str, Path]] = set()
    for operation in operations:
        if not isinstance(operation, ContentMove):
            continue
        source_key = (operation.source_side, operation.source_path)
        destination_key = (operation.destination_side, operation.destination_path)
        source = before.get(source_key)
        destination = after.get(destination_key)
        if source is not None and destination is not None:
            changes.append(
                ContentChange(
                    "moved",
                    operation.destination_side,
                    operation.destination_path,
                    operation.source_side,
                    operation.source_path,
                    source.digest,
                    destination.digest,
                )
            )
            handled.update((source_key, destination_key))
    for key in sorted(
        before.keys() | after.keys(),
        key=lambda item: (_SIDE_ORDER[item[0]], item[1].as_posix()),
    ):
        if key in handled:
            continue
        old = before.get(key)
        new = after.get(key)
        if old is None:
            action = "created"
        elif new is None:
            action = "deleted"
        elif (
            old.kind,
            old.mode,
            old.size,
            old.digest,
        ) != (new.kind, new.mode, new.size, new.digest):
            action = "updated"
        else:
            action = "unchanged"
        changes.append(
            ContentChange(
                action,  # type: ignore[arg-type]
                key[0],
                key[1],
                before_digest=old.digest if old is not None else None,
                after_digest=new.digest if new is not None else None,
            )
        )
    return tuple(changes)


class ContentDiscardOperation:
    def __init__(self, plan: "ContentChangePlan", deadline: float) -> None:
        self.plan = plan
        self.deadline = deadline
        self.done = threading.Event()
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._started = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._execute,
                name=f"huroshiki-content-discard-{self.plan.project_key}",
                daemon=False,
            )
            try:
                self._thread.start()
            except BaseException as error:
                self.error = error
                self.plan.state = "failed"
                self.done.set()
                raise

    def run(self) -> None:
        with self._lock:
            owner = not self._started
            if owner:
                self._started = True
        if owner:
            self._execute()
        else:
            self.done.wait(max(0.0, self.deadline - time.monotonic()))

    def _execute(self) -> None:
        try:
            self.plan._run_discard(self)
        except BaseException as error:
            self.error = error
        finally:
            self.done.set()

    def raise_for_error(self) -> None:
        if not self.done.is_set():
            raise ContentCleanupError("Content plan discard is still running")
        if self.error is not None:
            raise self.error


class ContentChangePlan:
    def __init__(
        self,
        *,
        project_key: str,
        project_root: Path,
        transaction_root: Path,
        project_lock: object,
        baseline_snapshot: ContentSnapshot,
        result_snapshot: ContentSnapshot,
        operations: tuple[ContentOperation, ...],
        changes: tuple[ContentChange, ...],
        conflicts: tuple[ContentConflict, ...],
        state: Literal["ready", "failed"],
    ) -> None:
        self.project_key = project_key
        self.project_root = project_root
        self.transaction_root = transaction_root
        self.staging_content = transaction_root / "staging-content"
        self.retained_original_content = transaction_root / "retained-original-content"
        self.retained_failed_content = transaction_root / "retained-failed-content"
        self.baseline_snapshot = baseline_snapshot
        self.result_snapshot = result_snapshot
        self.operations = operations
        self.changes = changes
        self.conflicts = conflicts
        self.warnings = tuple(
            conflict for conflict in conflicts if conflict.severity == "warning"
        )
        self.state = state
        self.cleanup_error: BaseException | None = None
        self._project_lock = project_lock
        self._transaction_identity = _identity(transaction_root)
        self._staging_identity = _identity(self.staging_content)
        self._lock = threading.RLock()
        self._discard_operation: ContentDiscardOperation | None = None
        self._publication_active = False
        self._publication_committed = False
        self._original_exchange_name: str | None = None

    def _release_project_lock(self) -> None:
        lock = self._project_lock
        if lock is None:
            return
        lock.release()
        self._project_lock = None

    def begin_discard(self, *, deadline: float | None = None) -> ContentDiscardOperation:
        operation_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + CONTENT_DISCARD_TIMEOUT_SECONDS
        )
        remaining = max(0.0, operation_deadline - time.monotonic())
        if not self._lock.acquire(timeout=remaining):
            operation = ContentDiscardOperation(self, operation_deadline)
            operation._started = True
            operation.error = ContentCleanupError(
                "Content plan remained busy until the discard deadline"
            )
            operation.done.set()
            return operation
        try:
            if self.state in {"discarded", "applied"} and self._project_lock is None:
                operation = ContentDiscardOperation(self, operation_deadline)
                operation._started = True
                operation.done.set()
                return operation
            if self._discard_operation is not None and not self._discard_operation.done.is_set():
                self._discard_operation.deadline = min(
                    self._discard_operation.deadline,
                    operation_deadline,
                )
                return self._discard_operation
            operation = ContentDiscardOperation(self, operation_deadline)
            self._discard_operation = operation
            return operation
        finally:
            self._lock.release()

    def retry_discard(self, *, deadline: float | None = None) -> ContentDiscardOperation:
        operation = self.begin_discard(deadline=deadline)
        operation.start()
        return operation

    def _run_discard(self, discard: ContentDiscardOperation) -> None:
        with self._lock:
            if time.monotonic() >= discard.deadline:
                self.state = "failed"
                raise ContentCleanupError("Content plan discard deadline exceeded")
            if self.state == "applying":
                self.state = "failed"
                raise ContentCleanupError("Content plan apply is still running")
            if self._publication_active and not self._publication_committed:
                self._rollback_publication(deadline=discard.deadline)
            if self._publication_active and not self._publication_committed:
                self.state = "failed"
                raise ContentCleanupError("Content publication rollback is incomplete")
            if self.staging_content.exists():
                retained = self.transaction_root / "retained-discarded-content"
                if not retained.exists():
                    self.staging_content.rename(retained)
            marker = self.transaction_root / ".completed"
            marker.touch(exist_ok=True)
            try:
                self._release_project_lock()
            except BaseException as error:
                self.cleanup_error = error
                self.state = "failed"
                raise ContentCleanupError(
                    f"Could not release Content plan lock: {error}"
                ) from error
            self.cleanup_error = None
            self.state = "applied" if self._publication_committed else "discarded"

    def _rollback_publication(self, *, deadline: float | None = None) -> None:
        checkpoint = lambda: _checkpoint(None, deadline)
        checkpoint()
        project_fd = os.open(self.project_root, _DIRECTORY_FLAGS)
        transaction_fd = os.open(self.transaction_root, _DIRECTORY_FLAGS)
        try:
            current = content_snapshot_at(
                self.project_key,
                self.project_root,
                checkpoint=checkpoint,
            )
            if (
                current.project_identity != self.baseline_snapshot.project_identity
                or current.content_parent_identity
                != self.baseline_snapshot.content_parent_identity
            ):
                raise ContentCleanupError(
                    "Project root changed before Content rollback; recovery state retained"
                )
            if _same_baseline(current, self.baseline_snapshot):
                self._publication_active = False
                self._original_exchange_name = None
                return
            if current.digest != self.result_snapshot.digest:
                raise ContentCleanupError(
                    "Published Content changed before rollback; recovery state retained"
                )
            if self.baseline_snapshot.content_identity.exists:
                original_name = self._original_exchange_name
                if original_name is None:
                    raise ContentCleanupError("Original Content recovery tree is missing")
                original_root = self.transaction_root / original_name
                original = content_snapshot_at(
                    self.project_key,
                    self.transaction_root,
                    content_root=original_root,
                    checkpoint=checkpoint,
                )
                if (
                    original.digest != self.baseline_snapshot.digest
                    or original.content_identity
                    != self.baseline_snapshot.content_identity
                ):
                    packctl.renameat2(
                        transaction_fd,
                        original_name,
                        project_fd,
                        "content",
                        packctl.RENAME_EXCHANGE,
                    )
                    restored_external = content_snapshot_at(
                        self.project_key,
                        self.project_root,
                        checkpoint=checkpoint,
                    )
                    if (
                        restored_external.digest != original.digest
                        or restored_external.content_identity
                        != original.content_identity
                    ):
                        raise ContentCleanupError(
                            "External Content restoration verification failed"
                        )
                    self._publication_active = False
                    self._original_exchange_name = None
                    return
                packctl.renameat2(
                    transaction_fd,
                    original_name,
                    project_fd,
                    "content",
                    packctl.RENAME_EXCHANGE,
                )
                checkpoint()
                failed_name = "retained-failed-content"
                packctl.renameat2(
                    transaction_fd,
                    original_name,
                    transaction_fd,
                    failed_name,
                    packctl.RENAME_NOREPLACE,
                )
                checkpoint()
            else:
                packctl.renameat2(
                    project_fd,
                    "content",
                    transaction_fd,
                    "retained-failed-content",
                    packctl.RENAME_NOREPLACE,
                )
                checkpoint()
            restored = content_snapshot_at(
                self.project_key,
                self.project_root,
                checkpoint=checkpoint,
            )
            if not _same_baseline(restored, self.baseline_snapshot):
                raise ContentCleanupError("Content rollback verification failed")
            self._publication_active = False
            self._original_exchange_name = None
        finally:
            os.close(transaction_fd)
            os.close(project_fd)


def _same_baseline(current: ContentSnapshot, expected: ContentSnapshot) -> bool:
    return (
        current.project_key == expected.project_key
        and current.digest == expected.digest
        and current.project_identity == expected.project_identity
        and current.content_parent_identity == expected.content_parent_identity
        and current.content_identity == expected.content_identity
    )


def _write_plan_file(plan: ContentChangePlan) -> None:
    payload = {
        "project_key": plan.project_key,
        "state": plan.state,
        "baseline_digest": plan.baseline_snapshot.digest,
        "result_digest": plan.result_snapshot.digest,
        "operations": [type(operation).__name__ for operation in plan.operations],
        "conflicts": [
            {
                "kind": conflict.kind,
                "severity": conflict.severity,
                "portable_path": conflict.portable_path,
                "message": conflict.message,
            }
            for conflict in plan.conflicts
        ],
    }
    (plan.transaction_root / "plan.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def plan_content_changes_at(
    project_key: str,
    project_root: Path,
    transaction_parent: Path,
    operations: tuple[ContentOperation, ...],
    *,
    expected_snapshot: ContentSnapshot | None = None,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> ContentChangePlan:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + CONTENT_OPERATION_TIMEOUT_SECONDS
    )
    _checkpoint(cancel_event, effective_deadline)
    try:
        project_lock = packctl.ProjectLock(
            project_key,
            "content transaction",
        ).acquire()
    except packctl.ConfigError as error:
        raise ContentOperationError(str(error)) from error
    transaction_root: Path | None = None
    try:
        packctl.make_state_directory(
            transaction_parent,
            state_root=transaction_parent.parent,
            repository_root=transaction_parent.parent.parent,
        )
        transaction_root = Path(
            tempfile.mkdtemp(
                prefix=f"{project_key.replace(':', '-')}-",
                dir=transaction_parent,
            )
        )
        checkpoint = lambda: _checkpoint(cancel_event, effective_deadline)
        baseline = content_snapshot_at(
            project_key,
            project_root,
            checkpoint=checkpoint,
        )
        if expected_snapshot is not None and not _same_baseline(
            baseline,
            expected_snapshot,
        ):
            raise ContentPlanStale(
                "Content changed after snapshot; create a new plan"
            )
        if any(entry.kind == "invalid" or entry.errors for entry in baseline.entries):
            raise ContentOperationError(
                "Content overlay contains unsafe or invalid entries"
            )
        staging = transaction_root / "staging-content"
        copy_content_tree(
            project_root / "content",
            staging,
            checkpoint=checkpoint,
        )
        after_copy = content_snapshot_at(
            project_key,
            project_root,
            checkpoint=checkpoint,
        )
        if not _same_baseline(after_copy, baseline):
            raise ContentPlanStale(
                "Content changed while planning; create a new plan"
            )
        normalized = _normalize_operations(tuple(operations))
        for operation in normalized:
            checkpoint()
            _apply_operation(staging, operation, checkpoint)
        checkpoint()
        staging_scan = scan_content_overlays(staging)
        if staging_scan.issues:
            details = "; ".join(
                f"{issue.relative_path}: {issue.message}"
                for issue in staging_scan.issues
            )
            raise ContentOperationError(
                f"Staged Content overlay is invalid: {details}"
            )
        result = content_snapshot_at(
            project_key,
            transaction_root,
            content_root=staging,
            checkpoint=checkpoint,
        )
        conflicts = _conflicts(result)
        changes = _changes(baseline, result, normalized)
        plan = ContentChangePlan(
            project_key=project_key,
            project_root=project_root,
            transaction_root=transaction_root,
            project_lock=project_lock,
            baseline_snapshot=baseline,
            result_snapshot=result,
            operations=normalized,
            changes=changes,
            conflicts=conflicts,
            state=(
                "failed"
                if any(conflict.severity == "error" for conflict in conflicts)
                else "ready"
            ),
        )
        _write_plan_file(plan)
        return plan
    except BaseException:
        if transaction_root is not None:
            try:
                (transaction_root / ".planning-failed").touch()
            except OSError:
                pass
        project_lock.release()
        raise


def apply_content_changes(
    plan: ContentChangePlan,
    *,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + CONTENT_OPERATION_TIMEOUT_SECONDS
    )
    with plan._lock:
        if plan.state != "ready":
            raise ContentOperationError(
                f"Content plan cannot be applied from state {plan.state}"
            )
        plan.state = "applying"
        try:
            checkpoint = lambda: _checkpoint(cancel_event, effective_deadline)
            _checkpoint(cancel_event, effective_deadline)
            if _identity(plan.transaction_root) != plan._transaction_identity:
                raise ContentPlanStale("Content transaction root was replaced")
            if _identity(plan.staging_content) != plan._staging_identity:
                raise ContentPlanStale("Content staging root was replaced")
            current = content_snapshot_at(
                plan.project_key,
                plan.project_root,
                checkpoint=checkpoint,
            )
            if not _same_baseline(current, plan.baseline_snapshot):
                raise ContentPlanStale(
                    "Content changed after planning; create a new plan"
                )
            staged = content_snapshot_at(
                plan.project_key,
                plan.transaction_root,
                content_root=plan.staging_content,
                checkpoint=checkpoint,
            )
            if staged.digest != plan.result_snapshot.digest:
                raise ContentPlanStale("Staged Content changed after planning")
            if any(entry.kind == "invalid" or entry.errors for entry in staged.entries):
                raise ContentPlanStale("Staged Content overlay is invalid")
            _checkpoint(cancel_event, effective_deadline)
            project_fd = os.open(plan.project_root, _DIRECTORY_FLAGS)
            transaction_fd = os.open(plan.transaction_root, _DIRECTORY_FLAGS)
            try:
                opened_project = os.fstat(project_fd)
                opened_transaction = os.fstat(transaction_fd)
                if (
                    opened_project.st_dev,
                    opened_project.st_ino,
                ) != (
                    plan.baseline_snapshot.project_identity.device,
                    plan.baseline_snapshot.project_identity.inode,
                ):
                    raise ContentPlanStale("Project root was replaced before publication")
                if (
                    opened_transaction.st_dev,
                    opened_transaction.st_ino,
                ) != (
                    plan._transaction_identity.device,
                    plan._transaction_identity.inode,
                ):
                    raise ContentPlanStale(
                        "Content transaction root was replaced before publication"
                    )
                if plan.baseline_snapshot.content_identity.exists:
                    plan._publication_active = True
                    plan._original_exchange_name = "staging-content"
                    packctl.renameat2(
                        transaction_fd,
                        "staging-content",
                        project_fd,
                        "content",
                        packctl.RENAME_EXCHANGE,
                    )
                else:
                    plan._publication_active = True
                    plan._original_exchange_name = None
                    packctl.renameat2(
                        transaction_fd,
                        "staging-content",
                        project_fd,
                        "content",
                        packctl.RENAME_NOREPLACE,
                    )
                published = content_snapshot_at(
                    plan.project_key,
                    plan.project_root,
                    checkpoint=checkpoint,
                )
                if (
                    published.digest != plan.result_snapshot.digest
                    or published.content_identity != plan._staging_identity
                    or published.project_identity
                    != plan.baseline_snapshot.project_identity
                ):
                    raise ContentOperationError(
                        "Published Content verification failed"
                    )
                if plan.baseline_snapshot.content_identity.exists:
                    exchanged = content_snapshot_at(
                        plan.project_key,
                        plan.transaction_root,
                        content_root=plan.staging_content,
                        checkpoint=checkpoint,
                    )
                    if (
                        exchanged.digest != plan.baseline_snapshot.digest
                        or exchanged.content_identity
                        != plan.baseline_snapshot.content_identity
                    ):
                        raise ContentOperationError(
                            "Original Content verification failed after exchange"
                        )
                    packctl.renameat2(
                        transaction_fd,
                        "staging-content",
                        transaction_fd,
                        "retained-original-content",
                        packctl.RENAME_NOREPLACE,
                    )
                    plan._original_exchange_name = "retained-original-content"
                _checkpoint(cancel_event, effective_deadline)
                (plan.transaction_root / ".completed").touch(exist_ok=False)
                plan._publication_committed = True
            finally:
                os.close(transaction_fd)
                os.close(project_fd)
            try:
                plan._release_project_lock()
            except BaseException as error:
                plan.cleanup_error = error
                raise ContentCleanupError(
                    f"Content was published but lock release failed: {error}"
                ) from error
            plan.cleanup_error = None
            plan.state = "applied"
        except BaseException as error:
            if plan._publication_active and not plan._publication_committed:
                try:
                    plan._rollback_publication(
                        deadline=(
                            time.monotonic() + CONTENT_CLEANUP_TIMEOUT_SECONDS
                        )
                    )
                except BaseException as rollback_error:
                    plan.cleanup_error = rollback_error
            plan.state = "failed"
            try:
                (plan.transaction_root / ".apply-failed").write_text(
                    f"{error}\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            if plan.cleanup_error is not None:
                raise ContentCleanupError(
                    f"{error}; Content rollback/cleanup failed: {plan.cleanup_error}"
                ) from error
            raise


def discard_content_plan(
    plan: ContentChangePlan,
    *,
    deadline: float | None = None,
) -> None:
    operation = plan.begin_discard(deadline=deadline)
    operation.run()
    operation.raise_for_error()
