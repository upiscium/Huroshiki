"""Network-free, snapshot-bound publication manifest planning."""
from __future__ import annotations

from dataclasses import dataclass
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
        packctl.validate_local_config("pack", Path("pack.local.yaml"), local)
    except packctl.ConfigError as error:
        raise PackPublishError(str(error)) from error
    if committed.get("id") != pack_id or "url_allow_private_networks" in committed:
        raise PackPublishError("pack.yaml identity or machine-local setting is invalid")
    config = packctl.merge(committed, local)
    if not isinstance(config.get("enabled"), bool) or not isinstance(config.get("display_name"), str) or not str(config["display_name"]).strip():
        raise PackPublishError("pack.yaml enabled/display_name is invalid")
    minecraft = config.get("minecraft")
    if not isinstance(minecraft, dict):
        raise PackPublishError("pack.yaml minecraft must be a mapping")
    for key in ("version", "loader", "loader_version"):
        if not isinstance(minecraft.get(key), str) or not minecraft[key].strip():
            raise PackPublishError(f"pack.yaml minecraft.{key} is required")
    if str(minecraft["loader"]).strip().lower() not in packctl.LOADER_FLAGS:
        raise PackPublishError("pack.yaml minecraft.loader is unsupported")
    distribution = config.get("distribution")
    if distribution is not None and not isinstance(distribution, dict):
        raise PackPublishError("pack.yaml distribution must be a mapping")
    try:
        packctl.validate_url_policy(config, "pack.yaml")
    except packctl.ConfigError as error:
        raise PackPublishError(str(error)) from error
    if isinstance(distribution, dict):
        unknown = set(distribution) - {"rsync_target", "public_pack_url"}
        if unknown:
            raise PackPublishError("pack.yaml distribution contains unsupported fields")
        if "rsync_target" in distribution:
            if not isinstance(distribution["rsync_target"], str):
                raise PackPublishError("distribution.rsync_target must be a string")
            try:
                packctl.validate_rsync_target(distribution["rsync_target"])
            except (packctl.ConfigError, ValueError) as error:
                raise PackPublishError(str(error)) from error
        if "public_pack_url" in distribution:
            if not isinstance(distribution["public_pack_url"], str):
                raise PackPublishError("distribution.public_pack_url must be a string")
            try:
                packctl.validate_public_pack_url(distribution["public_pack_url"])
            except packctl.ConfigError as error:
                raise PackPublishError(str(error)) from error
    return config


def _content_files(scan: PackTreeScan, target_side: str) -> list[tuple[Path, object]]:
    """Validate overlay shape/policy solely from the fixed tree scan."""
    entries = _entry_map(scan)
    root = entries.get(Path("content"))
    if root is None or root.kind != "directory":
        raise PackPublishError("content must be an ordinary directory")
    for target in ("common", "client", "server"):
        item = entries.get(Path("content") / target)
        if item is None or item.kind != "directory":
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


