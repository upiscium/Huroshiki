"""Transactional, Pack-independent copying of Template recipes."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal
from uuid import uuid4

import yaml

import packctl

__all__ = [
    "TemplateMigrationError", "TemplateMigrationOperationError", "TemplateMigrationPlanningError", "TemplateMigrationUnresolved",
    "TemplateMigrationTarget", "TemplateRootIntent", "TemplateExactConstraint",
    "TemplateUrlEvidence", "TemplateResolvedRoot", "TemplateUnresolvedRoot",
    "TemplateResolutionResult", "TemplateMigrationSourceSnapshot", "TemplateMigrationPlan",
    "TemplateMigrationPublication", "snapshot_template_migration_source_at",
    "plan_template_copy_migration_at", "resolve_template_migration_plan_at",
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
class TemplateResolvedRoot:
    source_index: int; provider: str; project_id: str; side: str; metadata_digest: str
    artifact_id: str | None = None; closure_identities: tuple[str, ...] = ()
    classification: Literal["unchanged", "updated"] = "updated"
    url_evidence: TemplateUrlEvidence | None = None


@dataclass(frozen=True)
class TemplateUnresolvedRoot:
    source_index: int; canonical_identity: str; code: str; detail: str
    retry: bool = False; replacement_supported: bool = True; version_issue: str | None = None


@dataclass(frozen=True)
class TemplateResolutionResult:
    status: Literal["resolved", "resolution-required"]
    source_snapshot_digest: str; target: TemplateMigrationTarget
    resolved: tuple[TemplateResolvedRoot, ...]; unresolved: tuple[TemplateUnresolvedRoot, ...]
    url_evidence: tuple[TemplateUrlEvidence, ...]; warnings: tuple[str, ...]
    resolution_attempt: int; staging_digest: str | None; digest: str


@dataclass(frozen=True)
class TemplateMigrationSourceSnapshot:
    source_id: str; project_identity: tuple[int, int]; target: TemplateMigrationTarget; enabled: bool; roots: tuple[TemplateRootIntent, ...]
    overrides: tuple[TemplateExactConstraint, ...]; committed_bytes: bytes; local_bytes: bytes | None
    committed_identity: tuple[int, int, int, int]; local_identity: tuple[int, int, int, int] | None
    committed_digest: str; local_digest: str | None; tree_digest: str; snapshot_digest: str; url_policy: MappingProxyType
    cancel_event: threading.Event = field(repr=False, compare=False); operation_deadline: float = field(repr=False, compare=False)


class _State:
    __slots__ = ("event", "deadline", "snapshot", "target", "locks", "tx", "detached", "staging", "plan_digest", "resolution", "result_digest", "committed", "tx_identity", "detached_identity", "staging_identity", "target_parent_identity", "publication_token", "cleanup_error", "attempt", "process_results")
    def __init__(self, event, deadline, snapshot, target, locks, tx, detached, staging, plan_digest):
        self.event, self.deadline, self.snapshot, self.target, self.locks, self.tx, self.detached, self.staging, self.plan_digest = event, deadline, snapshot, target, locks, tx, detached, staging, plan_digest
        self.resolution = None; self.result_digest = None; self.committed = False
        self.tx_identity = _identity(tx); self.detached_identity = _identity(detached); self.staging_identity = _identity(staging)
        self.target_parent_identity = _identity(packctl.get_template_root(target.target_id, must_exist=False).parent)
        self.publication_token = None; self.cleanup_error = None; self.attempt = 0; self.process_results = []


@dataclass(frozen=True)
class TemplateMigrationPlan:
    source_id: str; target: TemplateMigrationTarget; source_snapshot_digest: str; plan_digest: str; roots: tuple[TemplateRootIntent, ...]
    _state: _State = field(repr=False, compare=False)
    @property
    def cancel_event(self) -> threading.Event: return self._state.event
    @property
    def deadline(self) -> float: return self._state.deadline


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
    semantic = (target, bool(effective.get("enabled", True)), tuple(roots), tuple(constraints), policy, tree_digest)
    if _identity(root) != root_identity: raise TemplateMigrationOperationError("Template directory changed while snapshotting")
    return TemplateMigrationSourceSnapshot(source_id, root_identity, target, bool(effective.get("enabled", True)), tuple(roots), tuple(constraints), committed, local, ci, li, committed_digest, local_digest, tree_digest, _digest(semantic), policy, event, effective_deadline)


def plan_template_copy_migration_at(source_id: str, target: TemplateMigrationTarget, *, root: Path | None = None, expected_snapshot: TemplateMigrationSourceSnapshot | None = None, deadline: float, cancel_event: threading.Event | None = None, progress: Callable[[str], None] | None = None) -> TemplateMigrationPlan:
    event = cancel_event or (expected_snapshot.cancel_event if expected_snapshot is not None else threading.Event()); _check(event, deadline)
    if expected_snapshot is not None and (event is not expected_snapshot.cancel_event or deadline != expected_snapshot.operation_deadline): raise TemplateMigrationOperationError("Snapshot, plan, and resolution must use one Event and deadline")
    if source_id == target.target_id: raise TemplateMigrationError("Source and target Template IDs must be different")
    locks = packctl.acquire_project_locks((f"template:{source_id}", f"template:{target.target_id}"), deadline=deadline, cancel_event=event, operation="Template copy migration")
    plan: TemplateMigrationPlan | None = None
    try:
        if not _target_missing(packctl.get_template_root(target.target_id, must_exist=False)): raise TemplateMigrationError("target Template already exists")
        snapshot = snapshot_template_migration_source_at(source_id, root, cancel_event=event, deadline=deadline)
        if expected_snapshot is not None and (expected_snapshot.source_id != source_id or expected_snapshot.project_identity != snapshot.project_identity or expected_snapshot.tree_digest != snapshot.tree_digest or expected_snapshot.snapshot_digest != snapshot.snapshot_digest):
            raise TemplateMigrationOperationError("Source Template changed after migration snapshot")
        if snapshot.target.minecraft_version != target.minecraft_version or snapshot.target.loader != target.loader:
            # The target authority is allowed to differ; it is precisely what
            # drives resolution.  Only the source enabled bit is implicit.
            pass
        repository = packctl.get_template_root(source_id).parent.parent
        state_root = packctl.make_state_directory(repository / ".huroshiki", repository_root=repository)
        tx = packctl.make_state_directory(state_root / "transactions" / ("template-copy-" + uuid4().hex), state_root=state_root, repository_root=repository)
        detached, staging = tx / "detached", tx / "staging"; detached.mkdir(mode=0o700); staging.mkdir(mode=0o700)
        plan_digest = _digest((source_id, target, snapshot.snapshot_digest))
        state = _State(event, deadline, snapshot, target, locks, tx, detached, staging, plan_digest)
        plan = TemplateMigrationPlan(source_id, target, snapshot.snapshot_digest, plan_digest, snapshot.roots, state)
        _write_new_regular(detached, "template.yaml", snapshot.committed_bytes, mode=stat.S_IMODE(snapshot.committed_identity[2]))
        if snapshot.local_bytes is not None:
            _write_new_regular(detached, "template.local.yaml", snapshot.local_bytes, mode=stat.S_IMODE(snapshot.local_identity[2]))
        if progress: progress("snapshot complete")
        return plan
    except BaseException as original:
        if plan is not None:
            try: _cleanup(plan._state, min(deadline, time.monotonic() + 10))
            except BaseException as cleanup_error:
                plan._state.cleanup_error = cleanup_error
                raise TemplateMigrationPlanningError(f"{original}; Template migration cleanup failed: {cleanup_error}", plan) from original
        else:
            locks.release()
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


def _unresolved(root: TemplateRootIntent, code: str, detail: str, *, retry: bool = True, version: str | None = None) -> TemplateUnresolvedRoot:
    return TemplateUnresolvedRoot(root.source_index, f"{root.provider}:{root.project_id}", code, detail[:240], retry, root.provider != "url", version)


def resolve_template_migration_plan_at(plan: TemplateMigrationPlan, *, cancel_event: threading.Event | None = None, deadline: float | None = None, progress: Callable[[str], None] | None = None) -> TemplateResolutionResult:
    state = plan._state
    if (cancel_event is not None and cancel_event is not state.event) or (deadline is not None and deadline != state.deadline): raise TemplateMigrationOperationError("Event/deadline does not belong to plan")
    _check(state.event, state.deadline)
    import huroshiki_core as core
    state.attempt += 1
    attempt_root = state.tx / f"resolver-attempt-{state.attempt:04d}"; attempt_root.mkdir(mode=0o700)
    (attempt_root / "roots").mkdir(mode=0o700); (attempt_root / "equivalence").mkdir(mode=0o700)
    resolved: list[TemplateResolvedRoot] = []; unresolved: list[TemplateUnresolvedRoot] = []; closures: list[tuple[TemplateRootIntent, object]] = []; url_facts: list[TemplateUrlEvidence] = []
    root_constraints = {(value.provider, value.project_id): value for value in state.snapshot.overrides if value.scope == "root"}
    dependency_constraints = tuple(value for value in state.snapshot.overrides if value.scope == "dependency")
    for root in plan.roots:
        _check(state.event, state.deadline)
        try:
            if root.provider == "modrinth":
                canonical = core.resolve_project_selector(root.provider, root.project_id, cancel_event=state.event, deadline=state.deadline, process_result_callback=lambda value: _record_process(state, value))
                provider = getattr(canonical, "provider", root.provider); project = str(getattr(canonical, "canonical_project_id", root.project_id))
                if project != root.project_id: raise TemplateMigrationError("canonical provider identity changed")
            elif root.provider == "curseforge":
                if not root.project_id.isdigit() or root.project_id.startswith("0"): raise TemplateMigrationError("CurseForge root is not a canonical positive ID")
                provider, project = root.provider, root.project_id
            else:
                provider, project = "url", root.project_id
            exact = root_constraints.get((provider, project))
            if exact is not None:
                selection = core.exact_mod_artifact_selection(exact.provider, exact.project_id, exact.artifact_id)
                exact_source = attempt_root / "roots" / f"root-{root.source_index}"
                core.create_resolver_source(exact_source, display_name=f"Resolve {root.name}", minecraft=plan.target.minecraft_version, loader=plan.target.loader, loader_version=plan.target.reference_loader_version)
                closure = core.resolve_exact_mod_closure(selection, source=exact_source, cancel_event=state.event, deadline=state.deadline, checkpoint=lambda: _check(state.event, state.deadline), process_result_callback=lambda value: _record_process(state, value), diagnostic_project_id=plan.target.target_id)
                core.verify_exact_mod_metadata(selection, closure.metadata)
            else:
                closure = core.resolve_mod_closure(provider=provider, selector=root.url if provider == "url" else root.project_id, canonical_project_id=None if provider == "url" else project, minecraft=plan.target.minecraft_version, loader=plan.target.loader, loader_version=plan.target.reference_loader_version, cancel_event=state.event, deadline=state.deadline, resolver_root=attempt_root / "roots" / f"root-{root.source_index}", url_max_jar_size_bytes=state.snapshot.url_policy.get("url_max_jar_size_bytes"), url_allow_private_networks=bool(state.snapshot.url_policy.get("url_allow_private_networks", False)), process_result_callback=lambda value: _record_process(state, value), diagnostic_project_id=plan.target.target_id)
            if provider != "url" and tuple(getattr(closure, "root_identity", ())) != (provider, project): raise TemplateMigrationError("resolver returned a different canonical root identity")
            evidence = None
            if root.provider == "url":
                evidence = _url_evidence(root, closure, state); url_facts.append(evidence)
                if evidence.status != "compatible":
                    code = "url-incompatible-loader" if evidence.loader_status == "incompatible" else "url-incompatible-minecraft" if evidence.minecraft_status == "incompatible" else "url-compatible-unknown"
                    unresolved.append(_unresolved(root, code, evidence.detail, retry=evidence.status == "unknown")); continue
                provider, project = root.provider, root.project_id
            records = [_metadata_identity(item) for item in getattr(closure, "metadata", ())]
            root_records = [item for item in records if item.provider == getattr(closure, "root_identity", (provider, project))[0] and item.project_id == getattr(closure, "root_identity", (provider, project))[1]]
            if len(root_records) != 1: raise TemplateMigrationError("resolver did not return exactly one root")
            closures.append((root, closure))
            resolved.append(TemplateResolvedRoot(root.source_index, provider, project, root.side, core.resolved_closure_fingerprint(closure), root_records[0].file_id, tuple(sorted(f"{item.provider}:{item.project_id}" for item in records)), "unchanged" if (provider, project) == (root.provider, root.project_id) else "updated", evidence))
        except BaseException as error:
            if _resolver_integrity(error): raise TemplateMigrationOperationError(str(error)) from error
            exact = root_constraints.get((root.provider, root.project_id))
            unresolved.append(_unresolved(root, "version-intent-blocked" if exact else "no-compatible-file", str(error), retry=not bool(exact), version=exact.artifact_id if exact else None))
        if progress: progress(f"resolved root {root.source_index}")
    # Dependency-scoped exact intent constrains observed closures without ever
    # becoming root intent.  When the provider's automatic closure selected a
    # different artifact, rebuild that root closure with the dependency pins
    # preseeded instead of silently accepting Automatic or promoting the
    # dependency to a root.
    rebuilt_closures: list[tuple[TemplateRootIntent, object]] = []
    for root, closure in closures:
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
                resolved_index = next(index for index, item in enumerate(resolved) if item.source_index == root.source_index)
                previous = resolved[resolved_index]
                resolved[resolved_index] = TemplateResolvedRoot(previous.source_index, previous.provider, previous.project_id, previous.side, core.resolved_closure_fingerprint(closure), previous.artifact_id, tuple(sorted(f"{item.provider}:{item.project_id}" for item in records)), previous.classification, previous.url_evidence)
            except BaseException as error:
                if _resolver_integrity(error): raise TemplateMigrationOperationError(str(error)) from error
                constraint = mismatched[0]
                unresolved.append(_unresolved(root, "version-intent-blocked", str(error), retry=False, version=constraint.artifact_id))
        rebuilt_closures.append((root, closure))
    closures = rebuilt_closures

    # Missing or still-mismatched exact dependency artifacts fail closed.
    for constraint in dependency_constraints:
        matches = []
        for root, closure in closures:
            for item in getattr(closure, "metadata", ()):
                identity = _metadata_identity(item)
                if (identity.provider, identity.project_id) == (constraint.provider, constraint.project_id): matches.append((root, identity))
        if not matches or any(identity.file_id != constraint.artifact_id for _, identity in matches):
            owner = matches[0][0] if matches else (plan.roots[0] if plan.roots else TemplateRootIntent(0, constraint.project_id, constraint.provider, constraint.project_id, "both"))
            unresolved.append(_unresolved(owner, "version-intent-blocked", f"Exact dependency artifact {constraint.artifact_id} is unavailable in the target closure", retry=False, version=constraint.artifact_id))
    status: Literal["resolved", "resolution-required"] = "resolved" if not unresolved and len(resolved) == len(plan.roots) else "resolution-required"
    staging_digest = None
    warnings = tuple(f"{fact.url}: compatibility unknown" for fact in url_facts if fact.status == "unknown")
    if status == "resolved":
        combined = attempt_root / "combined"
        core.create_resolver_source(combined, display_name=plan.target.display_name, minecraft=plan.target.minecraft_version, loader=plan.target.loader, loader_version=plan.target.reference_loader_version)
        explicit = {(item.provider, item.project_id): item.side for item in plan.roots if item.provider != "url"}
        for root, closure in closures:
            core.merge_metadata_closure(combined, closure, requested_side=root.side, explicit_root_sides=explicit, cancel_event=state.event, deadline=state.deadline, equivalence_workspace=attempt_root / "equivalence", process_result_callback=lambda value: _record_process(state, value))
        core.run_noninteractive_packwiz(["packwiz", "refresh"], cwd=combined, cancel_event=state.event, deadline=state.deadline, label="Template migration refresh", process_result_callback=lambda value: _record_process(state, value), project_id=plan.target.target_id, operation="template-migration-refresh")
        mods = [{"name": r.name, "provider": r.provider, "project_id": r.project_id, "side": r.side, **({"url": r.url} if r.url else {})} for r in plan.roots]
        config = {"id": plan.target.target_id, "display_name": plan.target.display_name, "enabled": state.snapshot.enabled, "minecraft": plan.target.minecraft_version, "loader": plan.target.loader, "reference_loader_version": plan.target.reference_loader_version, "mods": mods, "mod_version_overrides": [c.__dict__ for c in state.snapshot.overrides]}
        packctl.prospective_template_config(plan.target.target_id, config, {})
        payload = yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode(); _write_new_regular(state.staging, "template.yaml", payload); state.result_digest = hashlib.sha256(payload).hexdigest(); staging_digest = _tree_digest(state.staging)
    result_digest = _digest((plan.plan_digest, status, resolved, unresolved, url_facts, warnings, state.attempt, staging_digest))
    result = TemplateResolutionResult(status, plan.source_snapshot_digest, plan.target, tuple(resolved), tuple(unresolved), tuple(url_facts), warnings, state.attempt, staging_digest, result_digest)
    state.resolution = result
    return result


def _verify(plan: TemplateMigrationPlan, result: TemplateResolutionResult) -> None:
    s = plan._state; _check(s.event, s.deadline)
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
    required = tuple(sorted(result.warnings))
    if tuple(sorted(warning_acknowledgements)) != required: raise TemplateMigrationOperationError("warning acknowledgement Authority is incomplete")
    token = (id(plan), result.resolution_attempt, plan.source_snapshot_digest, plan.target, result.digest, result.staging_digest, s.staging_identity, s.tx_identity, s.target_parent_identity, required)
    s.publication_token = token
    return TemplateMigrationPublication(plan, token, _PUBLICATION_SECRET)


def apply_template_migration_publication(publication: TemplateMigrationPublication) -> TemplateMigrationSourceSnapshot:
    if publication._used: raise TemplateMigrationOperationError("publication already consumed")
    plan = publication._plan; s = publication._state
    if s.publication_token is not publication._token: raise TemplateMigrationOperationError("publication handoff is stale")
    _verify(plan, s.resolution)
    parent = packctl.get_template_root(s.target.target_id, must_exist=False).parent; pfd = os.open(parent, _DIR); tfd = os.open(s.tx, _DIR)
    expected = s.staging_identity
    try:
        packctl.renameat2(tfd, "staging", pfd, s.target.target_id, _NOREPLACE); os.fsync(pfd)
    except OSError as e:
        target_identity = None; staged_identity = None
        try:
            value = os.stat(s.target.target_id, dir_fd=pfd, follow_symlinks=False); target_identity = (value.st_dev, value.st_ino)
        except FileNotFoundError: pass
        try:
            value = os.stat("staging", dir_fd=tfd, follow_symlinks=False); staged_identity = (value.st_dev, value.st_ino)
        except FileNotFoundError: pass
        if target_identity == expected and staged_identity is None: s.committed = True
        else: raise TemplateMigrationOperationError(f"publication failed or raced: {e}") from e
    finally: os.close(tfd); os.close(pfd)
    publication._used = True; s.committed = True
    try:
        published = snapshot_template_migration_source_at(s.target.target_id, cancel_event=s.event, deadline=s.deadline)
        if published.tree_digest != s.resolution.staging_digest: raise TemplateMigrationOperationError("published Template verification failed")
        _cleanup(s, min(s.deadline, time.monotonic() + 10))
    except BaseException as error:
        s.cleanup_error = error
        raise TemplateMigrationOperationError("published Template verification or cleanup is incomplete") from error
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
    if s.tx.exists():
        transaction_fd = os.open(s.tx, _DIR)
        try:
            opened = os.fstat(transaction_fd)
            if (opened.st_dev, opened.st_ino) != s.tx_identity: raise TemplateMigrationOperationError("migration transaction changed before cleanup")
            _remove_directory_contents(transaction_fd, deadline)
        finally: os.close(transaction_fd)
        parent_fd = os.open(s.tx.parent, _DIR)
        try:
            current = os.stat(s.tx.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != s.tx_identity: raise TemplateMigrationOperationError("migration transaction changed during cleanup")
            os.rmdir(s.tx.name, dir_fd=parent_fd); os.fsync(parent_fd)
        finally: os.close(parent_fd)
    if time.monotonic() >= deadline: raise TemplateMigrationOperationError("cleanup deadline exceeded")
    s.locks.release(); s.cleanup_error = None


def retry_template_migration_cleanup(publication: TemplateMigrationPublication, *, deadline: float, cancel_event: threading.Event | None = None) -> TemplateMigrationSourceSnapshot:
    if not isinstance(publication, TemplateMigrationPublication): raise TemplateMigrationOperationError("cleanup retry requires the committed publication handoff")
    s = publication._state
    if not s.committed or s.cleanup_error is None: raise TemplateMigrationOperationError("no committed Template cleanup requires retry")
    if cancel_event is not None and cancel_event.is_set(): raise TemplateMigrationOperationError("cleanup retry cancelled")
    retry_event = cancel_event or threading.Event()
    published = snapshot_template_migration_source_at(s.target.target_id, cancel_event=retry_event, deadline=deadline)
    if published.tree_digest != s.resolution.staging_digest: raise TemplateMigrationOperationError("published Template changed before cleanup retry")
    try: _cleanup(s, deadline)
    except BaseException as error: s.cleanup_error = error; raise
    return published


def discard_template_migration_plan(plan: TemplateMigrationPlan, *, deadline: float | None = None) -> None:
    s = plan._state
    if s.committed: raise TemplateMigrationOperationError("published migrations cannot be discarded")
    try: _cleanup(s, s.deadline if deadline is None else deadline)
    except BaseException as error: s.cleanup_error = error; raise
