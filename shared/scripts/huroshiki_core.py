#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field, replace
import errno
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Callable, Iterable, Mapping
from urllib.parse import unquote, urlparse
from uuid import uuid4


import tomlkit

import packctl
from overlay_policy import (
    OVERLAY_TARGETS,
    OverlayPolicyError,
    create_overlay_file,
    delete_overlay_file,
    normalize_overlay_relative_path,
    read_overlay_text,
    safe_overlay_child,
    scan_content_overlays,
    write_overlay_text,
)
from template_merge import (
    ConflictResolution,
    ConflictSelection,
    MergedTemplateMod,
    ResolvedTemplateComposition,
    TemplateComposition,
    TemplateConflict,
    TemplateMergeError,
    TemplateModEntry,
    compose_templates,
    resolve_composition,
    union_side,
)
from packwiz_parser import ParserEvent
from packwiz_pty import PackwizPtySession, PtyResult
from url_artifacts import (
    DEFAULT_URL_MAX_JAR_SIZE_BYTES,
    HuroshikiError,
    URL_CHUNK_SIZE,
    URL_USER_AGENT,
    UrlArtifact,
    append_url_log,
    download_url_artifact,
    ensure_url_error_log,
    parse_jar_identity,
    sanitize_mod_id,
    url_log_paths,
    validate_public_url,
    write_url_metadata,
)
from portable_paths import (
    PortablePathError,
    portable_basename,
    portable_basename_key,
    portable_relative_path,
    portable_relative_path_key,
)

ROOT = packctl.ROOT
PACKS = packctl.PACKS
TEMPLATES = packctl.TEMPLATES
SCRIPTS = Path(__file__).resolve().parent
STATE_ROOT = ROOT / ".huroshiki"
TRANSACTION_ROOT = STATE_ROOT / "transactions"
LOG_ROOT = STATE_ROOT / "logs"
TRASH_ROOT = STATE_ROOT / "trash"


PROJECT_KINDS = ("pack", "template")
StateItem = packctl.StateItem


def project_key(kind: str, project_id: str) -> str:
    if kind not in PROJECT_KINDS:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    packctl.validate_project_id(project_id)
    return f"{kind}:{project_id}"


def split_project_key(key: str) -> tuple[str, str]:
    try:
        kind, project_id = key.split(":", 1)
    except ValueError as error:
        raise HuroshikiError(f"Invalid project key: {key}") from error
    if kind not in PROJECT_KINDS:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    packctl.validate_project_id(project_id)
    return kind, project_id


@dataclass(frozen=True)
class ProjectDeployPreview:
    project_key: str
    action: str
    target: str
    dist_digest: str
    changes: tuple[packctl.RsyncChange, ...]
    raw_lines: tuple[str, ...]
    restart_target: tuple[str, str, str] | None = None
    snapshot: Path | None = None

    @property
    def confirmation_lines(self) -> tuple[str, ...]:
        counts = {
            category: sum(change.category == category for change in self.changes)
            for category in ("added", "updated", "deleted")
        }
        lines = [
            f"Pack: {split_project_key(self.project_key)[1]}",
            f"Action: {self.action}",
            f"Rsync target: {self.target}",
            "Changes: "
            f"{counts['added']} added, {counts['updated']} updated, "
            f"{counts['deleted']} deleted",
        ]
        if self.restart_target is not None:
            host, stack, service = self.restart_target
            lines.extend(
                (
                    f"SSH target: {host}",
                    f"Stack directory: {stack}",
                    f"Compose service: {service}",
                )
            )
        if self.raw_lines:
            lines.extend(("", "Rsync detail:", *self.raw_lines))
        else:
            lines.append("No rsync changes.")
        return tuple(lines)


def project_root(key: str) -> Path:
    kind, project_id = split_project_key(key)
    return packctl.get_project_root(kind, project_id)


def project_config(key: str) -> dict[str, object]:
    kind, project_id = split_project_key(key)
    return packctl.load_project_config(kind, project_id)


def project_source(key: str) -> Path:
    kind, _ = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Template manifests do not have a persistent Packwiz source")
    return project_root(key) / "source"


@dataclass(frozen=True)
class ProjectInfo:
    kind: str
    project_id: str
    display_name: str
    minecraft: str
    loader: str
    loader_version: str
    enabled: bool
    error: str | None = None
    mod_count: int | None = None

    @property
    def key(self) -> str:
        return project_key(self.kind, self.project_id)

    @property
    def type_label(self) -> str:
        return "MODPACK" if self.kind == "pack" else "TEMPLATE"

    @property
    def manifest_path(self) -> Path:
        name = "pack.yaml" if self.kind == "pack" else "template.yaml"
        parent = PACKS if self.kind == "pack" else TEMPLATES
        return parent / self.project_id / name


@dataclass
class ModInfo:
    relative_path: Path
    slug: str
    name: str
    provider: str
    project_id: str
    filename: str
    client: bool
    server: bool
    source_url: str = ""
    selected: bool = False
    side_error: str | None = None

    @property
    def side(self) -> str:
        if self.side_error is not None:
            return "invalid"
        return side_from_flags(self.client, self.server)

    @property
    def side_label(self) -> str:
        if self.side_error is not None:
            return "[?] [?]"
        return f"[{'x' if self.client else ' '}] [{'x' if self.server else ' '}]"


@dataclass(frozen=True)
class UpdateChange:
    relative_path: Path
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True)
class UpdateCandidate:
    key: str
    root: Path
    slug: str
    name: str
    provider: str
    current_version: str
    new_version: str
    status: str
    changes: tuple[UpdateChange, ...] = ()
    added_dependencies: int = 0
    error: str | None = None
    error_returncode: int | None = None

    @property
    def relative_path(self) -> Path:
        return self.root

    @property
    def available(self) -> bool:
        return self.status == "update"

    @property
    def file_count(self) -> int:
        return len(self.changes)


@dataclass(frozen=True)
class TemplateInfo:
    target: str
    relative_path: Path
    full_path: Path
    size: int
    error: str | None = None


@dataclass(frozen=True)
class TransactionBatch:
    provider: str
    query: str
    changed_files: tuple[Path, ...]


@dataclass(frozen=True)
class AddOperationResult:
    returncode: int
    changed_files: tuple[Path, ...]
    raw_log: Path
    text_log: Path
    event_log: Path
    message: str
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0 and bool(self.changed_files)


@dataclass(frozen=True)
class TemplateInstallFailure:
    name: str
    provider: str
    project_id: str
    reason: str


@dataclass(frozen=True)
class RetainedTemplateCandidate:
    candidate_key: str
    name: str
    requested_provider: str
    requested_project_id: str
    actual_provider: str
    actual_project_id: str
    relative_path: Path
    filename: str


@dataclass(frozen=True)
class TemplateCreationReport:
    pack_key: str
    template_ids: tuple[str, ...]
    conflict_selections: tuple[ConflictSelection, ...] = field(default_factory=tuple)
    conflict_warnings: tuple[str, ...] = field(default_factory=tuple)
    installed: tuple[str, ...] = field(default_factory=tuple)
    failed: tuple[TemplateInstallFailure, ...] = field(default_factory=tuple)
    retained: tuple[RetainedTemplateCandidate, ...] = field(default_factory=tuple)

    @property
    def template_id(self) -> str:
        return self.template_ids[0]

    @property
    def warning_lines(self) -> list[str]:
        lines = ["Applied templates: " + " -> ".join(self.template_ids)]
        if self.conflict_selections:
            lines.append("Conflict decisions:")
            lines.extend(
                f"- {item.name}: {', '.join(item.candidate_labels)}"
                for item in self.conflict_selections
            )
        lines.extend(f"Warning: {warning}" for warning in self.conflict_warnings)
        lines.append(f"Installed {len(self.installed)} MOD(s).")
        if self.retained:
            lines.append("Retained template candidates:")
            lines.extend(
                f"- {item.name}: {item.actual_provider}:{item.actual_project_id} "
                f"at {item.relative_path} ({item.filename})"
                for item in self.retained
            )
        if not self.failed:
            lines.append("No installation failures.")
            return lines
        lines.append(f"Could not install {len(self.failed)} MOD(s):")
        lines.extend(
            f"- {item.name} ({item.provider}:{item.project_id}): {item.reason}"
            for item in self.failed
        )
        return lines


class PackwizAddOperation:
    def __init__(
        self,
        transaction: "PackTransaction",
        provider: str,
        query: str,
        *,
        client: bool,
        server: bool,
        on_event: Callable[[ParserEvent], None] | None = None,
    ) -> None:
        self.transaction = transaction
        self.provider, self.query = normalize_add_selector(provider, query)
        self.client = client
        self.server = server
        self.on_event = on_event
        self.cancelled = False
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.result: AddOperationResult | None = None

        self.checkpoint = transaction.root / f"checkpoint-{uuid4().hex}"
        copy_transaction_source(transaction.source, self.checkpoint)
        self.before = metadata_digest_snapshot(transaction.source)

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_dir = (
            ROOT / ".huroshiki" / "logs"
            / transaction.project_key.replace(":", "-")
            / f"{timestamp}-{uuid4().hex[:8]}"
        )
        packctl.ensure_safe_state_path(
            self.log_dir,
            state_root=ROOT / ".huroshiki",
            repository_root=ROOT,
        )
        self.session: PackwizPtySession | None = None
        if self.provider != "url":
            self.session = PackwizPtySession(
                build_add_command(self.provider, self.query),
                cwd=transaction.source,
                log_dir=self.log_dir,
                on_event=on_event,
            )

    def run(self) -> AddOperationResult:
        try:
            if self.provider == "url":
                self.result = self.transaction._finish_url_add(self)
            else:
                if self.session is None:
                    raise HuroshikiError("Packwiz PTY session was not initialized")
                pty_result = self.session.run()
                self.result = self.transaction._finish_add(self, pty_result)
            return self.result
        except Exception as error:
            self.transaction._rollback_add(self)
            raw_log, text_log, event_log = url_log_paths(self.log_dir)
            if self.provider == "url":
                ensure_url_error_log(self.log_dir, str(error))
            self.result = AddOperationResult(
                returncode=1,
                changed_files=(),
                raw_log=raw_log,
                text_log=text_log,
                event_log=event_log,
                message=str(error),
                cancelled=self.cancelled,
            )
            return self.result
        finally:
            self.done.set()

    def send_selection(self, index: int) -> None:
        if self.session is None:
            raise HuroshikiError("URL additions do not expose search results")
        self.session.send_line(str(index))

    def confirm(self, accepted: bool = True) -> None:
        if self.session is not None:
            self.session.send_line("y" if accepted else "n")

    def cancel_menu(self) -> None:
        self.cancelled = True
        self.cancel_event.set()
        if self.session is None:
            return
        try:
            self.session.send_line("0")
        except (OSError, RuntimeError):
            self.session.cancel()

    def cancel(self) -> None:
        self.cancelled = True
        self.cancel_event.set()
        if self.session is not None:
            self.session.cancel()

    def resize(self, width: int, height: int) -> None:
        if self.session is not None:
            self.session.resize(width, height)

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)


