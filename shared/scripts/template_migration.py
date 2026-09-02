"""Transactional, Pack-independent copying of Template recipes."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal
from uuid import uuid4

import yaml

import packctl
from url_diagnostics import redact_url

__all__ = [
    "TemplateMigrationError", "TemplateMigrationOperationError", "TemplateMigrationPlanningError", "TemplateMigrationUnresolved",
    "TemplateMigrationTarget", "TemplateRootIntent", "TemplateExactConstraint",
    "TemplateUrlEvidence", "TemplateArtifactFact", "TemplateRootResolutionFact",
    "TemplateVersionIntentFact", "TemplateVersionIntentIssue", "TemplateCollisionFact",
    "TemplateResolvedRoot", "TemplateUnresolvedRoot",
    "TemplateResolutionResult", "TemplateMigrationSourceSnapshot", "TemplateMigrationPlan",
    "TemplateMigrationPublication", "snapshot_template_migration_source_at",
    "plan_template_copy_migration_at", "resolve_template_migration_plan_at",
    "resolve_template_migration_conflicts_at",
    "prepare_template_migration_publication", "apply_template_migration_publication",
    "retry_template_migration_cleanup", "discard_template_migration_plan",
]

_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_NOREPLACE = 1


class TemplateMigrationError(RuntimeError): pass
class TemplateMigrationOperationError(TemplateMigrationError): pass
class TemplateMigrationPlanningError(TemplateMigrationError):
    def __init__(self, message: str, plan: "TemplateMigrationPlan"):
        self.plan = plan; super().__init__(message)
class TemplateMigrationUnresolved(TemplateMigrationError):
    def __init__(self, roots: tuple["TemplateUnresolvedRoot", ...]):
        self.roots = roots
        super().__init__("template migration has unresolved roots")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict): return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list): return tuple(_freeze(v) for v in value)
    return value


def _json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type): return _json(asdict(value))
    if isinstance(value, MappingProxyType): return {k: _json(v) for k, v in value.items()}
    if isinstance(value, dict): return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, Path): return value.as_posix()
    if isinstance(value, (tuple, list)): return [_json(v) for v in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _check(event: threading.Event, deadline: float) -> None:
    if event.is_set(): raise TemplateMigrationOperationError("template migration cancelled")
    if time.monotonic() >= deadline: raise TemplateMigrationOperationError("template migration deadline exceeded")


@dataclass(frozen=True)
class TemplateMigrationTarget:
    target_id: str; display_name: str; minecraft_version: str; loader: str
    reference_loader_version: str; mode: Literal["copy"] = "copy"
    def __post_init__(self) -> None:
        try: packctl.validate_project_id(self.target_id); packctl.validate_project_text("display_name", self.display_name)
        except Exception as e: raise TemplateMigrationError(str(e)) from e
        if not all(isinstance(x, str) and x.strip() for x in (self.minecraft_version, self.loader, self.reference_loader_version)):
            raise TemplateMigrationError("target versions and loader must be non-empty strings")
        if self.loader.strip().lower() not in {"fabric", "quilt", "forge", "neoforge"} or self.mode != "copy":
            raise TemplateMigrationError("invalid Template migration target")
        object.__setattr__(self, "target_id", self.target_id.strip()); object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "minecraft_version", self.minecraft_version.strip()); object.__setattr__(self, "loader", self.loader.strip().lower()); object.__setattr__(self, "reference_loader_version", self.reference_loader_version.strip())


@dataclass(frozen=True)
class TemplateRootIntent:
    source_index: int; name: str; provider: str; project_id: str; side: str; url: str | None = None


@dataclass(frozen=True)
class TemplateExactConstraint:
    provider: str; project_id: str; artifact_id: str; scope: str


@dataclass(frozen=True)
class TemplateUrlEvidence:
    url: str; status: Literal["compatible", "incompatible", "unknown"]
    loader_status: Literal["compatible", "incompatible", "unknown"]
    minecraft_status: Literal["compatible", "incompatible", "unknown"]
    detected_loaders: tuple[str, ...]; detected_minecraft_versions: tuple[str, ...]
    effective_max_size_bytes: int; effective_allow_private_networks: bool; detail: str


@dataclass(frozen=True)
class TemplateArtifactFact:
    canonical_identity: str; provider: str; project_id: str
    artifact_id: str | None; version: str | None; metadata_path: Path
    filename: str; metadata_digest: str


@dataclass(frozen=True)
class TemplateRootResolutionFact:
    source_index: int; name: str; provider: str; source_selector: str
    source_canonical_identity: str; target_canonical_identity: str; side: str
    source_artifact: TemplateArtifactFact; target_artifact: TemplateArtifactFact
    classification: Literal["unchanged", "updated"]


@dataclass(frozen=True)
class TemplateVersionIntentFact:
    provider: str; project_id: str; artifact_id: str; scope: str
    satisfied: bool; owner_source_indices: tuple[int, ...]


@dataclass(frozen=True)
class TemplateVersionIntentIssue:
    provider: str; project_id: str; artifact_id: str; scope: str
    reason_code: Literal["version-intent-blocked"]; detail: str
    owner_source_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class TemplateCollisionFact:
    reason_code: Literal["identity-collision", "path-collision", "filename-collision"]
    source_indices: tuple[int, ...]; canonical_identities: tuple[str, ...]
    metadata_paths: tuple[Path, ...]; filenames: tuple[str, ...]; detail: str


@dataclass(frozen=True)
class TemplateResolvedRoot:
    source_index: int; provider: str; project_id: str; side: str; metadata_digest: str
    artifact_id: str | None = None; closure_identities: tuple[str, ...] = ()
    classification: Literal["unchanged", "updated"] = "updated"
    url_evidence: TemplateUrlEvidence | None = None


@dataclass(frozen=True)
class TemplateUnresolvedRoot:
    source_index: int; source_selector: str; canonical_identity: str | None; code: str; detail: str
    retry: bool = False; replacement_supported: bool = True; version_issue: str | None = None


@dataclass(frozen=True)
class TemplateResolutionResult:
    status: Literal["resolved", "resolution-required"]
    source_snapshot_digest: str; target: TemplateMigrationTarget
    resolved: tuple[TemplateResolvedRoot, ...]; unresolved: tuple[TemplateUnresolvedRoot, ...]
    source_minecraft_version: str; source_loader: str; source_reference_loader_version: str
    ordered_roots: tuple[TemplateRootIntent, ...]
    ordered_root_facts: tuple[TemplateRootResolutionFact, ...]
    version_intent_facts: tuple[TemplateVersionIntentFact, ...]
    version_intent_issues: tuple[TemplateVersionIntentIssue, ...]
    collisions: tuple[TemplateCollisionFact, ...]
    identity_collisions: tuple[TemplateCollisionFact, ...]
    path_collisions: tuple[TemplateCollisionFact, ...]
    filename_collisions: tuple[TemplateCollisionFact, ...]
    url_evidence: tuple[TemplateUrlEvidence, ...]; warnings: tuple[str, ...]
    removed_roots: tuple[object, ...]; replaced_roots: tuple[object, ...]
    resolution_attempt: int; staging_digest: str | None; digest: str


@dataclass(frozen=True)
class TemplateMigrationSourceSnapshot:
    source_id: str; project_identity: tuple[int, int]; target: TemplateMigrationTarget; enabled: bool; roots: tuple[TemplateRootIntent, ...]
    overrides: tuple[TemplateExactConstraint, ...]; committed_bytes: bytes; local_bytes: bytes | None
    committed_identity: tuple[int, int, int, int]; local_identity: tuple[int, int, int, int] | None
    committed_digest: str; local_digest: str | None; tree_digest: str; snapshot_digest: str; url_policy: MappingProxyType
    committed_url_max_jar_size_bytes: int | None
    cancel_event: threading.Event = field(repr=False, compare=False); operation_deadline: float = field(repr=False, compare=False)


class _State:
    __slots__ = ("event", "deadline", "snapshot", "target", "locks", "tx", "detached", "staging", "plan_digest", "resolution", "result_digest", "committed", "publication_state", "tx_identity", "tx_parent_identity", "detached_identity", "staging_identity", "target_parent_identity", "publication_token", "cleanup_error", "attempt", "process_results", "effective_roots", "effective_overrides", "removed_roots", "replaced_roots", "pending_replacements", "consumed_resolution_requests", "transaction_cleaned", "transaction_removal_pending")
    def __init__(self, event, deadline, snapshot, target, locks, tx, detached, staging, plan_digest):
        self.event, self.deadline, self.snapshot, self.target, self.locks, self.tx, self.detached, self.staging, self.plan_digest = event, deadline, snapshot, target, locks, tx, detached, staging, plan_digest
        self.resolution = None; self.result_digest = None; self.committed = False; self.publication_state = "not-published"
        self.tx_identity = _identity(tx) if tx is not None else None
        self.tx_parent_identity = _identity(tx.parent) if tx is not None else None
        self.detached_identity = _identity(detached) if detached is not None else None
        self.staging_identity = _identity(staging) if staging is not None else None
        try: self.target_parent_identity = _identity(packctl.get_template_root(target.target_id, must_exist=False).parent)
        except BaseException: self.target_parent_identity = None
        self.publication_token = None; self.cleanup_error = None; self.attempt = 0; self.process_results = []
        self.effective_roots = tuple(snapshot.roots); self.effective_overrides = tuple(snapshot.overrides)
        self.removed_roots = (); self.replaced_roots = (); self.pending_replacements = (); self.consumed_resolution_requests = set()
        self.transaction_cleaned = False
        self.transaction_removal_pending = False


@dataclass(frozen=True)
class TemplateMigrationPlan:
    source_id: str; target: TemplateMigrationTarget; source_snapshot_digest: str; plan_digest: str; roots: tuple[TemplateRootIntent, ...]
    _state: _State = field(repr=False, compare=False)
    @property
    def cancel_event(self) -> threading.Event: return self._state.event
    @property
    def deadline(self) -> float: return self._state.deadline
    @property
    def resolution(self) -> TemplateResolutionResult | None: return self._state.resolution
    @property
    def publication_lifecycle(self) -> Literal["precommit", "committed", "uncertain"]:
        if self._state.publication_state == "uncertain": return "uncertain"
        if self._state.committed: return "committed"
        return "precommit"
    @property
    def cleanup_pending(self) -> bool: return self._state.cleanup_error is not None


class TemplateMigrationPublication:
    __slots__ = ("_plan", "_state", "_secret", "_token", "_used")
    def __init__(self, plan, token, secret):
        if secret is not _PUBLICATION_SECRET: raise TypeError("opaque handoff")
        self._plan, self._state, self._token, self._secret, self._used = plan, plan._state, token, secret, False


_PUBLICATION_SECRET = object()


def _identity(path: Path) -> tuple[int, int]:
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode): raise TemplateMigrationOperationError("migration directory is unsafe")
    return value.st_dev, value.st_ino


def _target_missing(path: Path) -> bool:
    descriptor = os.open(path.parent, _DIR)
    try:
        try: os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError: return True
        return False
    finally: os.close(descriptor)


def _tree_digest(root: Path) -> str:
    if _identity(root) is None: raise TemplateMigrationOperationError("migration tree is unsafe")
    records = []
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda value: value.name):
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode): raise TemplateMigrationOperationError("migration tree contains an unsafe entry")
            contents, _ = _file(root, entry.name)
            records.append((entry.name, stat.S_IMODE(metadata.st_mode), hashlib.sha256(contents or b"").hexdigest()))
    return _digest(records)


def _write_new_regular(root: Path, name: str, contents: bytes, *, mode: int = 0o644) -> None:
    root_fd = os.open(root, _DIR); descriptor = -1
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=root_fd)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0: raise TemplateMigrationOperationError("short write while staging Template migration")
            view = view[written:]
        os.fsync(descriptor); metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode): raise TemplateMigrationOperationError("staged Template manifest is unsafe")
        os.fsync(root_fd)
    finally:
        if descriptor >= 0: os.close(descriptor)
        os.close(root_fd)


def _file(root: Path, name: str) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    root_fd = -1; fd = -1
    try:
        root_fd = os.open(root, _DIR); fd = os.open(name, _FILE, dir_fd=root_fd); st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode): raise TemplateMigrationError("unsafe manifest entry")
        chunks = []; total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, 2 * 1024 * 1024 + 1 - total))
            if not chunk: break
            chunks.append(chunk); total += len(chunk)
            if total > 2 * 1024 * 1024: raise TemplateMigrationError("Template manifest is too large")
        data = b"".join(chunks)
        final = os.fstat(fd)
        if (final.st_dev, final.st_ino, final.st_mode, final.st_size) != (st.st_dev, st.st_ino, st.st_mode, st.st_size):
            raise TemplateMigrationOperationError("Template manifest changed while snapshotting")
        return data, (st.st_dev, st.st_ino, st.st_mode, st.st_size)
    except FileNotFoundError: return None, None
    except OSError as error: raise TemplateMigrationError(f"unsafe Template manifest entry: {name}") from error
    finally:
        if fd >= 0: os.close(fd)
        if root_fd >= 0: os.close(root_fd)


def snapshot_template_migration_source_at(source_id: str, root: Path | None = None, *, cancel_event: threading.Event | None = None, deadline: float | None = None) -> TemplateMigrationSourceSnapshot:
    event = cancel_event or threading.Event(); effective_deadline = deadline if deadline is not None else time.monotonic() + 600
    _check(event, effective_deadline)
    root = root or packctl.get_template_root(source_id)
    root_identity = _identity(root)
    root_fd = os.open(root, _DIR)
    try: names = {entry.name for entry in os.scandir(root_fd)}
    finally: os.close(root_fd)
    if names - {"template.yaml", "template.local.yaml"}: raise TemplateMigrationError("Template contains unsupported entries")
    committed, ci = _file(root, "template.yaml"); _check(event, effective_deadline); local, li = _file(root, "template.local.yaml")
    if committed is None or ci is None: raise TemplateMigrationError("template.yaml is missing")
    try: c = yaml.safe_load(committed) or {}; l = yaml.safe_load(local) if local is not None else {}
    except yaml.YAMLError as e: raise TemplateMigrationError("invalid Template YAML") from e
    if not isinstance(c, dict) or not isinstance(l or {}, dict): raise TemplateMigrationError("Template YAML must be mappings")
    try: effective = packctl.prospective_template_config(source_id, c, l or {})
    except Exception as e: raise TemplateMigrationError(str(e)) from e
    roots = []
    for i, raw in enumerate(effective.get("mods", [])):
        if not isinstance(raw, dict): raise TemplateMigrationError("invalid Template root")
        roots.append(TemplateRootIntent(i, str(raw.get("name", raw.get("project_id", ""))), str(raw.get("provider", "")), str(raw.get("project_id", "")), str(raw.get("side", "")), raw.get("url")))
    constraints = []
    for raw in effective.get("mod_version_overrides", []):
        if isinstance(raw, dict): constraints.append(TemplateExactConstraint(str(raw.get("provider", "")), str(raw.get("project_id", "")), str(raw.get("artifact_id", "")), str(raw.get("scope", ""))))
    target = TemplateMigrationTarget(source_id, str(effective.get("display_name", source_id)), str(effective.get("minecraft", "")), str(effective.get("loader", "")), str(effective.get("reference_loader_version", "")))
    policy = MappingProxyType({k: effective[k] for k in ("url_max_jar_size_bytes", "url_allow_private_networks") if k in effective})
    committed_digest = hashlib.sha256(committed).hexdigest(); local_digest = hashlib.sha256(local).hexdigest() if local is not None else None
    tree_records = [("template.yaml", stat.S_IMODE(ci[2]), committed_digest)]
    if li is not None: tree_records.append(("template.local.yaml", stat.S_IMODE(li[2]), local_digest))
    tree_digest = _digest(tree_records)
    committed_url_limit = c.get("url_max_jar_size_bytes") if isinstance(c.get("url_max_jar_size_bytes"), int) and not isinstance(c.get("url_max_jar_size_bytes"), bool) else None
    semantic = (target, bool(effective.get("enabled", True)), tuple(roots), tuple(constraints), policy, committed_url_limit, tree_digest)
    if _identity(root) != root_identity: raise TemplateMigrationOperationError("Template directory changed while snapshotting")
    return TemplateMigrationSourceSnapshot(source_id, root_identity, target, bool(effective.get("enabled", True)), tuple(roots), tuple(constraints), committed, local, ci, li, committed_digest, local_digest, tree_digest, _digest(semantic), policy, committed_url_limit, event, effective_deadline)


def plan_template_copy_migration_at(source_id: str, target: TemplateMigrationTarget, *, root: Path | None = None, expected_snapshot: TemplateMigrationSourceSnapshot | None = None, deadline: float, cancel_event: threading.Event | None = None, progress: Callable[[str], None] | None = None) -> TemplateMigrationPlan:
    event = cancel_event or (expected_snapshot.cancel_event if expected_snapshot is not None else threading.Event()); _check(event, deadline)
    if expected_snapshot is not None and (event is not expected_snapshot.cancel_event or deadline != expected_snapshot.operation_deadline): raise TemplateMigrationOperationError("Snapshot, plan, and resolution must use one Event and deadline")
    if source_id == target.target_id: raise TemplateMigrationError("Source and target Template IDs must be different")
    source_root = root or packctl.get_template_root(source_id)
    preliminary = expected_snapshot or snapshot_template_migration_source_at(source_id, source_root, cancel_event=event, deadline=deadline)
    plan_digest = _digest((source_id, target, preliminary.snapshot_digest))
    try:
        locks = packctl.acquire_project_locks((f"template:{source_id}", f"template:{target.target_id}"), deadline=deadline, cancel_event=event, operation="Template copy migration")
    except BaseException as acquisition_error:
        retained = getattr(acquisition_error, "lock_set", None)
        if retained is None or not getattr(retained, "owned", False):
            raise
        state = _State(event, deadline, preliminary, target, retained, None, None, None, plan_digest)
        plan = TemplateMigrationPlan(source_id, target, preliminary.snapshot_digest, plan_digest, preliminary.roots, state)
        state.cleanup_error = acquisition_error
        raise TemplateMigrationPlanningError(str(acquisition_error), plan) from acquisition_error
    state = _State(event, deadline, preliminary, target, locks, None, None, None, plan_digest)
    plan = TemplateMigrationPlan(source_id, target, preliminary.snapshot_digest, plan_digest, preliminary.roots, state)
    try:
        if not _target_missing(packctl.get_template_root(target.target_id, must_exist=False)): raise TemplateMigrationError("target Template already exists")
        snapshot = snapshot_template_migration_source_at(source_id, source_root, cancel_event=event, deadline=deadline)
        if preliminary.source_id != source_id or preliminary.project_identity != snapshot.project_identity or preliminary.tree_digest != snapshot.tree_digest or preliminary.snapshot_digest != snapshot.snapshot_digest:
            raise TemplateMigrationOperationError("Source Template changed after migration snapshot")
        repository = packctl.get_template_root(source_id).parent.parent
        state_root = packctl.make_state_directory(repository / ".huroshiki", repository_root=repository)
        tx = packctl.make_state_directory(state_root / "transactions" / ("template-copy-" + uuid4().hex), state_root=state_root, repository_root=repository)
        detached, staging = tx / "detached", tx / "staging"; detached.mkdir(mode=0o700); staging.mkdir(mode=0o700)
        state.snapshot = snapshot; state.tx = tx; state.detached = detached; state.staging = staging
        state.tx_identity = _identity(tx); state.detached_identity = _identity(detached); state.staging_identity = _identity(staging)
        state.tx_parent_identity = _identity(tx.parent)
        _write_new_regular(detached, "template.yaml", snapshot.committed_bytes, mode=stat.S_IMODE(snapshot.committed_identity[2]))
        if snapshot.local_bytes is not None:
            _write_new_regular(detached, "template.local.yaml", snapshot.local_bytes, mode=stat.S_IMODE(snapshot.local_identity[2]))
        if progress: progress("snapshot complete")
        return plan
    except BaseException as original:
        try: _cleanup(plan._state, min(deadline, time.monotonic() + 10))
        except BaseException as cleanup_error:
            plan._state.cleanup_error = cleanup_error
            raise TemplateMigrationPlanningError(f"{original}; Template migration cleanup failed: {cleanup_error}", plan) from original
        raise


def _resolver_integrity(error: BaseException) -> bool:
    text = str(error).lower()
    return any(x in text for x in ("cancel", "deadline", "timed out", "termination", "orphan", "background process", "protocol", "invalid json")) or any(getattr(error, x, False) for x in ("termination_incomplete", "orphaned_descendants"))


def _record_process(state: _State, result: object) -> None:
    state.process_results.append(result)
    if getattr(result, "termination_incomplete", False) or getattr(result, "orphaned_descendants", False):
        raise TemplateMigrationOperationError("resolver process termination integrity failed")


def _metadata_identity(item: object) -> object:
    from provider_identity import parse_provider_metadata
    return parse_provider_metadata(getattr(item, "relative_path"), getattr(item, "contents"))


def _url_evidence(root: TemplateRootIntent, closure: object, state: _State) -> TemplateUrlEvidence:
    import tomllib
    records = [item for item in getattr(closure, "metadata", ()) if getattr(item, "identity", None) == getattr(closure, "root_identity", None)]
    loaders: tuple[str, ...] = (); versions: tuple[str, ...] = ()
    if len(records) == 1:
        try:
            document = tomllib.loads(records[0].contents.decode("utf-8")); huroshiki = document.get("huroshiki", {})
            if isinstance(huroshiki, dict):
                raw_loaders = huroshiki.get("loaders", []); raw_versions = huroshiki.get("minecraft-versions", [])
                if isinstance(raw_loaders, list): loaders = tuple(sorted(str(value) for value in raw_loaders))
                if isinstance(raw_versions, list): versions = tuple(sorted(str(value) for value in raw_versions))
        except (UnicodeError, ValueError):
            pass
    loader_status: Literal["compatible", "incompatible", "unknown"] = "compatible" if state.target.loader in loaders else "incompatible" if loaders else "unknown"
    exact = {value[1:-1] if value.startswith("[") and value.endswith("]") else value for value in versions if value == "*" or value.replace(".", "").isdigit() or (value.startswith("[") and value.endswith("]") and value[1:-1].replace(".", "").isdigit())}
    minecraft_status: Literal["compatible", "incompatible", "unknown"] = "compatible" if state.target.minecraft_version in exact or "*" in exact else "incompatible" if versions and len(exact) == len(versions) else "unknown"
    status: Literal["compatible", "incompatible", "unknown"] = "incompatible" if "incompatible" in {loader_status, minecraft_status} else "compatible" if loader_status == minecraft_status == "compatible" else "unknown"
    policy = state.snapshot.url_policy
    return TemplateUrlEvidence(root.url or "", status, loader_status, minecraft_status, loaders, versions, int(policy.get("url_max_jar_size_bytes", 256 * 1024 * 1024)), bool(policy.get("url_allow_private_networks", False)), "URL artifact compatibility is verified" if status == "compatible" else "URL artifact compatibility requires resolution")


def _unresolved(
    root: TemplateRootIntent,
    code: str,
    detail: str,
    *,
    canonical_identity: str | None = None,
    retry: bool = True,
    version: str | None = None,
    replacement_supported: bool | None = None,
) -> TemplateUnresolvedRoot:
    if replacement_supported is None:
        replacement_supported = root.provider != "url" and code != "version-intent-blocked"
    return TemplateUnresolvedRoot(
        root.source_index,
        root.project_id,
        canonical_identity,
        code,
        detail[:240],
        retry,
        replacement_supported,
        version,
    )


def _root_artifact_fact(closure: object) -> tuple[TemplateArtifactFact, tuple[str, ...]]:
    records = [_metadata_identity(item) for item in getattr(closure, "metadata", ())]
    root_identity = tuple(getattr(closure, "root_identity", ()))
    roots = [item for item in records if (item.provider, item.project_id) == root_identity]
    if len(roots) != 1:
        raise TemplateMigrationError("resolver did not return exactly one canonical root")
    root = roots[0]
    metadata_item = next(item for item in getattr(closure, "metadata", ()) if getattr(item, "identity", None) == root_identity)
    fact = TemplateArtifactFact(
        f"{root.provider}:{root.project_id}", root.provider, root.project_id,
        root.file_id, root.version, root.metadata_path, root.filename,
        hashlib.sha256(metadata_item.contents).hexdigest(),
    )
    identities = tuple(sorted(f"{item.provider}:{item.project_id}" for item in records))
    return fact, identities


def _collision_reason(error: BaseException) -> Literal["identity-collision", "path-collision", "filename-collision"] | None:
    detail = str(error).lower()
    if "filename collision" in detail: return "filename-collision"
    if "path collision" in detail or "metadata path" in detail: return "path-collision"
    if "identity" in detail or "equivalent" in detail or "equivalence" in detail or "collision" in detail: return "identity-collision"
    return None


def resolve_template_migration_plan_at(plan: TemplateMigrationPlan, *, cancel_event: threading.Event | None = None, deadline: float | None = None, progress: Callable[[str], None] | None = None) -> TemplateResolutionResult:
    state = plan._state
    if (cancel_event is not None and cancel_event is not state.event) or (deadline is not None and deadline != state.deadline): raise TemplateMigrationOperationError("Event/deadline does not belong to plan")
    _check(state.event, state.deadline)
    if state.tx is None or state.staging is None:
        raise TemplateMigrationOperationError("Template migration planning is incomplete")
    if _identity(state.tx) != state.tx_identity or _identity(state.detached) != state.detached_identity or _identity(state.staging) != state.staging_identity:
        raise TemplateMigrationOperationError("Template migration staging identity changed before resolution")
    if state.committed or state.publication_state == "uncertain":
        raise TemplateMigrationOperationError("Template migration can no longer be resolved")
    staged_manifest = state.staging / "template.yaml"
    try:
        metadata = staged_manifest.stat(follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise TemplateMigrationOperationError("Template migration staging is unsafe")
        staged_manifest.unlink()
    state.result_digest = None; state.publication_token = None
    import huroshiki_core as core
    from template_migration_conflicts import TemplateMigrationConflictResolutionError
    state.attempt += 1
    attempt_root = state.tx / f"resolver-attempt-{state.attempt:04d}"; attempt_root.mkdir(mode=0o700)
    (attempt_root / "roots").mkdir(mode=0o700); (attempt_root / "equivalence").mkdir(mode=0o700)
    unresolved: list[TemplateUnresolvedRoot] = []; url_facts: list[TemplateUrlEvidence] = []
    collisions: list[TemplateCollisionFact] = []
    provisional: dict[int, dict[str, object]] = {}
    effective_roots = tuple(state.effective_roots)
    effective_overrides = tuple(state.effective_overrides)
    replaced_source_indices = {item.source_root.source_index for item in state.replaced_roots}
    replacement_baselines = {item.source_root.source_index: item.source_root for item in state.replaced_roots}
    pending_replacements = {item.source_root.source_index: item for item in state.pending_replacements}
    root_constraints = {(value.provider, value.project_id): value for value in effective_overrides if value.scope == "root"}
    dependency_constraints = tuple(value for value in effective_overrides if value.scope == "dependency")

    def resolve_runtime(root: TemplateRootIntent, provider: str, project: str, exact: TemplateExactConstraint | None, *, phase: str, runtime: TemplateMigrationTarget) -> object:
        workspace = attempt_root / "roots" / f"{phase}-{root.source_index}"
        if exact is not None:
            selection = core.exact_mod_artifact_selection(exact.provider, exact.project_id, exact.artifact_id)
            core.create_resolver_source(workspace, display_name=f"Resolve {root.name}", minecraft=runtime.minecraft_version, loader=runtime.loader, loader_version=runtime.reference_loader_version)
            closure = core.resolve_exact_mod_closure(selection, source=workspace, cancel_event=state.event, deadline=state.deadline, checkpoint=lambda: _check(state.event, state.deadline), process_result_callback=lambda value: _record_process(state, value), diagnostic_project_id=plan.target.target_id)
            core.verify_exact_mod_metadata(selection, closure.metadata)
            return closure
        return core.resolve_mod_closure(provider=provider, selector=root.url if provider == "url" else root.project_id, canonical_project_id=None if provider == "url" else project, minecraft=runtime.minecraft_version, loader=runtime.loader, loader_version=runtime.reference_loader_version, cancel_event=state.event, deadline=state.deadline, resolver_root=workspace, url_max_jar_size_bytes=state.snapshot.url_policy.get("url_max_jar_size_bytes"), url_allow_private_networks=bool(state.snapshot.url_policy.get("url_allow_private_networks", False)), process_result_callback=lambda value: _record_process(state, value), diagnostic_project_id=plan.target.target_id)

    def canonical_root(root: TemplateRootIntent) -> tuple[str, str]:
        if root.provider == "modrinth":
            canonical = core.resolve_project_selector(root.provider, root.project_id, cancel_event=state.event, deadline=state.deadline, process_result_callback=lambda value: _record_process(state, value))
            provider = getattr(canonical, "provider", root.provider)
            project = str(getattr(canonical, "canonical_project_id", root.project_id))
            if not project:
                raise TemplateMigrationError("canonical Modrinth project identity is unavailable")
            return provider, project
        if root.provider == "curseforge":
            if not root.project_id.isdigit() or root.project_id.startswith("0"):
                raise TemplateMigrationError("CurseForge root is not a canonical positive ID")
            return root.provider, root.project_id
        return "url", root.project_id

    for root in effective_roots:
        _check(state.event, state.deadline)
        canonical_identity: str | None = None
        exact: TemplateExactConstraint | None = None
        try:
            provider, project = canonical_root(root)
            canonical_identity = f"{provider}:{project}"
            pending = pending_replacements.get(root.source_index)
            if pending is not None and pending.old_identity and canonical_identity == pending.old_identity:
                raise TemplateMigrationConflictResolutionError(
                    "Replacement selector resolved to the original canonical identity"
                )
            exact = root_constraints.get((provider, project))
            source_root = root
            source_provider, source_project = provider, project
            source_exact = exact
            if pending is not None:
                source_root = replacement_baselines.get(root.source_index, pending.source_root)
                baseline_identity = next(
                    (item.old_identity for item in state.replaced_roots if item.source_root.source_index == root.source_index),
                    pending.old_identity,
                )
                old_provider, separator, old_project = baseline_identity.partition(":")
                if separator and old_provider in {"modrinth", "curseforge"} and old_project:
                    source_provider, source_project = old_provider, old_project
                else:
                    source_provider, source_project = canonical_root(source_root)
                source_exact = root_constraints.get((source_provider, source_project))
                if source_exact is not None:
                    raise TemplateMigrationConflictResolutionError(
                        "Replacement cannot transfer root exact constraints"
                    )
            source_closure = resolve_runtime(source_root, source_provider, source_project, source_exact, phase="source", runtime=state.snapshot.target)
            target_closure = resolve_runtime(root, provider, project, exact, phase="target", runtime=plan.target)
            if source_provider != "url" and tuple(getattr(source_closure, "root_identity", ())) != (source_provider, source_project):
                raise TemplateMigrationError("source resolver returned a different canonical root identity")
            if provider != "url" and tuple(getattr(target_closure, "root_identity", ())) != (provider, project):
                raise TemplateMigrationError("resolver returned a different canonical root identity")
            evidence = None
            if root.provider == "url":
                evidence = _url_evidence(root, target_closure, state); url_facts.append(evidence)
                if evidence.status != "compatible":
                    code = "url-incompatible-loader" if evidence.loader_status == "incompatible" else "url-incompatible-minecraft" if evidence.minecraft_status == "incompatible" else "url-compatible-unknown"
                    actual = tuple(getattr(target_closure, "root_identity", ()))
                    actual_identity = f"{actual[0]}:{actual[1]}" if len(actual) == 2 else canonical_identity
                    unresolved.append(_unresolved(root, code, evidence.detail, canonical_identity=actual_identity, retry=evidence.status == "unknown")); continue
            source_fact, _ = _root_artifact_fact(source_closure); target_fact, identities = _root_artifact_fact(target_closure)
            same_artifact = source_fact.canonical_identity == target_fact.canonical_identity and (
                source_fact.artifact_id == target_fact.artifact_id if source_fact.artifact_id is not None and target_fact.artifact_id is not None else source_fact.metadata_digest == target_fact.metadata_digest
            )
            provisional[root.source_index] = {"root": root, "provider": provider, "project": project, "source": source_closure, "target": target_closure, "source_fact": source_fact, "target_fact": target_fact, "source_selector": source_root.project_id, "identities": identities, "evidence": evidence, "classification": "updated" if root.source_index in replaced_source_indices else "unchanged" if same_artifact else "updated"}
        except TemplateMigrationConflictResolutionError:
            raise
        except BaseException as error:
            if _resolver_integrity(error): raise TemplateMigrationOperationError(str(error)) from error
            unresolved.append(_unresolved(root, "version-intent-blocked" if exact else "no-compatible-file", str(error), canonical_identity=canonical_identity, retry=not bool(exact), version=exact.artifact_id if exact else None))
        if progress: progress(f"resolved root {root.source_index}")

    # Canonically identical explicit roots are a typed root conflict.  This is
    # derived only after selector resolution and before closure persistence.
    identity_owners: dict[str, list[int]] = {}
    for source_index, item in provisional.items():
        identity_owners.setdefault(f"{item['provider']}:{item['project']}", []).append(source_index)
    for identity, owners in sorted(identity_owners.items()):
        if len(owners) < 2:
            continue
        affected = tuple(sorted(owners))
        target_facts = [provisional[index]["target_fact"] for index in affected]
        collision = TemplateCollisionFact(
            "identity-collision", affected, (identity,),
            tuple(sorted({fact.metadata_path for fact in target_facts}, key=lambda value: value.as_posix())),
            tuple(sorted({fact.filename for fact in target_facts})),
            f"Multiple Template roots resolve to {identity}",
        )
        collisions.append(collision)
        for index in affected:
            root = provisional[index]["root"]
            unresolved.append(_unresolved(root, collision.reason_code, collision.detail, canonical_identity=identity, retry=False))
            provisional.pop(index, None)

    dependency_owner_indices: dict[tuple[str, str, str], tuple[int, ...]] = {}
    for constraint in dependency_constraints:
        owners = []
        for source_index, value in provisional.items():
            if any(
                (_metadata_identity(record).provider, _metadata_identity(record).project_id)
                == (constraint.provider, constraint.project_id)
                for record in getattr(value["target"], "metadata", ())
            ):
                owners.append(source_index)
        dependency_owner_indices[(constraint.provider, constraint.project_id, constraint.artifact_id)] = tuple(sorted(owners))

    # Apply dependency-scoped exact target intent without promoting dependencies.
    for source_index in tuple(sorted(provisional)):
        item = provisional[source_index]; root = item["root"]; closure = item["target"]
        records = [_metadata_identity(item) for item in getattr(closure, "metadata", ())]
        applicable = [
            constraint
            for constraint in dependency_constraints
            if any((item.provider, item.project_id) == (constraint.provider, constraint.project_id) for item in records)
        ]
        mismatched = [
            constraint
            for constraint in applicable
            if not any((item.provider, item.project_id, item.file_id) == (constraint.provider, constraint.project_id, constraint.artifact_id) for item in records)
        ]
        if mismatched:
            try:
                root_identity = tuple(getattr(closure, "root_identity", ()))
                root_record = next(item for item in records if (item.provider, item.project_id) == root_identity)
                if root_identity[0] not in {"modrinth", "curseforge"} or not root_record.file_id:
                    raise TemplateMigrationError("Exact dependency constraints require a provider root artifact")
                root_selection = core.exact_mod_artifact_selection(root_identity[0], root_identity[1], root_record.file_id)
                preseed = tuple(core.exact_mod_artifact_selection(item.provider, item.project_id, item.artifact_id) for item in applicable)
                constrained_source = attempt_root / "roots" / f"constrained-{root.source_index}"
                core.create_resolver_source(constrained_source, display_name=f"Constrain {root.name}", minecraft=plan.target.minecraft_version, loader=plan.target.loader, loader_version=plan.target.reference_loader_version)
                closure = core.resolve_exact_mod_closure(root_selection, source=constrained_source, cancel_event=state.event, deadline=state.deadline, checkpoint=lambda: _check(state.event, state.deadline), preseed_selections=preseed, process_result_callback=lambda value: _record_process(state, value), diagnostic_project_id=plan.target.target_id)
                records = [_metadata_identity(item) for item in getattr(closure, "metadata", ())]
                for constraint in applicable:
                    if not any((item.provider, item.project_id, item.file_id) == (constraint.provider, constraint.project_id, constraint.artifact_id) for item in records):
                        raise TemplateMigrationError(f"Exact dependency artifact {constraint.artifact_id} was not retained")
                target_fact, identities = _root_artifact_fact(closure)
                source_fact = item["source_fact"]
                item["target"] = closure; item["target_fact"] = target_fact; item["identities"] = identities
                item["classification"] = "updated" if root.source_index in replaced_source_indices else "unchanged" if source_fact.canonical_identity == target_fact.canonical_identity and source_fact.artifact_id == target_fact.artifact_id else "updated"
            except BaseException as error:
                if _resolver_integrity(error): raise TemplateMigrationOperationError(str(error)) from error
                constraint = mismatched[0]
                unresolved.append(_unresolved(root, "version-intent-blocked", str(error), canonical_identity=item["target_fact"].canonical_identity, retry=False, version=constraint.artifact_id))
                provisional.pop(source_index, None)

    # Missing or still-mismatched exact dependency artifacts fail closed.
    version_facts: list[TemplateVersionIntentFact] = []
    version_issues: list[TemplateVersionIntentIssue] = []
    for constraint in dependency_constraints:
        matches = []
        for item in provisional.values():
            root, closure = item["root"], item["target"]
            for item in getattr(closure, "metadata", ()):
                identity = _metadata_identity(item)
                if (identity.provider, identity.project_id) == (constraint.provider, constraint.project_id): matches.append((root, identity))
        satisfied = bool(matches) and all(identity.file_id == constraint.artifact_id for _, identity in matches)
        owners = tuple(sorted({root.source_index for root, _ in matches})) or dependency_owner_indices.get((constraint.provider, constraint.project_id, constraint.artifact_id), ())
        version_facts.append(TemplateVersionIntentFact(constraint.provider, constraint.project_id, constraint.artifact_id, constraint.scope, satisfied, owners))
        if not satisfied:
            detail = f"Exact dependency artifact {constraint.artifact_id} is unavailable in the target closure"
            version_issues.append(TemplateVersionIntentIssue(constraint.provider, constraint.project_id, constraint.artifact_id, constraint.scope, "version-intent-blocked", detail, owners))
            for source_index in owners:
                root = next(value for value in effective_roots if value.source_index == source_index)
                canonical = provisional[source_index]["target_fact"].canonical_identity if source_index in provisional else None
                if not any(value.source_index == source_index for value in unresolved): unresolved.append(_unresolved(root, "version-intent-blocked", detail, canonical_identity=canonical, retry=False, version=constraint.artifact_id))
                provisional.pop(source_index, None)
    for constraint in effective_overrides:
        if constraint.scope == "root":
            owners = tuple(sorted(index for index, item in provisional.items() if (item["provider"], item["project"]) == (constraint.provider, constraint.project_id)))
            satisfied = bool(owners)
            version_facts.append(TemplateVersionIntentFact(constraint.provider, constraint.project_id, constraint.artifact_id, constraint.scope, satisfied, owners))
            if not satisfied: version_issues.append(TemplateVersionIntentIssue(constraint.provider, constraint.project_id, constraint.artifact_id, constraint.scope, "version-intent-blocked", "Exact root artifact is unavailable", tuple(root.source_index for root in effective_roots if (root.provider, root.project_id) == (constraint.provider, constraint.project_id))))

    staging_digest = None
    warnings = tuple(dict.fromkeys(
        f"{redact_url(fact.url)}: compatibility unknown"
        for fact in url_facts if fact.status == "unknown"
    ))
    if not unresolved and not version_issues and len(provisional) == len(effective_roots):
        collision_member_owners: dict[
            tuple[tuple[str, str], Path, str], dict[int, int]
        ] = {}
        for owner_index, value in provisional.items():
            for record in getattr(value["target"], "metadata", ()):
                identity = getattr(record, "identity", None)
                if isinstance(identity, tuple) and len(identity) == 2:
                    member = _metadata_identity(record)
                    key = (identity, record.relative_path, member.filename)
                    counts = collision_member_owners.setdefault(key, {})
                    counts[owner_index] = counts.get(owner_index, 0) + 1
        combined = attempt_root / "combined"
        core.create_resolver_source(combined, display_name=plan.target.display_name, minecraft=plan.target.minecraft_version, loader=plan.target.loader, loader_version=plan.target.reference_loader_version)
        explicit = {(item["provider"], item["project"]): item["root"].side for item in provisional.values() if item["provider"] != "url"}
        merged_indices: list[int] = []
        try:
            for source_index in sorted(provisional):
                item = provisional[source_index]; root, closure = item["root"], item["target"]
                core.merge_metadata_closure(combined, closure, requested_side=root.side, explicit_root_sides=explicit, cancel_event=state.event, deadline=state.deadline, equivalence_workspace=attempt_root / "equivalence", process_result_callback=lambda value: _record_process(state, value))
                merged_indices.append(source_index)
            core.run_noninteractive_packwiz(["packwiz", "refresh"], cwd=combined, cancel_event=state.event, deadline=state.deadline, label="Template migration refresh", process_result_callback=lambda value: _record_process(state, value), project_id=plan.target.target_id, operation="template-migration-refresh")
        except BaseException as error:
            if _resolver_integrity(error): raise TemplateMigrationOperationError(str(error)) from error
            structured = error if isinstance(error, core.MetadataClosureCollisionError) else None
            if structured is not None:
                affected_collisions: dict[int, TemplateCollisionFact] = {}
                for evidence in structured.evidences:
                    identities = tuple(sorted((f"{evidence.left_identity[0]}:{evidence.left_identity[1]}", f"{evidence.right_identity[0]}:{evidence.right_identity[1]}")))
                    left_key = (evidence.left_identity, evidence.left_path, evidence.left_filename)
                    right_key = (evidence.right_identity, evidence.right_path, evidence.right_filename)
                    left_owners = collision_member_owners.get(left_key, {})
                    right_owners = collision_member_owners.get(right_key, {})
                    if evidence.left_identity != evidence.right_identity:
                        affected_set = set(left_owners) | set(right_owners)
                    elif left_key != right_key:
                        affected_set = set(left_owners) & set(right_owners)
                        if not affected_set:
                            affected_set = set(left_owners) | set(right_owners)
                    else:
                        affected_set = {
                            index for index, count in left_owners.items() if count >= 2
                        }
                        if not affected_set:
                            affected_set = set(left_owners)
                    affected = tuple(sorted(affected_set or {source_index}))
                    paths = tuple(sorted({evidence.left_path, evidence.right_path}, key=lambda value: value.as_posix()))
                    filenames = tuple(sorted({evidence.left_filename, evidence.right_filename}))
                    collision = TemplateCollisionFact(evidence.reason_code, affected, identities, paths, filenames, str(error)[:240])
                    collisions.append(collision)
                    for index in affected:
                        affected_collisions.setdefault(index, collision)
                for index, collision in sorted(affected_collisions.items()):
                    root = provisional[index]["root"]
                    unresolved.append(_unresolved(root, collision.reason_code, collision.detail, canonical_identity=provisional[index]["target_fact"].canonical_identity, retry=False))
                    provisional.pop(index, None)
            else:
                reason = _collision_reason(error)
                if reason is None: raise
                # Evidence-free legacy collision errors cannot safely identify
                # one owner.  Bind every participating root rather than
                # authorizing a destructive choice for an arbitrary last root.
                affected = tuple(sorted(provisional))
                target_facts = [provisional[index]["target_fact"] for index in affected]
                identities = tuple(sorted({fact.canonical_identity for fact in target_facts}))
                paths = tuple(sorted({fact.metadata_path for fact in target_facts}, key=lambda value: value.as_posix()))
                filenames = tuple(sorted({fact.filename for fact in target_facts}))
                collision = TemplateCollisionFact(reason, affected, identities, paths, filenames, str(error)[:240]); collisions.append(collision)
                for index in affected:
                    root = provisional[index]["root"]
                    unresolved.append(_unresolved(root, reason, collision.detail, canonical_identity=provisional[index]["target_fact"].canonical_identity, retry=False))
                    provisional.pop(index, None)

    resolved: list[TemplateResolvedRoot] = []
    root_facts: list[TemplateRootResolutionFact] = []
    for source_index in sorted(provisional):
        item = provisional[source_index]; root = item["root"]; source_fact = item["source_fact"]; target_fact = item["target_fact"]
        classification = item["classification"]
        resolved.append(TemplateResolvedRoot(source_index, item["provider"], item["project"], root.side, core.resolved_closure_fingerprint(item["target"]), target_fact.artifact_id, item["identities"], classification, item["evidence"]))
        root_facts.append(TemplateRootResolutionFact(source_index, root.name, item["provider"], item["source_selector"], source_fact.canonical_identity, target_fact.canonical_identity, root.side, source_fact, target_fact, classification))

    unresolved_by_index = {item.source_index: item for item in unresolved}
    unresolved = [unresolved_by_index[index] for index in sorted(unresolved_by_index)]
    if set(provisional).intersection(unresolved_by_index): raise TemplateMigrationOperationError("Template resolution membership is incoherent")
    canonical_roots: list[TemplateRootIntent] = []
    facts_by_index = {fact.source_index: fact for fact in root_facts}
    for root in effective_roots:
        fact = facts_by_index.get(root.source_index)
        unresolved_fact = unresolved_by_index.get(root.source_index)
        identity = fact.target_canonical_identity if fact is not None else getattr(unresolved_fact, "canonical_identity", None)
        if root.provider != "url" and isinstance(identity, str) and ":" in identity:
            provider, project = identity.split(":", 1)
            canonical_roots.append(TemplateRootIntent(root.source_index, root.name, provider, project, root.side))
        else:
            canonical_roots.append(root)
    state.effective_roots = tuple(canonical_roots)
    status: Literal["resolved", "resolution-required"] = "resolved" if len(resolved) == len(effective_roots) and not unresolved and not version_issues else "resolution-required"
    if status == "resolved":
        target_roots = {fact.source_index: fact for fact in root_facts}
        mods = [{"name": root.name, "provider": root.provider, "project_id": (target_roots[root.source_index].target_artifact.project_id if root.provider != "url" else root.project_id), "side": root.side, **({"url": root.url} if root.url else {})} for root in state.effective_roots]
        config = {"id": plan.target.target_id, "display_name": plan.target.display_name, "enabled": state.snapshot.enabled, "minecraft": plan.target.minecraft_version, "loader": plan.target.loader, "reference_loader_version": plan.target.reference_loader_version, "mods": mods, "mod_version_overrides": [c.__dict__ for c in effective_overrides]}
        if state.snapshot.committed_url_max_jar_size_bytes is not None:
            config["url_max_jar_size_bytes"] = state.snapshot.committed_url_max_jar_size_bytes
        packctl.prospective_template_config(plan.target.target_id, config, {})
        payload = yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode(); _write_new_regular(state.staging, "template.yaml", payload); state.result_digest = hashlib.sha256(payload).hexdigest(); staging_digest = _tree_digest(state.staging)
    identity_collisions = tuple(value for value in collisions if value.reason_code == "identity-collision")
    path_collisions = tuple(value for value in collisions if value.reason_code == "path-collision")
    filename_collisions = tuple(value for value in collisions if value.reason_code == "filename-collision")
    result_payload = (plan.plan_digest, status, resolved, unresolved, state.effective_roots, root_facts, version_facts, version_issues, collisions, url_facts, warnings, state.removed_roots, state.replaced_roots, state.attempt, staging_digest)
    result_digest = _digest(result_payload)
    result = TemplateResolutionResult(status, plan.source_snapshot_digest, plan.target, tuple(resolved), tuple(unresolved), state.snapshot.target.minecraft_version, state.snapshot.target.loader, state.snapshot.target.reference_loader_version, state.effective_roots, tuple(root_facts), tuple(version_facts), tuple(version_issues), tuple(collisions), identity_collisions, path_collisions, filename_collisions, tuple(url_facts), warnings, tuple(state.removed_roots), tuple(state.replaced_roots), state.attempt, staging_digest, result_digest)
    state.resolution = result
    return result


def resolve_template_migration_conflicts_at(
    plan: TemplateMigrationPlan,
    request: object,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> object:
    """Apply exact conflict Authority and re-resolve the existing plan."""
    from template_migration_conflicts import (
        TemplateMigrationConflictResolutionResult,
        TemplateMigrationReplacedRoot,
        validate_template_migration_resolution_request,
    )
    state = plan._state
    if (cancel_event is not None and cancel_event is not state.event) or (deadline is not None and deadline != state.deadline):
        raise TemplateMigrationOperationError("Event/deadline does not belong to plan")
    validated = validate_template_migration_resolution_request(plan, request)
    previous = (
        state.effective_roots, state.effective_overrides, state.removed_roots,
        state.replaced_roots, state.pending_replacements, state.resolution, state.result_digest,
        state.publication_token,
    )
    state.effective_roots = validated.effective_roots
    state.effective_overrides = validated.effective_overrides
    state.removed_roots = tuple(state.removed_roots) + validated.removed_roots
    state.pending_replacements = validated.replaced_roots
    replacements = {item.source_root.source_index: item for item in state.replaced_roots}
    for item in validated.replaced_roots:
        prior = replacements.get(item.source_root.source_index)
        if prior is None:
            replacements[item.source_root.source_index] = item
        else:
            replacements[item.source_root.source_index] = TemplateMigrationReplacedRoot(
                prior.source_root, item.replacement_root, item.replacement_selector, prior.old_identity,
                item.new_identity, prior.provider_changed or item.provider_changed,
            )
    state.replaced_roots = tuple(replacements[index] for index in sorted(replacements))
    try:
        resolution = resolve_template_migration_plan_at(
            plan, cancel_event=state.event, deadline=state.deadline, progress=progress
        )
    except BaseException:
        (
            state.effective_roots, state.effective_overrides, state.removed_roots,
            state.replaced_roots, state.pending_replacements, state.resolution, state.result_digest,
            state.publication_token,
        ) = previous
        raise
    state.consumed_resolution_requests.add(validated.request_digest)
    state.pending_replacements = ()
    identities = {
        item.source_index: f"{item.provider}:{item.project_id}" for item in resolution.resolved
    }
    identities.update(
        (item.source_index, item.canonical_identity)
        for item in resolution.unresolved if item.canonical_identity
    )
    finalized = []
    replaced_indices = {item.source_root.source_index for item in validated.replaced_roots}
    effective_by_index = {item.source_index: item for item in resolution.ordered_roots}
    for item in state.replaced_roots:
        if item.source_root.source_index in replaced_indices:
            finalized.append(TemplateMigrationReplacedRoot(
                item.source_root,
                effective_by_index.get(item.source_root.source_index, item.replacement_root),
                item.replacement_selector, item.old_identity,
                identities.get(item.source_root.source_index, item.new_identity),
                item.provider_changed,
            ))
        else:
            finalized.append(item)
    state.replaced_roots = tuple(finalized)
    payload = (
        plan.plan_digest, resolution.status, resolution.resolved, resolution.unresolved,
        resolution.ordered_roots, resolution.ordered_root_facts,
        resolution.version_intent_facts, resolution.version_intent_issues,
        resolution.collisions, resolution.url_evidence, resolution.warnings,
        state.removed_roots, state.replaced_roots, resolution.resolution_attempt,
        resolution.staging_digest,
    )
    resolution = replace(
        resolution,
        removed_roots=tuple(state.removed_roots),
        replaced_roots=tuple(state.replaced_roots),
        digest=_digest(payload),
    )
    state.resolution = resolution
    return TemplateMigrationConflictResolutionResult(
        resolution, validated.removed_roots, tuple(
            item for item in state.replaced_roots
            if item.source_root.source_index in replaced_indices
        ), resolution.resolution_attempt, resolution.status,
    )


def _verify(plan: TemplateMigrationPlan, result: TemplateResolutionResult) -> None:
    s = plan._state; _check(s.event, s.deadline)
    if s.tx is None or s.detached is None or s.staging is None or s.tx_identity is None or s.detached_identity is None or s.staging_identity is None:
        raise TemplateMigrationOperationError("Template migration staging is incomplete")
    current = snapshot_template_migration_source_at(plan.source_id, cancel_event=s.event, deadline=s.deadline)
    if current.project_identity != s.snapshot.project_identity or current.tree_digest != s.snapshot.tree_digest or current.snapshot_digest != plan.source_snapshot_digest or result is not s.resolution or result.status != "resolved": raise TemplateMigrationOperationError("stale migration plan or resolution")
    if _identity(s.tx) != s.tx_identity or _identity(s.detached) != s.detached_identity or _identity(s.staging) != s.staging_identity: raise TemplateMigrationOperationError("migration staging identity changed")
    if _tree_digest(s.detached) != s.snapshot.tree_digest: raise TemplateMigrationOperationError("detached Template snapshot changed")
    if _identity(packctl.get_template_root(plan.target.target_id, must_exist=False).parent) != s.target_parent_identity: raise TemplateMigrationOperationError("target Template parent changed")
    if set(s.locks.owned_keys) != {f"template:{plan.source_id}", f"template:{plan.target.target_id}"}: raise TemplateMigrationOperationError("Template migration locks are not fully owned")
    if not _target_missing(packctl.get_template_root(plan.target.target_id, must_exist=False)): raise TemplateMigrationOperationError("target Template appeared")
    staged_bytes, _ = _file(s.staging, "template.yaml")
    if not s.result_digest or staged_bytes is None or hashlib.sha256(staged_bytes).hexdigest() != s.result_digest or result.staging_digest != _tree_digest(s.staging): raise TemplateMigrationOperationError("staging changed")


def prepare_template_migration_publication(plan: TemplateMigrationPlan, result: TemplateResolutionResult, *, warning_acknowledgements: tuple[str, ...] = ()) -> TemplateMigrationPublication:
    _verify(plan, result); s = plan._state
    required = tuple(sorted(set(result.warnings)))
    supplied = tuple(warning_acknowledgements)
    if len(set(supplied)) != len(supplied) or tuple(sorted(supplied)) != required: raise TemplateMigrationOperationError("warning acknowledgement Authority is incomplete")
    token = (id(plan), result.resolution_attempt, plan.source_snapshot_digest, plan.target, result.digest, result.staging_digest, s.staging_identity, s.tx_identity, s.target_parent_identity, required)
    s.publication_token = token
    return TemplateMigrationPublication(plan, token, _PUBLICATION_SECRET)


def apply_template_migration_publication(publication: TemplateMigrationPublication) -> TemplateMigrationSourceSnapshot:
    if publication._used: raise TemplateMigrationOperationError("publication already consumed")
    plan = publication._plan; s = publication._state
    if s.publication_token is not publication._token: raise TemplateMigrationOperationError("publication handoff is stale")
    _verify(plan, s.resolution)
    parent = packctl.get_template_root(s.target.target_id, must_exist=False).parent
    pfd = os.open(parent, _DIR); tfd = os.open(s.tx, _DIR)
    expected = s.staging_identity
    rename_error: BaseException | None = None
    published: TemplateMigrationSourceSnapshot | None = None
    postcommit_error: BaseException | None = None
    try:
        opened_parent = os.fstat(pfd); opened_transaction = os.fstat(tfd)
        if (opened_parent.st_dev, opened_parent.st_ino) != s.target_parent_identity:
            raise TemplateMigrationOperationError("target Template parent identity changed before publication")
        if (opened_transaction.st_dev, opened_transaction.st_ino) != s.tx_identity:
            raise TemplateMigrationOperationError("Template migration transaction identity changed before publication")
        try:
            staged = os.stat("staging", dir_fd=tfd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise TemplateMigrationOperationError("Template migration staging disappeared before publication") from error
        if not stat.S_ISDIR(staged.st_mode) or (staged.st_dev, staged.st_ino) != expected:
            raise TemplateMigrationOperationError("Template migration staging identity changed before publication")
        try:
            os.stat(s.target.target_id, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise TemplateMigrationOperationError("target Template appeared before publication")
        _check(s.event, s.deadline)
        s.publication_state = "uncertain"; publication._used = True
        try:
            packctl.renameat2(tfd, "staging", pfd, s.target.target_id, _NOREPLACE); os.fsync(pfd)
        except BaseException as error:
            rename_error = error
        target_identity = None; staged_identity = None
        try:
            value = os.stat(s.target.target_id, dir_fd=pfd, follow_symlinks=False); target_identity = (value.st_dev, value.st_ino)
        except FileNotFoundError:
            pass
        try:
            value = os.stat("staging", dir_fd=tfd, follow_symlinks=False); staged_identity = (value.st_dev, value.st_ino)
        except FileNotFoundError:
            pass
        if target_identity == expected and staged_identity is None:
            s.committed = True; s.publication_state = "published"
        elif staged_identity == expected and target_identity != expected:
            s.publication_state = "not-published"
            raise TemplateMigrationOperationError(f"publication failed or raced: {rename_error or 'target appeared'}") from rename_error
        else:
            s.publication_state = "uncertain"
            raise TemplateMigrationOperationError("Template migration publication result is ambiguous") from rename_error
        try:
            descriptor_target = Path(f"/proc/self/fd/{pfd}") / s.target.target_id
            published = snapshot_template_migration_source_at(s.target.target_id, descriptor_target, cancel_event=s.event, deadline=s.deadline)
            if published.project_identity != expected or published.tree_digest != s.resolution.staging_digest:
                raise TemplateMigrationOperationError("published Template verification failed")
        except BaseException as error:
            postcommit_error = error
    finally:
        os.close(tfd); os.close(pfd)
    if postcommit_error is not None:
        s.cleanup_error = postcommit_error
        raise TemplateMigrationOperationError("published Template verification is incomplete") from postcommit_error
    try:
        _cleanup(s, min(s.deadline, time.monotonic() + 10))
    except BaseException as error:
        s.cleanup_error = error
        raise TemplateMigrationOperationError("published Template verification or cleanup is incomplete") from error
    if published is None: raise TemplateMigrationOperationError("published Template verification is unavailable")
    return published


def _remove_directory_contents(descriptor: int, deadline: float) -> None:
    if time.monotonic() >= deadline: raise TemplateMigrationOperationError("cleanup deadline exceeded")
    with os.scandir(descriptor) as entries: values = list(entries)
    for entry in values:
        if time.monotonic() >= deadline: raise TemplateMigrationOperationError("cleanup deadline exceeded")
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(entry.name, _DIR, dir_fd=descriptor)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino): raise TemplateMigrationOperationError("cleanup directory changed")
                _remove_directory_contents(child_fd, deadline)
            finally: os.close(child_fd)
            os.rmdir(entry.name, dir_fd=descriptor)
        elif stat.S_ISREG(metadata.st_mode): os.unlink(entry.name, dir_fd=descriptor)
        else: raise TemplateMigrationOperationError("cleanup encountered an unsafe entry")


def _cleanup(s: _State, deadline: float) -> None:
    if s.tx is not None:
        try:
            os.stat(s.tx, follow_symlinks=False)
        except FileNotFoundError:
            transaction_exists = False
        else:
            transaction_exists = True
    else:
        transaction_exists = False
    if transaction_exists:
        transaction_fd = os.open(s.tx, _DIR)
        try:
            opened = os.fstat(transaction_fd)
            if (opened.st_dev, opened.st_ino) != s.tx_identity: raise TemplateMigrationOperationError("migration transaction changed before cleanup")
            _remove_directory_contents(transaction_fd, deadline)
        finally: os.close(transaction_fd)
        parent_fd = os.open(s.tx.parent, _DIR)
        try:
            opened_parent = os.fstat(parent_fd)
            if (opened_parent.st_dev, opened_parent.st_ino) != s.tx_parent_identity:
                raise TemplateMigrationOperationError("migration transaction parent changed before cleanup")
            current = os.stat(s.tx.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != s.tx_identity: raise TemplateMigrationOperationError("migration transaction changed during cleanup")
            os.rmdir(s.tx.name, dir_fd=parent_fd); s.transaction_removal_pending = True
            os.fsync(parent_fd); s.transaction_cleaned = True; s.transaction_removal_pending = False
        finally: os.close(parent_fd)
    elif s.transaction_removal_pending:
        parent_fd = os.open(s.tx.parent, _DIR)
        try:
            opened_parent = os.fstat(parent_fd)
            if (opened_parent.st_dev, opened_parent.st_ino) != s.tx_parent_identity:
                raise TemplateMigrationOperationError("migration transaction parent changed before cleanup retry")
            os.fsync(parent_fd); s.transaction_cleaned = True; s.transaction_removal_pending = False
        finally:
            os.close(parent_fd)
    elif s.tx_identity is not None and not s.transaction_cleaned:
        raise TemplateMigrationOperationError("migration transaction disappeared before cleanup")
    if time.monotonic() >= deadline: raise TemplateMigrationOperationError("cleanup deadline exceeded")
    s.locks.release(); s.cleanup_error = None


def retry_template_migration_cleanup(publication: TemplateMigrationPublication, *, deadline: float, cancel_event: threading.Event | None = None) -> TemplateMigrationSourceSnapshot:
    if not isinstance(publication, TemplateMigrationPublication): raise TemplateMigrationOperationError("cleanup retry requires the committed publication handoff")
    s = publication._state
    if s.publication_state != "published" or not s.committed or s.cleanup_error is None: raise TemplateMigrationOperationError("no committed Template cleanup requires retry")
    if cancel_event is not None and cancel_event.is_set(): raise TemplateMigrationOperationError("cleanup retry cancelled")
    retry_event = cancel_event or threading.Event()
    parent = packctl.get_template_root(s.target.target_id, must_exist=False).parent
    parent_fd = os.open(parent, _DIR)
    try:
        opened_parent = os.fstat(parent_fd)
        if (opened_parent.st_dev, opened_parent.st_ino) != s.target_parent_identity:
            raise TemplateMigrationOperationError("target Template parent changed before cleanup retry")
        target_entry = os.stat(s.target.target_id, dir_fd=parent_fd, follow_symlinks=False)
        if (target_entry.st_dev, target_entry.st_ino) != s.staging_identity:
            raise TemplateMigrationOperationError("published Template changed before cleanup retry")
        descriptor_target = Path(f"/proc/self/fd/{parent_fd}") / s.target.target_id
        published = snapshot_template_migration_source_at(s.target.target_id, descriptor_target, cancel_event=retry_event, deadline=deadline)
        if _identity(parent) != s.target_parent_identity:
            raise TemplateMigrationOperationError("target Template parent changed during cleanup retry")
    finally:
        os.close(parent_fd)
    if published.project_identity != s.staging_identity or published.tree_digest != s.resolution.staging_digest: raise TemplateMigrationOperationError("published Template changed before cleanup retry")
    try: _cleanup(s, deadline)
    except BaseException as error: s.cleanup_error = error; raise
    return published


def discard_template_migration_plan(plan: TemplateMigrationPlan, *, deadline: float | None = None) -> None:
    s = plan._state
    if s.committed or s.publication_state in {"published", "uncertain"}: raise TemplateMigrationOperationError("published or uncertain migrations cannot be discarded")
    try: _cleanup(s, s.deadline if deadline is None else deadline)
    except BaseException as error: s.cleanup_error = error; raise
