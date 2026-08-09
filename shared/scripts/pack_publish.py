"""Network-free, snapshot-bound publication manifest planning."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import threading
import time
import tomllib
from typing import Callable, Literal
import unicodedata

import tomlkit
import yaml

import packctl
from overlay_policy import is_packwiz_owned_name
from pack_tree_policy import PackTreePolicyError, PackTreeScan, scan_pack_migration_source
from portable_paths import PortablePathError, portable_basename_key, portable_relative_path_key
from provider_identity import ProviderIdentityError, parse_provider_metadata


class PackPublishError(RuntimeError):
    pass


class PackPublishCancelled(PackPublishError):
    pass


class PackPublishDeadlineExceeded(PackPublishError):
    pass


@dataclass(frozen=True)
class PublishWarning:
    code: str
    message: str


@dataclass(frozen=True)
class PublishFileEntry:
    relative_path: PurePosixPath
    size: int
    sha256: str
    mode: int
    source_kind: Literal["packwiz", "content", "generated"]
    contents: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.source_kind == "generated":
            if self.contents is None:
                raise ValueError("generated publication files must retain their contents")
            if self.size != len(self.contents):
                raise ValueError("generated publication file size does not match contents")
            if self.sha256 != hashlib.sha256(self.contents).hexdigest():
                raise ValueError("generated publication file digest does not match contents")
        elif self.contents is not None:
            raise ValueError("source-backed publication files must not retain duplicate contents")


@dataclass(frozen=True)
class PackPublishManifest:
    pack_id: str
    target_side: Literal["client", "server"]
    source_snapshot_digest: str
    minecraft_version: str
    loader: str
    loader_version: str
    files: tuple[PublishFileEntry, ...]
    total_bytes: int
    manifest_digest: str
    warnings: tuple[PublishWarning, ...] = ()


Progress = Callable[[str], object]
_D_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_F_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 1024 * 1024
_MAX_DESCRIPTOR = 64 * 1024 * 1024
_MAX_INDEX_RECORDS = 100_000
_MAX_TOTAL_BYTES = sys.maxsize
_IGNORED_ROOTS = frozenset(
    {
        ".huroshiki",
        "crash-reports",
        "dist",
        "logs",
        "saves",
        "screenshots",
        "secrets",
        "world",
        "worlds",
    }
)


def _checkpoint(cancel_event: threading.Event | None, deadline: float | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PackPublishCancelled("Pack publication planning was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise PackPublishDeadlineExceeded("Pack publication planning deadline exceeded")


def _progress(progress: Progress | None, phase: str) -> None:
    if progress is not None:
        try:
            progress(phase)
        except Exception:
            pass


def _entry_map(scan: PackTreeScan) -> dict[Path, object]:
    return {entry.relative_path: entry for entry in scan.entries}


def _directory_map(entries: dict[Path, object]) -> dict[Path, object]:
    return {path: entry for path, entry in entries.items() if entry.kind == "directory"}


def _read_bound(
    root_fd: int,
    relative: Path,
    expected: object,
    *,
    directories: dict[Path, object] | None = None,
    cancel_event: threading.Event | None,
    deadline: float | None,
    max_bytes: int | None = None,
    retain_bytes: bool = True,
) -> bytes:
    """Read one initially-scanned file through an already-open root FD."""
    current = root_fd
    opened: list[int] = []
    fd = -1
    try:
        for part in relative.parts[:-1]:
            _checkpoint(cancel_event, deadline)
            child = os.open(part, _D_FLAGS, dir_fd=current)
            ancestor = Path(*relative.parts[: len(opened) + 1])
            expected_ancestor = directories.get(ancestor) if directories is not None else None
            if expected_ancestor is not None:
                opened_metadata = os.fstat(child)
                if (opened_metadata.st_dev, opened_metadata.st_ino) != (expected_ancestor.device, expected_ancestor.inode):
                    os.close(child)
                    raise PackPublishError(f"directory changed while opening: {ancestor}")
            opened.append(child)
            current = child
        _checkpoint(cancel_event, deadline)
        fd = os.open(relative.name, _F_FLAGS, dir_fd=current)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PackPublishError(f"unsafe publication file: {relative}")
        if (metadata.st_dev, metadata.st_ino) != (expected.device, expected.inode):
            raise PackPublishError(f"file changed before reading: {relative}")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            _checkpoint(cancel_event, deadline)
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise PackPublishError(f"descriptor is too large: {relative}")
            digest.update(chunk)
            if retain_bytes:
                chunks.append(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            expected.device, expected.inode, expected.size, expected.mtime_ns, expected.ctime_ns
        ) or digest.hexdigest() != expected.digest:
            raise PackPublishError(f"file changed while reading: {relative}")
        for index, ancestor_fd in enumerate(opened):
            _checkpoint(cancel_event, deadline)
            ancestor_path = Path(*relative.parts[: index + 1])
            expected_ancestor = directories.get(ancestor_path) if directories is not None else None
            if expected_ancestor is not None:
                bound = os.fstat(ancestor_fd)
                if (bound.st_dev, bound.st_ino, bound.st_mtime_ns, bound.st_ctime_ns) != (
                    expected_ancestor.device, expected_ancestor.inode,
                    expected_ancestor.mtime_ns, expected_ancestor.ctime_ns,
                ):
                    raise PackPublishError(f"directory changed while reading: {ancestor_path}")
        return b"".join(chunks) if retain_bytes else b""
    except OSError as error:
        raise PackPublishError(f"cannot read descriptor-bound file {relative}: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        for item in reversed(opened):
            os.close(item)


def _yaml_bytes(data: bytes, name: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise PackPublishError(f"invalid {name}: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PackPublishError(f"{name} must contain a YAML mapping")
    return value


def _config(root_fd: int, entries: dict[Path, object], pack_id: str, *, cancel_event: threading.Event | None, deadline: float | None) -> dict[str, object]:
    directories = _directory_map(entries)
    pack_entry = entries.get(Path("pack.yaml"))
    if pack_entry is None or pack_entry.kind != "file":
        raise PackPublishError("pack.yaml is required")
    committed = _yaml_bytes(_read_bound(root_fd, Path("pack.yaml"), pack_entry, directories=directories, cancel_event=cancel_event, deadline=deadline, max_bytes=_MAX_DESCRIPTOR), "pack.yaml")
    local_entry = entries.get(Path("pack.local.yaml"))
    local = {} if local_entry is None else _yaml_bytes(_read_bound(root_fd, Path("pack.local.yaml"), local_entry, directories=directories, cancel_event=cancel_event, deadline=deadline, max_bytes=_MAX_DESCRIPTOR), "pack.local.yaml")
    try:
        return packctl.prospective_pack_config(pack_id, committed, local)
    except packctl.ConfigError as error:
        raise PackPublishError(str(error)) from error


def _content_files(scan: PackTreeScan, target_side: str) -> list[tuple[Path, object]]:
    """Validate overlay shape/policy solely from the fixed tree scan."""
    entries = _entry_map(scan)
    root = entries.get(Path("content"))
    if root is None:
        return []
    if root.kind != "directory":
        raise PackPublishError("content must be an ordinary directory")
    for target in ("common", "client", "server"):
        item = entries.get(Path("content") / target)
        if item is not None and item.kind != "directory":
            raise PackPublishError(f"content/{target} must be an ordinary directory")
    selected: list[tuple[Path, object]] = []
    for path, item in sorted(entries.items(), key=lambda pair: pair[0].as_posix()):
        if not path.parts or path.parts[0] != "content" or path == Path("content"):
            continue
        if path.parts[1] not in {"common", "client", "server"}:
            raise PackPublishError(f"unsupported content overlay root: {path.parts[1]}")
        if any(is_packwiz_owned_name(part) for part in path.parts[2:]):
            raise PackPublishError(f"Packwiz-owned content path: {path}")
        if item.kind == "file" and path.parts[1] in {"common", target_side}:
            selected.append((path, item))
    # Detect portable collisions in each publication variant before selection.
    for side in ("client", "server"):
        seen: dict[str, Path] = {}
        for path, item in entries.items():
            if len(path.parts) < 3 or path.parts[0] != "content" or path.parts[1] not in {"common", side} or item.kind != "file":
                continue
            try:
                key = portable_relative_path_key(Path(*path.parts[2:]))
            except Exception as error:
                raise PackPublishError(f"invalid content path: {path}") from error
            if key in seen:
                raise PackPublishError(f"content portable collision: {seen[key]} and {path}")
            seen[key] = path
    return selected


def _file_entry(scan: PackTreeScan, relative: Path, output: PurePosixPath, kind: Literal["packwiz", "content"]) -> PublishFileEntry:
    item = next((entry for entry in scan.entries if entry.relative_path == relative), None)
    if item is None or item.kind != "file" or item.digest is None:
        raise PackPublishError(f"unsafe or missing publication file: {relative}")
    return PublishFileEntry(output, item.size, item.digest, stat.S_IMODE(item.mode), kind)


def _generated_entry(
    scan: PackTreeScan,
    source_relative: Path,
    output: PurePosixPath,
    contents: bytes,
) -> PublishFileEntry:
    item = next(
        (entry for entry in scan.entries if entry.relative_path == source_relative),
        None,
    )
    if item is None or item.kind != "file":
        raise PackPublishError(f"unsafe or missing publication file: {source_relative}")
    return PublishFileEntry(
        output,
        len(contents),
        hashlib.sha256(contents).hexdigest(),
        stat.S_IMODE(item.mode),
        "generated",
        contents,
    )


def _portable_output_map(
    files: list[PublishFileEntry],
) -> dict[str, PurePosixPath]:
    result: dict[str, PurePosixPath] = {}
    for entry in files:
        path = entry.relative_path
        if not path.parts or path.is_absolute() or path.as_posix() != str(path):
            raise PackPublishError(f"publication path is not normalized: {path}")
        try:
            key = portable_relative_path_key(path)
        except PortablePathError as error:
            raise PackPublishError(f"invalid publication path: {path}") from error
        previous = result.get(key)
        if previous is not None:
            raise PackPublishError(
                f"publication path collision: {previous} and {path}"
            )
        result[key] = path
    return result


def _collides_with_generated_descriptor(path: Path) -> bool:
    compatibility_name = unicodedata.normalize("NFKC", path.as_posix()).casefold()
    return compatibility_name in {"pack.toml", "index.toml"}


def _variant_index_bytes(
    records: list[tuple[Path, str, bool]],
    checkpoint: Callable[[], None],
) -> bytes:
    checkpoint()
    ordered = sorted(records, key=lambda item: item[0].as_posix())
    checkpoint()
    result = bytearray(b'hash-format = "sha256"\n')
    for path, digest, metafile in ordered:
        checkpoint()
        record = (
            "\n[[files]]\n"
            f"file = {json.dumps(path.as_posix(), ensure_ascii=False)}\n"
        ).encode("utf-8")
        if metafile:
            record += b"metafile = true\n"
        record += f'hash = "{digest}"\n'.encode("ascii")
        if len(result) > _MAX_DESCRIPTOR - len(record):
            raise PackPublishError("generated index.toml is too large")
        result.extend(record)
    return bytes(result)


def _packwiz_files(root_fd: int, scan: PackTreeScan, side: str, *, cancel_event: threading.Event | None, deadline: float | None) -> tuple[list[PublishFileEntry], tuple[str, str, str], frozenset[str]]:
    entries = _entry_map(scan)
    directories = _directory_map(entries)
    def read(name: str) -> bytes:
        item = entries.get(Path("source") / name)
        if item is None or item.kind != "file":
            raise PackPublishError(f"source/{name} is required")
        return _read_bound(root_fd, Path("source") / name, item, directories=directories, cancel_event=cancel_event, deadline=deadline, max_bytes=_MAX_DESCRIPTOR)
    pack_bytes = read("pack.toml")
    index_bytes = read("index.toml")
    try:
        pack = tomllib.loads(pack_bytes.decode("utf-8"))
        index = tomllib.loads(index_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackPublishError(f"invalid Packwiz TOML: {error}") from error
    versions = pack.get("versions")
    if not isinstance(versions, dict) or not isinstance(versions.get("minecraft"), str) or not versions["minecraft"].strip():
        raise PackPublishError("pack.toml versions tuple is invalid")
    loaders = [name for name in packctl.LOADER_FLAGS if name in versions]
    if len(loaders) != 1 or not isinstance(versions[loaders[0]], str) or not versions[loaders[0]].strip():
        raise PackPublishError("pack.toml must define exactly one loader version")
    tuple_value = (versions["minecraft"].strip(), loaders[0], versions[loaders[0]].strip())
    pack_index = pack.get("index")
    if (
        not isinstance(pack_index, dict)
        or pack_index.get("file") != "index.toml"
        or pack_index.get("hash-format") != "sha256"
        or not isinstance(pack_index.get("hash"), str)
        or pack_index["hash"] != hashlib.sha256(index_bytes).hexdigest()
    ):
        raise PackPublishError("pack.toml index descriptor is invalid or stale")
    index_records = index.get("files", [])
    if index.get("hash-format") != "sha256" or not isinstance(index_records, list):
        raise PackPublishError("index.toml structure is invalid")
    if len(index_records) > _MAX_INDEX_RECORDS:
        raise PackPublishError("index.toml contains too many file records")
    indexed_records: dict[Path, tuple[str, bool]] = {}
    indexed_paths: set[str] = set()
    for record in index_records:
        _checkpoint(cancel_event, deadline)
        if not isinstance(record, dict) or not isinstance(record.get("file"), str) or not isinstance(record.get("hash"), str):
            raise PackPublishError("index.toml contains an invalid file record")
        if set(record) - {"file", "hash", "metafile"}:
            raise PackPublishError("index.toml contains unsupported file record fields")
        raw_path = record["file"]
        posix_path = PurePosixPath(raw_path)
        if (
            posix_path.is_absolute()
            or not posix_path.parts
            or ".." in posix_path.parts
            or posix_path.as_posix() != raw_path
        ):
            raise PackPublishError("index.toml contains an unsafe path")
        path = Path(*posix_path.parts)
        try:
            indexed_key = portable_relative_path_key(path)
        except PortablePathError as error:
            raise PackPublishError(f"index.toml contains an invalid path: {path}") from error
        if indexed_key in indexed_paths:
            raise PackPublishError(f"index.toml contains a duplicate path: {path}")
        if _collides_with_generated_descriptor(path):
            raise PackPublishError(
                f"index.toml file record collides with generated descriptor: {path}"
            )
        indexed_paths.add(indexed_key)
        found = _file_entry(scan, Path("source") / path, PurePosixPath(path.as_posix()), "packwiz")
        if found.sha256 != record["hash"]:
            raise PackPublishError(f"index hash mismatch: {path}")
        metafile = record.get("metafile", False)
        if not isinstance(metafile, bool):
            raise PackPublishError(f"index metafile flag is invalid: {path}")
        if path.name.endswith(".pw.toml"):
            if metafile is not True:
                raise PackPublishError(f"index metadata record is not marked metafile: {path}")
        elif metafile:
            raise PackPublishError(f"index ordinary file is marked metafile: {path}")
        indexed_records[path] = (record["hash"], metafile)
    metadata: list[tuple[Path, object]] = []
    identities: dict[str, Path] = {}
    paths: dict[str, Path] = {}
    filenames: dict[str, Path] = {}
    metadata_paths: set[Path] = set()
    jar_destinations: set[str] = set()
    for path, item in sorted(entries.items(), key=lambda pair: pair[0].as_posix()):
        _checkpoint(cancel_event, deadline)
        if not path.parts or path.parts[0] != "source" or not path.name.endswith(".pw.toml") or item.kind != "file":
            continue
        relative = path.relative_to("source")
        try:
            parsed = parse_provider_metadata(relative, _read_bound(root_fd, path, item, directories=directories, cancel_event=cancel_event, deadline=deadline, max_bytes=_MAX_DESCRIPTOR))
        except (ProviderIdentityError, UnicodeError) as error:
            raise PackPublishError(f"invalid provider metadata {relative}: {error}") from error
        if parsed.canonical_identity in identities:
            raise PackPublishError(f"duplicate provider identity: {parsed.canonical_identity}")
        identities[parsed.canonical_identity] = relative
        try:
            path_key = portable_relative_path_key(parsed.metadata_path)
            filename_key = portable_basename_key(parsed.filename)
        except PortablePathError as error:
            raise PackPublishError(f"invalid portable metadata identity: {relative}") from error
        if path_key in paths:
            raise PackPublishError(f"metadata path collision: {relative}")
        paths[path_key] = relative
        if filename_key in filenames:
            raise PackPublishError(f"filename collision: {parsed.filename}")
        filenames[filename_key] = relative
        metadata.append((relative, parsed))
        metadata_paths.add(relative)
        if parsed.side in (side, "both"):
            jar_destination = relative.parent / parsed.filename
            try:
                jar_destinations.add(portable_relative_path_key(jar_destination))
            except PortablePathError as error:
                raise PackPublishError(
                    f"invalid metadata filename destination: {jar_destination}"
                ) from error
    indexed_metadata = {
        path for path, (_, metafile) in indexed_records.items() if metafile
    }
    if indexed_metadata != metadata_paths:
        missing = sorted(path.as_posix() for path in metadata_paths - indexed_metadata)
        extra = sorted(path.as_posix() for path in indexed_metadata - metadata_paths)
        raise PackPublishError(
            f"index metadata set does not match source metadata: missing={missing}, extra={extra}"
        )
    selected_metadata = {
        relative for relative, parsed in metadata if parsed.side in (side, "both")
    }
    selected_records: list[tuple[Path, str, bool]] = []
    for path, (digest, metafile) in indexed_records.items():
        _checkpoint(cancel_event, deadline)
        if not metafile or path in selected_metadata:
            selected_records.append((path, digest, metafile))
    selected_source_keys = {
        portable_relative_path_key(path) for path, _, _ in selected_records
    } | {
        portable_relative_path_key(Path("pack.toml")),
        portable_relative_path_key(Path("index.toml")),
    }
    if selected_source_keys & jar_destinations:
        raise PackPublishError(
            "selected Packwiz file collides with metadata JAR destination"
        )
    generated_index = _variant_index_bytes(
        selected_records,
        lambda: _checkpoint(cancel_event, deadline),
    )
    generated_index_digest = hashlib.sha256(generated_index).hexdigest()
    try:
        pack_document = tomlkit.parse(pack_bytes.decode("utf-8"))
        document_index = pack_document.get("index")
        if not isinstance(document_index, dict):
            raise PackPublishError("pack.toml index descriptor is invalid")
        document_index["hash"] = generated_index_digest
        generated_pack = tomlkit.dumps(pack_document).encode("utf-8")
    except (UnicodeError, tomlkit.exceptions.ParseError) as error:
        raise PackPublishError(f"invalid Packwiz TOML: {error}") from error
    files = [
        _generated_entry(
            scan,
            Path("source/pack.toml"),
            PurePosixPath("pack.toml"),
            generated_pack,
        ),
        _generated_entry(
            scan,
            Path("source/index.toml"),
            PurePosixPath("index.toml"),
            generated_index,
        ),
    ]
    for path, _, metafile in selected_records:
        if not metafile:
            files.append(
                _file_entry(
                    scan,
                    Path("source") / path,
                    PurePosixPath(path.as_posix()),
                    "packwiz",
                )
            )
    for relative, parsed in metadata:
        if parsed.side in (side, "both"):
            files.append(_file_entry(scan, Path("source") / relative, PurePosixPath(relative.as_posix()), "packwiz"))
    return files, tuple(tuple_value), frozenset(jar_destinations)


def _manifest_digest(manifest: PackPublishManifest) -> str:
    payload = {
        "pack_id": manifest.pack_id, "target_side": manifest.target_side,
        "source_snapshot_digest": manifest.source_snapshot_digest,
        "minecraft_version": manifest.minecraft_version, "loader": manifest.loader,
        "loader_version": manifest.loader_version,
        "files": [{"path": e.relative_path.as_posix(), "size": e.size, "sha256": e.sha256, "mode": e.mode, "source_kind": e.source_kind} for e in manifest.files],
        "total_bytes": manifest.total_bytes,
        "warnings": [{"code": w.code, "message": w.message} for w in manifest.warnings],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _semantic_snapshot_digest(scan: PackTreeScan) -> str:
    """Bind the plan to meaningful Pack inputs, never to absolute paths/inodes."""
    payload = []
    for entry in scan.entries:
        payload.append({
            "path": entry.relative_path.as_posix(), "kind": entry.kind,
            "size": entry.size, "mode": stat.S_IMODE(entry.mode),
            "digest": entry.digest, "portable": entry.portable_key,
            "errors": list(entry.errors),
        })
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_pack_publish_manifest(pack_id: str, *, target_side: str = "server", cancel_event: threading.Event | None = None, deadline: float | None = None, progress: Progress | None = None) -> PackPublishManifest:
    if target_side not in {"client", "server"}:
        raise PackPublishError("target_side must be client or server")
    try:
        packctl.validate_pack_id(pack_id)
    except packctl.ConfigError as error:
        raise PackPublishError(str(error)) from error
    _checkpoint(cancel_event, deadline)
    with packctl.ProjectLock(f"pack:{pack_id}", "plan publication manifest"):
        root = Path(os.path.abspath(packctl.PACKS)) / pack_id
        _progress(progress, "snapshotting")
        try:
            scan = scan_pack_migration_source(
                root,
                checkpoint=lambda: _checkpoint(cancel_event, deadline),
                excluded_roots=_IGNORED_ROOTS,
            )
        except (OSError, PackTreePolicyError) as error:
            raise PackPublishError(f"cannot snapshot Pack safely: {error}") from error
        unsafe = [entry for entry in scan.entries if entry.kind == "invalid"]
        if unsafe:
            raise PackPublishError("; ".join(f"{e.relative_path}: {', '.join(e.errors)}" for e in unsafe))
        root_fd = os.open(root, _D_FLAGS)
        try:
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != scan.root_identity:
                raise PackPublishError("Pack root changed while opening")
            entries = _entry_map(scan)
            _progress(progress, "validating-config")
            _config(root_fd, entries, pack_id, cancel_event=cancel_event, deadline=deadline)
            _progress(progress, "validating-packwiz")
            pack_files, tuple_value, jar_destinations = _packwiz_files(root_fd, scan, target_side, cancel_event=cancel_event, deadline=deadline)
            _progress(progress, "validating-content")
            content_files = _content_files(scan, target_side)
            files = list(pack_files)
            portable = _portable_output_map(files)
            for overlay_path, overlay in content_files:
                _checkpoint(cancel_event, deadline)
                relative = Path(*overlay_path.parts[2:])
                destination = PurePosixPath(relative.as_posix())
                try:
                    key = portable_relative_path_key(destination)
                except PortablePathError as error:
                    raise PackPublishError(f"invalid content destination: {destination}") from error
                if key in portable or key in jar_destinations:
                    raise PackPublishError(f"content destination collision: {destination}")
                portable[key] = destination
                item = entries.get(overlay_path)
                _read_bound(
                    root_fd,
                    overlay_path,
                    item,
                    directories=_directory_map(entries),
                    cancel_event=cancel_event,
                    deadline=deadline,
                    retain_bytes=False,
                )
                files.append(PublishFileEntry(destination, item.size, item.digest, stat.S_IMODE(item.mode), "content"))
            files.sort(key=lambda entry: entry.relative_path.as_posix())
            _portable_output_map(files)
            _progress(progress, "building-manifest")
            total_bytes = 0
            for entry in files:
                if entry.size < 0 or total_bytes > _MAX_TOTAL_BYTES - entry.size:
                    raise PackPublishError("publication manifest byte total overflow")
                total_bytes += entry.size
            result = PackPublishManifest(pack_id, target_side, _semantic_snapshot_digest(scan), tuple_value[0], tuple_value[1], tuple_value[2], tuple(files), total_bytes, "", ())
            result = PackPublishManifest(result.pack_id, result.target_side, result.source_snapshot_digest, result.minecraft_version, result.loader, result.loader_version, result.files, result.total_bytes, _manifest_digest(result), result.warnings)
            _checkpoint(cancel_event, deadline)
            try:
                final = scan_pack_migration_source(
                    root,
                    checkpoint=lambda: _checkpoint(cancel_event, deadline),
                    excluded_roots=_IGNORED_ROOTS,
                )
            except (OSError, PackTreePolicyError) as error:
                raise PackPublishError(f"Pack changed while planning publication: {error}") from error
            if final.root_identity != scan.root_identity or final.snapshot_digest != scan.snapshot_digest:
                raise PackPublishError("Pack changed while planning publication")
            return result
        finally:
            os.close(root_fd)