@dataclass
class PackTransaction:
    project_key: str
    root: Path
    source: Path
    baseline: dict[Path, str]
    baseline_contents: dict[Path, bytes] = field(default_factory=dict)
    real_source_baseline: dict[Path, str] = field(default_factory=dict)
    pack_config_baseline: dict[str, str] = field(default_factory=dict)
    template_config_baseline: dict[str, str] = field(default_factory=dict)
    template_manifest: list[object] | None = None
    batches: list[TransactionBatch] = field(default_factory=list)
    update_candidates: tuple[UpdateCandidate, ...] = field(default_factory=tuple)
    selected_update_changes: tuple[UpdateChange, ...] = field(default_factory=tuple)
    active: bool = True
    _project_lock: packctl.ProjectLock | None = field(default=None, init=False, repr=False)
    _operation: PackwizAddOperation | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    @classmethod
    def create(cls, project_key_value: str) -> "PackTransaction":
        kind, project_id = split_project_key(project_key_value)
        try:
            project_lock = packctl.ProjectLock(
                project_key_value, "transaction"
            ).acquire()
        except packctl.ConfigError as error:
            raise HuroshikiError(str(error)) from error
        tx_root: Path | None = None
        try:
            real_root = project_root(project_key_value)
            transaction_root = ROOT / ".huroshiki" / "transactions"
            packctl.make_state_directory(
                transaction_root,
                state_root=ROOT / ".huroshiki",
                repository_root=ROOT,
            )
            safe_prefix = project_key_value.replace(":", "-")
            tx_root = Path(
                tempfile.mkdtemp(
                    prefix=f"{safe_prefix}-",
                    dir=transaction_root,
                )
            )
            tx_source = tx_root / "source"

            if kind == "pack":
                real_source = real_root / "source"
                if not real_source.is_dir():
                    raise HuroshikiError(
                        f"Missing Packwiz source directory: {real_source}"
                    )
                ensure_safe_pack_source(real_source)
                verified_config = pack_config_snapshot(real_root)
                verified_baseline = tree_digest_snapshot(real_source)
                copy_transaction_source(real_source, tx_source)
                if (
                    tree_digest_snapshot(real_source) != verified_baseline
                    or tree_digest_snapshot(tx_source) != verified_baseline
                    or pack_config_snapshot(real_root) != verified_config
                ):
                    raise HuroshikiError(
                        "The pack source or configuration changed while the transaction "
                        "copy was being created; retry the operation."
                    )
                transaction = cls(
                    project_key=project_key_value,
                    root=tx_root,
                    source=tx_source,
                    baseline=metadata_digest_snapshot(tx_source),
                    baseline_contents=metadata_content_snapshot(tx_source),
                    real_source_baseline=verified_baseline,
                    pack_config_baseline=verified_config,
                )
            else:
                verified_config = template_config_snapshot(real_root)
                config = packctl.load_template_config(project_id)
                minecraft, loader, loader_version = packctl.template_versions(
                    project_id
                )
                create_resolver_source(
                    tx_source,
                    display_name=str(config.get("display_name", project_id)),
                    minecraft=minecraft,
                    loader=loader,
                    loader_version=loader_version,
                )
                if template_config_snapshot(real_root) != verified_config:
                    raise HuroshikiError(
                        "The template configuration changed while the transaction "
                        "resolver was being created; retry the operation."
                    )
                transaction = cls(
                    project_key=project_key_value,
                    root=tx_root,
                    source=tx_source,
                    baseline={},
                    baseline_contents={},
                    template_config_baseline=verified_config,
                )
            transaction._project_lock = project_lock
            return transaction
        except BaseException:
            if tx_root is not None:
                shutil.rmtree(tx_root, ignore_errors=True)
            project_lock.release()
            raise

    def _finish_state(self) -> None:
        try:
            (self.root / ".completed").touch(exist_ok=True)
        except OSError:
            pass
        if self._project_lock is not None:
            self._project_lock.release()
            self._project_lock = None

    def __del__(self) -> None:
        project_lock = getattr(self, "_project_lock", None)
        if project_lock is not None:
            project_lock.release()
            self._project_lock = None

    def ensure_active(self) -> None:
        if not self.active or not self.source.is_dir():
            raise HuroshikiError("This transaction is no longer active")

    def begin_add(
        self,
        provider: str,
        query: str,
        *,
        client: bool,
        server: bool,
        on_event: Callable[[ParserEvent], None] | None = None,
    ) -> PackwizAddOperation:
        self.ensure_active()
        side_from_flags(client, server)
        with self._lock:
            if self._operation is not None and not self._operation.done.is_set():
                raise HuroshikiError("Another Packwiz search is already running")
            operation = PackwizAddOperation(
                self,
                provider,
                query,
                client=client,
                server=server,
                on_event=on_event,
            )
            self._operation = operation
            return operation

    def add(self, provider: str, query: str) -> subprocess.CompletedProcess[str]:
        """Compatibility path for synchronous add callers."""
        self.ensure_active()
        provider, query = normalize_add_selector(provider, query)
        if provider == "url":
            operation = self.begin_add(
                provider,
                query,
                client=True,
                server=True,
            )
            result = operation.run()
            return subprocess.CompletedProcess(
                ["huroshiki", "url", "add", query],
                result.returncode,
                "",
                "" if result.success else result.message,
            )
        checkpoint = self.root / f"checkpoint-{uuid4().hex}"
        copy_transaction_source(self.source, checkpoint)
        before = metadata_digest_snapshot(self.source)
        result = subprocess.run(
            build_add_command(provider, query), cwd=self.source, text=True, check=False
        )
        if result.returncode != 0:
            shutil.rmtree(self.source, ignore_errors=True)
            checkpoint.rename(self.source)
            return result
        try:
            ensure_safe_pack_source(self.source)
        except BaseException:
            shutil.rmtree(self.source, ignore_errors=True)
            checkpoint.rename(self.source)
            raise
        changed = tuple(
            sorted(changed_paths(before, metadata_digest_snapshot(self.source)))
        )
        if not changed:
            shutil.rmtree(self.source, ignore_errors=True)
            checkpoint.rename(self.source)
            return result
        self.batches.append(
            TransactionBatch(provider=provider, query=query, changed_files=changed)
        )
        shutil.rmtree(checkpoint, ignore_errors=True)
        return result

    def add_mod_transactionally(self, provider: str, selector: str, side: str) -> int:
        """Run one synchronous add entirely in staging, then atomically apply it."""
        self.ensure_active()
        kind, _ = split_project_key(self.project_key)
        if kind != "pack":
            raise HuroshikiError("Synchronous add can only modify MODPACK projects")
        try:
            normalized_side = packctl.normalize_side(side)
        except packctl.ConfigError as error:
            raise HuroshikiError(str(error)) from error
        provider, selector = normalize_add_selector(provider, selector)
        ensure_safe_pack_source(self.source)
        before = metadata_digest_snapshot(self.source)

        if provider == "url":
            client, server = flags_from_side(normalized_side)
            result = self.begin_add(
                provider,
                selector,
                client=client,
                server=server,
            ).run()
            if not result.success:
                if result.returncode == 0:
                    raise HuroshikiError(result.message)
                return result.returncode or 1
        else:
            command = build_add_command(provider, selector)
            result = subprocess.run(
                command,
                cwd=self.source,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return result.returncode
            ensure_safe_pack_source(self.source)
            changed = self._classify_add_changes(before, normalized_side)
            self.batches.append(
                TransactionBatch(
                    provider=provider,
                    query=selector,
                    changed_files=changed,
                )
            )

        self.apply()
        return 0

    def _classify_add_changes(
        self,
        before: dict[Path, str],
        side: str,
    ) -> tuple[Path, ...]:
        ensure_safe_pack_source(self.source)
        changed = tuple(
            sorted(changed_paths(before, metadata_digest_snapshot(self.source)))
        )
        metadata_paths = tuple(
            relative_path
            for relative_path in changed
            if (self.source / relative_path).is_file()
            and relative_path.name.endswith(".pw.toml")
        )
        if not metadata_paths:
            raise HuroshikiError(
                "Packwiz did not create or modify any .pw.toml files. "
                "The project may already be installed."
            )
        baseline_by_path: dict[Path, ModInfo] = {}
        baseline_by_identity: dict[tuple[str, str], ModInfo] = {}
        for baseline_path, contents in self.baseline_contents.items():
            try:
                baseline_mod = read_mod_data(
                    baseline_path,
                    tomllib.loads(contents.decode("utf-8")),
                )
            except (UnicodeError, tomllib.TOMLDecodeError) as error:
                raise HuroshikiError(
                    f"Could not preserve baseline side for {baseline_path}: {error}"
                ) from error
            baseline_by_path[baseline_path] = baseline_mod
            identity = (canonical_provider(baseline_mod.provider), baseline_mod.project_id)
            if identity[0] in {"modrinth", "curseforge", "url"} and identity[1]:
                baseline_by_identity[identity] = baseline_mod

        for relative_path in metadata_paths:
            try:
                current = read_mod(self.source, relative_path)
                baseline_mod = baseline_by_path.get(relative_path)
                identity = (canonical_provider(current.provider), current.project_id)
                identity_match = baseline_by_identity.get(identity)
                if baseline_mod is None:
                    baseline_mod = identity_match
                elif identity_match is not None and identity_match.side != baseline_mod.side:
                    raise HuroshikiError(
                        f"Conflicting baseline side identity for {relative_path}"
                    )
                assigned_side = side
                if baseline_mod is not None:
                    if baseline_mod.side_error is not None:
                        raise HuroshikiError(
                            f"Cannot preserve invalid baseline side for "
                            f"{baseline_mod.relative_path}: {baseline_mod.side_error}"
                        )
                    assigned_side = union_side(baseline_mod.side, side)
                packctl.set_side_file(self.source / relative_path, assigned_side)
            except Exception as error:
                raise HuroshikiError(
                    f"Could not assign side to {relative_path}: {error}"
                ) from error
        return metadata_paths

    def _finish_add(
        self,
        operation: PackwizAddOperation,
        pty_result: PtyResult,
    ) -> AddOperationResult:
        with self._lock:
            if not self.active or self._operation is not operation:
                self._rollback_add(operation)
                return AddOperationResult(
                    returncode=pty_result.returncode or 1,
                    changed_files=(),
                    raw_log=pty_result.raw_log,
                    text_log=pty_result.text_log,
                    event_log=pty_result.event_log,
                    message="Transaction was closed before Packwiz completed",
                    cancelled=True,
                )

            ensure_safe_pack_source(self.source)
            changed = tuple(
                sorted(
                    changed_paths(
                        operation.before,
                        metadata_digest_snapshot(self.source),
                    )
                )
            )
            if pty_result.returncode != 0 or not changed:
                self._rollback_add(operation)
                reason = (
                    "Packwiz was cancelled or failed"
                    if pty_result.returncode != 0
                    else "Packwiz made no metadata changes"
                )
                return AddOperationResult(
                    returncode=pty_result.returncode,
                    changed_files=(),
                    raw_log=pty_result.raw_log,
                    text_log=pty_result.text_log,
                    event_log=pty_result.event_log,
                    message=reason,
                    cancelled=operation.cancelled,
                )

            changed = self._classify_add_changes(
                operation.before,
                side_from_flags(operation.client, operation.server),
            )

            self.batches.append(
                TransactionBatch(
                    provider=operation.provider,
                    query=operation.query,
                    changed_files=changed,
                )
            )
            shutil.rmtree(operation.checkpoint, ignore_errors=True)
            self._operation = None
            return AddOperationResult(
                returncode=0,
                changed_files=changed,
                raw_log=pty_result.raw_log,
                text_log=pty_result.text_log,
                event_log=pty_result.event_log,
                message=f"Staged {len(changed)} metadata file(s)",
            )

    def _finish_url_add(
        self,
        operation: PackwizAddOperation,
    ) -> AddOperationResult:
        try:
            artifact = download_url_artifact(
                operation.query,
                operation.cancel_event,
                operation.log_dir,
                project_info(self.project_key).loader,
                project_url_max_jar_size_bytes(self.project_key),
                allow_private_networks=project_url_allow_private_networks(
                    self.project_key
                ),
            )
            if operation.cancelled or operation.cancel_event.is_set():
                raise HuroshikiError("URL addition was cancelled")

            with self._lock:
                if not self.active or self._operation is not operation:
                    raise HuroshikiError(
                        "Transaction was closed before the URL download completed"
                    )
                relative_path = Path("mods") / f"{artifact.mod_id}.pw.toml"
                write_url_metadata(
                    self.source,
                    relative_path,
                    artifact,
                    side_from_flags(operation.client, operation.server),
                )
                ensure_safe_pack_source(self.source)
                changed = self._classify_add_changes(
                    operation.before,
                    side_from_flags(operation.client, operation.server),
                )
                if not changed:
                    raise HuroshikiError("The URL metadata is already current")
                self.batches.append(
                    TransactionBatch(
                        provider="url",
                        query=operation.query,
                        changed_files=changed,
                    )
                )
                shutil.rmtree(operation.checkpoint, ignore_errors=True)
                self._operation = None

            raw_log, text_log, event_log = url_log_paths(operation.log_dir)
            version = f" {artifact.version}" if artifact.version else ""
            return AddOperationResult(
                returncode=0,
                changed_files=changed,
                raw_log=raw_log,
                text_log=text_log,
                event_log=event_log,
                message=f"Staged {artifact.name}{version} from self-hosted URL",
            )
        except Exception as error:
            self._rollback_add(operation)
            ensure_url_error_log(operation.log_dir, str(error))
            raw_log, text_log, event_log = url_log_paths(operation.log_dir)
            return AddOperationResult(
                returncode=130 if operation.cancelled else 1,
                changed_files=(),
                raw_log=raw_log,
                text_log=text_log,
                event_log=event_log,
                message=str(error),
                cancelled=operation.cancelled,
            )

    def _rollback_add(self, operation: PackwizAddOperation) -> None:
        with self._lock:
            if operation.checkpoint.exists():
                shutil.rmtree(self.source, ignore_errors=True)
                operation.checkpoint.rename(self.source)
            if self._operation is operation:
                self._operation = None

    def staged_mods(self) -> list[ModInfo]:
        self.ensure_active()
        current = metadata_digest_snapshot(self.source)
        paths = sorted(changed_paths(self.baseline, current))
        return [
            read_mod(self.source, path)
            for path in paths
            if (self.source / path).exists()
        ]

    def set_side(self, relative_path: Path, client: bool, server: bool) -> None:
        self.ensure_active()
        side = side_from_flags(client, server)
        path = safe_child(self.source, relative_path)
        if not path.is_file() or not path.name.endswith(".pw.toml"):
            raise HuroshikiError(f"Unknown metadata file: {relative_path}")
        packctl.set_side_file(path, side)

    def unstage(self, relative_path: Path) -> None:
        """Remove one selected metadata change from the transaction.

        Newly added metadata is deleted. Metadata that existed when the
        transaction started is restored byte-for-byte to its original state.
        index.toml and pack.toml are reconciled by the normal refresh performed
        when a MODPACK transaction is applied.
        """
        self.ensure_active()
        with self._lock:
            if self._operation is not None and not self._operation.done.is_set():
                raise HuroshikiError(
                    "Wait for the active Packwiz search to finish"
                )

            current = metadata_digest_snapshot(self.source)
            if relative_path not in changed_paths(self.baseline, current):
                raise HuroshikiError(
                    f"Metadata is not staged: {relative_path}"
                )

            path = safe_child(self.source, relative_path)
            if not path.is_file() or not path.name.endswith(".pw.toml"):
                raise HuroshikiError(
                    f"Unknown staged metadata file: {relative_path}"
                )

            if relative_path in self.baseline_contents:
                temporary = path.with_name(
                    f".{path.name}.huroshiki-unstage-{uuid4().hex}"
                )
                temporary.write_bytes(self.baseline_contents[relative_path])
                temporary.replace(path)
            else:
                path.unlink()
                current_parent = path.parent
                while current_parent != self.source:
                    try:
                        current_parent.rmdir()
                    except OSError:
                        break
                    current_parent = current_parent.parent

            remaining_batches: list[TransactionBatch] = []
            for batch in self.batches:
                remaining = tuple(
                    item for item in batch.changed_files
                    if item != relative_path
                )
                if remaining:
                    remaining_batches.append(
                        TransactionBatch(
                            provider=batch.provider,
                            query=batch.query,
                            changed_files=remaining,
                        )
                    )
            self.batches = remaining_batches

    def prepare_updates(self) -> list[UpdateCandidate]:
        self.ensure_active()
        kind, _ = split_project_key(self.project_key)
        if kind != "pack":
            raise HuroshikiError(
                "Template entries resolve compatible versions during MODPACK creation"
            )
        if tree_digest_snapshot(self.source) != self.real_source_baseline:
            raise HuroshikiError(
                "Updates must be prepared before other transaction changes are staged"
            )
        candidates = _prepare_update_candidates(
            self.source,
            self.root,
            self.baseline_contents,
        )
        self.update_candidates = tuple(candidates)
        return candidates

    def select_updates(self, selected_paths: Iterable[Path]) -> None:
        self.ensure_active()
        selected = set(selected_paths)
        available = {
            candidate.root: candidate
            for candidate in self.update_candidates
            if candidate.available
        }
        unknown = selected - available.keys()
        if unknown:
            raise HuroshikiError(
                f"Unknown update selection: {', '.join(map(str, sorted(unknown)))}"
            )
        for change in reversed(self.selected_update_changes):
            _apply_update_change(self.source, change, use_after=False)
        merged = _merge_update_closures(
            available[path] for path in sorted(selected)
        )
        for change in merged:
            _apply_update_change(self.source, change, use_after=True)
        self.selected_update_changes = merged

    def remove_mods(self, slugs: Iterable[str]) -> int:
        self.ensure_active()
        selected = set(slugs)
        kind, project_id = split_project_key(self.project_key)
        if kind == "template":
            config = packctl.load_yaml(project_root(self.project_key) / "template.yaml")
            raw_mods = config.get("mods", [])
            if not isinstance(raw_mods, list):
                raise HuroshikiError(
                    f"templates/{project_id}/template.yaml mods must be a list"
                )
            selected_indexes = {
                index
                for value in selected
                if (index := template_mod_raw_index(Path(value))) is not None
            }
            legacy_slugs = {
                value
                for value in selected
                if template_mod_raw_index(Path(value)) is None
            }
            for index, entry in packctl.template_mods_indexed(
                project_id,
                allow_invalid_sides=True,
                deduplicate=False,
            ):
                slug = f"{canonical_provider(entry['provider'])}-{entry['project_id']}"
                if slug in legacy_slugs:
                    selected_indexes.add(index)
            self.template_manifest = [
                entry
                for index, entry in enumerate(raw_mods)
                if index not in selected_indexes
            ]
            return 0

        for slug in sorted(selected):
            ensure_safe_pack_source(self.source)
            result = subprocess.run(
                ["packwiz", "remove", slug],
                cwd=self.source,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return result.returncode
            ensure_safe_pack_source(self.source)
        return 0

    def apply(self) -> None:
        self.ensure_active()
        with self._lock:
            if self._operation is not None and not self._operation.done.is_set():
                raise HuroshikiError("Wait for the active Packwiz search to finish")

        kind, project_id = split_project_key(self.project_key)
        if kind == "template":
            real_root = project_root(self.project_key)
            if template_config_snapshot(real_root) != self.template_config_baseline:
                raise HuroshikiError(
                    "The template configuration changed while this transaction was open. "
                    "Discard the staged transaction and retry."
                )
            if self.template_manifest is not None:
                packctl.save_template_mods_raw(project_id, self.template_manifest)
                self._finish_state()
                shutil.rmtree(self.root, ignore_errors=True)
                self.active = False
                return
            existing = packctl.template_mods(project_id)
            merged: dict[tuple[str, str], dict[str, str]] = {
                (item["provider"], item["project_id"]): dict(item)
                for item in existing
            }
            for mod in self.staged_mods():
                provider = canonical_provider(mod.provider)
                if not mod.project_id or provider not in {
                    "modrinth",
                    "curseforge",
                    "url",
                }:
                    continue
                entry = {
                    "name": mod.name,
                    "provider": provider,
                    "project_id": mod.project_id,
                    "side": mod.side,
                }
                if provider == "url":
                    if not mod.source_url:
                        continue
                    entry["url"] = mod.source_url
                merged[(provider, mod.project_id)] = entry
            packctl.save_template_mods(project_id, list(merged.values()))
            self._finish_state()
            shutil.rmtree(self.root, ignore_errors=True)
            self.active = False
            return

        ensure_safe_pack_source(self.source)
        refresh = subprocess.run(
            ["packwiz", "refresh"],
            cwd=self.source,
            text=True,
            check=False,
        )
        if refresh.returncode != 0:
            raise HuroshikiError("packwiz refresh failed; transaction was not applied")
        ensure_safe_pack_source(self.source)

        real_root = project_root(self.project_key)
        real_source = real_root / "source"
        backup = self.root / "replaced-source"
        try:
            os.stat(backup, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HuroshikiError(f"Backup path already exists: {backup}")

        staged_fd, staged_metadata = _open_pinned_source(self.source)
        original_fd = -1
        try:
            staged_issues = packctl.pack_source_fd_entry_issues(staged_fd)
            if staged_issues:
                details = "; ".join(
                    f"{relative}: {message}" for relative, message in staged_issues
                )
                raise HuroshikiError(f"Unsafe staged Packwiz source: {details}")
            original_fd, original_metadata = _open_pinned_source(real_source)
            if (
                _source_fd_snapshot(original_fd) != self.real_source_baseline
                or pack_config_snapshot(real_root) != self.pack_config_baseline
            ):
                raise HuroshikiError(
                    "The real Packwiz source changed or pack configuration changed while "
                    "this transaction was open. Discard the staged transaction and retry "
                    "to avoid overwriting external changes."
                )
            real_source.rename(backup)
            backup_metadata = os.stat(backup, follow_symlinks=False)
            if (
                not _same_entry(original_metadata, backup_metadata)
                or tree_digest_snapshot(backup) != self.real_source_baseline
                or pack_config_snapshot(real_root) != self.pack_config_baseline
            ):
                _restore_source_backup(real_source, backup, original_metadata)
                raise HuroshikiError(
                    "The real Packwiz source changed or pack configuration changed while "
                    "this transaction was open. Discard the staged transaction and retry "
                    "to avoid overwriting external changes."
                )
            current_staged = os.stat(self.source, follow_symlinks=False)
            if not _same_entry(staged_metadata, current_staged):
                _restore_source_backup(real_source, backup, original_metadata)
                raise HuroshikiError(
                    "The staged Packwiz source was replaced before publication; the "
                    "original source was restored."
                )
            self.source.rename(real_source)
            try:
                installed = os.stat(real_source, follow_symlinks=False)
            except OSError as error:
                _rollback_source_publication(
                    real_source,
                    backup,
                    staged_metadata,
                    original_metadata,
                    self.root,
                )
                raise HuroshikiError(
                    f"The staged Packwiz source was not installed safely: {error}"
                ) from error
            if not _same_entry(staged_metadata, installed):
                _rollback_source_publication(
                    real_source,
                    backup,
                    staged_metadata,
                    original_metadata,
                    self.root,
                )
                raise HuroshikiError(
                    "The installed Packwiz source was replaced during publication"
                )
        except BaseException as swap_error:
            try:
                backup_present = os.stat(backup, follow_symlinks=False)
            except FileNotFoundError:
                backup_present = None
            if backup_present is not None:
                try:
                    _rollback_source_publication(
                        real_source,
                        backup,
                        staged_metadata,
                        original_metadata,
                        self.root,
                    )
                except HuroshikiError as rollback_error:
                    raise rollback_error from swap_error
            raise
        finally:
            if original_fd >= 0:
                os.close(original_fd)
            os.close(staged_fd)

        self.active = False
        self._finish_state()

    def discard(self) -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
            operation = self._operation
        if operation is not None and not operation.done.is_set():
            operation.cancel()
            if not operation.wait(3.0):
                threading.Thread(
                    target=self._finish_discard_after_operation,
                    args=(operation,),
                    daemon=True,
                    name=f"huroshiki-discard-{self.project_key}",
                ).start()
                return
        self._finish_discard()

    def _finish_discard_after_operation(self, operation: PackwizAddOperation) -> None:
        operation.wait()
        self._finish_discard()

    def _finish_discard(self) -> None:
        self._finish_state()
        if not (self.root / "replaced-source").exists():
            shutil.rmtree(self.root, ignore_errors=True)


def create_resolver_source(
    source: Path,
    *,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "mods").mkdir()
    def quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'

    pack_text = (
        f'name = {quote(display_name)}\n'
        'author = "huroshiki"\n'
        'version = "0.0.0"\n'
        'pack-format = "packwiz:1.1.0"\n\n'
        '[index]\n'
        'file = "index.toml"\n'
        'hash-format = "sha256"\n'
        'hash = "resolver"\n\n'
        '[versions]\n'
        f'minecraft = {quote(minecraft)}\n'
        f'{loader} = {quote(loader_version)}\n'
    )
    (source / "pack.toml").write_text(pack_text, encoding="utf-8")
    (source / "index.toml").write_text(
        'hash-format = "sha256"\n', encoding="utf-8"
    )


def canonical_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized in {"mr", "modrinth"}:
        return "modrinth"
    if normalized in {"cf", "curseforge"}:
        return "curseforge"
    if normalized in {"u", "url", "selfhost", "self-hosted"}:
        return "url"
    return normalized


def build_add_command(provider: str, query: str) -> list[str]:
    if provider == "url":
        raise HuroshikiError("URL additions are handled by huroshiki")
    if provider == "curseforge" and query.isdecimal():
        return ["packwiz", "curseforge", "add", "--addon-id", query]
    return ["packwiz", provider, "add", query]


def normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {
        "m": "modrinth",
        "mr": "modrinth",
        "modrinth": "modrinth",
        "c": "curseforge",
        "cf": "curseforge",
        "curseforge": "curseforge",
        "u": "url",
        "url": "url",
        "selfhost": "url",
        "self-hosted": "url",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise HuroshikiError(f"Unsupported provider: {provider}") from error


def normalize_add_selector(provider: str, query: str) -> tuple[str, str]:
    normalized_provider = normalize_provider(provider)
    selector = query.strip()
    if not selector:
        raise HuroshikiError("Search query is empty")

    lowered = selector.lower()
    if lowered.startswith("mr:"):
        normalized_provider = "modrinth"
        selector = selector[3:].strip()
    elif lowered.startswith("cf:"):
        normalized_provider = "curseforge"
        selector = selector[3:].strip()
    elif "modrinth.com/" in lowered:
        normalized_provider = "modrinth"
    elif lowered.startswith("url:"):
        normalized_provider = "url"
        selector = selector[4:].strip()
    elif "curseforge.com/" in lowered:
        normalized_provider = "curseforge"

    if not selector:
        raise HuroshikiError("Project selector is empty")
    if normalized_provider == "url":
        validate_public_url(selector)
    return normalized_provider, selector


def url_max_jar_size_bytes(config: dict[str, object]) -> int:
    value = config.get(
        "url_max_jar_size_bytes",
        DEFAULT_URL_MAX_JAR_SIZE_BYTES,
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise packctl.ConfigError("url_max_jar_size_bytes must be a positive integer")
    return value


def project_url_max_jar_size_bytes(project_key_value: str) -> int:
    return url_max_jar_size_bytes(project_config(project_key_value))


def url_allow_private_networks(config: dict[str, object]) -> bool:
    value = config.get("url_allow_private_networks", False)
    if not isinstance(value, bool):
        raise packctl.ConfigError("url_allow_private_networks must be a boolean")
    return value


def project_url_allow_private_networks(project_key_value: str) -> bool:
    return url_allow_private_networks(project_config(project_key_value))


def safe_child(root: Path, relative: Path) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HuroshikiError(f"Path escaped root: {relative}")
    return candidate


TEMPLATE_TARGETS = OVERLAY_TARGETS


def normalize_template_target(target: str) -> str:
    normalized = target.strip().lower()
    if normalized not in TEMPLATE_TARGETS:
        raise HuroshikiError(
            "Template target must be common, client, or server"
        )
    return normalized


def normalize_template_relative_path(value: str | Path) -> Path:
    try:
        return normalize_overlay_relative_path(value)
    except OverlayPolicyError as error:
        raise HuroshikiError(str(error)) from error


def template_base(project_key_value: str, target: str) -> Path:
    normalized_target = normalize_template_target(target)
    return project_root(project_key_value) / "content" / normalized_target


def resolve_template_path(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> Path:
    relative = normalize_template_relative_path(relative_path)
    try:
        return safe_overlay_child(
            project_root(project_key_value) / "content",
            normalize_template_target(target),
            relative,
        )
    except OverlayPolicyError as error:
        raise HuroshikiError(str(error)) from error


def list_templates(project_key_value: str) -> list[TemplateInfo]:
    root = project_root(project_key_value)
    templates: list[TemplateInfo] = []
    scan = scan_content_overlays(root / "content")
    issue_by_path: dict[Path, list[str]] = {}
    for issue in scan.issues:
        issue_by_path.setdefault(issue.relative_path, []).append(issue.message)
    for entry in scan.entries:
        if entry.relative_path.name == ".gitkeep" or entry.kind == "directory":
            continue
        if entry.relative_path == Path("."):
            templates.append(
                TemplateInfo(
                    target="content",
                    relative_path=Path("."),
                    full_path=root / "content",
                    size=entry.size,
                    error="; ".join(issue_by_path[entry.relative_path]),
                )
            )
            continue
        target = entry.relative_path.parts[0]
        relative = Path(*entry.relative_path.parts[1:])
        templates.append(
            TemplateInfo(
                target=target,
                relative_path=relative,
                full_path=root / "content" / entry.relative_path,
                size=entry.size,
                error="; ".join(issue_by_path.get(entry.relative_path, ())) or None,
            )
        )
    return templates


def filter_templates(
    templates: Iterable[TemplateInfo],
    query: str,
) -> list[TemplateInfo]:
    needle = query.strip().casefold()
    if not needle:
        return list(templates)
    return [
        template
        for template in templates
        if needle in f"{template.target} {template.relative_path}".casefold()
    ]


def create_template(
    project_key_value: str,
    target: str,
    relative_path: str,
) -> TemplateInfo:
    try:
        with packctl.ProjectLock(project_key_value, "create template file"):
            normalized_target = normalize_template_target(target)
            relative = normalize_template_relative_path(relative_path)
            try:
                create_overlay_file(
                    project_root(project_key_value) / "content",
                    normalized_target,
                    relative,
                )
            except OverlayPolicyError as error:
                raise HuroshikiError(str(error)) from error
            return TemplateInfo(
                target=normalized_target,
                relative_path=relative,
                full_path=template_base(project_key_value, normalized_target) / relative,
                size=0,
            )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def read_template_text(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> str:
    try:
        return read_overlay_text(
            project_root(project_key_value) / "content",
            normalize_template_target(target),
            normalize_template_relative_path(relative_path),
        )
    except OverlayPolicyError as error:
        raise HuroshikiError(str(error)) from error
    except UnicodeDecodeError as error:
        raise HuroshikiError(
            f"Template file is not UTF-8 text: {target}/{relative_path}"
        ) from error


def write_template_text(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
    text: str,
) -> None:
    try:
        with packctl.ProjectLock(project_key_value, "write template file"):
            try:
                write_overlay_text(
                    project_root(project_key_value) / "content",
                    normalize_template_target(target),
                    normalize_template_relative_path(relative_path),
                    text,
                )
            except OverlayPolicyError as error:
                raise HuroshikiError(str(error)) from error
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def delete_template(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> None:
    try:
        with packctl.ProjectLock(project_key_value, "delete template file"):
            try:
                delete_overlay_file(
                    project_root(project_key_value) / "content",
                    normalize_template_target(target),
                    normalize_template_relative_path(relative_path),
                )
            except OverlayPolicyError as error:
                raise HuroshikiError(str(error)) from error
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SOURCE_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
_SOURCE_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _rename_noreplace(source: Path, destination: Path) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "collision-safe rename is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "collision-safe rename is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _open_pinned_source(source: Path) -> tuple[int, os.stat_result]:
    try:
        metadata = os.stat(source, follow_symlinks=False)
        source_fd = os.open(source, _SOURCE_DIRECTORY_FLAGS)
    except OSError as error:
        raise HuroshikiError(f"Unsafe Packwiz source {source}: {error}") from error
    try:
        opened = os.fstat(source_fd)
    except BaseException:
        os.close(source_fd)
        raise
    if not stat.S_ISDIR(metadata.st_mode) or not _same_entry(metadata, opened):
        os.close(source_fd)
        raise HuroshikiError(
            f"Unsafe Packwiz source {source}: source was replaced while opening"
        )
    return source_fd, opened


def _digest_fd(file_fd: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(file_fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _source_fd_snapshot(
    directory_fd: int,
    relative: Path = Path("."),
) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    try:
        with os.scandir(directory_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as error:
        raise HuroshikiError(
            f"Unsafe Packwiz source at {relative}: could not list directory: {error}"
        ) from error
    for name in names:
        item_relative = relative / name
        if item_relative.parts[0] == ".":
            item_relative = Path(*item_relative.parts[1:])
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: {error}"
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = os.open(name, _SOURCE_DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
                ) from error
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(metadata, opened):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                    )
                snapshot[item_relative] = "directory"
                snapshot.update(_source_fd_snapshot(child_fd, item_relative))
                current = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_entry(opened, current):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while scanning"
                    )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            kind = "symlink" if stat.S_ISLNK(metadata.st_mode) else "special entry"
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: {kind} is not allowed"
            )
        try:
            file_fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=directory_fd)
        except OSError as error:
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
            ) from error
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_entry(metadata, opened):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                )
            snapshot[item_relative] = _digest_fd(file_fd)
            after_read = os.fstat(file_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not _same_entry(opened, current)
                or opened.st_size != after_read.st_size
                or opened.st_mtime_ns != after_read.st_mtime_ns
                or opened.st_ctime_ns != after_read.st_ctime_ns
            ):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while reading"
                )
        finally:
            os.close(file_fd)
    return snapshot


def _copy_source_fd(
    source_fd: int,
    destination_fd: int,
    relative: Path = Path("."),
) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    with os.scandir(source_fd) as iterator:
        names = sorted(entry.name for entry in iterator)
    for name in names:
        item_relative = relative / name
        if item_relative.parts[0] == ".":
            item_relative = Path(*item_relative.parts[1:])
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = os.open(name, _SOURCE_DIRECTORY_FLAGS, dir_fd=source_fd)
            except OSError as error:
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
                ) from error
            output_fd = -1
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(metadata, opened):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                    )
                source_mode = stat.S_IMODE(opened.st_mode)
                os.mkdir(name, source_mode | stat.S_IRWXU, dir_fd=destination_fd)
                output_fd = os.open(name, _SOURCE_DIRECTORY_FLAGS, dir_fd=destination_fd)
                snapshot[item_relative] = "directory"
                snapshot.update(_copy_source_fd(child_fd, output_fd, item_relative))
                current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or not _same_entry(opened, current):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while copying"
                    )
                current_output = os.stat(
                    name, dir_fd=destination_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(current_output.st_mode) or not _same_entry(
                    os.fstat(output_fd), current_output
                ):
                    raise HuroshikiError(
                        f"Transaction destination at {item_relative} was replaced"
                    )
                os.fchmod(output_fd, source_mode)
            finally:
                if output_fd >= 0:
                    os.close(output_fd)
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            kind = "symlink" if stat.S_ISLNK(metadata.st_mode) else "special entry"
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: {kind} is not allowed"
            )
        try:
            file_fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=source_fd)
        except OSError as error:
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
            ) from error
        output_fd = -1
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_entry(metadata, opened):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                )
            output_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                stat.S_IMODE(opened.st_mode),
                dir_fd=destination_fd,
            )
            digest = hashlib.sha256()
            while chunk := os.read(file_fd, 1024 * 1024):
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    view = view[written:]
            os.fchmod(output_fd, stat.S_IMODE(opened.st_mode))
            snapshot[item_relative] = digest.hexdigest()
            after_read = os.fstat(file_fd)
            current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            current_output = os.stat(
                name, dir_fd=destination_fd, follow_symlinks=False
            )
            if (
                not _same_entry(opened, current)
                or opened.st_size != after_read.st_size
                or opened.st_mtime_ns != after_read.st_mtime_ns
                or opened.st_ctime_ns != after_read.st_ctime_ns
            ):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while copying"
                )
            if not stat.S_ISREG(current_output.st_mode) or not _same_entry(
                os.fstat(output_fd), current_output
            ):
                raise HuroshikiError(
                    f"Transaction destination at {item_relative} was replaced"
                )
        finally:
            if output_fd >= 0:
                os.close(output_fd)
            os.close(file_fd)
    return snapshot