def _packwiz_files(root_fd: int, scan: PackTreeScan, side: str, *, cancel_event: threading.Event | None, deadline: float | None) -> tuple[list[PublishFileEntry], tuple[str, str, str], frozenset[str]]:
    entries = _entry_map(scan)
    directories = _directory_map(entries)
    def read(name: str) -> bytes:
        item = entries.get(Path("source") / name)
        if item is None or item.kind != "file":
            raise PackPublishError(f"source/{name} is required")
        return _read_bound(root_fd, Path("source") / name, item, directories=directories, cancel_event=cancel_event, deadline=deadline, max_bytes=_MAX_DESCRIPTOR)
    try:
        pack = tomllib.loads(read("pack.toml").decode("utf-8"))
        index = tomllib.loads(read("index.toml").decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackPublishError(f"invalid Packwiz TOML: {error}") from error
    versions = pack.get("versions")
    if not isinstance(versions, dict) or not isinstance(versions.get("minecraft"), str):
        raise PackPublishError("pack.toml versions tuple is invalid")
    loaders = [name for name in packctl.LOADER_FLAGS if name in versions]
    if len(loaders) != 1 or not isinstance(versions[loaders[0]], str):
        raise PackPublishError("pack.toml must define exactly one loader version")
    tuple_value = (versions["minecraft"], loaders[0], versions[loaders[0]])
    if index.get("hash-format") != "sha256" or not isinstance(index.get("files", []), list):
        raise PackPublishError("index.toml structure is invalid")
    indexed_metadata: dict[Path, str] = {}
    indexed_paths: set[str] = set()
    for record in index.get("files", []):
        if not isinstance(record, dict) or not isinstance(record.get("file"), str) or not isinstance(record.get("hash"), str):
            raise PackPublishError("index.toml contains an invalid file record")
        path = Path(record["file"])
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise PackPublishError("index.toml contains an unsafe path")
        try:
            indexed_key = portable_relative_path_key(path)
        except PortablePathError as error:
            raise PackPublishError(f"index.toml contains an invalid path: {path}") from error
        if indexed_key in indexed_paths:
            raise PackPublishError(f"index.toml contains a duplicate path: {path}")
        indexed_paths.add(indexed_key)
        found = _file_entry(scan, Path("source") / path, PurePosixPath(path.as_posix()), "packwiz")
        if found.sha256 != record["hash"]:
            raise PackPublishError(f"index hash mismatch: {path}")
        if path.name.endswith(".pw.toml"):
            if record.get("metafile") is not True:
                raise PackPublishError(f"index metadata record is not marked metafile: {path}")
            indexed_metadata[path] = record["hash"]
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
    if set(indexed_metadata) != metadata_paths:
        missing = sorted(path.as_posix() for path in metadata_paths - set(indexed_metadata))
        extra = sorted(path.as_posix() for path in set(indexed_metadata) - metadata_paths)
        raise PackPublishError(
            f"index metadata set does not match source metadata: missing={missing}, extra={extra}"
        )
    files = [_file_entry(scan, Path("source") / name, PurePosixPath(name), "packwiz") for name in (Path("pack.toml"), Path("index.toml"))]
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
            config = _config(root_fd, entries, pack_id, cancel_event=cancel_event, deadline=deadline)
            minecraft = config["minecraft"]
            assert isinstance(minecraft, dict)
            _progress(progress, "validating-packwiz")
            pack_files, tuple_value, jar_destinations = _packwiz_files(root_fd, scan, target_side, cancel_event=cancel_event, deadline=deadline)
            expected_tuple = (str(minecraft["version"]), str(minecraft["loader"]).lower(), str(minecraft["loader_version"]))
            if tuple_value != expected_tuple:
                raise PackPublishError("Packwiz versions tuple does not match pack.yaml")
            _progress(progress, "validating-content")
            content_files = _content_files(scan, target_side)
            portable = {portable_relative_path_key(entry.relative_path): entry.relative_path for entry in pack_files}
            files = list(pack_files)
            destinations: set[str] = set()
            for overlay_path, overlay in content_files:
                _checkpoint(cancel_event, deadline)
                relative = Path(*overlay_path.parts[2:])
                destination = PurePosixPath(relative.as_posix())
                try:
                    key = portable_relative_path_key(destination)
                except PortablePathError as error:
                    raise PackPublishError(f"invalid content destination: {destination}") from error
                if key in portable or key in jar_destinations or key in destinations:
                    raise PackPublishError(f"content destination collision: {destination}")
                destinations.add(key)
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
            _progress(progress, "building-manifest")
            total_bytes = 0
            for entry in files:
                if entry.size < 0 or total_bytes > _MAX_TOTAL_BYTES - entry.size:
                    raise PackPublishError("publication manifest byte total overflow")
                total_bytes += entry.size
            result = PackPublishManifest(pack_id, target_side, _semantic_snapshot_digest(scan), expected_tuple[0], expected_tuple[1], expected_tuple[2], tuple(files), total_bytes, "", ())
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
