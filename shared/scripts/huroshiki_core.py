#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import BinaryIO, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
import zipfile


import tomlkit

import packctl
from packwiz_parser import ParserEvent
from packwiz_pty import PackwizPtySession, PtyResult

ROOT = packctl.ROOT
PACKS = packctl.PACKS
TEMPLATES = packctl.TEMPLATES
SCRIPTS = ROOT / "shared" / "scripts"
STATE_ROOT = ROOT / ".huroshiki"
TRANSACTION_ROOT = STATE_ROOT / "transactions"
LOG_ROOT = STATE_ROOT / "logs"
TRASH_ROOT = STATE_ROOT / "trash"


class HuroshikiError(RuntimeError):
    pass


class PackwizCommandError(HuroshikiError):
    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


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

    @property
    def key(self) -> str:
        return project_key(self.kind, self.project_id)

    @property
    def type_label(self) -> str:
        return "MODPACK" if self.kind == "pack" else "TEMPLATE"


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

    @property
    def side(self) -> str:
        return side_from_flags(self.client, self.server)

    @property
    def side_label(self) -> str:
        return f"[{'x' if self.client else ' '}] [{'x' if self.server else ' '}]"


@dataclass(frozen=True)
class UpdateCandidate:
    relative_path: Path
    slug: str
    name: str
    provider: str
    current_version: str
    new_version: str
    status: str

    @property
    def available(self) -> bool:
        return self.status == "update"


@dataclass(frozen=True)
class TemplateInfo:
    target: str
    relative_path: Path
    full_path: Path
    size: int