def copy_transaction_source(source: Path, destination: Path) -> None:
    source_fd, source_metadata = _open_pinned_source(source)
    parent_fd = destination_fd = -1
    try:
        issues = packctl.pack_source_fd_entry_issues(source_fd)
        if issues:
            details = "; ".join(f"{relative}: {message}" for relative, message in issues)
            raise HuroshikiError(f"Unsafe Packwiz source {source}: {details}")
        parent_fd = os.open(destination.parent, _SOURCE_DIRECTORY_FLAGS)
        os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
        destination_fd = os.open(
            destination.name, _SOURCE_DIRECTORY_FLAGS, dir_fd=parent_fd
        )
        copied = _copy_source_fd(source_fd, destination_fd)
        try:
            current = os.stat(source, follow_symlinks=False)
            current_destination = os.stat(
                destination.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError as error:
            raise HuroshikiError(
                f"A transaction source entry was replaced while copying: {error}"
            ) from error
        if not _same_entry(source_metadata, current):
            raise HuroshikiError(
                "The pack source was replaced while the transaction copy was being created"
            )
        if not _same_entry(os.fstat(destination_fd), current_destination):
            raise HuroshikiError(
                "The transaction destination was replaced while the copy was being created"
            )
        if (
            _source_fd_snapshot(source_fd) != copied
            or _source_fd_snapshot(destination_fd) != copied
        ):
            raise HuroshikiError(
                "The pack source changed while the transaction copy was being created"
            )
        os.fchmod(destination_fd, stat.S_IMODE(source_metadata.st_mode))
    except BaseException:
        if destination_fd >= 0:
            os.close(destination_fd)
            destination_fd = -1
        if parent_fd >= 0:
            os.close(parent_fd)
            parent_fd = -1
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(source_fd)


def ensure_safe_pack_source(source: Path) -> None:
    issues = packctl.pack_source_entry_issues(source)
    if not issues:
        return
    details = "; ".join(
        f"{source if relative == Path('.') else source / relative}: {message}"
        for relative, message in issues
    )
    raise HuroshikiError(f"Unsafe Packwiz source: {details}")


def _restore_source_backup(
    real_source: Path,
    backup: Path,
    expected_backup: os.stat_result,
) -> None:
    try:
        backup_metadata = os.stat(backup, follow_symlinks=False)
    except OSError as error:
        raise HuroshikiError(
            f"Cannot restore the exact original Packwiz source from {backup}: {error}"
        ) from error
    if not _same_entry(expected_backup, backup_metadata):
        raise HuroshikiError(
            f"Cannot restore the exact original Packwiz source because {backup} was replaced"
        )
    try:
        os.stat(real_source, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise HuroshikiError(
            f"Cannot restore the original Packwiz source because {real_source} was "
            f"recreated externally; external changes were preserved and the exact "
            f"original remains at {backup}."
        )
    try:
        backup.rename(real_source)
    except BaseException as error:
        raise HuroshikiError(
            f"Failed to restore the original Packwiz source from {backup}: {error}"
        ) from error


def _rollback_source_publication(
    real_source: Path,
    backup: Path,
    expected_installed_staged: os.stat_result,
    original_metadata: os.stat_result,
    transaction_root: Path,
) -> None:
    try:
        current = os.stat(real_source, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    except OSError as error:
        raise HuroshikiError(
            f"Cannot inspect the published Packwiz source during rollback: {error}; "
            f"the exact original remains at {backup}."
        ) from error

    if current is not None and _same_entry(current, expected_installed_staged):
        failed_staged = transaction_root / "failed-staged-source"
        try:
            os.stat(failed_staged, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HuroshikiError(
                f"Cannot retain the failed staged source because {failed_staged} exists; "
                f"the exact original remains at {backup}."
            )
        rename_error: BaseException | None = None
        try:
            real_source.rename(failed_staged)
        except BaseException as error:
            rename_error = error
        try:
            moved = os.stat(failed_staged, follow_symlinks=False)
        except OSError:
            moved = None
        if moved is None:
            raise HuroshikiError(
                "Failed to move the staged Packwiz source out of publication; "
                f"the exact original remains at {backup}: {rename_error}"
            ) from rename_error
        if not _same_entry(moved, expected_installed_staged):
            try:
                _rename_noreplace(failed_staged, real_source)
            except OSError as restore_error:
                raise HuroshikiError(
                    "The installed staged Packwiz source was replaced during rollback; "
                    f"external entries at {real_source} or {failed_staged} were "
                    f"preserved, and the exact original remains at {backup}: "
                    f"{restore_error}"
                ) from restore_error
            raise HuroshikiError(
                "The installed staged Packwiz source was replaced during rollback; "
                f"the moved external source was restored to {real_source}, the exact "
                f"original remains at {backup}, and competing entries were retained."
            ) from rename_error
        current = None

    if current is not None:
        raise HuroshikiError(
            f"Cannot restore the original Packwiz source because {real_source} was "
            f"recreated externally; external changes were preserved and the exact "
            f"original remains at {backup}."
        )
    _restore_source_backup(real_source, backup, original_metadata)


def template_config_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ("template.yaml", "template.local.yaml"):
        path = root / name
        snapshot[name] = file_digest(path) if path.is_file() else "missing"
    return snapshot


def pack_config_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ("pack.yaml", "pack.local.yaml"):
        path = root / name
        snapshot[name] = file_digest(path) if path.is_file() else "missing"
    return snapshot


def tree_digest_snapshot(source: Path) -> dict[Path, str]:
    source_fd, source_metadata = _open_pinned_source(source)
    try:
        snapshot = _source_fd_snapshot(source_fd)
        current = os.stat(source, follow_symlinks=False)
        if not _same_entry(source_metadata, current):
            raise HuroshikiError(
                f"Unsafe Packwiz source {source}: source was replaced while scanning"
            )
        return snapshot
    finally:
        os.close(source_fd)


def metadata_digest_snapshot(source: Path) -> dict[Path, str]:
    return {
        path.relative_to(source): file_digest(path)
        for path in sorted(source.rglob("*.pw.toml"))
        if path.is_file()
    }


def metadata_content_snapshot(source: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(source): path.read_bytes()
        for path in sorted(source.rglob("*.pw.toml"))
        if path.is_file()
    }


def changed_paths(
    before: dict[Path, str],
    after: dict[Path, str],
) -> set[Path]:
    return {
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }


def side_from_flags(client: bool, server: bool) -> str:
    if client and server:
        return "both"
    if client:
        return "client"
    if server:
        return "server"
    raise HuroshikiError("A mod must be enabled on the client, server, or both")


def flags_from_side(side: object) -> tuple[bool, bool]:
    if side == "both":
        return True, True
    if side == "client":
        return True, False
    if side == "server":
        return False, True
    return False, False


def pack_versions(source: Path) -> tuple[str, str, str]:
    pack_file = source / "pack.toml"
    if not pack_file.exists():
        return "", "", ""
    try:
        data = tomllib.loads(pack_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"{pack_file}: {error}") from error
    versions = data.get("versions", {})
    if not isinstance(versions, dict):
        return "", "", ""
    minecraft = str(versions.get("minecraft", ""))
    for loader in ("neoforge", "forge", "fabric", "quilt"):
        value = versions.get(loader)
        if value is not None:
            return minecraft, loader, str(value)
    return minecraft, "", ""


def project_info(project_key_value: str) -> ProjectInfo:
    kind, project_id = split_project_key(project_key_value)
    parent = PACKS if kind == "pack" else TEMPLATES
    manifest = parent / project_id / (
        "pack.yaml" if kind == "pack" else "template.yaml"
    )
    fallback = ProjectInfo(
        kind=kind,
        project_id=project_id,
        display_name=project_id,
        minecraft="",
        loader="",
        loader_version="",
        enabled=False,
    )
    try:
        config = packctl.load_project_config(kind, project_id)
        if kind == "pack":
            minecraft, loader, loader_version = pack_versions(
                packctl.get_pack_root(project_id) / "source"
            )
        else:
            minecraft, loader, loader_version = packctl.template_versions(project_id)
        info = replace(
            fallback,
            display_name=str(config.get("display_name", project_id)),
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
            enabled=bool(config.get("enabled", True)),
        )
    except Exception as error:
        return replace(fallback, error=f"{manifest}: {error}")

    try:
        return replace(info, mod_count=len(list_mods(project_key_value)))
    except Exception as error:
        return replace(info, error=str(error))


def list_projects() -> list[ProjectInfo]:
    projects = [
        project_info(project_key("pack", pack_id))
        for pack_id in packctl.pack_ids()
    ]
    projects.extend(
        project_info(project_key("template", template_id))
        for template_id in packctl.template_ids()
    )
    return sorted(projects, key=lambda item: (item.kind, item.display_name.casefold()))


def filter_projects(
    projects: Iterable[ProjectInfo],
    query: str,
) -> list[ProjectInfo]:
    needle = query.strip().casefold()
    if not needle:
        return list(projects)
    return [
        project
        for project in projects
        if needle
        in " ".join(
            (
                project.kind,
                project.project_id,
                project.display_name,
                project.minecraft,
                project.loader,
                project.loader_version,
            )
        ).casefold()
    ]


def provider_from_metadata(data: dict[str, object]) -> tuple[str, str]:
    update = data.get("update", {})
    if isinstance(update, dict):
        if "modrinth" in update:
            project = update.get("modrinth")
            return "MR", extract_project_id(project)
        if "curseforge" in update:
            project = update.get("curseforge")
            return "CF", extract_project_id(project)

    download = data.get("download", {})
    if isinstance(download, dict):
        mode = str(download.get("mode", ""))
        if "curseforge" in mode:
            return "CF", ""
    return "URL", ""


def extract_project_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in (
        "mod-id",
        "project-id",
        "project_id",
        "projectID",
        "projectId",
    ):
        if key in value:
            return str(value[key])
    return ""


def read_mod(source: Path, relative_path: Path) -> ModInfo:
    path = safe_child(source, relative_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"{path}: {error}") from error
    return read_mod_data(relative_path, data)


def metadata_version(data: dict[str, object], provider: str) -> str:
    update = data.get("update", {})
    if isinstance(update, dict):
        provider_data = update.get(canonical_provider(provider), {})
        if isinstance(provider_data, dict):
            for key in ("version", "version-id", "file-id", "fileId"):
                if key in provider_data:
                    return str(provider_data[key])
    filename = str(data.get("filename", ""))
    return filename or "unknown"


def metadata_is_pinned(data: dict[str, object]) -> bool:
    if data.get("pin") is True:
        return True
    update = data.get("update", {})
    return isinstance(update, dict) and update.get("pin") is True


@dataclass(frozen=True)
class _UpdateMetadata:
    relative_path: Path
    provider: str
    project_id: str
    filename: str
    contents: bytes

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.project_id


UPDATE_RESOLVER_TIMEOUT_SECONDS = 120
PACKWIZ_GENERATED_PATHS = {Path("index.toml"), Path("pack.toml")}


def _file_content_snapshot(source: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(source): path.read_bytes()
        for path in sorted(source.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _content_changes(
    before: Mapping[Path, bytes],
    after: Mapping[Path, bytes],
) -> tuple[UpdateChange, ...]:
    return tuple(
        UpdateChange(path, before.get(path), after.get(path))
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    )


def _update_metadata_record(relative_path: Path, contents: bytes) -> _UpdateMetadata:
    try:
        relative = portable_relative_path(relative_path)
        data = tomllib.loads(contents.decode("utf-8"))
        mod = read_mod_data(relative, data)
        provider = canonical_provider(mod.provider)
        if provider not in {"modrinth", "curseforge", "url"} or not mod.project_id:
            raise HuroshikiError(
                f"Update metadata {relative} has no stable provider/project identity"
            )
        filename = portable_basename(mod.filename, context="Metadata filename")
    except (PortablePathError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"Invalid update metadata {relative_path}: {error}") from error
    return _UpdateMetadata(
        relative,
        provider,
        mod.project_id,
        filename,
        contents,
    )


def _update_metadata_snapshot(source: Path) -> dict[tuple[str, str], _UpdateMetadata]:
    records: dict[tuple[str, str], _UpdateMetadata] = {}
    paths: dict[str, tuple[str, str]] = {}
    filenames: dict[str, tuple[str, str]] = {}
    for path in sorted(source.rglob("*.pw.toml")):
        if not path.is_file() or path.is_symlink():
            continue
        record = _update_metadata_record(path.relative_to(source), path.read_bytes())
        path_key = portable_relative_path_key(record.relative_path)
        filename_key = portable_basename_key(record.filename)
        if record.identity in records:
            previous = records[record.identity]
            raise HuroshikiError(
                f"Update resolver produced identity {record.provider}:{record.project_id} "
                f"at both {previous.relative_path} and {record.relative_path}"
            )
        path_owner = paths.get(path_key)
        if path_owner is not None and path_owner != record.identity:
            raise HuroshikiError(
                f"Update resolver produced portable metadata path collision at "
                f"{record.relative_path}"
            )
        filename_owner = filenames.get(filename_key)
        if filename_owner is not None and filename_owner != record.identity:
            raise HuroshikiError(
                f"Update resolver produced portable filename collision "
                f"{record.filename!r}"
            )
        records[record.identity] = record
        paths[path_key] = record.identity
        filenames[filename_key] = record.identity
    return records


def _candidate_error(
    root: Path,
    mod: ModInfo,
    data: dict[str, object],
    message: str,
    returncode: int | None = None,
) -> UpdateCandidate:
    provider = canonical_provider(mod.provider)
    return UpdateCandidate(
        key=f"{provider}:{mod.project_id}",
        root=root,
        slug=mod.slug,
        name=mod.name,
        provider=mod.provider,
        current_version=metadata_version(data, mod.provider),
        new_version="-",
        status="unavailable",
        error=message,
        error_returncode=returncode,
    )


def _prepare_update_candidates(
    source: Path,
    transaction_root: Path,
    baseline_contents: dict[Path, bytes],
) -> list[UpdateCandidate]:
    parsed: list[tuple[Path, bytes, dict[str, object], ModInfo]] = []
    slugs: dict[str, list[Path]] = {}
    for relative_path, original in sorted(baseline_contents.items()):
        old_data = tomllib.loads(original.decode("utf-8"))
        old_mod = read_mod_data(relative_path, old_data)
        parsed.append((relative_path, original, old_data, old_mod))
        slugs.setdefault(old_mod.slug, []).append(relative_path)
    ambiguous = {slug: paths for slug, paths in slugs.items() if len(paths) > 1}
    candidates: list[UpdateCandidate] = []
    eligible: list[tuple[Path, bytes, dict[str, object], ModInfo]] = []
    for relative_path, original, old_data, old_mod in parsed:
        provider = canonical_provider(old_mod.provider)
        key = f"{provider}:{old_mod.project_id}"
        common = dict(
            key=key,
            root=relative_path,
            slug=old_mod.slug,
            name=old_mod.name,
            provider=old_mod.provider,
            current_version=metadata_version(old_data, old_mod.provider),
        )
        if old_mod.slug in ambiguous:
            paths = ", ".join(str(path) for path in ambiguous[old_mod.slug])
            candidates.append(
                _candidate_error(
                    relative_path,
                    old_mod,
                    old_data,
                    f"ambiguous Packwiz update slug {old_mod.slug!r}; metadata exists at {paths}",
                )
            )
            continue
        if metadata_is_pinned(old_data):
            candidates.append(
                UpdateCandidate(**common, new_version="-", status="pinned")
            )
            continue
        if provider not in {"modrinth", "curseforge"} or not old_mod.project_id:
            candidates.append(
                UpdateCandidate(**common, new_version="-", status="unavailable")
            )
            continue
        eligible.append((relative_path, original, old_data, old_mod))

    if not eligible:
        return sorted(candidates, key=lambda item: item.root)

    resolver_root = transaction_root / "update-resolvers"
    resolver_root.mkdir()
    normalized = resolver_root / "normalized-source"
    normalization_returncode: int | None = None
    try:
        copy_transaction_source(source, normalized)
        try:
            normalization = subprocess.run(
                ["packwiz", "refresh"],
                cwd=normalized,
                text=True,
                capture_output=True,
                check=False,
                timeout=UPDATE_RESOLVER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            normalization_error = (
                "disposable baseline normalization deadline exceeded after "
                f"{UPDATE_RESOLVER_TIMEOUT_SECONDS} seconds"
            )
        else:
            normalization_returncode = normalization.returncode
            normalization_error = (
                None
                if normalization.returncode == 0
                else "disposable baseline normalization failed: "
                + concise_process_error(normalization)
            )
        if normalization_error is not None:
            for relative_path, _, old_data, old_mod in eligible:
                candidates.append(
                    _candidate_error(
                        relative_path,
                        old_mod,
                        old_data,
                        normalization_error,
                        normalization_returncode,
                    )
                )
            shutil.rmtree(resolver_root, ignore_errors=True)
            return sorted(candidates, key=lambda item: item.root)
        ensure_safe_pack_source(normalized)
        before_files = _file_content_snapshot(normalized)
        baseline_records = _update_metadata_snapshot(normalized)
    except (OSError, HuroshikiError) as error:
        message = f"disposable baseline normalization failed: {error}"
        for relative_path, _, old_data, old_mod in eligible:
            candidates.append(_candidate_error(relative_path, old_mod, old_data, message))
        shutil.rmtree(resolver_root, ignore_errors=True)
        return sorted(candidates, key=lambda item: item.root)

    for relative_path, original, old_data, old_mod in eligible:
        provider = canonical_provider(old_mod.provider)
        key = f"{provider}:{old_mod.project_id}"
        common = dict(
            key=key,
            root=relative_path,
            slug=old_mod.slug,
            name=old_mod.name,
            provider=old_mod.provider,
            current_version=metadata_version(old_data, old_mod.provider),
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix=f"{old_mod.slug}-", dir=resolver_root
            ) as directory:
                resolver = Path(directory) / "source"
                copy_transaction_source(normalized, resolver)
                try:
                    result = subprocess.run(
                        ["packwiz", "--yes", "update", old_mod.slug],
                        cwd=resolver,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=UPDATE_RESOLVER_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            f"resolver deadline exceeded after "
                            f"{UPDATE_RESOLVER_TIMEOUT_SECONDS} seconds",
                        )
                    )
                    continue
                if result.returncode != 0:
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            concise_process_error(result),
                            result.returncode,
                        )
                    )
                    continue
                ensure_safe_pack_source(resolver)
                resolved_records = _update_metadata_snapshot(resolver)
                changes = _content_changes(
                    before_files,
                    _file_content_snapshot(resolver),
                )
        except (OSError, HuroshikiError) as error:
            candidates.append(
                _candidate_error(relative_path, old_mod, old_data, str(error))
            )
            continue

        metadata_changed = any(
            change.relative_path.name.endswith(".pw.toml") for change in changes
        )
        if not metadata_changed:
            candidates.append(
                UpdateCandidate(**common, new_version="-", status="current")
            )
            continue
        resolved_root = resolved_records.get((provider, old_mod.project_id))
        new_version = (
            metadata_version(
                tomllib.loads(resolved_root.contents.decode("utf-8")),
                old_mod.provider,
            )
            if resolved_root is not None
            else "-"
        )
        added_dependencies = sum(
            identity not in baseline_records and identity != (provider, old_mod.project_id)
            for identity in resolved_records
        )
        candidates.append(
            UpdateCandidate(
                **common,
                new_version=new_version,
                status="update",
                changes=changes,
                added_dependencies=added_dependencies,
            )
        )
    shutil.rmtree(resolver_root)
    return sorted(candidates, key=lambda item: item.root)


def _metadata_semantics(record: _UpdateMetadata) -> dict[str, object]:
    document = tomllib.loads(record.contents.decode("utf-8"))
    document.pop("side", None)
    document["filename"] = portable_basename(
        str(document.get("filename", "")), context="Metadata filename"
    )
    return document


def _merge_metadata_records(
    existing: _UpdateMetadata,
    incoming: _UpdateMetadata,
) -> _UpdateMetadata:
    if (
        portable_relative_path_key(existing.relative_path)
        != portable_relative_path_key(incoming.relative_path)
        or portable_basename_key(existing.filename)
        != portable_basename_key(incoming.filename)
        or _metadata_semantics(existing) != _metadata_semantics(incoming)
    ):
        raise HuroshikiError(
            "Update closure metadata disagreement for shared identity "
            f"{incoming.provider}:{incoming.project_id}; dependency versions, paths, "
            "downloads, or update metadata differ"
        )
    existing_mod = read_mod_data(
        existing.relative_path,
        tomllib.loads(existing.contents.decode("utf-8")),
    )
    incoming_mod = read_mod_data(
        incoming.relative_path,
        tomllib.loads(incoming.contents.decode("utf-8")),
    )
    return replace(
        existing,
        contents=_metadata_contents_with_side(
            existing.contents,
            union_side(existing_mod.side, incoming_mod.side),
        ),
    )


def _merge_update_closures(
    candidates: Iterable[UpdateCandidate],
) -> tuple[UpdateChange, ...]:
    selected = tuple(candidates)
    if not selected:
        return ()
    baseline_by_identity: dict[tuple[str, str], _UpdateMetadata] = {}
    metadata_operations: dict[tuple[str, str], _UpdateMetadata | None] = {}
    other_operations: dict[str, UpdateChange] = {}

    for candidate in selected:
        before_records: dict[tuple[str, str], _UpdateMetadata] = {}
        after_records: dict[tuple[str, str], _UpdateMetadata] = {}
        for change in candidate.changes:
            if not change.relative_path.name.endswith(".pw.toml"):
                if change.relative_path in PACKWIZ_GENERATED_PATHS:
                    continue
                path_key = portable_relative_path_key(change.relative_path)
                previous = other_operations.get(path_key)
                if previous is not None and (
                    previous.relative_path != change.relative_path
                    or previous.before != change.before
                    or previous.after != change.after
                ):
                    raise HuroshikiError(
                        f"Update closure file disagreement at {change.relative_path}"
                    )
                other_operations[path_key] = change
                continue
            if change.before is not None:
                record = _update_metadata_record(change.relative_path, change.before)
                before_records[record.identity] = record
                baseline_by_identity.setdefault(record.identity, record)
            if change.after is not None:
                record = _update_metadata_record(change.relative_path, change.after)
                after_records[record.identity] = record

        for identity in before_records.keys() | after_records.keys():
            incoming = after_records.get(identity)
            if identity not in metadata_operations:
                baseline = baseline_by_identity.get(identity)
                if baseline is not None and incoming is not None:
                    baseline_mod = read_mod_data(
                        baseline.relative_path,
                        tomllib.loads(baseline.contents.decode("utf-8")),
                    )
                    incoming_mod = read_mod_data(
                        incoming.relative_path,
                        tomllib.loads(incoming.contents.decode("utf-8")),
                    )
                    incoming = replace(
                        incoming,
                        contents=_metadata_contents_with_side(
                            incoming.contents,
                            union_side(baseline_mod.side, incoming_mod.side),
                        ),
                    )
                metadata_operations[identity] = incoming
                continue
            existing = metadata_operations[identity]
            if (existing is None) != (incoming is None):
                raise HuroshikiError(
                    "Update closure delete-vs-update conflict for "
                    f"{identity[0]}:{identity[1]}"
                )
            if existing is not None and incoming is not None:
                metadata_operations[identity] = _merge_metadata_records(
                    existing, incoming
                )

    final_records = dict(baseline_by_identity)
    final_records.update(
        (identity, record)
        for identity, record in metadata_operations.items()
        if record is not None
    )
    for identity, record in metadata_operations.items():
        if record is None:
            final_records.pop(identity, None)

    path_owners: dict[str, tuple[str, str]] = {}
    filename_owners: dict[str, tuple[str, str]] = {}
    for identity, record in final_records.items():
        path_key = portable_relative_path_key(record.relative_path)
        filename_key = portable_basename_key(record.filename)
        if path_key in path_owners and path_owners[path_key] != identity:
            raise HuroshikiError(
                f"Update closure metadata path collision at {record.relative_path}"
            )
        if filename_key in filename_owners and filename_owners[filename_key] != identity:
            raise HuroshikiError(
                f"Update closure filename collision for {record.filename!r}"
            )
        path_owners[path_key] = identity
        filename_owners[filename_key] = identity

    merged: list[UpdateChange] = list(other_operations.values())
    for identity, desired in metadata_operations.items():
        original = baseline_by_identity.get(identity)
        if original is not None and (
            desired is None or original.relative_path != desired.relative_path
        ):
            merged.append(UpdateChange(original.relative_path, original.contents, None))
        if desired is not None:
            before = (
                original.contents
                if original is not None and original.relative_path == desired.relative_path
                else None
            )
            if before != desired.contents:
                merged.append(UpdateChange(desired.relative_path, before, desired.contents))
    return tuple(sorted(merged, key=lambda item: item.relative_path))


def _apply_update_change(
    source: Path,
    change: UpdateChange,
    *,
    use_after: bool,
) -> None:
    contents = change.after if use_after else change.before
    path = safe_child(source, change.relative_path)
    if contents is None:
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != source:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def read_mod_data(relative_path: Path, data: dict[str, object]) -> ModInfo:
    side = data.get("side")
    side_error = packctl.side_validation_error(side)
    client, server = flags_from_side(side)
    provider, project_id = provider_from_metadata(data)
    slug = relative_path.name.removesuffix(".pw.toml")
    download = data.get("download", {})
    source_url = (
        str(download.get("url", ""))
        if isinstance(download, dict)
        else ""
    )
    if canonical_provider(provider) == "url":
        project_id = slug
    return ModInfo(
        relative_path=relative_path,
        slug=slug,
        name=str(data.get("name", slug)),
        provider=provider,
        project_id=project_id,
        filename=str(data.get("filename", "")),
        client=client,
        server=server,
        source_url=source_url,
        side_error=side_error,
    )


def template_mod_relative(
    provider: str, project_id: str, occurrence: int = 0
) -> Path:
    safe_provider = canonical_provider(provider)
    safe_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in project_id
    )
    suffix = "" if occurrence == 0 else f"-{occurrence + 1}"
    return Path("mods") / f"{safe_provider}-{safe_id}{suffix}.pw.toml"


TEMPLATE_MOD_PATH_ROOT = Path(".huroshiki-template-manifest") / "mods"


def template_mod_index_relative(index: int) -> Path:
    if index < 0:
        raise HuroshikiError(f"Invalid template MOD list index: {index}")
    return TEMPLATE_MOD_PATH_ROOT / str(index)


def template_mod_raw_index(relative_path: Path) -> int | None:
    try:
        relative = relative_path.relative_to(TEMPLATE_MOD_PATH_ROOT)
    except ValueError:
        return None
    if len(relative.parts) != 1 or not relative.name.isdecimal():
        return None
    return int(relative.name)


def template_mod_info(
    entry: dict[str, str], occurrence: int = 0, *, raw_index: int | None = None
) -> ModInfo:
    side = entry.get("side")
    side_error = packctl.side_validation_error(side)
    client, server = flags_from_side(side)
    provider = canonical_provider(entry["provider"])
    project_id = entry["project_id"]
    source_url = entry.get("url", "")
    return ModInfo(
        relative_path=(
            template_mod_relative(provider, project_id, occurrence)
            if raw_index is None
            else template_mod_index_relative(raw_index)
        ),
        slug=f"{provider}-{project_id}",
        name=entry["name"],
        provider={"modrinth": "MR", "curseforge": "CF", "url": "URL"}[provider],
        project_id=project_id,
        filename=(
            unquote(Path(urlparse(source_url).path).name)
            if source_url
            else ""
        ),
        client=client,
        server=server,
        source_url=source_url,
        side_error=side_error,
    )


def list_mods(project_key_value: str) -> list[ModInfo]:
    kind, project_id = split_project_key(project_key_value)
    if kind == "template":
        return [
            template_mod_info(entry, raw_index=index)
            for index, entry in packctl.template_mods_indexed(
                project_id,
                allow_invalid_sides=True,
                deduplicate=False,
            )
        ]
    source = project_source(project_key_value)
    return [
        read_mod(source, path.relative_to(source))
        for path in sorted(source.rglob("*.pw.toml"))
        if path.is_file()
    ]


def filter_mods(mods: Iterable[ModInfo], query: str) -> list[ModInfo]:
    needle = query.strip().casefold()
    if not needle:
        return list(mods)
    return [
        mod
        for mod in mods
        if needle
        in " ".join(
            (
                mod.name,
                mod.slug,
                mod.provider,
                mod.project_id,
                mod.filename,
                mod.source_url,
                str(mod.relative_path),
            )
        ).casefold()
    ]


def set_installed_mod_side(
    project_key_value: str,
    relative_path: Path,
    client: bool,
    server: bool,
) -> None:
    try:
        with packctl.ProjectLock(project_key_value, "side"):
            kind, project_id = split_project_key(project_key_value)
            side = side_from_flags(client, server)
            if kind == "template":
                raw_index = template_mod_raw_index(relative_path)
                if raw_index is None:
                    raise HuroshikiError(f"Unknown template MOD: {relative_path}")
                packctl.set_template_mod_side_at_index(project_id, raw_index, side)
                return

            source = project_source(project_key_value)
            path = safe_child(source, relative_path)
            packctl.set_side_and_refresh(source, path, side)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def _apply_profile_entry(transaction: PackTransaction, entry: Mapping[str, object]) -> Path:
    provider = entry.get("source")
    project = entry.get("project")
    requested_side = entry.get("side")
    if provider not in {"modrinth", "curseforge"}:
        raise HuroshikiError(f"Unsupported profile source: {provider!r}")
    if project is None:
        raise HuroshikiError("Profile entry is missing project")
    if requested_side not in packctl.VALID_SIDES:
        raise HuroshikiError(
            f"Invalid/missing side for {project!r}: {requested_side!r}"
        )

    if provider == "modrinth":
        project_id: str | int = packctl.resolve_modrinth(str(project))
        add_command = [
            "packwiz",
            "--yes",
            "modrinth",
            "add",
            "--project-id",
            str(project_id),
        ]
    else:
        try:
            project_id = int(project)
        except (TypeError, ValueError) as error:
            raise HuroshikiError(
                "CurseForge profiles require numeric project IDs"
            ) from error
        add_command = [
            "packwiz",
            "--yes",
            "curseforge",
            "add",
            "--addon-id",
            str(project_id),
        ]

    metadata = packctl.find_metadata(transaction.source, provider, project_id)
    already_installed = metadata is not None
    if not already_installed:
        packctl.run(add_command, cwd=transaction.source)
        metadata = packctl.find_metadata(transaction.source, provider, project_id)
    if metadata is None:
        raise HuroshikiError(
            f"Metadata not found after adding {provider}:{project}"
        )

    current_side = packctl.read_toml(metadata).get("side")
    side = (
        union_side(str(current_side), str(requested_side))
        if already_installed and current_side in packctl.VALID_SIDES
        else str(requested_side)
    )
    packctl.set_side_file(metadata, side)
    return metadata.relative_to(transaction.source)


def apply_profiles(
    project_key_value: str,
    profiles: Mapping[str, object],
    names: Iterable[str],
    *,
    on_profile: Callable[[str], None] | None = None,
    on_entry: Callable[[str, Path, str], None] | None = None,
) -> None:
    """Apply selected profiles in order as one all-or-nothing pack transaction."""
    kind, _ = split_project_key(project_key_value)
    if kind != "pack":
        raise HuroshikiError("Profiles can only be applied to MODPACK projects")
    selected_names = tuple(names)
    transaction = PackTransaction.create(project_key_value)
    try:
        for name in selected_names:
            if name not in profiles:
                available = ", ".join(sorted(profiles))
                raise HuroshikiError(
                    f"Unknown profile {name!r}; available: {available}"
                )
            entries = profiles[name] or []
            if not isinstance(entries, list):
                raise HuroshikiError(f"Profile {name!r} must be a list")
            if on_profile is not None:
                on_profile(name)
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    raise HuroshikiError(
                        f"Profile {name!r} entry {index} {entry!r}: expected a mapping"
                    )
                try:
                    relative_path = _apply_profile_entry(transaction, entry)
                    side = str(packctl.read_toml(transaction.source / relative_path)["side"])
                except Exception as error:
                    raise HuroshikiError(
                        f"Profile {name!r} entry {index} {entry!r}: {error}"
                    ) from error
                if on_entry is not None:
                    on_entry(name, relative_path, side)
        try:
            transaction.apply()
        except Exception as error:
            profile_list = ", ".join(repr(name) for name in selected_names)
            raise HuroshikiError(
                f"Profiles {profile_list} could not be applied: {error}"
            ) from error
    finally:
        transaction.discard()


def add_mod_transactionally(
    project_key_value: str,
    provider: str,
    selector: str,
    side: str,
) -> int:
    """Add a mod on a disposable source copy and atomically publish on success."""
    transaction = PackTransaction.create(project_key_value)
    try:
        return transaction.add_mod_transactionally(provider, selector, side)
    finally:
        transaction.discard()


def remove_installed_mods(project_key_value: str, slugs: Iterable[str]) -> int:
    selected = set(slugs)
    if not selected:
        return 0
    transaction = PackTransaction.create(project_key_value)
    try:
        result = transaction.remove_mods(selected)
        if result != 0:
            return result
        transaction.apply()
        return 0
    finally:
        transaction.discard()


def create_project(
    kind: str,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    lock_held: bool = False,
) -> int:
    try:
        packctl.validate_project_creation_fields(
            display_name=display_name,
            minecraft=minecraft,
            loader_version=loader_version,
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error
    if kind == "pack":
        command_name = "new"
    elif kind == "template":
        command_name = "new-template"
    else:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    args = argparse.Namespace(
        **{
            "pack" if kind == "pack" else "template": project_id,
            "display_name": display_name,
            "minecraft": minecraft,
            "loader": loader,
            "loader_version": loader_version,
        }
    )
    create = packctl._new_pack if command_name == "new" else packctl._new_template
    if lock_held:
        return create(args)
    try:
        with packctl.ProjectLock(project_key(kind, project_id), "create project"):
            return create(args)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def delete_project(project_key_value: str) -> packctl.TrashEntry:
    kind, project_id = split_project_key(project_key_value)
    try:
        return packctl.trash_project(kind, project_id)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def list_trash() -> list[packctl.TrashEntry]:
    return packctl.list_trash()


def restore_trash(name: str) -> Path:
    try:
        return packctl.restore_trash(name)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def purge_trash(name: str) -> tuple[int, int]:
    try:
        return packctl.purge_trash(name=name)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def state_items() -> list[StateItem]:
    return packctl.classify_state()


def clean_state(
    *,
    apply: bool = False,
    expected: tuple[StateItem, ...] | None = None,
) -> packctl.StateCleanupReport:
    try:
        return packctl.clean_state(apply=apply, expected=expected)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def project_actions(project_key_value: str) -> tuple[str, ...]:
    kind, _ = split_project_key(project_key_value)
    if kind == "pack":
        return ("build", "publish", "deploy", "restart")
    return ("create MODPACK", "validate")


def project_action_confirmation(
    project_key_value: str,
    action: str,
) -> tuple[str, ...] | None:
    kind, project_id = split_project_key(project_key_value)
    if kind != "pack" or action not in {"deploy", "publish", "restart"}:
        return None

    lines = [f"Pack: {project_id}", f"Action: {action}"]
    if action in {"deploy", "publish"}:
        lines.append(f"Rsync target: {packctl.distribution_target(project_id)}")
    if action in {"publish", "restart"}:
        host, stack, service = packctl.minecraft_server_target(project_id)
        lines.extend(
            (
                f"SSH target: {host}",
                f"Stack directory: {stack}",
                f"Compose service: {service}",
            )
        )
    return tuple(lines)


def _restart_confirmation(
    project_id: str,
    action: str,
    target: tuple[str, str, str],
) -> tuple[str, ...]:
    host, stack, service = target
    return (
        f"Pack: {project_id}",
        f"Action: {action}",
        f"SSH target: {host}",
        f"Stack directory: {stack}",
        f"Compose service: {service}",
    )


def prepare_deploy_preview(
    project_key_value: str,
    action: str,
) -> ProjectDeployPreview:
    kind, project_id = split_project_key(project_key_value)
    if kind != "pack" or action not in {"deploy", "publish"}:
        raise HuroshikiError(f"Deploy preview is not available for {action}")
    try:
        with packctl.ProjectLock(project_key_value, f"{action} preview"):
            if packctl._build_pack(project_id) != 0:
                raise HuroshikiError("Build failed; deploy preview was not created")
            preview = packctl._deploy_preview(project_id)
            restart_target = (
                packctl.minecraft_server_target(project_id)
                if action == "publish"
                else None
            )
            return ProjectDeployPreview(
                project_key_value,
                action,
                preview.target,
                preview.dist_digest,
                preview.changes,
                preview.raw_lines,
                restart_target,
                preview.snapshot,
            )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def discard_deploy_preview(preview: ProjectDeployPreview) -> None:
    if preview.snapshot is None:
        return
    try:
        packctl.discard_deploy_snapshot(preview.snapshot)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def run_project_action(
    project_key_value: str,
    action: str,
    confirmation: tuple[str, ...] | ProjectDeployPreview | None = None,
) -> int:
    kind, project_id = split_project_key(project_key_value)
    ctl = [sys.executable, str(SCRIPTS / "packctl.py")]
    if kind == "template":
        if action == "create MODPACK":
            raise HuroshikiError("MODPACK creation must be started from the TUI form")
        if action != "validate":
            raise HuroshikiError(
                f"Action {action} is not available for template projects"
            )
        return subprocess.run(
            ctl + ["validate-template", project_id],
            cwd=ROOT,
            text=True,
            check=False,
        ).returncode

    if action not in {"build", "deploy", "restart", "publish"}:
        raise HuroshikiError(f"Unknown project action: {action}")
    deploy_confirmation = (
        confirmation if isinstance(confirmation, ProjectDeployPreview) else None
    )
    try:
        with packctl.ProjectLock(project_key_value, action):
            if action in {"deploy", "publish"}:
                snapshot = (
                    None
                    if deploy_confirmation is None
                    or deploy_confirmation.snapshot is None
                    else packctl.ensure_safe_state_path(
                        deploy_confirmation.snapshot,
                        state_root=packctl.PACKS.parent / ".huroshiki",
                        repository_root=packctl.PACKS.parent,
                    )
                )
                if (
                    deploy_confirmation is None
                    or deploy_confirmation.project_key != project_key_value
                    or deploy_confirmation.action != action
                    or packctl.distribution_target(project_id)
                    != deploy_confirmation.target
                    or snapshot is None
                    or packctl.distribution_digest(snapshot)
                    != deploy_confirmation.dist_digest
                    or (
                        action == "publish"
                        and packctl.minecraft_server_target(project_id)
                        != deploy_confirmation.restart_target
                    )
                ):
                    raise HuroshikiError(
                        "Deploy target or distribution changed after preview; action aborted"
                    )
                result = packctl._deploy_pack(
                    project_id,
                    expected_target=deploy_confirmation.target,
                    expected_dist_digest=deploy_confirmation.dist_digest,
                    snapshot=snapshot,
                )
                if result != 0 or action == "deploy":
                    return result
                restart_target = packctl.minecraft_server_target(project_id)
                if restart_target != deploy_confirmation.restart_target:
                    raise HuroshikiError(
                        "Restart target changed during deployment; restart aborted"
                    )
            elif action == "build":
                return packctl._build_pack(project_id)
            else:
                restart_target = packctl.minecraft_server_target(project_id)
                if _restart_confirmation(project_id, action, restart_target) != confirmation:
                    raise HuroshikiError(
                        "Remote configuration changed after confirmation; action aborted"
                    )

            if deploy_confirmation is not None:
                if deploy_confirmation.restart_target is None:
                    raise HuroshikiError("Confirmed publish has no restart target")
                host, stack, service = deploy_confirmation.restart_target
            else:
                host, stack, service = restart_target
            remote = (
                f"cd {shlex.quote(stack)} && docker compose restart "
                f"{shlex.quote(service)}"
            )
            packctl.run(["ssh", "--", host, remote])
            return 0
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error
    finally:
        if deploy_confirmation is not None and deploy_confirmation.snapshot is not None:
            try:
                packctl.discard_deploy_snapshot(deploy_confirmation.snapshot)
            except packctl.ConfigError:
                pass


def update_all(project_key_value: str) -> int:
    kind, _ = split_project_key(project_key_value)
    if kind == "template":
        raise HuroshikiError(
            "Template entries always resolve the newest compatible file when a MODPACK is created"
        )
    transaction = PackTransaction.create(project_key_value)
    try:
        candidates = transaction.prepare_updates()
        available = [candidate for candidate in candidates if candidate.available]
        failures = [candidate for candidate in candidates if candidate.error]
        for candidate in failures:
            print(
                f"Unable to resolve {candidate.name} [{candidate.provider}]: "
                f"{candidate.error}",
                file=sys.stderr,
            )
        if not available:
            if failures:
                return failures[0].error_returncode or 1
            print("No MOD updates are available.")
            return 0
        print("MOD updates:")
        for candidate in available:
            print(
                f"  {candidate.name} [{candidate.provider}] "
                f"{candidate.current_version} -> {candidate.new_version} "
                f"({candidate.file_count} files, "
                f"{candidate.added_dependencies} added dependencies)"
            )
        transaction.select_updates(
            candidate.relative_path for candidate in available
        )
        transaction.apply()
        return 0
    finally:
        transaction.discard()


def compatible_templates(minecraft: str, loader: str) -> list[ProjectInfo]:
    ids = packctl.compatible_template_ids(minecraft, loader)
    return [
        info
        for template_id in ids
        if not (info := project_info(project_key("template", template_id))).error
    ]


def template_install_command(provider: str, project_id: str) -> list[str]:
    normalized = canonical_provider(provider)
    if normalized == "curseforge":
        return [
            "packwiz", "--yes", "curseforge", "add", "--addon-id", project_id
        ]
    if normalized == "modrinth":
        return ["packwiz", "--yes", "modrinth", "add", project_id]
    raise HuroshikiError(f"Unsupported Packwiz provider in template: {provider}")


def concise_process_error(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "Packwiz returned a non-zero exit code").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:240] if lines else f"exit code {result.returncode}"


TEMPLATE_RESOLVER_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class _ResolvedTemplateMetadata:
    relative_path: Path
    provider: str
    project_id: str
    filename: str
    contents: bytes

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.project_id


@dataclass(frozen=True)
class _ResolvedTemplateRoot:
    entry: MergedTemplateMod
    metadata: tuple[_ResolvedTemplateMetadata, ...]
    root_identity: tuple[str, str]


def _metadata_contents_with_side(contents: bytes, side: str) -> bytes:
    document = tomlkit.parse(contents.decode("utf-8"))
    document["side"] = packctl.normalize_side(side)
    return tomlkit.dumps(document).encode("utf-8")


def _read_resolver_metadata(
    source: Path,
    side: str,
) -> tuple[_ResolvedTemplateMetadata, ...]:
    records: list[_ResolvedTemplateMetadata] = []
    identities: dict[tuple[str, str], Path] = {}
    paths: dict[str, tuple[str, str]] = {}
    filenames: dict[str, tuple[str, str]] = {}
    for path in sorted(source.rglob("*.pw.toml")):
        relative = path.relative_to(source)
        try:
            relative = portable_relative_path(relative)
            path_key = portable_relative_path_key(relative)
        except PortablePathError as error:
            raise HuroshikiError(f"Resolver metadata path {relative}: {error}") from error
        mod = read_mod(source, relative)
        provider = canonical_provider(mod.provider)
        identity = (provider, mod.project_id)
        if provider not in {"modrinth", "curseforge", "url"} or not mod.project_id:
            raise HuroshikiError(
                f"Resolver metadata {relative} has no stable provider/project identity"
            )
        try:
            filename = portable_basename(mod.filename, context="Metadata filename")
            filename_key = portable_basename_key(filename)
        except PortablePathError as error:
            raise HuroshikiError(f"Resolver metadata {relative}: {error}") from error
        previous_path = identities.get(identity)
        if previous_path is not None:
            raise HuroshikiError(
                f"Resolver produced identity {provider}:{mod.project_id} at both "
                f"{previous_path} and {relative}"
            )
        path_owner = paths.get(path_key)
        if path_owner is not None and path_owner != identity:
            raise HuroshikiError(
                f"Resolver produced portable metadata path collision at {relative} for "
                f"{path_owner[0]}:{path_owner[1]} and {provider}:{mod.project_id}"
            )
        filename_owner = filenames.get(filename_key)
        if filename_owner is not None and filename_owner != identity:
            raise HuroshikiError(
                f"Resolver produced portable filename collision {filename!r} for "
                f"{filename_owner[0]}:{filename_owner[1]} and {provider}:{mod.project_id}"
            )
        identities[identity] = relative
        paths[path_key] = identity
        filenames[filename_key] = identity
        records.append(
            _ResolvedTemplateMetadata(
                relative,
                provider,
                mod.project_id,
                filename,
                _metadata_contents_with_side(path.read_bytes(), side),
            )
        )
    if not records:
        raise HuroshikiError("No metadata changes were produced")
    return tuple(records)


def _resolve_template_root(
    entry: MergedTemplateMod,
    *,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> _ResolvedTemplateRoot:
    state_root = ROOT / ".huroshiki"
    transaction_root = state_root / "transactions"
    packctl.make_state_directory(
        transaction_root,
        state_root=state_root,
        repository_root=ROOT,
    )
    with tempfile.TemporaryDirectory(
        prefix="template-resolver-", dir=transaction_root
    ) as directory:
        source = Path(directory) / "source"
        create_resolver_source(
            source,
            display_name=f"Resolve {entry.name}",
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
        )
        expected_identity = (
            canonical_provider(entry.provider),
            entry.project_id,
        )
        if expected_identity[0] == "url":
            log_dir = (
                state_root
                / "logs"
                / "template-create"
                / f"resolver-{uuid4().hex[:8]}"
            )
            packctl.ensure_safe_state_path(
                log_dir,
                state_root=state_root,
                repository_root=ROOT,
            )
            artifact = download_url_artifact(
                entry.url or "",
                threading.Event(),
                log_dir,
                loader,
                entry.max_url_jar_size_bytes or DEFAULT_URL_MAX_JAR_SIZE_BYTES,
                allow_private_networks=entry.url_allow_private_networks,
            )
            if artifact.mod_id != entry.project_id:
                raise HuroshikiError(
                    f"URL now contains MOD ID {artifact.mod_id}, expected {entry.project_id}"
                )
            write_url_metadata(
                source,
                Path("mods") / f"{entry.project_id}.pw.toml",
                artifact,
                entry.side,
            )
        else:
            command = template_install_command(entry.provider, entry.project_id)
            try:
                process = subprocess.run(
                    command,
                    cwd=source,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=TEMPLATE_RESOLVER_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                raise HuroshikiError(
                    f"Packwiz resolver deadline exceeded after "
                    f"{TEMPLATE_RESOLVER_TIMEOUT_SECONDS} seconds"
                ) from error
            if process.returncode != 0:
                raise HuroshikiError(concise_process_error(process))

        metadata = _read_resolver_metadata(source, entry.side)
        matching_roots = [item for item in metadata if item.identity == expected_identity]
        if len(matching_roots) != 1:
            found = ", ".join(
                f"{item.provider}:{item.project_id}" for item in metadata
            )
            raise HuroshikiError(
                f"Requested root identity {expected_identity[0]}:{expected_identity[1]} "
                f"was not independently resolved (resolver produced: {found or 'none'})"
            )
        return _ResolvedTemplateRoot(entry, metadata, expected_identity)


def _merge_resolved_template_roots(
    roots: Iterable[_ResolvedTemplateRoot],
) -> tuple[
    tuple[_ResolvedTemplateMetadata, ...],
    tuple[RetainedTemplateCandidate, ...],
    tuple[TemplateInstallFailure, ...],
]:
    merged: dict[tuple[str, str], _ResolvedTemplateMetadata] = {}
    path_owners: dict[str, tuple[str, str]] = {}
    filename_owners: dict[str, tuple[str, str]] = {}
    url_root_owners: dict[tuple[str, str], str] = {}
    retained: list[RetainedTemplateCandidate] = []
    failures: list[TemplateInstallFailure] = []

    for root in roots:
        entry = root.entry
        reason: str | None = None
        if root.root_identity[0] == "url":
            previous = url_root_owners.get(root.root_identity)
            if previous is not None and previous != entry.candidate_key:
                reason = (
                    f"URL MOD ID/path collision for {root.root_identity[1]!r}; "
                    "the selected URLs cannot both be represented"
                )

        pending_paths = dict(path_owners)
        pending_filenames = dict(filename_owners)
        if reason is None:
            for item in root.metadata:
                path_key = portable_relative_path_key(item.relative_path)
                filename_key = portable_basename_key(item.filename)
                existing = merged.get(item.identity)
                if existing is not None:
                    existing_document = tomllib.loads(
                        existing.contents.decode("utf-8")
                    )
                    incoming_document = tomllib.loads(item.contents.decode("utf-8"))
                    existing_document.pop("side", None)
                    incoming_document.pop("side", None)
                    existing_document["filename"] = portable_basename(
                        str(existing_document.get("filename", "")),
                        context="Metadata filename",
                    )
                    incoming_document["filename"] = portable_basename(
                        str(incoming_document.get("filename", "")),
                        context="Metadata filename",
                    )
                    if (
                        portable_relative_path_key(existing.relative_path) != path_key
                        or portable_basename_key(existing.filename) != filename_key
                        or existing_document != incoming_document
                    ):
                        reason = (
                            "resolved metadata disagreement for shared identity "
                            f"{item.provider}:{item.project_id}; dependency versions, "
                            "paths, downloads, or update metadata differ"
                        )
                        break
                    continue
                path_owner = pending_paths.get(path_key)
                if path_owner is not None and path_owner != item.identity:
                    reason = (
                        f"metadata path collision at {item.relative_path}: "
                        f"{path_owner[0]}:{path_owner[1]} vs "
                        f"{item.provider}:{item.project_id}"
                    )
                    break
                filename_owner = pending_filenames.get(filename_key)
                if filename_owner is not None and filename_owner != item.identity:
                    reason = (
                        f"filename collision for {item.filename!r}: "
                        f"{filename_owner[0]}:{filename_owner[1]} vs "
                        f"{item.provider}:{item.project_id}"
                    )
                    break
                pending_paths[path_key] = item.identity
                pending_filenames[filename_key] = item.identity

        if reason is not None:
            failures.append(
                TemplateInstallFailure(
                    entry.name, entry.provider, entry.project_id, reason
                )
            )
            continue

        path_owners = pending_paths
        filename_owners = pending_filenames
        if root.root_identity[0] == "url":
            url_root_owners[root.root_identity] = entry.candidate_key
        for item in root.metadata:
            existing = merged.get(item.identity)
            if existing is None:
                merged[item.identity] = item
                continue
            existing_mod = read_mod_data(
                existing.relative_path,
                tomllib.loads(existing.contents.decode("utf-8")),
            )
            side = union_side(existing_mod.side, entry.side)
            merged[item.identity] = replace(
                existing,
                contents=_metadata_contents_with_side(existing.contents, side),
            )

        actual = merged[root.root_identity]
        retained.append(
            RetainedTemplateCandidate(
                entry.candidate_key,
                entry.name,
                canonical_provider(entry.provider),
                entry.project_id,
                actual.provider,
                actual.project_id,
                actual.relative_path,
                actual.filename,
            )
        )

    return tuple(merged.values()), tuple(retained), tuple(failures)


def prepare_template_composition(
    *,
    template_ids: list[str],
    minecraft: str,
    loader: str,
) -> TemplateComposition:
    try:
        if not template_ids:
            raise TemplateMergeError("At least one template must be selected")
        if len(set(template_ids)) != len(template_ids):
            raise TemplateMergeError("Template selection contains duplicate IDs")
        entries: list[TemplateModEntry] = []
        for template_id in template_ids:
            config = packctl.load_template_config(template_id)
            max_url_size = url_max_jar_size_bytes(config)
            allow_private_networks = url_allow_private_networks(config)
            template_minecraft, template_loader, _ = packctl.template_versions(template_id)
            if template_minecraft != minecraft or template_loader != loader.strip().lower():
                raise HuroshikiError(
                    f"Template {template_id} must use Minecraft {minecraft} and loader {loader}"
                )
            raw_mods = config.get("mods", [])
            if raw_mods is None:
                raw_mods = []
            if not isinstance(raw_mods, list):
                raise packctl.ConfigError(
                    f"templates/{template_id}/template.yaml mods must be a list"
                )
            entries.extend(
                TemplateModEntry(
                    template_id=template_id,
                    name=entry["name"],
                    provider=entry["provider"],
                    project_id=entry["project_id"],
                    url=entry.get("url"),
                    side=entry["side"],
                    max_url_jar_size_bytes=max_url_size,
                    url_allow_private_networks=allow_private_networks,
                )
                for index, raw_entry in enumerate(raw_mods)
                for entry in (
                    packctl.normalize_template_mod(
                        raw_entry,
                        f"templates/{template_id}/mods[{index}]",
                    ),
                )
            )
        return compose_templates(template_ids, entries)
    except TemplateMergeError as error:
        raise HuroshikiError(str(error)) from error


def conflict_multi_selection_error(
    conflict: TemplateConflict,
    candidate_keys: Iterable[str],
) -> str | None:
    requested = set(candidate_keys)
    selected = [
        candidate
        for candidate in conflict.candidates
        if candidate.candidate_key in requested
    ]
    identities: dict[tuple[str, str], MergedTemplateMod] = {}
    for candidate in selected:
        identity = (canonical_provider(candidate.provider), candidate.project_id)
        previous = identities.get(identity)
        if previous is not None:
            return (
                f"Cannot retain both {previous.provider}:{previous.project_id} (A) and "
                f"{candidate.provider}:{candidate.project_id} (B): they resolve to the "
                "same metadata identity/path. Re-select A or B."
            )
        identities[identity] = candidate
    return None


def _create_pack_from_templates(
    *,
    composition: TemplateComposition,
    resolved: ResolvedTemplateComposition,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> TemplateCreationReport:
    resolved_roots: list[_ResolvedTemplateRoot] = []
    resolution_failures: list[TemplateInstallFailure] = []
    for entry in resolved.mods:
        print(
            f"== Resolving {entry.name} ({entry.provider}:{entry.project_id}) ==",
            flush=True,
        )
        try:
            resolved_roots.append(
                _resolve_template_root(
                    entry,
                    minecraft=minecraft,
                    loader=loader,
                    loader_version=loader_version,
                )
            )
        except (HuroshikiError, UnicodeError, tomllib.TOMLDecodeError) as error:
            reason = str(error)
            print(f"warning: {entry.name}: {reason}", file=sys.stderr, flush=True)
            resolution_failures.append(
                TemplateInstallFailure(
                    entry.name, entry.provider, entry.project_id, reason
                )
            )

    metadata, retained, merge_failures = _merge_resolved_template_roots(resolved_roots)
    failures = [*resolution_failures, *merge_failures]
    multi_selected = {
        candidate_key
        for selection in resolved.conflict_selections
        if len(selection.candidate_keys) > 1
        for candidate_key in selection.candidate_keys
    }
    blocking = [
        failure
        for failure in failures
        if next(
            (
                entry.candidate_key
                for entry in resolved.mods
                if entry.provider == failure.provider
                and entry.project_id == failure.project_id
            ),
            None,
        )
        in multi_selected
    ]
    if blocking:
        detail = "; ".join(f"{item.name}: {item.reason}" for item in blocking)
        raise HuroshikiError(
            "Selected conflict candidates cannot all be retained: "
            f"{detail}. Re-select candidate A or B and retry."
        )

    current_composition = prepare_template_composition(
        template_ids=list(composition.template_ids),
        minecraft=minecraft,
        loader=loader,
    )
    if current_composition != composition:
        raise HuroshikiError(
            "Template composition changed during resolver preflight; "
            "review candidates again"
        )

    destination = packctl.get_pack_root(project_id, must_exist=False)
    destination_existed = destination.exists()
    result = create_project(
        "pack",
        project_id,
        display_name,
        minecraft,
        loader,
        loader_version,
        True,
    )
    if result != 0:
        raise HuroshikiError("Failed to create the destination MODPACK")
    owns_destination = not destination_existed
    try:
        pack_key = project_key("pack", project_id)
        source = project_source(pack_key)
        for item in metadata:
            path = safe_child(source, item.relative_path)
            if path.exists():
                raise HuroshikiError(
                    f"Destination metadata path unexpectedly exists: {item.relative_path}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.contents)

        refresh = subprocess.run(
            ["packwiz", "refresh"],
            cwd=source,
            text=True,
            capture_output=True,
            check=False,
        )
        if refresh.returncode != 0:
            raise HuroshikiError(concise_process_error(refresh))

        return TemplateCreationReport(
            pack_key=pack_key,
            template_ids=resolved.template_ids,
            conflict_selections=resolved.conflict_selections,
            conflict_warnings=resolved.warnings,
            installed=tuple(item.name for item in retained),
            failed=tuple(failures),
            retained=retained,
        )
    except BaseException as error:
        if owns_destination:
            try:
                shutil.rmtree(destination)
            except BaseException as rollback_error:
                raise HuroshikiError(
                    f"{error}; failed to roll back destination {destination}: "
                    f"{rollback_error}"
                ) from error
        raise


def create_pack_from_templates(
    *,
    template_ids: list[str],
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    conflict_resolutions: Mapping[str, ConflictResolution] | None = None,
    expected_composition: TemplateComposition | None = None,
) -> TemplateCreationReport:
    try:
        packctl.validate_project_creation_fields(
            display_name=display_name,
            minecraft=minecraft,
            loader_version=loader_version,
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error
    pack_key = project_key("pack", project_id)
    with packctl.ProjectLock(pack_key, "create project"):
        composition = prepare_template_composition(
            template_ids=template_ids,
            minecraft=minecraft,
            loader=loader,
        )
        if expected_composition is not None and composition != expected_composition:
            raise HuroshikiError(
                "Template composition changed after preview; review candidates again"
            )
        try:
            resolved = resolve_composition(composition, conflict_resolutions)
        except TemplateMergeError as error:
            raise HuroshikiError(str(error)) from error
        return _create_pack_from_templates(
            composition=composition,
            resolved=resolved,
            project_id=project_id,
            display_name=display_name,
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
        )


def create_pack_from_template(
    *,
    template_id: str,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> TemplateCreationReport:
    return create_pack_from_templates(
        template_ids=[template_id],
        project_id=project_id,
        display_name=display_name,
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
    )
