from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Callable, Literal
from uuid import uuid4

import packctl
import tomlkit
from pack_tree_policy import PackTreeScan, scan_pack_migration_source
from provider_identity import (
    ProviderIdentityError,
    ProviderMetadataIdentity,
    ProviderName,
    Side,
    canonical_identity,
    canonical_provider,
    parse_provider_metadata,
    parse_provider_metadata_candidate,
)


ROOT_MANIFEST_NAME = ".huroshiki-roots.json"
ROOT_MANIFEST_PATH = Path(ROOT_MANIFEST_NAME)
ROOT_MANIFEST_MAX_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class PackMigrationRootError(RuntimeError):
    pass


class PackMigrationRootManifestMissing(PackMigrationRootError):
    pass


@dataclass(frozen=True)
class PackRootRecord:
    provider: ProviderName
    project_id: str
    side: Side

    @property
    def canonical_identity(self) -> str:
        return canonical_identity(self.provider, self.project_id)


@dataclass(frozen=True)
class PackMigrationRoot:
    canonical_identity: str
    provider: ProviderName
    project_id: str
    source_file_id: str | None
    source_version: str | None
    source_side: Side
    source_metadata_path: Path
    source_filename: str
    is_explicit_root: bool
    source_download_url: str | None = None


@dataclass(frozen=True)
class PackMigrationRootCandidate:
    canonical_identity: str | None
    provider: ProviderName
    project_id: str | None
    source_file_id: str | None
    source_version: str | None
    source_side: Side
    source_metadata_path: Path
    source_filename: str
    source_download_url: str | None = None


@dataclass(frozen=True)
class PackMigrationRootSelection:
    source_metadata_path: Path
    provider: ProviderName
    project_id: str


def read_pack_control_file(
    root: Path,
    scan: PackTreeScan,
    relative: Path,
    *,
    max_bytes: int,
) -> bytes:
    entries = {entry.relative_path: entry for entry in scan.entries}
    expected = entries.get(relative)
    if expected is None or expected.kind != "file" or expected.errors:
        raise PackMigrationRootError(f"Missing regular file: {relative}")
    if expected.size > max_bytes:
        raise PackMigrationRootError(f"File exceeds migration read limit: {relative}")
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    opened: list[int] = []
    descriptor = -1
    try:
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != scan.root_identity:
            raise PackMigrationRootError("Pack control-file root was replaced")
        current = root_fd
        for part in relative.parts[:-1]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            opened.append(child)
            current = child
        descriptor = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=current,
        )
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (expected.device, expected.inode):
            raise PackMigrationRootError(f"File changed while opening: {relative}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise PackMigrationRootError(f"File exceeds migration read limit: {relative}")
        contents = b"".join(chunks)
        if total != expected.size:
            raise PackMigrationRootError(f"File changed while reading: {relative}")
        return contents
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for item in reversed(opened):
            os.close(item)
        os.close(root_fd)


_read_relative_file = read_pack_control_file


def _manifest_records(contents: bytes) -> tuple[PackRootRecord, ...]:
    try:
        value = json.loads(contents.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackMigrationRootError("Root manifest contains invalid JSON") from error
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise PackMigrationRootError("Root manifest schema must be 1")
    if set(value) != {"schema", "roots"}:
        raise PackMigrationRootError("Root manifest contains unknown fields")
    raw_roots = value.get("roots")
    if not isinstance(raw_roots, list):
        raise PackMigrationRootError("Root manifest roots must be a list")
    records: list[PackRootRecord] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_roots):
        if not isinstance(raw, dict):
            raise PackMigrationRootError(f"Root manifest entry {index} must be an object")
        if set(raw) != {"provider", "project_id", "side"}:
            raise PackMigrationRootError(
                f"Root manifest entry {index} contains unknown fields"
            )
        try:
            provider = canonical_provider(str(raw.get("provider", "")))
            project_id = str(raw.get("project_id", "")).strip()
            identity = canonical_identity(provider, project_id)
        except ProviderIdentityError as error:
            raise PackMigrationRootError(f"Root manifest entry {index}: {error}") from error
        side = raw.get("side")
        if side not in {"client", "server", "both"}:
            raise PackMigrationRootError(f"Root manifest entry {index} has invalid side")
        if identity in seen:
            raise PackMigrationRootError(f"Duplicate root identity: {identity}")
        seen.add(identity)
        records.append(PackRootRecord(provider, project_id, side))  # type: ignore[arg-type]
    return tuple(sorted(records, key=lambda item: item.canonical_identity))


def read_pack_root_manifest(source_root: Path) -> tuple[PackRootRecord, ...]:
    scan = scan_pack_migration_source(source_root, checkpoint=lambda: None)
    contents = _read_relative_file(
        source_root,
        scan,
        ROOT_MANIFEST_PATH,
        max_bytes=ROOT_MANIFEST_MAX_BYTES,
    )
    return _manifest_records(contents)


def write_pack_root_manifest(
    source_root: Path,
    roots: tuple[PackRootRecord, ...],
) -> None:
    ordered = tuple(sorted(roots, key=lambda item: item.canonical_identity))
    if len({item.canonical_identity for item in ordered}) != len(ordered):
        raise PackMigrationRootError("Duplicate root identity")
    payload = {
        "schema": 1,
        "roots": [
            {
                "provider": item.provider,
                "project_id": item.project_id,
                "side": item.side,
            }
            for item in ordered
        ],
    }
    contents = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    root_fd = os.open(source_root, _DIRECTORY_FLAGS)
    temporary = f".{ROOT_MANIFEST_NAME}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short root manifest write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.stat(ROOT_MANIFEST_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            packctl.renameat2(
                root_fd, temporary, root_fd, ROOT_MANIFEST_NAME, packctl.RENAME_NOREPLACE
            )
        else:
            packctl.renameat2(
                root_fd, temporary, root_fd, ROOT_MANIFEST_NAME, packctl.RENAME_EXCHANGE
            )
            os.unlink(temporary, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)