@dataclass(frozen=True)
class UrlArtifact:
    name: str
    mod_id: str
    version: str
    filename: str
    url: str
    sha256: str
    loaders: tuple[str, ...]


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
class TemplateCreationReport:
    pack_key: str
    template_id: str
    installed: tuple[str, ...]
    failed: tuple[TemplateInstallFailure, ...]

    @property
    def warning_lines(self) -> list[str]:
        if not self.failed:
            return [f"Installed {len(self.installed)} MOD(s); no failures."]
        lines = [
            f"Installed {len(self.installed)} MOD(s).",
            f"Could not install {len(self.failed)} MOD(s):",
        ]
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
        shutil.copytree(transaction.source, self.checkpoint, symlinks=True)
        self.before = metadata_digest_snapshot(transaction.source)

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_dir = (
            LOG_ROOT
            / transaction.project_key.replace(":", "-")
            / f"{timestamp}-{uuid4().hex[:8]}"
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
    template_config_baseline: str = ""
    template_manifest: list[dict[str, str]] | None = None
    batches: list[TransactionBatch] = field(default_factory=list)
    active: bool = True
    _state_lock: BinaryIO | None = field(default=None, init=False, repr=False)
    _operation: PackwizAddOperation | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    @classmethod
    def create(cls, project_key_value: str) -> "PackTransaction":
        kind, project_id = split_project_key(project_key_value)
        real_root = project_root(project_key_value)

        TRANSACTION_ROOT.mkdir(parents=True, exist_ok=True)
        safe_prefix = project_key_value.replace(":", "-")
        tx_root = Path(
            tempfile.mkdtemp(
                prefix=f"{safe_prefix}-",
                dir=TRANSACTION_ROOT,
            )
        )
        tx_source = tx_root / "source"
        state_lock = (tx_root / ".lock").open("a+b")
        fcntl.flock(state_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        if kind == "pack":
            real_source = real_root / "source"
            if not real_source.is_dir():
                raise HuroshikiError(
                    f"Missing Packwiz source directory: {real_source}"
                )
            shutil.copytree(real_source, tx_source, symlinks=True)
            transaction = cls(
                project_key=project_key_value,
                root=tx_root,
                source=tx_source,
                baseline=metadata_digest_snapshot(tx_source),
                baseline_contents=metadata_content_snapshot(tx_source),
                real_source_baseline=tree_digest_snapshot(real_source),
            )
            transaction._state_lock = state_lock
            return transaction

        config_path = real_root / "template.yaml"
        config = packctl.load_template_config(project_id)
        minecraft, loader, loader_version = packctl.template_versions(project_id)
        create_resolver_source(
            tx_source,
            display_name=str(config.get("display_name", project_id)),
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
        )
        transaction = cls(
            project_key=project_key_value,
            root=tx_root,
            source=tx_source,
            baseline={},
            baseline_contents={},
            template_config_baseline=file_digest(config_path),
        )
        transaction._state_lock = state_lock
        return transaction

    def _finish_state(self) -> None:
        try:
            (self.root / ".completed").touch(exist_ok=True)
        except OSError:
            pass
        if self._state_lock is not None:
            try:
                fcntl.flock(self._state_lock, fcntl.LOCK_UN)
            finally:
                self._state_lock.close()
                self._state_lock = None

    def __del__(self) -> None:
        state_lock = getattr(self, "_state_lock", None)
        if state_lock is not None:
            try:
                fcntl.flock(state_lock, fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                state_lock.close()
            except OSError:
                pass
            self._state_lock = None

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
        shutil.copytree(self.source, checkpoint, symlinks=True)
        before = metadata_digest_snapshot(self.source)
        result = subprocess.run(
            build_add_command(provider, query), cwd=self.source, text=True, check=False
        )
        if result.returncode != 0:
            shutil.rmtree(self.source, ignore_errors=True)
            checkpoint.rename(self.source)
            return result
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

            after = metadata_digest_snapshot(self.source)
            changed = tuple(sorted(changed_paths(operation.before, after)))
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

            side = side_from_flags(operation.client, operation.server)
            for relative_path in changed:
                metadata = self.source / relative_path
                if metadata.is_file() and metadata.name.endswith(".pw.toml"):
                    packctl.set_side_file(metadata, side)

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
            )
            if operation.cancelled or operation.cancel_event.is_set():
                raise HuroshikiError("URL addition was cancelled")

            relative_path = Path("mods") / f"{artifact.mod_id}.pw.toml"
            write_url_metadata(
                self.source,
                relative_path,
                artifact,
                side_from_flags(operation.client, operation.server),
            )

            with self._lock:
                if not self.active or self._operation is not operation:
                    raise HuroshikiError(
                        "Transaction was closed before the URL download completed"
                    )
                changed = tuple(
                    sorted(
                        changed_paths(
                            operation.before,
                            metadata_digest_snapshot(self.source),
                        )
                    )
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
        result = subprocess.run(
            ["packwiz", "--yes", "update", "--all"],
            cwd=self.source,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PackwizCommandError(
                f"packwiz update failed with exit code {result.returncode}; "
                "transaction was not applied",
                result.returncode,
            )
        return update_candidates(self.source, self.baseline_contents)

    def select_updates(self, selected_paths: Iterable[Path]) -> None:
        self.ensure_active()
        selected = set(selected_paths)
        changed = changed_paths(
            self.baseline,
            metadata_digest_snapshot(self.source),
        )
        unknown = selected - changed
        if unknown:
            raise HuroshikiError(
                f"Unknown update selection: {', '.join(map(str, sorted(unknown)))}"
            )
        for relative_path in changed - selected:
            original = self.baseline_contents.get(relative_path)
            path = safe_child(self.source, relative_path)
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)

    def remove_mods(self, slugs: Iterable[str]) -> int:
        self.ensure_active()
        selected = set(slugs)
        kind, project_id = split_project_key(self.project_key)
        if kind == "template":
            self.template_manifest = [
                entry
                for entry in packctl.template_mods(project_id)
                if f"{canonical_provider(entry['provider'])}-{entry['project_id']}"
                not in selected
            ]
            return 0

        for slug in sorted(selected):
            result = subprocess.run(
                ["packwiz", "remove", slug],
                cwd=self.source,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return result.returncode
        return 0

    def apply(self) -> None:
        self.ensure_active()
        with self._lock:
            if self._operation is not None and not self._operation.done.is_set():
                raise HuroshikiError("Wait for the active Packwiz search to finish")

        kind, project_id = split_project_key(self.project_key)
        if kind == "template":
            config_path = project_root(self.project_key) / "template.yaml"
            if file_digest(config_path) != self.template_config_baseline:
                raise HuroshikiError(
                    "The template manifest changed while this transaction was open. "
                    "Discard the staged transaction and retry."
                )
            existing = (
                self.template_manifest
                if self.template_manifest is not None
                else packctl.template_mods(project_id)
            )
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

        refresh = subprocess.run(
            ["packwiz", "refresh"],
            cwd=self.source,
            text=True,
            check=False,
        )
        if refresh.returncode != 0:
            raise HuroshikiError("packwiz refresh failed; transaction was not applied")

        real_root = project_root(self.project_key)
        real_source = real_root / "source"
        if tree_digest_snapshot(real_source) != self.real_source_baseline:
            raise HuroshikiError(
                "The real Packwiz source changed while this transaction was open. "
                "Discard the staged transaction and retry to avoid overwriting external changes."
            )

        backup = real_root / f".source.huroshiki-backup-{uuid4().hex}"
        if backup.exists():
            raise HuroshikiError(f"Backup path already exists: {backup}")

        real_source.rename(backup)
        try:
            self.source.rename(real_source)
        except Exception:
            if not real_source.exists() and backup.exists():
                backup.rename(real_source)
            raise

        shutil.rmtree(backup)
        self._finish_state()
        shutil.rmtree(self.root, ignore_errors=True)
        self.active = False

    def discard(self) -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
            operation = self._operation
        if operation is not None and not operation.done.is_set():
            operation.cancel()
            operation.wait(3.0)
        self._finish_state()
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


URL_USER_AGENT = "huroshiki/1 self-hosted-mod-fetcher"
URL_CHUNK_SIZE = 1024 * 1024
DEFAULT_URL_MAX_JAR_SIZE_BYTES = 256 * 1024 * 1024


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


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HuroshikiError("URL provider requires an http:// or https:// public URL")
    filename = unquote(Path(parsed.path).name)
    if not filename:
        raise HuroshikiError("The public URL must end with a downloadable filename")
    if not filename.lower().endswith(".jar"):
        raise HuroshikiError("The self-hosted MOD URL must point to a .jar file")


def sanitize_mod_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-._")
    if not normalized:
        normalized = "self-hosted-mod"
    if not normalized[0].isalnum():
        normalized = f"mod-{normalized}"
    return normalized[:128]


def parse_jar_identity(
    path: Path,
    target_loader: str,
) -> tuple[str, str, str, tuple[str, ...]]:
    identities: list[tuple[str, str, str, str]] = []
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            for metadata_name, loader in (
                ("META-INF/neoforge.mods.toml", "neoforge"),
                ("META-INF/mods.toml", "forge"),
            ):
                if metadata_name not in names:
                    continue
                try:
                    data = tomllib.loads(jar.read(metadata_name).decode("utf-8"))
                except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                    continue
                mods = data.get("mods", [])
                if isinstance(mods, list) and mods and isinstance(mods[0], dict):
                    entry = mods[0]
                    raw_mod_id = str(entry.get("modId", "")).strip()
                    if raw_mod_id:
                        mod_id = sanitize_mod_id(raw_mod_id)
                        name = str(entry.get("displayName", mod_id)).strip() or mod_id
                        version = str(entry.get("version", "")).strip()
                        identities.append((mod_id, name, version, loader))

            if "fabric.mod.json" in names:
                try:
                    data = json.loads(jar.read("fabric.mod.json").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None
                if isinstance(data, dict):
                    raw_mod_id = str(data.get("id", "")).strip()
                    if raw_mod_id:
                        mod_id = sanitize_mod_id(raw_mod_id)
                        name = str(data.get("name", mod_id)).strip() or mod_id
                        version = str(data.get("version", "")).strip()
                        identities.append((mod_id, name, version, "fabric"))

            if "quilt.mod.json" in names:
                try:
                    data = json.loads(jar.read("quilt.mod.json").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None
                loader_data = data.get("quilt_loader", {}) if isinstance(data, dict) else {}
                metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
                if isinstance(loader_data, dict):
                    raw_mod_id = str(loader_data.get("id", "")).strip()
                    if raw_mod_id:
                        mod_id = sanitize_mod_id(raw_mod_id)
                        name = (
                            str(metadata.get("name", mod_id)).strip()
                            if isinstance(metadata, dict)
                            else mod_id
                        ) or mod_id
                        version = str(loader_data.get("version", "")).strip()
                        identities.append((mod_id, name, version, "quilt"))
    except zipfile.BadZipFile as error:
        raise HuroshikiError("The downloaded JAR is not a valid archive") from error

    if not identities:
        raise HuroshikiError(
            "The downloaded JAR does not contain recognized mod metadata"
        )
    identity = next(
        (item for item in identities if item[3] == target_loader), identities[0]
    )
    mod_id, name, version, _ = identity
    return mod_id, name, version, tuple(item[3] for item in identities)


def url_log_paths(log_dir: Path) -> tuple[Path, Path, Path]:
    return (
        log_dir / "session.raw",
        log_dir / "session.txt",
        log_dir / "session.jsonl",
    )


def append_url_log(log_dir: Path, message: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    raw_log, text_log, event_log = url_log_paths(log_dir)
    line = message.rstrip() + "\n"
    with raw_log.open("ab") as handle:
        handle.write(line.encode("utf-8", errors="replace"))
    with text_log.open("a", encoding="utf-8") as handle:
        handle.write(line)
    event = {
        "ts": time.time(),
        "direction": "output",
        "message": message.rstrip(),
    }
    with event_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def ensure_url_error_log(log_dir: Path, message: str) -> None:
    append_url_log(log_dir, f"error: {message}")


def download_url_artifact(
    url: str,
    cancel_event: threading.Event,
    log_dir: Path,
    target_loader: str,
    max_size_bytes: int = DEFAULT_URL_MAX_JAR_SIZE_BYTES,
) -> UrlArtifact:
    validate_public_url(url)
    if (
        isinstance(max_size_bytes, bool)
        or not isinstance(max_size_bytes, int)
        or max_size_bytes <= 0
    ):
        raise HuroshikiError("URL JAR size limit must be a positive integer")
    filename = unquote(Path(urlparse(url).path).name)
    append_url_log(log_dir, f"Downloading {url}")
    request = Request(
        url,
        headers={"User-Agent": URL_USER_AGENT, "Connection": "close"},
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="huroshiki-url-",
            suffix=".jar",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            digest = hashlib.sha256()
            try:
                with urlopen(request, timeout=60) as response:
                    if cancel_event.is_set():
                        raise HuroshikiError("URL download cancelled")
                    raw_content_length = response.headers.get("Content-Length")
                    declared_size = (
                        int(raw_content_length)
                        if raw_content_length is not None
                        and re.fullmatch(r"[0-9]+", raw_content_length.strip())
                        else None
                    )
                    if declared_size is not None and declared_size > max_size_bytes:
                        raise HuroshikiError(
                            "Self-hosted JAR exceeds configured limit of "
                            f"{max_size_bytes} bytes: declared size is "
                            f"{declared_size} bytes"
                        )

                    # HTTPResponse otherwise stops at Content-Length and hides an
                    # understated body. Connection: close makes EOF unambiguous.
                    if declared_size is not None and not response.chunked:
                        response.length = None
                    received_size = 0
                    while True:
                        if cancel_event.is_set():
                            raise HuroshikiError("URL download cancelled")
                        chunk = response.read(
                            min(URL_CHUNK_SIZE, max_size_bytes - received_size + 1)
                        )
                        if not chunk:
                            break
                        received_size += len(chunk)
                        if received_size > max_size_bytes:
                            declared = (
                                f" (declared {declared_size} bytes)"
                                if declared_size is not None
                                else ""
                            )
                            raise HuroshikiError(
                                "Self-hosted JAR exceeds configured limit of "
                                f"{max_size_bytes} bytes: received "
                                f"{received_size} bytes{declared}"
                            )
                        temporary.write(chunk)
                        digest.update(chunk)
            except HTTPError as error:
                raise HuroshikiError(
                    f"Self-hosted URL returned HTTP {error.code}: {url}"
                ) from error
            except URLError as error:
                raise HuroshikiError(
                    f"Could not download self-hosted MOD: {error.reason}"
                ) from error

        mod_id, name, version, loaders = parse_jar_identity(
            temporary_path, target_loader
        )
        if target_loader not in loaders:
            raise HuroshikiError(
                f"The downloaded MOD supports {', '.join(loaders)}, not {target_loader}"
            )
        append_url_log(
            log_dir,
            f"Resolved {name} ({mod_id}) version={version or 'unknown'} "
            f"loaders={','.join(loaders)}",
        )
        return UrlArtifact(
            name=name,
            mod_id=mod_id,
            version=version,
            filename=filename,
            url=url,
            sha256=digest.hexdigest(),
            loaders=loaders,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_url_metadata(
    source: Path,
    relative_path: Path,
    artifact: UrlArtifact,
    side: str,
) -> None:
    path = safe_child(source, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = tomlkit.document()
    document["name"] = artifact.name
    document["filename"] = artifact.filename
    document["side"] = side
    download = tomlkit.table()
    download["url"] = artifact.url
    download["hash-format"] = "sha256"
    download["hash"] = artifact.sha256
    document["download"] = download
    temporary = path.with_name(f".{path.name}.huroshiki-url-{uuid4().hex}")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)


def safe_child(root: Path, relative: Path) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HuroshikiError(f"Path escaped root: {relative}")
    return candidate


TEMPLATE_TARGETS = ("common", "client", "server")


def normalize_template_target(target: str) -> str:
    normalized = target.strip().lower()
    if normalized not in TEMPLATE_TARGETS:
        raise HuroshikiError(
            "Template target must be common, client, or server"
        )
    return normalized


def normalize_template_relative_path(value: str | Path) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts:
        raise HuroshikiError("Template path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise HuroshikiError(
            "Template path cannot contain '.', '..', or empty components"
        )
    if relative.name == ".gitkeep":
        raise HuroshikiError(".gitkeep is managed internally")
    return relative


def template_base(project_key_value: str, target: str) -> Path:
    normalized_target = normalize_template_target(target)
    return project_root(project_key_value) / "content" / normalized_target


def resolve_template_path(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> Path:
    base = template_base(project_key_value, target)
    relative = normalize_template_relative_path(relative_path)
    return safe_child(base, relative)


def list_templates(project_key_value: str) -> list[TemplateInfo]:
    project_root(project_key_value)
    templates: list[TemplateInfo] = []
    for target in TEMPLATE_TARGETS:
        base = template_base(project_key_value, target)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name == ".gitkeep"
            ):
                continue
            templates.append(
                TemplateInfo(
                    target=target,
                    relative_path=path.relative_to(base),
                    full_path=path,
                    size=path.stat().st_size,
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
    normalized_target = normalize_template_target(target)
    relative = normalize_template_relative_path(relative_path)
    path = resolve_template_path(
        project_key_value,
        normalized_target,
        relative,
    )
    if path.exists():
        raise HuroshikiError(
            f"Template file already exists: {normalized_target}/{relative}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return TemplateInfo(
        target=normalized_target,
        relative_path=relative,
        full_path=path,
        size=0,
    )


def read_template_text(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> str:
    path = resolve_template_path(
        project_key_value,
        target,
        relative_path,
    )
    if not path.is_file() or path.is_symlink():
        raise HuroshikiError(f"Template file does not exist: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise HuroshikiError(
            f"Template file is not UTF-8 text: {path}"
        ) from error


def write_template_text(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
    text: str,
) -> None:
    path = resolve_template_path(
        project_key_value,
        target,
        relative_path,
    )
    if not path.is_file() or path.is_symlink():
        raise HuroshikiError(f"Template file does not exist: {path}")
    temporary = path.with_name(f".{path.name}.huroshiki-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def delete_template(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> None:
    base = template_base(project_key_value, target).resolve()
    path = resolve_template_path(
        project_key_value,
        target,
        relative_path,
    )
    if not path.is_file() or path.is_symlink():
        raise HuroshikiError(f"Template file does not exist: {path}")
    path.unlink()

    current = path.parent
    while current != base and current.is_relative_to(base):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest_snapshot(source: Path) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            snapshot[path.relative_to(source)] = f"symlink:{path.readlink()}"
        elif path.is_file():
            snapshot[path.relative_to(source)] = file_digest(path)
    return snapshot


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


def flags_from_side(side: str) -> tuple[bool, bool]:
    normalized = (side or "both").lower()
    if normalized in {"", "both"}:
        return True, True
    if normalized == "client":
        return True, False
    if normalized == "server":
        return False, True
    return True, True


def pack_versions(source: Path) -> tuple[str, str, str]:
    pack_file = source / "pack.toml"
    if not pack_file.exists():
        return "", "", ""
    data = tomllib.loads(pack_file.read_text(encoding="utf-8"))
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
    config = packctl.load_project_config(kind, project_id)
    if kind == "pack":
        minecraft, loader, loader_version = pack_versions(
            packctl.get_pack_root(project_id) / "source"
        )
    else:
        minecraft, loader, loader_version = packctl.template_versions(project_id)
    return ProjectInfo(
        kind=kind,
        project_id=project_id,
        display_name=str(config.get("display_name", project_id)),
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        enabled=bool(config.get("enabled", True)),
    )


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
    data = tomllib.loads(path.read_text(encoding="utf-8"))
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


def update_candidates(
    source: Path,
    baseline_contents: dict[Path, bytes],
) -> list[UpdateCandidate]:
    current = metadata_digest_snapshot(source)
    baseline = {
        path: hashlib.sha256(contents).hexdigest()
        for path, contents in baseline_contents.items()
    }
    changed = changed_paths(baseline, current)
    candidates: list[UpdateCandidate] = []
    for relative_path, original in sorted(baseline_contents.items()):
        old_data = tomllib.loads(original.decode("utf-8"))
        old_mod = read_mod_data(relative_path, old_data)
        path = safe_child(source, relative_path)
        new_data = (
            tomllib.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else old_data
        )
        provider = old_mod.provider
        has_update_provider = canonical_provider(provider) in {
            "modrinth",
            "curseforge",
        }
        if relative_path in changed:
            status = "update"
        elif metadata_is_pinned(old_data):
            status = "pinned"
        elif not has_update_provider:
            status = "unavailable"
        else:
            status = "current"
        candidates.append(
            UpdateCandidate(
                relative_path=relative_path,
                slug=old_mod.slug,
                name=old_mod.name,
                provider=provider,
                current_version=metadata_version(old_data, provider),
                new_version=(
                    metadata_version(new_data, provider)
                    if status == "update"
                    else "-"
                ),
                status=status,
            )
        )
    return candidates


def read_mod_data(relative_path: Path, data: dict[str, object]) -> ModInfo:
    client, server = flags_from_side(str(data.get("side", "both")))
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
    )


def template_mod_relative(provider: str, project_id: str) -> Path:
    safe_provider = canonical_provider(provider)
    safe_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in project_id
    )
    return Path("mods") / f"{safe_provider}-{safe_id}.pw.toml"


def template_mod_info(entry: dict[str, str]) -> ModInfo:
    client, server = flags_from_side(entry["side"])
    provider = canonical_provider(entry["provider"])
    project_id = entry["project_id"]
    source_url = entry.get("url", "")
    return ModInfo(
        relative_path=template_mod_relative(provider, project_id),
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
    )


def list_mods(project_key_value: str) -> list[ModInfo]:
    kind, project_id = split_project_key(project_key_value)
    if kind == "template":
        return [template_mod_info(entry) for entry in packctl.template_mods(project_id)]
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
    kind, project_id = split_project_key(project_key_value)
    side = side_from_flags(client, server)
    if kind == "template":
        mods = packctl.template_mods(project_id)
        target = str(relative_path)
        found = False
        for entry in mods:
            if str(template_mod_relative(entry["provider"], entry["project_id"])) == target:
                entry["side"] = side
                found = True
                break
        if not found:
            raise HuroshikiError(f"Unknown template MOD: {relative_path}")
        packctl.save_template_mods(project_id, mods)
        return

    source = project_source(project_key_value)
    path = safe_child(source, relative_path)
    try:
        packctl.set_side_and_refresh(source, path, side)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


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
) -> int:
    if kind == "pack":
        command_name = "new"
    elif kind == "template":
        command_name = "new-template"
    else:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    command = [
        sys.executable,
        str(SCRIPTS / "packctl.py"),
        command_name,
        project_id,
        display_name,
        minecraft,
        loader,
        loader_version,
    ]
    return subprocess.run(command, cwd=ROOT, text=True, check=False).returncode


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


def clean_state(*, apply: bool = False) -> packctl.StateCleanupReport:
    try:
        return packctl.clean_state(apply=apply)
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


def prepare_deploy_preview(
    project_key_value: str,
    action: str,
) -> ProjectDeployPreview:
    kind, project_id = split_project_key(project_key_value)
    if kind != "pack" or action not in {"deploy", "publish"}:
        raise HuroshikiError(f"Deploy preview is not available for {action}")
    ctl = [sys.executable, str(SCRIPTS / "packctl.py")]
    result = subprocess.run(
        ctl + ["build", project_id], cwd=ROOT, text=True, check=False
    )
    if result.returncode != 0:
        raise HuroshikiError("Build failed; deploy preview was not created")
    preview = packctl.deploy_preview(project_id)
    restart_target = (
        packctl.minecraft_server_target(project_id) if action == "publish" else None
    )
    return ProjectDeployPreview(
        project_key_value,
        action,
        preview.target,
        preview.dist_digest,
        preview.changes,
        preview.raw_lines,
        restart_target,
    )


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

    deploy_confirmation = (
        confirmation if isinstance(confirmation, ProjectDeployPreview) else None
    )
    deploy_command = ctl + ["deploy", project_id]
    if deploy_confirmation is not None:
        deploy_command.extend(
            (
                "--expected-target",
                deploy_confirmation.target,
                "--expected-dist-digest",
                deploy_confirmation.dist_digest,
            )
        )
    commands: dict[str, list[tuple[list[str], bool]]] = {
        "build": [(ctl + ["build", project_id], False)],
        "deploy": [(deploy_command, True)],
        "restart": [(ctl + ["restart", project_id], True)],
        "publish": [
            (deploy_command, True),
            (ctl + ["restart", project_id], True),
        ],
    }
    try:
        selected = commands[action]
    except KeyError as error:
        raise HuroshikiError(f"Unknown project action: {action}") from error

    for command, remote in selected:
        if action in {"deploy", "publish"} and remote:
            if (
                deploy_confirmation is None
                or deploy_confirmation.project_key != project_key_value
                or deploy_confirmation.action != action
                or packctl.distribution_target(project_id)
                != deploy_confirmation.target
                or packctl.distribution_digest(packctl.distribution_root(project_id))
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
        elif remote and project_action_confirmation(project_key_value, action) != confirmation:
            raise HuroshikiError(
                "Remote configuration changed after confirmation; action aborted"
            )
        result = subprocess.run(command, cwd=ROOT, text=True, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def update_all(project_key_value: str) -> int:
    kind, _ = split_project_key(project_key_value)
    if kind == "template":
        raise HuroshikiError(
            "Template entries always resolve the newest compatible file when a MODPACK is created"
        )
    transaction = PackTransaction.create(project_key_value)
    try:
        try:
            candidates = transaction.prepare_updates()
        except PackwizCommandError as error:
            return error.returncode
        available = [candidate for candidate in candidates if candidate.available]
        if not available:
            print("No MOD updates are available.")
            return 0
        print("MOD updates:")
        for candidate in available:
            print(
                f"  {candidate.name} [{candidate.provider}] "
                f"{candidate.current_version} -> {candidate.new_version}"
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
    return [project_info(project_key("template", template_id)) for template_id in ids]


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


def find_installed_provider_mod(
    source: Path,
    provider: str,
    project_id: str,
) -> ModInfo | None:
    normalized = canonical_provider(provider)
    for metadata in sorted(source.rglob("*.pw.toml")):
        if not metadata.is_file():
            continue
        mod = read_mod(source, metadata.relative_to(source))
        if (
            canonical_provider(mod.provider) == normalized
            and mod.project_id == project_id
        ):
            return mod
    return None


def create_pack_from_template(
    *,
    template_id: str,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> TemplateCreationReport:
    if template_id not in packctl.compatible_template_ids(minecraft, loader):
        raise HuroshikiError(
            "The template must use the same Minecraft version and loader as the new MODPACK"
        )
    mods = packctl.template_mods(template_id)
    max_url_jar_size_bytes = url_max_jar_size_bytes(
        packctl.load_template_config(template_id)
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
    )
    if result != 0:
        raise HuroshikiError("Failed to create the destination MODPACK")
    owns_destination = not destination_existed

    pack_key = project_key("pack", project_id)
    source = project_source(pack_key)
    installed: list[str] = []
    failures: list[TemplateInstallFailure] = []

    try:
        for entry in mods:
            name = entry["name"]
            provider = entry["provider"]
            remote_id = entry["project_id"]
            print(f"== Installing {name} ({provider}:{remote_id}) ==", flush=True)
            existing = find_installed_provider_mod(source, provider, remote_id)
            if existing is not None:
                packctl.set_side_file(source / existing.relative_path, entry["side"])
                installed.append(name)
                print(f"already installed as dependency: {name}", flush=True)
                continue
            checkpoint = source.parent / f".template-checkpoint-{uuid4().hex}"
            shutil.copytree(source, checkpoint, symlinks=True)
            before = metadata_digest_snapshot(source)
            if canonical_provider(provider) == "url":
                try:
                    artifact = download_url_artifact(
                        entry["url"],
                        threading.Event(),
                        LOG_ROOT
                        / "template-create"
                        / f"{project_id}-{uuid4().hex[:8]}",
                        loader,
                        max_url_jar_size_bytes,
                    )
                    if artifact.mod_id != remote_id:
                        raise HuroshikiError(
                            f"URL now contains MOD ID {artifact.mod_id}, expected {remote_id}"
                        )
                    write_url_metadata(
                        source,
                        Path("mods") / f"{remote_id}.pw.toml",
                        artifact,
                        entry["side"],
                    )
                    process = subprocess.CompletedProcess(
                        ["huroshiki", "url", "add"], 0, "", ""
                    )
                except HuroshikiError as error:
                    process = subprocess.CompletedProcess(
                        ["huroshiki", "url", "add"], 1, "", str(error)
                    )
            else:
                command = template_install_command(provider, remote_id)
                process = subprocess.run(
                    command,
                    cwd=source,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            after = metadata_digest_snapshot(source)
            changed = sorted(changed_paths(before, after))
            if process.returncode != 0 or not changed:
                shutil.rmtree(source, ignore_errors=True)
                checkpoint.rename(source)
                reason = (
                    concise_process_error(process)
                    if process.returncode != 0
                    else "No metadata changes were produced"
                )
                print(f"warning: {name}: {reason}", file=sys.stderr, flush=True)
                failures.append(
                    TemplateInstallFailure(name, provider, remote_id, reason)
                )
                continue

            side = entry["side"]
            for relative in changed:
                metadata = source / relative
                if metadata.is_file() and metadata.name.endswith(".pw.toml"):
                    packctl.set_side_file(metadata, side)
            shutil.rmtree(checkpoint, ignore_errors=True)
            installed.append(name)

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
            template_id=template_id,
            installed=tuple(installed),
            failed=tuple(failures),
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