def write_pack_control_file(
    source_root: Path,
    relative: Path,
    contents: bytes,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> None:
    root_fd = os.open(source_root, _DIRECTORY_FLAGS)
    opened: list[int] = []
    descriptor = -1
    temporary = f".{relative.name}.huroshiki-{uuid4().hex}.tmp"
    try:
        opened_root = os.fstat(root_fd)
        if expected_root_identity is not None and (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != expected_root_identity:
            raise PackMigrationRootError("Provenance staging root was replaced")
        parent_fd = root_fd
        for part in relative.parts[:-1]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened.append(child)
            parent_fd = child
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short provenance write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            existing = os.stat(
                relative.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            packctl.renameat2(
                parent_fd,
                temporary,
                parent_fd,
                relative.name,
                packctl.RENAME_NOREPLACE,
            )
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise PackMigrationRootError(
                    f"Provenance target is not a regular file: {relative}"
                )
            packctl.renameat2(
                parent_fd,
                temporary,
                parent_fd,
                relative.name,
                packctl.RENAME_EXCHANGE,
            )
            os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        parent_fd = opened[-1] if opened else root_fd
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        for item in reversed(opened):
            os.close(item)
        os.close(root_fd)


_atomic_write_relative = write_pack_control_file


def ensure_pack_root_manifest_ignored(source_root: Path) -> None:
    scan = scan_pack_migration_source(source_root, checkpoint=lambda: None)
    ignore_entry = next(
        (
            entry
            for entry in scan.entries
            if entry.relative_path == Path(".packwizignore")
        ),
        None,
    )
    contents = (
        b""
        if ignore_entry is None
        else read_pack_control_file(
            source_root,
            scan,
            Path(".packwizignore"),
            max_bytes=1024 * 1024,
        )
    )
    try:
        text = contents.decode("utf-8")
    except UnicodeError as error:
        raise PackMigrationRootError(".packwizignore is not valid UTF-8") from error
    if "/.huroshiki-roots.json" in {line.strip() for line in text.splitlines()}:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    write_pack_control_file(
        source_root,
        Path(".packwizignore"),
        (text + "/.huroshiki-roots.json\n").encode("utf-8"),
        expected_root_identity=scan.root_identity,
    )


def set_url_metadata_project_id(
    source_root: Path,
    relative_path: Path,
    project_id: str,
) -> None:
    canonical_identity("url", project_id)
    scan = scan_pack_migration_source(source_root, checkpoint=lambda: None)
    try:
        document = tomlkit.parse(
            read_pack_control_file(
                source_root,
                scan,
                relative_path,
                max_bytes=2 * 1024 * 1024,
            ).decode("utf-8")
        )
    except (OSError, UnicodeError, tomlkit.exceptions.ParseError) as error:
        raise PackMigrationRootError(f"Invalid URL metadata {relative_path}") from error
    update = document.get("update")
    if isinstance(update, dict) and update:
        raise PackMigrationRootError(
            f"Provider metadata cannot be rewritten as URL metadata: {relative_path}"
        )
    huroshiki = document.get("huroshiki")
    if huroshiki is None:
        huroshiki = tomlkit.table()
        document["huroshiki"] = huroshiki
    if not isinstance(huroshiki, dict):
        raise PackMigrationRootError(f"Invalid huroshiki metadata: {relative_path}")
    existing = huroshiki.get("project-id")
    if existing is not None and str(existing) != project_id:
        raise PackMigrationRootError(
            f"URL metadata identity disagrees with selection: {relative_path}"
        )
    huroshiki["project-id"] = project_id
    write_pack_control_file(
        source_root,
        relative_path,
        tomlkit.dumps(document).encode("utf-8"),
        expected_root_identity=scan.root_identity,
    )


def record_pack_root(
    source_root: Path,
    provider: str,
    project_id: str,
    side: str,
) -> None:
    normalized_provider = canonical_provider(provider)
    identity = canonical_identity(normalized_provider, project_id)
    if side not in {"client", "server", "both"}:
        raise PackMigrationRootError("Root side must be client, server, or both")
    existing = {item.canonical_identity: item for item in read_pack_root_manifest(source_root)}
    existing[identity] = PackRootRecord(
        normalized_provider, project_id, side  # type: ignore[arg-type]
    )
    write_pack_root_manifest(source_root, tuple(existing.values()))


def remove_pack_root(source_root: Path, canonical_root_identity: str) -> None:
    existing = {
        item.canonical_identity: item for item in read_pack_root_manifest(source_root)
    }
    existing.pop(canonical_root_identity, None)
    write_pack_root_manifest(source_root, tuple(existing.values()))


def identify_pack_metadata_by_slug(source_root: Path, slug: str) -> str | None:
    scan = scan_pack_migration_source(source_root, checkpoint=lambda: None)
    matches = [
        entry
        for entry in scan.entries
        if entry.kind == "file"
        and entry.relative_path.name.endswith(".pw.toml")
        and entry.relative_path.name.removesuffix(".pw.toml") == slug
    ]
    if len(matches) > 1:
        raise PackMigrationRootError(f"Ambiguous Packwiz metadata slug: {slug}")
    if not matches:
        return None
    try:
        metadata = parse_provider_metadata(
            matches[0].relative_path,
            _read_relative_file(
                source_root,
                scan,
                matches[0].relative_path,
                max_bytes=2 * 1024 * 1024,
            ),
        )
    except ProviderIdentityError as error:
        raise PackMigrationRootError(str(error)) from error
    final_scan = scan_pack_migration_source(source_root, checkpoint=lambda: None)
    if final_scan != scan:
        raise PackMigrationRootError("Pack source changed while identifying metadata")
    return metadata.canonical_identity


def extract_pack_migration_root_candidates(
    detached_source_root: Path,
    *,
    expected_identity: tuple[int, int],
    checkpoint: Callable[[], None],
    expected_snapshot_digest: str | None = None,
) -> tuple[PackMigrationRootCandidate, ...]:
    checkpoint()
    scan = scan_pack_migration_source(detached_source_root, checkpoint=checkpoint)
    if scan.root_identity != expected_identity or (
        expected_snapshot_digest is not None
        and scan.snapshot_digest != expected_snapshot_digest
    ):
        raise PackMigrationRootError("Detached source changed before root inspection")
    unsafe = [
        entry.relative_path
        for entry in scan.entries
        if entry.kind == "invalid" or entry.errors
    ]
    if unsafe:
        raise PackMigrationRootError(f"Detached source contains unsafe entry: {unsafe[0]}")
    candidates: list[PackMigrationRootCandidate] = []
    seen: set[str] = set()
    for entry in scan.entries:
        checkpoint()
        if entry.kind != "file" or not entry.relative_path.name.endswith(".pw.toml"):
            continue
        contents = _read_relative_file(
            detached_source_root,
            scan,
            entry.relative_path,
            max_bytes=2 * 1024 * 1024,
        )
        try:
            metadata = parse_provider_metadata_candidate(entry.relative_path, contents)
        except ProviderIdentityError as error:
            raise PackMigrationRootError(str(error)) from error
        if metadata.canonical_identity is not None:
            if metadata.canonical_identity in seen:
                raise PackMigrationRootError(
                    f"Duplicate metadata identity: {metadata.canonical_identity}"
                )
            seen.add(metadata.canonical_identity)
        candidates.append(
            PackMigrationRootCandidate(
                metadata.canonical_identity,
                metadata.provider,
                metadata.project_id,
                metadata.file_id,
                metadata.version,
                metadata.side,
                metadata.metadata_path,
                metadata.filename,
                metadata.download_url,
            )
        )
    checkpoint()
    final_scan = scan_pack_migration_source(detached_source_root, checkpoint=checkpoint)
    if final_scan != scan:
        raise PackMigrationRootError("Detached source changed during root inspection")
    return tuple(sorted(candidates, key=lambda item: item.source_metadata_path.as_posix()))


def extract_pack_migration_roots(
    detached_source_root: Path,
    *,
    expected_identity: tuple[int, int],
    checkpoint: Callable[[], None],
    expected_snapshot_digest: str | None = None,
) -> tuple[PackMigrationRoot, ...]:
    checkpoint()
    scan = scan_pack_migration_source(detached_source_root, checkpoint=checkpoint)
    if scan.root_identity != expected_identity or (
        expected_snapshot_digest is not None
        and scan.snapshot_digest != expected_snapshot_digest
    ):
        raise PackMigrationRootError("Detached source changed before root extraction")
    unsafe = [
        entry.relative_path
        for entry in scan.entries
        if entry.kind == "invalid" or entry.errors
    ]
    if unsafe:
        raise PackMigrationRootError(f"Detached source contains unsafe entry: {unsafe[0]}")
    if not any(entry.relative_path == ROOT_MANIFEST_PATH for entry in scan.entries):
        raise PackMigrationRootManifestMissing(
            "Pack root provenance has not been initialized"
        )
    manifest = _manifest_records(
        _read_relative_file(
            detached_source_root,
            scan,
            ROOT_MANIFEST_PATH,
            max_bytes=ROOT_MANIFEST_MAX_BYTES,
        )
    )
    ignore_contents = _read_relative_file(
        detached_source_root,
        scan,
        Path(".packwizignore"),
        max_bytes=1024 * 1024,
    )
    try:
        ignore_lines = {
            line.strip() for line in ignore_contents.decode("utf-8").splitlines()
        }
    except UnicodeError as error:
        raise PackMigrationRootError(".packwizignore is not valid UTF-8") from error
    if "/.huroshiki-roots.json" not in ignore_lines:
        raise PackMigrationRootError(
            "Root manifest must be excluded by .packwizignore"
        )
    metadata_by_identity: dict[str, ProviderMetadataIdentity] = {}
    for entry in scan.entries:
        checkpoint()
        if entry.kind != "file" or not entry.relative_path.name.endswith(".pw.toml"):
            continue
        contents = _read_relative_file(
            detached_source_root,
            scan,
            entry.relative_path,
            max_bytes=2 * 1024 * 1024,
        )
        try:
            metadata = parse_provider_metadata(entry.relative_path, contents)
        except ProviderIdentityError as error:
            raise PackMigrationRootError(str(error)) from error
        if metadata.canonical_identity in metadata_by_identity:
            raise PackMigrationRootError(
                f"Duplicate metadata identity: {metadata.canonical_identity}"
            )
        metadata_by_identity[metadata.canonical_identity] = metadata
    roots: list[PackMigrationRoot] = []
    for record in manifest:
        metadata = metadata_by_identity.get(record.canonical_identity)
        if metadata is None:
            raise PackMigrationRootError(
                f"Root metadata is missing: {record.canonical_identity}"
            )
        if metadata.side != record.side:
            raise PackMigrationRootError(
                f"Root side disagrees with metadata: {record.canonical_identity}"
            )
        roots.append(
            PackMigrationRoot(
                record.canonical_identity,
                metadata.provider,
                metadata.project_id,
                metadata.file_id,
                metadata.version,
                record.side,
                metadata.metadata_path,
                metadata.filename,
                True,
                metadata.download_url,
            )
        )
    checkpoint()
    final_scan = scan_pack_migration_source(detached_source_root, checkpoint=checkpoint)
    if final_scan != scan:
        raise PackMigrationRootError("Detached source changed during root extraction")
    return tuple(roots)
