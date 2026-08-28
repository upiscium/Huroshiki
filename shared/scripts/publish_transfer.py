"""Bounded detached Publish transfer and immutable generation staging.

The local side materializes a verified publication manifest before starting any
network process.  The remote side is a fixed, stdin-driven Python helper: user
controlled paths are protocol data and never part of an SSH command string.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tempfile
import threading
import time
import tomllib
from typing import Callable, Literal
from uuid import uuid4
import shlex

import packctl
from pack_publish import (
    PackPublishError,
    PackPublishManifest,
    PublishFileEntry,
    plan_pack_publish_manifest,
    validate_publish_manifest,
)
from pack_snapshot_io import PackSnapshotReadError, read_snapshot_file
from pack_tree_policy import scan_pack_migration_source
from process_runner import (
    BoundedProcessResult,
    process_failure_message,
    run_bounded_process,
)
from publish_target import (
    PublishRemoteTarget,
    PublishTargetError,
    publish_remote_target_from_legacy_settings,
)


class PublishTransferError(RuntimeError):
    """Base error for detached transfer and generation staging."""


class PublishTransferPlanningError(PublishTransferError):
    pass


class PublishTransferExecutionError(PublishTransferError):
    pass


class PublishTransferUncertainError(PublishTransferExecutionError):
    pass


class PublishTransferCleanupError(PublishTransferError):
    def __init__(
        self,
        message: str,
        *,
        plan: PublishTransferPlan | None = None,
        primary_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.plan = plan
        self.primary_error = primary_error


@dataclass(frozen=True)
class PublishTransferProgress:
    phase: str
    completed_files: int
    total_files: int
    completed_bytes: int
    total_bytes: int
    current_path: PurePosixPath | None = None


@dataclass(frozen=True)
class PublishStagedFile:
    relative_path: PurePosixPath
    size: int
    sha256: str
    mode: int


@dataclass(frozen=True)
class PublishStagedGeneration:
    manifest_digest: str
    target_config_digest: str
    generation_id: str
    generation_path: PurePosixPath
    files: tuple[PublishStagedFile, ...]
    total_bytes: int
    reused: bool


class PublishTransferPlan:
    """Opaque owner of a ready detached publication workspace."""

    def __init__(
        self,
        *,
        pack_id: str,
        manifest: PackPublishManifest,
        target: PublishRemoteTarget,
        operation_id: str,
        workspace: Path,
        payload_root: Path,
        workspace_identity: tuple[int, int],
        workspace_digest: str,
        generation_id: str,
    ) -> None:
        self._pack_id = pack_id
        self._manifest = manifest
        self._target = target
        self._operation_id = operation_id
        self._workspace = workspace
        self._payload_root = payload_root
        self._workspace_identity = workspace_identity
        self._workspace_digest = workspace_digest
        self._generation_id = generation_id
        self._state = "ready"
        self._lock = threading.RLock()
        self._recovery_path: PurePosixPath | None = None

    @property
    def manifest(self) -> PackPublishManifest:
        return self._manifest

    @property
    def target(self) -> PublishRemoteTarget:
        return self._target

    @property
    def manifest_digest(self) -> str:
        return self._manifest.manifest_digest

    @property
    def source_snapshot_digest(self) -> str:
        return self._manifest.source_snapshot_digest

    @property
    def target_config_digest(self) -> str:
        return self._target.config_digest

    @property
    def target_side(self) -> str:
        return self._manifest.target_side

    @property
    def pack_id(self) -> str:
        return self._pack_id

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def generation_path(self) -> PurePosixPath:
        return self._target.publication_root / "generations" / self._generation_id

    @property
    def staging_path(self) -> PurePosixPath:
        return (
            self._target.publication_root
            / "generations"
            / f".huroshiki-stage-{self._operation_id}"
        )

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def recovery_path(self) -> PurePosixPath | None:
        with self._lock:
            return self._recovery_path


_TRANSFER_SCHEMA = "huroshiki-publish-transfer-v1"
_GENERATION_SCHEMA = "huroshiki-publish-generation-v1"
_GENERATION_ID_RE = re.compile(r"^v1-[0-9a-f]{64}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSFER_TIMEOUT_SECONDS = 600.0
_SSH_CONNECT_TIMEOUT_SECONDS = 10
_MAX_REMOTE_OUTPUT = 1024 * 1024
_MAX_HEADER_BYTES = 16 * 1024 * 1024
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 512 * 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_MAX_DESCRIPTOR_BYTES = 64 * 1024 * 1024
_FRAME_HEADER = b"HUROSHIKI-PUBLISH-TRANSFER\x00"
_FRAME_VERSION = 1
_CHUNK = 1024 * 1024
_D_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_F_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_F_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
PublishRemoteRequest = Literal[
    "transfer",
    "status",
    "cleanup",
    "verify",
    "activate",
    "activation-status",
    "activation-cleanup",
]


def _remote_helper_source() -> str:
    return r'''import ctypes, errno, fcntl, hashlib, json, os, re, stat, struct, sys, tomllib, unicodedata

MAGIC = b"HUROSHIKI-PUBLISH-TRANSFER\x00"
VERSION = 1
MAX_HEADER = 16 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 64 * 1024 * 1024
MAX_INDEX_RECORDS = 100000
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2

class TransferIntegrityFailure(RuntimeError):
    def __init__(self, message, recovery_path=None):
        super().__init__(message)
        self.recovery_path = recovery_path

def read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(min(CHUNK, remaining))
        if not chunk:
            raise RuntimeError("truncated publish transfer protocol")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def discard_exact(stream, size):
    remaining = size
    while remaining:
        chunk = stream.read(min(CHUNK, remaining))
        if not chunk:
            raise RuntimeError("truncated publish transfer protocol")
        remaining -= len(chunk)

def send(value):
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def validate_absolute(value):
    if not isinstance(value, str) or not value or value == "/":
        raise RuntimeError("publication root is invalid")
    if not value.startswith("/") or value.endswith("/") or "//" in value or "\\" in value:
        raise RuntimeError("publication root is not canonical")
    if value != unicodedata.normalize("NFC", value) or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RuntimeError("publication root contains unsafe characters")
    if len(value.encode("utf-8")) > 4096:
        raise RuntimeError("publication root is too long")
    if any(part in (".", "..") for part in value.split("/")[1:]):
        raise RuntimeError("publication root contains unsafe components")
    return value.split("/")[1:]

def validate_relative(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or "//" in value:
        raise RuntimeError("publication file path is invalid")
    if value != unicodedata.normalize("NFC", value) or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RuntimeError("publication file path contains unsafe characters")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RuntimeError("publication file path contains unsafe components")
    return parts

def open_child_dir(parent, name, create=False):
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o700, dir_fd=parent)
        return os.open(name, DIR_FLAGS, dir_fd=parent)

def open_absolute(value, create=False):
    current = os.open("/", DIR_FLAGS)
    try:
        for part in validate_absolute(value):
            child = open_child_dir(current, part, create=create)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise

def open_relative_dir(root, parts, create=False):
    current = root
    owned = []
    try:
        for part in parts:
            child = open_child_dir(current, part, create=create)
            if current != root:
                owned.append(current)
            current = child
        return current, owned
    except BaseException:
        if current != root:
            os.close(current)
        for fd in reversed(owned):
            os.close(fd)
        raise

def close_owned(root, current, owned):
    if current != root:
        os.close(current)
    for fd in reversed(owned):
        os.close(fd)

def rename_noreplace(old_dir_fd, old_name, new_dir_fd, new_name):
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "atomic generation commit is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "atomic generation commit is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        RENAME_NOREPLACE,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), new_name)

def rename_exchange(first_dir_fd, first_name, second_dir_fd, second_name):
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "atomic activation exchange is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "atomic activation exchange is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        first_dir_fd,
        os.fsencode(first_name),
        second_dir_fd,
        os.fsencode(second_name),
        RENAME_EXCHANGE,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), second_name)

def remove_tree(parent, name):
    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("refusing to remove symlink")
    if stat.S_ISDIR(metadata.st_mode):
        child = os.open(name, DIR_FLAGS, dir_fd=parent)
        try:
            for entry in os.scandir(child):
                remove_tree(child, entry.name)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=parent)
    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
        os.unlink(name, dir_fd=parent)
    else:
        raise RuntimeError("refusing to remove special or hard-linked entry")

def read_file(fd, size):
    digest = hashlib.sha256()
    total = 0
    while total < size:
        chunk = read_exact(sys.stdin.buffer, min(CHUNK, size - total))
        offset = 0
        while offset < len(chunk):
            written = os.write(fd, chunk[offset:])
            if written <= 0:
                raise RuntimeError("could not write staged file")
            offset += written
        digest.update(chunk)
        total += len(chunk)
    return total, digest.hexdigest()

def expected_map(header):
    files = header.get("files")
    if not isinstance(files, list) or not files or len(files) > 100000:
        raise RuntimeError("invalid publication file list")
    result = {}
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("invalid publication file descriptor")
        path = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        mode = item.get("mode")
        parts = validate_relative(path)
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            raise RuntimeError("invalid publication file size")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise RuntimeError("invalid publication file digest")
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise RuntimeError("unsupported publication file mode")
        if path in result:
            raise RuntimeError("duplicate publication file")
        result[path] = (parts, size, digest, mode)
    if sum(item[1] for item in result.values()) > MAX_TOTAL_BYTES:
        raise RuntimeError("publication byte total exceeds the supported limit")
    return result

def validate_ids(header):
    operation = header.get("operation_id")
    generation = header.get("generation_id")
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise RuntimeError("invalid publish operation ID")
    if not isinstance(generation, str) or not re.fullmatch(r"v1-[0-9a-f]{64}", generation):
        raise RuntimeError("invalid publish generation ID")

def verify_tree(root, expected):
    found = {}
    def walk(directory, prefix):
        for entry in os.scandir(directory):
            relative = (prefix + "/" if prefix else "") + entry.name
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise RuntimeError("unexpected special or symlink entry")
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(entry.name, DIR_FLAGS, dir_fd=directory)
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
                continue
            if metadata.st_nlink != 1:
                raise RuntimeError("hard-linked staged file")
            found[relative] = metadata
    walk(root, "")
    if set(found) != set(expected):
        raise RuntimeError("staging tree contains an unexpected file")
    verified = {}
    for path, (parts, size, digest, mode) in expected.items():
        metadata = found[path]
        if metadata.st_size != size or stat.S_IMODE(metadata.st_mode) != mode:
            raise RuntimeError("staged file metadata mismatch")
        parts = path.split("/")
        parent, owned = open_relative_dir(root, parts[:-1], create=False)
        fd = os.open(parts[-1], READ_FLAGS, dir_fd=parent)
        try:
            actual = hashlib.sha256()
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                actual.update(chunk)
            if actual.hexdigest() != digest:
                raise RuntimeError("staged file digest mismatch")
            verified[path] = (size, digest, mode)
        finally:
            os.close(fd)
            close_owned(root, parent, owned)
    return verified

def read_generation_file(root, path):
    parts = validate_relative(path)
    parent, owned = open_relative_dir(root, parts[:-1], create=False)
    fd = os.open(parts[-1], READ_FLAGS, dir_fd=parent)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("semantic descriptor is not a regular file")
        if metadata.st_size > MAX_DESCRIPTOR_BYTES:
            raise RuntimeError("semantic descriptor exceeds the supported limit")
        chunks = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(CHUNK, remaining))
            if not chunk:
                raise RuntimeError("semantic descriptor was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
        close_owned(root, parent, owned)

def validate_generation_shape(generations, generation):
    fd = os.open(generation, DIR_FLAGS, dir_fd=generations)
    found = set()
    try:
        def walk(directory, prefix):
            for entry in os.scandir(directory):
                relative = (prefix + "/" if prefix else "") + entry.name
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    raise RuntimeError("previous generation contains an unsafe entry")
                if stat.S_ISDIR(metadata.st_mode):
                    child = os.open(entry.name, DIR_FLAGS, dir_fd=directory)
                    try:
                        walk(child, relative)
                    finally:
                        os.close(child)
                else:
                    if metadata.st_nlink != 1:
                        raise RuntimeError("previous generation contains a hard-linked file")
                    found.add(relative)
        walk(fd, "")
        if "pack.toml" not in found or "index.toml" not in found:
            raise RuntimeError("previous generation is missing semantic descriptors")
        pack_bytes = read_generation_file(fd, "pack.toml")
        index_bytes = read_generation_file(fd, "index.toml")
        try:
            pack = tomllib.loads(pack_bytes.decode("utf-8"))
            tomllib.loads(index_bytes.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as error:
            raise RuntimeError("previous generation descriptors are invalid") from error
        pack_index = pack.get("index")
        if (
            not isinstance(pack_index, dict)
            or pack_index.get("file") != "index.toml"
            or pack_index.get("hash-format") != "sha256"
            or pack_index.get("hash") != hashlib.sha256(index_bytes).hexdigest()
        ):
            raise RuntimeError("previous generation index reference is invalid")
    finally:
        os.close(fd)

def semantic_file_map(header):
    files = header.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("invalid semantic publication file list")
    result = {}
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("invalid semantic publication file descriptor")
        path = item.get("path")
        source_kind = item.get("source_kind")
        if not isinstance(path, str) or not isinstance(source_kind, str):
            raise RuntimeError("semantic publication file source is missing")
        validate_relative(path)
        if source_kind not in {"packwiz", "content", "generated"}:
            raise RuntimeError("unsupported semantic publication source")
        if path in result:
            raise RuntimeError("duplicate semantic publication file")
        result[path] = source_kind
    return result

def verify_semantics(header, generation, expected, verified):
    semantic_files = semantic_file_map(header)
    if set(semantic_files) != set(expected):
        raise RuntimeError("semantic file set does not match the manifest")
    if semantic_files.get("pack.toml") != "generated" or semantic_files.get("index.toml") != "generated":
        raise RuntimeError("semantic descriptors must be generated manifest files")
    target_side = header.get("target_side")
    minecraft = header.get("minecraft_version")
    loader = header.get("loader")
    loader_version = header.get("loader_version")
    loader_names = header.get("loader_names")
    if (
        target_side not in {"client", "server"}
        or not isinstance(minecraft, str) or not minecraft
        or not isinstance(loader, str) or not loader
        or not isinstance(loader_version, str) or not loader_version
        or not isinstance(loader_names, list)
        or any(not isinstance(name, str) or not name for name in loader_names)
        or len(set(loader_names)) != len(loader_names)
    ):
        raise RuntimeError("semantic publication metadata is invalid")
    pack_bytes = read_generation_file(generation, "pack.toml")
    index_bytes = read_generation_file(generation, "index.toml")
    try:
        pack = tomllib.loads(pack_bytes.decode("utf-8"))
        index = tomllib.loads(index_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError(f"invalid remote Packwiz TOML: {error}") from error
    versions = pack.get("versions")
    if not isinstance(versions, dict) or versions.get("minecraft") != minecraft:
        raise RuntimeError("remote pack.toml Minecraft version does not match manifest")
    active_loaders = [name for name in loader_names if name in versions]
    if active_loaders != [loader] or versions.get(loader) != loader_version:
        raise RuntimeError("remote pack.toml loader tuple does not match manifest")
    pack_index = pack.get("index")
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    if (
        not isinstance(pack_index, dict)
        or pack_index.get("file") != "index.toml"
        or pack_index.get("hash-format") != "sha256"
        or pack_index.get("hash") != index_digest
    ):
        raise RuntimeError("remote pack.toml index reference is invalid")
    records = index.get("files")
    if index.get("hash-format") != "sha256" or not isinstance(records, list) or len(records) > MAX_INDEX_RECORDS:
        raise RuntimeError("remote index.toml structure is invalid")
    expected_records = {
        path: path.endswith(".pw.toml")
        for path, source_kind in semantic_files.items()
        if source_kind == "packwiz" and path not in {"pack.toml", "index.toml"}
    }
    actual_records = {}
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("remote index.toml contains an invalid record")
        if set(record) - {"file", "hash", "metafile"}:
            raise RuntimeError("remote index.toml contains unsupported record fields")
        path = record.get("file")
        digest = record.get("hash")
        if not isinstance(path, str) or not isinstance(digest, str) or not HEX_DIGEST_RE.fullmatch(digest):
            raise RuntimeError("remote index.toml contains an invalid record")
        validate_relative(path)
        if path in actual_records or path not in expected_records:
            raise RuntimeError("remote index.toml contains an unexpected path")
        metafile = record.get("metafile", False)
        if not isinstance(metafile, bool) or metafile != expected_records[path]:
            raise RuntimeError("remote index.toml metafile semantics are invalid")
        if verified[path][1] != digest:
            raise RuntimeError("remote index.toml file digest does not match generation")
        actual_records[path] = metafile
    if actual_records != expected_records:
        raise RuntimeError("remote index.toml records do not match manifest semantics")
    return hashlib.sha256(pack_bytes).hexdigest(), index_digest

def lock_root(root):
    fd = os.open(".huroshiki-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd

def process_transfer(header):
    root = open_absolute(header["publication_root"], create=True)
    lock = None
    generations_fd = None
    stage_name = ".huroshiki-stage-" + header["operation_id"]
    generation_name = header["generation_id"]
    try:
        lock = lock_root(root)
        generations_fd = open_child_dir(root, "generations", create=True)
        expected = expected_map(header)
        if header.get("total_bytes") != sum(item[1] for item in expected.values()):
            raise RuntimeError("publication byte total does not match file list")
        try:
            final = os.open(generation_name, DIR_FLAGS, dir_fd=generations_fd)
        except FileNotFoundError:
            final = -1
        if final >= 0:
            try:
                verify_tree(final, expected)
            finally:
                os.close(final)
            for item in expected.values():
                frame_size = struct.unpack("!Q", read_exact(sys.stdin.buffer, 8))[0]
                if frame_size != item[1]:
                    raise RuntimeError("incoming file size frame mismatch")
                discard_exact(sys.stdin.buffer, frame_size)
            if sys.stdin.buffer.read(1):
                raise RuntimeError("trailing publish transfer data")
            return {
                "ok": True,
                "status": "reused",
                "operation_id": header["operation_id"],
                "manifest_digest": header["manifest_digest"],
                "target_config_digest": header["target_config_digest"],
                "generation_id": generation_name,
            }
        try:
            os.mkdir(stage_name, 0o700, dir_fd=generations_fd)
        except FileExistsError:
            raise RuntimeError("staging destination already exists")
        stage = os.open(stage_name, DIR_FLAGS, dir_fd=generations_fd)
        try:
            for path, (parts, size, digest, mode) in expected.items():
                parent_parts, filename = parts[:-1], parts[-1]
                parent, owned = open_relative_dir(stage, parent_parts, create=True)
                try:
                    fd = os.open(filename, WRITE_FLAGS, 0o600, dir_fd=parent)
                    try:
                        frame_size = struct.unpack("!Q", read_exact(sys.stdin.buffer, 8))[0]
                        if frame_size != size:
                            raise RuntimeError("incoming file size frame mismatch")
                        actual_size, actual_digest = read_file(fd, frame_size)
                        os.fchmod(fd, mode)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    close_owned(stage, parent, owned)
                if actual_size != size or actual_digest != digest:
                    raise RuntimeError("incoming file digest or size mismatch")
            verify_tree(stage, expected)
            os.fsync(stage)
            if sys.stdin.buffer.read(1):
                raise RuntimeError("trailing publish transfer data")
        finally:
            os.close(stage)
        try:
            rename_noreplace(
                generations_fd,
                stage_name,
                generations_fd,
                generation_name,
            )
        except FileExistsError:
            final = os.open(generation_name, DIR_FLAGS, dir_fd=generations_fd)
            try:
                verify_tree(final, expected)
            finally:
                os.close(final)
            try:
                remove_tree(generations_fd, stage_name)
            except FileNotFoundError:
                pass
            return {
                "ok": True,
                "status": "reused",
                "operation_id": header["operation_id"],
                "manifest_digest": header["manifest_digest"],
                "target_config_digest": header["target_config_digest"],
                "generation_id": generation_name,
            }
        return {
            "ok": True,
            "status": "committed",
            "operation_id": header["operation_id"],
            "manifest_digest": header["manifest_digest"],
            "target_config_digest": header["target_config_digest"],
            "generation_id": generation_name,
        }
    except BaseException as error:
        cleanup_error = None
        try:
            generations = open_child_dir(root, "generations", create=False)
            try:
                remove_tree(generations, stage_name)
            except FileNotFoundError:
                pass
            except BaseException as cleanup:
                cleanup_error = cleanup
            finally:
                os.close(generations)
        except BaseException as cleanup:
            cleanup_error = cleanup
        if cleanup_error is not None:
            recovery_path = header["publication_root"] + "/generations/" + stage_name
            raise TransferIntegrityFailure(
                f"{error}; remote staging cleanup failed: {cleanup_error}",
                recovery_path=recovery_path,
            ) from error
        raise
    finally:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
        if generations_fd is not None:
            os.close(generations_fd)
        os.close(root)

def process_status(header):
    root = open_absolute(header["publication_root"], create=False)
    lock = lock_root(root)
    try:
        generations = open_child_dir(root, "generations", create=False)
        try:
            expected = expected_map(header)
            try:
                final = os.open(header["generation_id"], DIR_FLAGS, dir_fd=generations)
            except FileNotFoundError:
                final = -1
            if final >= 0:
                try:
                    verify_tree(final, expected)
                finally:
                    os.close(final)
                response = {
                    "ok": True,
                    "status": "committed",
                    "operation_id": header["operation_id"],
                    "manifest_digest": header["manifest_digest"],
                    "target_config_digest": header["target_config_digest"],
                    "generation_id": header["generation_id"],
                }
                stage_name = ".huroshiki-stage-" + header["operation_id"]
                try:
                    os.stat(stage_name, dir_fd=generations, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    response["recovery_path"] = (
                        header["publication_root"]
                        + "/generations/"
                        + stage_name
                    )
                return response
            stage_name = ".huroshiki-stage-" + header["operation_id"]
            try:
                os.stat(stage_name, dir_fd=generations, follow_symlinks=False)
                return {
                    "ok": True,
                    "status": "not_committed",
                    "operation_id": header["operation_id"],
                    "manifest_digest": header["manifest_digest"],
                    "target_config_digest": header["target_config_digest"],
                    "generation_id": header["generation_id"],
                    "recovery_path": header["publication_root"] + "/generations/" + stage_name,
                }
            except FileNotFoundError:
                return {
                    "ok": True,
                    "status": "not_committed",
                    "operation_id": header["operation_id"],
                    "manifest_digest": header["manifest_digest"],
                    "target_config_digest": header["target_config_digest"],
                    "generation_id": header["generation_id"],
                }
        finally:
            os.close(generations)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        os.close(root)

def open_verified_generation(header, generations):
    expected = expected_map(header)
    generation = os.open(header["generation_id"], DIR_FLAGS, dir_fd=generations)
    try:
        verified = verify_tree(generation, expected)
        pack_digest, index_digest = verify_semantics(
            header,
            generation,
            expected,
            verified,
        )
        return expected, verified, pack_digest, index_digest
    finally:
        os.close(generation)

def current_generation(root, generations):
    try:
        metadata = os.stat("current", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("current is not a symlink")
    target = os.readlink("current", dir_fd=root)
    match = re.fullmatch(r"generations/(v1-[0-9a-f]{64})", target)
    if match is None:
        raise RuntimeError("current symlink target is unsafe")
    generation = match.group(1)
    validate_generation_shape(generations, generation)
    return generation

def activation_receipt_name(operation_id):
    return ".huroshiki-activation-" + operation_id + ".json"

def write_activation_receipt(root, header, previous, preexisting_expected):
    if not isinstance(preexisting_expected, bool):
        raise RuntimeError("activation receipt reuse state is invalid")
    payload = json.dumps(
        {
            "schema": "huroshiki-publish-activation-v1",
            "operation_id": header["operation_id"],
            "manifest_digest": header["manifest_digest"],
            "target_config_digest": header["target_config_digest"],
            "generation_id": header["generation_id"],
            "previous_generation_id": previous,
            "preexisting_expected": preexisting_expected,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    name = activation_receipt_name(header["operation_id"])
    fd = os.open(name, WRITE_FLAGS, 0o600, dir_fd=root)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise RuntimeError("could not write activation receipt")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(root)

def read_activation_receipt(root, header):
    name = activation_receipt_name(header["operation_id"])
    try:
        metadata = os.stat(name, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 4096:
        raise RuntimeError("activation receipt is unsafe")
    fd = os.open(name, READ_FLAGS, dir_fd=root)
    try:
        data = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        receipt = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("activation receipt is malformed") from error
    if not isinstance(receipt, dict) or receipt.get("schema") != "huroshiki-publish-activation-v1":
        raise RuntimeError("activation receipt is invalid")
    for key in ("operation_id", "manifest_digest", "target_config_digest", "generation_id"):
        if receipt.get(key) != header[key]:
            raise RuntimeError("activation receipt does not bind the request")
    previous = receipt.get("previous_generation_id")
    if previous is not None and (
        not isinstance(previous, str) or re.fullmatch(r"v1-[0-9a-f]{64}", previous) is None
    ):
        raise RuntimeError("activation receipt contains an invalid previous generation")
    preexisting_expected = receipt.get("preexisting_expected")
    if not isinstance(preexisting_expected, bool):
        raise RuntimeError("activation receipt contains an invalid reuse state")
    return {
        "previous_generation_id": previous,
        "preexisting_expected": preexisting_expected,
    }

def remove_activation_receipt(root, header):
    read_activation_receipt(root, header)
    try:
        os.unlink(activation_receipt_name(header["operation_id"]), dir_fd=root)
    except FileNotFoundError:
        pass

def activation_response(header, status, *, pack_digest=None, index_digest=None, previous=None, current=None, reused=None, error=None):
    response = {
        "ok": error is None,
        "request": header["request"],
        "status": status,
        "operation_id": header["operation_id"],
        "manifest_digest": header["manifest_digest"],
        "target_config_digest": header["target_config_digest"],
        "generation_id": header["generation_id"],
    }
    if pack_digest is not None:
        response["pack_toml_sha256"] = pack_digest
    if index_digest is not None:
        response["index_toml_sha256"] = index_digest
    if previous is not None or "previous_generation_id" in header or status == "reused":
        response["previous_generation_id"] = previous
    if current is not None or status in {"activated", "reused", "not_activated", "uncertain"}:
        response["current_generation_id"] = current
    if reused is not None:
        response["reused"] = reused
    if error is not None:
        response["error"] = str(error)[:512]
    return response

def process_verify(header):
    root = open_absolute(header["publication_root"], create=False)
    lock = lock_root(root)
    try:
        generations = open_child_dir(root, "generations", create=False)
        try:
            _, _, pack_digest, index_digest = open_verified_generation(header, generations)
            return {
                "ok": True,
                "request": "verify",
                "status": "verified",
                "operation_id": header["operation_id"],
                "manifest_digest": header["manifest_digest"],
                "target_config_digest": header["target_config_digest"],
                "generation_id": header["generation_id"],
                "pack_toml_sha256": pack_digest,
                "index_toml_sha256": index_digest,
            }
        finally:
            os.close(generations)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        os.close(root)

def remove_activation_temp(root, name, expected_target):
    try:
        metadata = os.stat(name, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISLNK(metadata.st_mode) or os.readlink(name, dir_fd=root) != expected_target:
        raise RuntimeError("activation temporary entry is unsafe")
    os.unlink(name, dir_fd=root)

def process_activate(header):
    root = open_absolute(header["publication_root"], create=False)
    lock = lock_root(root)
    generations = None
    temp_name = ".huroshiki-current-" + header["operation_id"]
    temp_target = "generations/" + header["generation_id"]
    temp_created = False
    try:
        generations = open_child_dir(root, "generations", create=False)
        _, _, pack_digest, index_digest = open_verified_generation(header, generations)
        current = current_generation(root, generations)
        if current == header["generation_id"]:
            write_activation_receipt(root, header, None, True)
            return activation_response(
                header,
                "reused",
                pack_digest=pack_digest,
                index_digest=index_digest,
                previous=None,
                current=current,
                reused=True,
            )
        previous = current
        try:
            os.stat(temp_name, dir_fd=root, follow_symlinks=False)
            raise RuntimeError("activation temporary entry already exists")
        except FileNotFoundError:
            pass
        write_activation_receipt(root, header, previous, False)
        os.symlink(temp_target, temp_name, dir_fd=root)
        temp_created = True
        os.fsync(root)
        if current is None:
            try:
                rename_noreplace(root, temp_name, root, "current")
            except FileExistsError as error:
                raise RuntimeError("current changed while activation was preparing") from error
            temp_created = False
        else:
            rename_exchange(root, temp_name, root, "current")
            temp_created = False
            try:
                if os.readlink(temp_name, dir_fd=root) != "generations/" + current:
                    raise RuntimeError("current changed while activation was preparing")
                os.unlink(temp_name, dir_fd=root)
            except BaseException:
                try:
                    rename_exchange(root, temp_name, root, "current")
                except BaseException as rollback:
                    raise TransferIntegrityFailure(
                        "activation compare-and-swap rollback failed",
                        recovery_path=header["publication_root"] + "/" + temp_name,
                    ) from rollback
                temp_created = True
                raise
        if current_generation(root, generations) != header["generation_id"]:
            raise RuntimeError("current did not activate the expected generation")
        current_fd = os.open(header["generation_id"], DIR_FLAGS, dir_fd=generations)
        try:
            verified = verify_tree(current_fd, expected_map(header))
            verify_semantics(header, current_fd, expected_map(header), verified)
        finally:
            os.close(current_fd)
        os.fsync(root)
        return activation_response(
            header,
            "activated",
            pack_digest=pack_digest,
            index_digest=index_digest,
            previous=previous,
            current=header["generation_id"],
            reused=False,
        )
    except BaseException as error:
        cleanup_error = None
        if temp_created:
            try:
                remove_activation_temp(root, temp_name, temp_target)
            except BaseException as cleanup:
                cleanup_error = cleanup
        if cleanup_error is not None:
            raise TransferIntegrityFailure(
                f"{error}; activation temporary cleanup failed: {cleanup_error}",
                recovery_path=header["publication_root"] + "/" + temp_name,
            ) from error
        raise
    finally:
        if generations is not None:
            os.close(generations)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        os.close(root)

def process_activation_status(header):
    root = open_absolute(header["publication_root"], create=False)
    lock = lock_root(root)
    generations = None
    try:
        generations = open_child_dir(root, "generations", create=False)
        try:
            receipt = read_activation_receipt(root, header)
            current = current_generation(root, generations)
        except BaseException:
            return activation_response(header, "uncertain", current=None)
        if receipt is None:
            return activation_response(header, "uncertain", current=current)
        receipt_previous = receipt["previous_generation_id"]
        if receipt["preexisting_expected"]:
            if current != header["generation_id"]:
                return activation_response(header, "uncertain", current=current)
            try:
                _, _, pack_digest, index_digest = open_verified_generation(header, generations)
            except BaseException:
                return activation_response(header, "uncertain", current=current)
            return activation_response(
                header,
                "reused",
                pack_digest=pack_digest,
                index_digest=index_digest,
                previous=None,
                current=current,
                reused=True,
            )
        if current == header["generation_id"]:
            try:
                _, _, pack_digest, index_digest = open_verified_generation(header, generations)
            except BaseException:
                return activation_response(header, "uncertain", current=current)
            return activation_response(
                header,
                "activated",
                pack_digest=pack_digest,
                index_digest=index_digest,
                previous=receipt_previous,
                current=current,
                reused=False,
            )
        if receipt_previous is not None and current == receipt_previous:
            return activation_response(
                header,
                "not_activated",
                previous=receipt_previous,
                current=current,
                reused=False,
            )
        if receipt_previous is None and current is None:
            return activation_response(
                header,
                "not_activated",
                previous=None,
                current=current,
                reused=False,
            )
        return activation_response(
            header,
            "uncertain",
            current=current,
        )
    finally:
        if generations is not None:
            os.close(generations)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        os.close(root)

def process_activation_cleanup(header):
    root = open_absolute(header["publication_root"], create=False)
    lock = lock_root(root)
    generations = None
    try:
        finalize_receipt = header.get("finalize_receipt", False)
        if not isinstance(finalize_receipt, bool):
            raise RuntimeError("activation cleanup finalization flag is invalid")
        expected_status = header.get("expected_activation_status")
        if finalize_receipt and expected_status not in {"activated", "reused", "not_activated"}:
            raise RuntimeError("activation cleanup outcome binding is invalid")
        if not finalize_receipt and expected_status is not None:
            raise RuntimeError("activation cleanup outcome binding is unexpected")
        receipt = None
        if finalize_receipt:
            receipt = read_activation_receipt(root, header)
            generations = open_child_dir(root, "generations", create=False)
            current = current_generation(root, generations)
            if receipt is None:
                if (
                    expected_status not in {"activated", "reused"}
                    or current != header["generation_id"]
                ):
                    raise RuntimeError("activation cleanup requires a causal receipt")
                open_verified_generation(header, generations)
            elif expected_status == "reused":
                if (
                    not receipt["preexisting_expected"]
                    or current != header["generation_id"]
                ):
                    raise RuntimeError("activation cleanup outcome is not reused")
                open_verified_generation(header, generations)
            elif expected_status == "activated":
                if receipt["preexisting_expected"] or current != header["generation_id"]:
                    raise RuntimeError("activation cleanup outcome is not activated")
                open_verified_generation(header, generations)
            elif receipt["preexisting_expected"]:
                raise RuntimeError("activation cleanup outcome is not not-activated")
            elif current != receipt["previous_generation_id"]:
                raise RuntimeError("activation cleanup outcome is not not-activated")
        remove_activation_temp(
            root,
            ".huroshiki-current-" + header["operation_id"],
            "generations/" + header["generation_id"],
        )
        if finalize_receipt:
            remove_activation_receipt(root, header)
        os.fsync(root)
        return {
            "ok": True,
            "request": "activation-cleanup",
            "status": "cleaned",
            "operation_id": header["operation_id"],
            "manifest_digest": header["manifest_digest"],
            "target_config_digest": header["target_config_digest"],
            "generation_id": header["generation_id"],
            "finalize_receipt": finalize_receipt,
            "expected_activation_status": expected_status,
        }
    finally:
        if generations is not None:
            os.close(generations)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        os.close(root)

def process_cleanup(header):
    root = open_absolute(header["publication_root"], create=False)
    lock = lock_root(root)
    try:
        generations = open_child_dir(root, "generations", create=False)
        try:
            stage_name = ".huroshiki-stage-" + header["operation_id"]
            try:
                remove_tree(generations, stage_name)
            except FileNotFoundError:
                pass
            return {"ok": True, "status": "cleaned"}
        finally:
            os.close(generations)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        os.close(root)

def main():
    try:
        if read_exact(sys.stdin.buffer, len(MAGIC)) != MAGIC:
            raise RuntimeError("invalid publish transfer magic")
        version = struct.unpack("!I", read_exact(sys.stdin.buffer, 4))[0]
        if version != VERSION:
            raise RuntimeError("unsupported publish transfer version")
        size = struct.unpack("!I", read_exact(sys.stdin.buffer, 4))[0]
        if size <= 0 or size > MAX_HEADER:
            raise RuntimeError("invalid publish transfer header size")
        header = json.loads(read_exact(sys.stdin.buffer, size).decode("utf-8"))
        if not isinstance(header, dict) or header.get("schema") != "huroshiki-publish-transfer-v1":
            raise RuntimeError("invalid publish transfer header")
        validate_ids(header)
        request = header.get("request")
        if request == "transfer":
            send(process_transfer(header))
        elif request == "status":
            send(process_status(header))
        elif request == "verify":
            send(process_verify(header))
        elif request == "activate":
            send(process_activate(header))
        elif request == "activation-status":
            send(process_activation_status(header))
        elif request == "activation-cleanup":
            send(process_activation_cleanup(header))
        elif request == "cleanup":
            send(process_cleanup(header))
        else:
            raise RuntimeError("unknown publish transfer request")
    except TransferIntegrityFailure as error:
        response = {
            "ok": False,
            "status": "integrity_failure",
            "error": str(error)[:512],
        }
        if error.recovery_path is not None:
            response["recovery_path"] = error.recovery_path
        send(response)
        return 1
    except BaseException as error:
        send({"ok": False, "status": "integrity_failure", "error": str(error)[:512]})
        return 1
    return 0

raise SystemExit(main())
'''


_REMOTE_HELPER_SCRIPT = _remote_helper_source()
_REMOTE_HELPER_PAYLOAD = base64.b64encode(_REMOTE_HELPER_SCRIPT.encode("utf-8")).decode("ascii")
_REMOTE_COMMAND = "python3 -c " + shlex.quote(
    f'import base64;exec(base64.b64decode("{_REMOTE_HELPER_PAYLOAD}"))'
)


def _default_deadline(deadline: float | None) -> float:
    return deadline if deadline is not None else time.monotonic() + _TRANSFER_TIMEOUT_SECONDS


def _checkpoint(
    cancel_event: threading.Event | None,
    deadline: float,
    message: str = "Publish transfer was cancelled",
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PublishTransferError(message)
    if time.monotonic() >= deadline:
        raise PublishTransferError("Publish transfer deadline exceeded")


def _emit(
    progress: Callable[[PublishTransferProgress], object] | Callable[[str], object] | None,
    value: PublishTransferProgress,
) -> None:
    if progress is None:
        return
    try:
        progress(value)
    except Exception:
        pass


def compute_publish_generation_id(
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
) -> str:
    validate_publish_manifest(manifest)
    if not isinstance(target, PublishRemoteTarget):
        raise PublishTransferError("publish transfer requires a PublishRemoteTarget")
    payload = (
        _GENERATION_SCHEMA.encode("ascii")
        + b"\0"
        + bytes.fromhex(manifest.manifest_digest)
        + bytes.fromhex(target.config_digest)
    )
    return "v1-" + hashlib.sha256(payload).hexdigest()


def _repository_root() -> Path:
    return Path(packctl.PACKS).parent


def _allocate_workspace(operation_id: str) -> tuple[Path, Path, tuple[int, int]]:
    repository = _repository_root()
    state_root = repository / ".huroshiki"
    transfer_root = packctl.make_state_directory(
        state_root / "transactions" / "publish-transfer",
        state_root=state_root,
        repository_root=repository,
    )
    workspace = packctl.make_state_directory(
        transfer_root / operation_id,
        state_root=state_root,
        repository_root=repository,
    )
    os.chmod(workspace, 0o700)
    payload = workspace / "payload"
    payload.mkdir(mode=0o700)
    os.chmod(payload, 0o700)
    identity = os.stat(workspace, follow_symlinks=False)
    return workspace, payload, (identity.st_dev, identity.st_ino)


def _directory_map(scan) -> dict[Path, object]:
    return {entry.relative_path: entry for entry in scan.entries if entry.kind == "directory"}


def _scan_pack(pack_id: str, cancel_event: threading.Event | None, deadline: float):
    root = Path(os.path.abspath(packctl.PACKS)) / pack_id
    _checkpoint(cancel_event, deadline)
    scan = scan_pack_migration_source(
        root,
        checkpoint=lambda: _checkpoint(cancel_event, deadline),
        excluded_roots=frozenset(
            {".huroshiki", "crash-reports", "dist", "logs", "saves", "screenshots", "secrets", "world", "worlds"}
        ),
    )
    if any(entry.kind == "invalid" for entry in scan.entries):
        raise PublishTransferPlanningError("Pack snapshot contains unsafe entries")
    return root, scan


def _write_exact(path: Path, data: bytes, mode: int, checkpoint: Callable[[], None]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, _F_WRITE_FLAGS, 0o600)
    try:
        offset = 0
        while offset < len(data):
            checkpoint()
            written = os.write(fd, data[offset : offset + _CHUNK])
            if written <= 0:
                raise PublishTransferPlanningError(f"could not write detached file: {path}")
            offset += written
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_source_entry(
    destination: Path,
    root_fd: int,
    source_relative: Path,
    source_entry,
    directories: dict[Path, object],
    mode: int,
    checkpoint: Callable[[], None],
) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, _F_WRITE_FLAGS, 0o600)
    output = os.fdopen(fd, "wb", closefd=False)
    try:
        read_snapshot_file(
            root_fd,
            source_relative,
            source_entry,
            directories=directories,
            checkpoint=checkpoint,
            retain_bytes=False,
            sink=output,
        )
        output.flush()
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        output.close()
        os.close(fd)


def _read_detached(path: Path, expected: PublishFileEntry, checkpoint: Callable[[], None]) -> None:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PublishTransferError(f"unsafe detached publication file: {expected.relative_path}")
    digest = hashlib.sha256()
    fd = os.open(path, _F_READ_FLAGS)
    try:
        while True:
            checkpoint()
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    after = os.stat(path, follow_symlinks=False)
    if (
        after.st_size != expected.size
        or stat.S_IMODE(after.st_mode) != expected.mode
        or digest.hexdigest() != expected.sha256
    ):
        raise PublishTransferError(f"detached publication file changed: {expected.relative_path}")
def _copy_detached_to_spool(
    path: Path,
    expected: PublishFileEntry,
    output,
    checkpoint: Callable[[], None],
) -> None:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PublishTransferExecutionError(f"unsafe detached publication file: {expected.relative_path}")
    digest = hashlib.sha256()
    total = 0
    fd = os.open(path, _F_READ_FLAGS)
    try:
        while True:
            checkpoint()
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    after = os.stat(path, follow_symlinks=False)
    if (
        total != expected.size
        or after.st_size != expected.size
        or stat.S_IMODE(after.st_mode) != expected.mode
        or digest.hexdigest() != expected.sha256
    ):
        raise PublishTransferExecutionError(f"detached publication file changed: {expected.relative_path}")


def _workspace_digest(files: tuple[PublishStagedFile, ...]) -> str:
    payload = [
        {"path": item.relative_path.as_posix(), "size": item.size, "sha256": item.sha256, "mode": item.mode}
        for item in files
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _verify_workspace(
    payload_root: Path,
    manifest: PackPublishManifest,
    checkpoint: Callable[[], None],
) -> tuple[tuple[PublishStagedFile, ...], str]:
    expected = {entry.relative_path.as_posix(): entry for entry in manifest.files}
    found: list[PublishStagedFile] = []
    def walk(directory: Path, prefix: PurePosixPath) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            checkpoint()
            relative = prefix / child.name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode):
                raise PublishTransferPlanningError(f"unsafe detached entry: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                walk(child, relative)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PublishTransferPlanningError(f"unsafe detached entry: {relative}")
            entry = expected.get(relative.as_posix())
            if entry is None:
                raise PublishTransferPlanningError(f"unexpected detached entry: {relative}")
            _read_detached(child, entry, checkpoint)
            found.append(PublishStagedFile(relative, entry.size, entry.sha256, entry.mode))

    walk(payload_root, PurePosixPath())
    if {item.relative_path.as_posix() for item in found} != set(expected):
        raise PublishTransferPlanningError("detached workspace does not match manifest")
    result = tuple(sorted(found, key=lambda item: item.relative_path.as_posix()))
    return result, _workspace_digest(result)


def prepare_publish_transfer(
    pack_id: str,
    manifest: PackPublishManifest,
    target: PublishRemoteTarget,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PublishTransferProgress], object] | Callable[[str], object] | None = None,
) -> PublishTransferPlan:
    operation_deadline = _default_deadline(deadline)
    try:
        validate_publish_manifest(manifest)
    except PackPublishError as error:
        raise PublishTransferPlanningError(str(error)) from error
    if not isinstance(target, PublishRemoteTarget):
        raise PublishTransferPlanningError("publish transfer requires a PublishRemoteTarget")
    if pack_id != manifest.pack_id:
        raise PublishTransferPlanningError("publish transfer pack ID does not match manifest")
    if len(manifest.files) > _MAX_FILES or manifest.total_bytes > _MAX_TOTAL_BYTES:
        raise PublishTransferPlanningError("publication manifest exceeds transfer limits")
    if any(entry.size > _MAX_FILE_BYTES for entry in manifest.files):
        raise PublishTransferPlanningError("publication file exceeds transfer limits")
    _checkpoint(cancel_event, operation_deadline)
    try:
        initial = plan_pack_publish_manifest(
            pack_id,
            target_side=manifest.target_side,
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
    except Exception as error:
        raise PublishTransferPlanningError(f"could not re-plan current Pack: {error}") from error
    if initial.manifest_digest != manifest.manifest_digest:
        raise PublishTransferPlanningError("stale source manifest")

    operation_id = uuid4().hex
    workspace: Path | None = None
    cleanup_owner: PublishTransferPlan | None = None
    lock_set = None
    try:
        _emit(progress, PublishTransferProgress("acquiring-lock", 0, len(manifest.files), 0, manifest.total_bytes))
        lock_set = packctl.acquire_project_locks(
            (f"pack:{pack_id}",),
            deadline=operation_deadline,
            cancel_event=cancel_event,
            operation="prepare Publish transfer",
        )
        root, scan = _scan_pack(pack_id, cancel_event, operation_deadline)
        if scan.root_identity != (os.stat(root, follow_symlinks=False).st_dev, os.stat(root, follow_symlinks=False).st_ino):
            raise PublishTransferPlanningError("Pack root changed while preparing transfer")
        if scan.content_digest != manifest.source_snapshot_digest:
            raise PublishTransferPlanningError("stale source snapshot")
        workspace, payload_root, workspace_identity = _allocate_workspace(operation_id)
        cleanup_owner = PublishTransferPlan(
            pack_id=pack_id,
            manifest=manifest,
            target=target,
            operation_id=operation_id,
            workspace=workspace,
            payload_root=payload_root,
            workspace_identity=workspace_identity,
            workspace_digest="",
            generation_id=compute_publish_generation_id(manifest, target),
        )
        total_files = len(manifest.files)
        completed_bytes = 0
        entries = {entry.relative_path: entry for entry in scan.entries}
        directories = _directory_map(scan)
        staged: list[PublishStagedFile] = []
        for index, entry in enumerate(manifest.files, start=1):
            _checkpoint(cancel_event, operation_deadline)
            destination = payload_root / Path(*entry.relative_path.parts)
            if entry.source_kind == "generated":
                data = entry.contents
                if data is None or len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise PublishTransferPlanningError(
                        f"materialized bytes do not match manifest: {entry.relative_path}"
                    )
                _write_exact(
                    destination,
                    data,
                    entry.mode,
                    lambda: _checkpoint(cancel_event, operation_deadline),
                )
            else:
                source_relative = Path(*entry.source_relative_path.parts)
                source_entry = entries.get(source_relative)
                if source_entry is None or source_entry.kind != "file":
                    raise PublishTransferPlanningError(f"source path is unavailable: {source_relative}")
                if (
                    source_entry.size != entry.size
                    or source_entry.digest != entry.sha256
                    or stat.S_IMODE(source_entry.mode) != entry.mode
                ):
                    raise PublishTransferPlanningError(
                        f"source metadata does not match manifest: {source_relative}"
                    )
                try:
                    root_fd = os.open(root, _D_FLAGS)
                    try:
                        _write_source_entry(
                            destination,
                            root_fd,
                            source_relative,
                            source_entry,
                            directories=directories,
                            mode=entry.mode,
                            checkpoint=lambda: _checkpoint(cancel_event, operation_deadline),
                        )
                    finally:
                        os.close(root_fd)
                except (OSError, PackSnapshotReadError) as error:
                    raise PublishTransferPlanningError(f"could not materialize {source_relative}: {error}") from error
            staged.append(PublishStagedFile(entry.relative_path, entry.size, entry.sha256, entry.mode))
            completed_bytes += entry.size
            _emit(progress, PublishTransferProgress("materializing", index, total_files, completed_bytes, manifest.total_bytes, entry.relative_path))
        staged_files, workspace_digest = _verify_workspace(
            payload_root,
            manifest,
            lambda: _checkpoint(cancel_event, operation_deadline),
        )
        if lock_set is not None:
            lock_set.release()
            lock_set = None
        _emit(progress, PublishTransferProgress("verifying-source", total_files, total_files, completed_bytes, manifest.total_bytes))
        final = plan_pack_publish_manifest(
            pack_id,
            target_side=manifest.target_side,
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
        if final.manifest_digest != manifest.manifest_digest:
            raise PublishTransferPlanningError("Pack changed while materializing transfer")
        with cleanup_owner._lock:
            cleanup_owner._workspace_digest = workspace_digest
        return cleanup_owner
    except BaseException as error:
        primary_error: BaseException = error
        if isinstance(error, Exception) and not isinstance(error, PublishTransferError):
            primary_error = PublishTransferPlanningError(str(error))
        if cleanup_owner is not None and workspace is not None and _workspace_exists(workspace):
            with cleanup_owner._lock:
                cleanup_owner._state = "failed"
            try:
                discard_publish_transfer_plan(
                    cleanup_owner,
                    deadline=time.monotonic() + 10.0,
                )
            except BaseException as cleanup_error:
                raise PublishTransferCleanupError(
                    "Publish transfer preparation cleanup is pending",
                    plan=cleanup_owner,
                    primary_error=primary_error,
                ) from cleanup_error
        if primary_error is error:
            raise
        raise primary_error from error
    finally:
        if lock_set is not None:
            try:
                lock_set.release()
            except BaseException:
                pass


def _workspace_exists(workspace: Path) -> bool:
    try:
        return workspace.exists() or workspace.is_symlink()
    except OSError:
        return True


def _remove_tree(path: Path, deadline: float) -> None:
    _checkpoint(None, deadline)
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise PublishTransferCleanupError("refusing to remove a symlinked transfer workspace")
    if stat.S_ISDIR(metadata.st_mode):
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _remove_tree(child, deadline)
        path.rmdir()
    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
        path.unlink()
    else:
        raise PublishTransferCleanupError("refusing to remove an unsafe transfer entry")


def _cleanup_plan_workspace(plan: PublishTransferPlan, deadline: float) -> None:
    try:
        metadata = os.stat(plan._workspace, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) != plan._workspace_identity:
        raise PublishTransferCleanupError("transfer workspace identity changed")
    _remove_tree(plan._workspace, deadline)


def discard_publish_transfer_plan(
    plan: PublishTransferPlan,
    *,
    deadline: float | None = None,
) -> None:
    if not isinstance(plan, PublishTransferPlan):
        raise PublishTransferCleanupError("foreign publish transfer plan")
    cleanup_deadline = _default_deadline(deadline)
    with plan._lock:
        if plan._state == "discarded":
            return
        if plan._state == "discarding":
            raise PublishTransferCleanupError("publish transfer cleanup is already running")
        remote_cleanup_pending = plan._recovery_path is not None
        plan._state = "discarding"
    try:
        if remote_cleanup_pending:
            _cleanup_remote_publish_stage(plan, cleanup_deadline)
        _cleanup_plan_workspace(plan, cleanup_deadline)
    except BaseException as error:
        with plan._lock:
            plan._state = "cleanup-pending"
        if isinstance(error, PublishTransferCleanupError) and error.plan is plan:
            raise
        raise PublishTransferCleanupError(
            str(error),
            plan=plan,
            primary_error=(
                error.primary_error
                if isinstance(error, PublishTransferCleanupError)
                else None
            ),
        ) from error
    with plan._lock:
        plan._state = "discarded"


def retry_discard_publish_transfer_plan(
    plan: PublishTransferPlan,
    *,
    deadline: float | None = None,
) -> None:
    discard_publish_transfer_plan(plan, deadline=deadline)


def _endpoint_destination(target: PublishRemoteTarget) -> str:
    endpoint = target.publication_endpoint
    host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    return f"{endpoint.user}@{host}" if endpoint.user else host


def _ssh_command(target: PublishRemoteTarget) -> list[str]:
    endpoint = target.publication_endpoint
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_SECONDS}",
        "-p",
        str(endpoint.port),
        "--",
        _endpoint_destination(target),
        _REMOTE_COMMAND,
    ]


def _header(
    request: PublishRemoteRequest,
    plan: PublishTransferPlan,
) -> dict[str, object]:
    files = [
        {
            "path": entry.relative_path.as_posix(),
            "size": entry.size,
            "sha256": entry.sha256,
            "mode": entry.mode,
            "source_kind": entry.source_kind,
        }
        for entry in plan.manifest.files
    ]
    return build_publish_remote_header(
        request,
        operation_id=plan.operation_id,
        manifest_digest=plan.manifest_digest,
        source_snapshot_digest=plan.source_snapshot_digest,
        target_config_digest=plan.target_config_digest,
        generation_id=plan.generation_id,
        publication_root=plan.target.publication_root,
        files=files,
        total_bytes=plan.manifest.total_bytes,
        semantic={
            "target_side": plan.target_side,
            "minecraft_version": plan.manifest.minecraft_version,
            "loader": plan.manifest.loader,
            "loader_version": plan.manifest.loader_version,
            "loader_names": sorted(packctl.LOADER_FLAGS),
        },
    )


def build_publish_remote_header(
    request: PublishRemoteRequest,
    *,
    operation_id: str,
    manifest_digest: str,
    source_snapshot_digest: str,
    target_config_digest: str,
    generation_id: str,
    publication_root: PurePosixPath,
    files: list[dict[str, object]],
    total_bytes: int,
    semantic: dict[str, object] | None = None,
) -> dict[str, object]:
    header = {
        "schema": _TRANSFER_SCHEMA,
        "version": _FRAME_VERSION,
        "request": request,
        "operation_id": operation_id,
        "manifest_digest": manifest_digest,
        "source_snapshot_digest": source_snapshot_digest,
        "target_config_digest": target_config_digest,
        "generation_id": generation_id,
        "publication_root": publication_root.as_posix(),
        "files": files,
        "total_bytes": total_bytes,
    }
    if semantic is not None:
        header.update(semantic)
    return header


def _write_protocol_header(handle, header: dict[str, object]) -> None:
    encoded = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_HEADER_BYTES:
        raise PublishTransferExecutionError("publish transfer control header is too large")
    handle.write(_FRAME_HEADER)
    handle.write(struct.pack("!I", _FRAME_VERSION))
    handle.write(struct.pack("!I", len(encoded)))
    handle.write(encoded)


def _make_spool(plan: PublishTransferPlan, spool_path: Path, deadline: float, cancel_event, progress) -> None:
    with spool_path.open("wb") as handle:
        _write_protocol_header(handle, _header("transfer", plan))
        completed = 0
        for index, entry in enumerate(plan.manifest.files, start=1):
            _checkpoint(cancel_event, deadline)
            payload = plan._payload_root / Path(*entry.relative_path.parts)
            handle.write(struct.pack("!Q", entry.size))
            _copy_detached_to_spool(
                payload,
                entry,
                handle,
                lambda: _checkpoint(cancel_event, deadline),
            )
            completed += entry.size
            _emit(progress, PublishTransferProgress("transferring", index, len(plan.manifest.files), completed, plan.manifest.total_bytes, entry.relative_path))
        handle.flush()
        os.fsync(handle.fileno())


def _run_remote_request(
    plan: PublishTransferPlan,
    request: PublishRemoteRequest,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[BoundedProcessResult, dict[str, object] | None]:
    if request != "transfer":
        return run_publish_remote_control_request(
            plan.target,
            _header(request, plan),
            deadline=deadline,
            cancel_event=cancel_event,
        )
    repository = _repository_root()
    state_root = repository / ".huroshiki"
    packctl.make_state_directory(
        state_root,
        state_root=state_root,
        repository_root=repository,
    )
    spool = tempfile.NamedTemporaryFile(
        prefix="publish-transfer-",
        dir=state_root,
        delete=False,
    )
    spool_path = Path(spool.name)
    spool.close()
    try:
        _make_spool(plan, spool_path, deadline, cancel_event, None)
        with spool_path.open("rb") as input_handle:
            result = run_bounded_process(
                _ssh_command(plan.target),
                cwd=repository,
                stdin_file=input_handle,
                cancel_event=cancel_event,
                deadline=deadline,
                max_output_bytes=_MAX_REMOTE_OUTPUT,
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return result, None
        if len(lines) != 1:
            return result, None
        try:
            response = json.loads(lines[0])
        except (TypeError, json.JSONDecodeError):
            return result, None
        if not isinstance(response, dict):
            return result, None
        return result, response
    finally:
        try:
            spool.close()
        except OSError:
            pass
        try:
            spool_path.unlink()
        except FileNotFoundError:
            pass


def run_publish_remote_control_request(
    target: PublishRemoteTarget,
    header: dict[str, object],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[BoundedProcessResult, dict[str, object] | None]:
    repository = _repository_root()
    state_root = repository / ".huroshiki"
    packctl.make_state_directory(
        state_root,
        state_root=state_root,
        repository_root=repository,
    )
    spool = tempfile.NamedTemporaryFile(
        prefix="publish-transfer-",
        dir=state_root,
        delete=False,
    )
    spool_path = Path(spool.name)
    spool.close()
    try:
        with spool_path.open("wb") as control:
            _write_protocol_header(control, header)
            control.flush()
            os.fsync(control.fileno())
        with spool_path.open("rb") as input_handle:
            result = run_bounded_process(
                _ssh_command(target),
                cwd=repository,
                stdin_file=input_handle,
                cancel_event=cancel_event,
                deadline=deadline,
                max_output_bytes=_MAX_REMOTE_OUTPUT,
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return result, None
        try:
            response = json.loads(lines[0])
        except (TypeError, json.JSONDecodeError):
            return result, None
        if not isinstance(response, dict):
            return result, None
        return result, response
    finally:
        try:
            spool.close()
        except OSError:
            pass
        try:
            spool_path.unlink()
        except FileNotFoundError:
            pass


def _cleanup_remote_publish_stage(
    plan: PublishTransferPlan,
    deadline: float,
) -> None:
    try:
        cleanup_result, cleanup_response = _run_remote_request(
            plan,
            "cleanup",
            deadline=min(deadline, time.monotonic() + 30.0),
            cancel_event=None,
        )
    except BaseException as error:
        with plan._lock:
            plan._state = "cleanup-pending"
        raise PublishTransferCleanupError(
            "remote transfer cleanup could not be verified"
        ) from error
    cleanup_lifecycle_failure = _process_lifecycle_failure(
        cleanup_result,
        label="Publish transfer cleanup",
    )
    if (
        cleanup_lifecycle_failure is not None
        or not cleanup_result.succeeded
        or not cleanup_response
        or cleanup_response.get("ok") is not True
        or cleanup_response.get("status") != "cleaned"
    ):
        with plan._lock:
            plan._state = "cleanup-pending"
        detail = (
            cleanup_lifecycle_failure
            or (
                str(cleanup_response.get("error"))
                if cleanup_response is not None
                and cleanup_response.get("error") is not None
                else "remote transfer cleanup did not complete"
            )
        )
        raise PublishTransferCleanupError(detail)
    with plan._lock:
        plan._recovery_path = None


def _resolve_current_target(plan: PublishTransferPlan) -> PublishRemoteTarget:
    try:
        settings = packctl.deployment_settings(plan.pack_id)
        return publish_remote_target_from_legacy_settings(
            rsync_target=settings.rsync_target,
            ssh_host=settings.ssh_host,
            stack_dir=settings.stack_dir,
            service=settings.service,
            remote_path=plan.target.publication_root.as_posix(),
        )
    except (packctl.ConfigError, PublishTargetError) as error:
        raise PublishTransferExecutionError(str(error)) from error


def _result_files(plan: PublishTransferPlan) -> tuple[PublishStagedFile, ...]:
    return tuple(
        PublishStagedFile(entry.relative_path, entry.size, entry.sha256, entry.mode)
        for entry in plan.manifest.files
    )


def _validate_committed_response(
    plan: PublishTransferPlan,
    response: dict[str, object],
) -> None:
    expected = {
        "operation_id": plan.operation_id,
        "manifest_digest": plan.manifest_digest,
        "target_config_digest": plan.target_config_digest,
        "generation_id": plan.generation_id,
    }
    for key, value in expected.items():
        if response.get(key) != value:
            raise PublishTransferExecutionError(
                f"remote helper response does not bind to transfer {key}"
            )
    if (
        "recovery_path" in response
        and response.get("recovery_path") != plan.staging_path.as_posix()
    ):
        raise PublishTransferExecutionError(
            "remote helper response does not bind to transfer recovery path"
        )


def _process_lifecycle_failure(
    result: BoundedProcessResult,
    *,
    label: str,
) -> str | None:
    if result.termination_incomplete:
        return f"{label} process termination was incomplete"
    if result.orphaned_descendants:
        return f"{label} left background processes after completion"
    return None


def _remember_recovery_path(
    plan: PublishTransferPlan,
    response: dict[str, object] | None,
) -> None:
    if response is None:
        return
    if "recovery_path" not in response:
        return
    recovery = response.get("recovery_path")
    if recovery == plan.staging_path.as_posix():
        with plan._lock:
            plan._recovery_path = plan.staging_path
    else:
        _retain_stage_recovery_path(plan)


def _retain_stage_recovery_path(plan: PublishTransferPlan) -> None:
    with plan._lock:
        if plan._recovery_path is None:
            plan._recovery_path = plan.staging_path


def execute_publish_transfer(
    plan: PublishTransferPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PublishTransferProgress], object] | Callable[[str], object] | None = None,
) -> PublishStagedGeneration:
    if not isinstance(plan, PublishTransferPlan):
        raise PublishTransferExecutionError("foreign publish transfer plan")
    operation_deadline = _default_deadline(deadline)
    with plan._lock:
        if plan._state != "ready":
            raise PublishTransferExecutionError(f"publish transfer plan is not ready: {plan._state}")
        plan._state = "executing"
    try:
        _checkpoint(cancel_event, operation_deadline)
        try:
            current_manifest = plan_pack_publish_manifest(
                plan.pack_id,
                target_side=plan.target_side,
                cancel_event=cancel_event,
                deadline=operation_deadline,
            )
        except Exception as error:
            raise PublishTransferExecutionError(
                f"could not revalidate Pack before transfer: {error}"
            ) from error
        if (
            current_manifest.manifest_digest != plan.manifest_digest
            or current_manifest.source_snapshot_digest != plan.source_snapshot_digest
        ):
            raise PublishTransferExecutionError(
                "Pack changed after transfer preparation"
            )
        current_target = _resolve_current_target(plan)
        if current_target.config_digest != plan.target_config_digest:
            raise PublishTransferExecutionError("publish target changed after transfer preparation")
        _emit(progress, PublishTransferProgress("connecting", 0, len(plan.manifest.files), 0, plan.manifest.total_bytes))
        result, response = _run_remote_request(
            plan,
            "transfer",
            deadline=operation_deadline,
            cancel_event=cancel_event,
        )
        lifecycle_failure = _process_lifecycle_failure(result, label="Publish transfer")
        if lifecycle_failure is not None:
            _retain_stage_recovery_path(plan)
            with plan._lock:
                plan._state = "uncertain"
            raise PublishTransferUncertainError(lifecycle_failure)
        if result.succeeded and response is not None and response.get("ok") is True:
            status = response.get("status")
            if status not in {"committed", "reused"}:
                raise PublishTransferExecutionError("remote helper did not commit a generation")
            try:
                _validate_committed_response(plan, response)
            except PublishTransferExecutionError as error:
                _retain_stage_recovery_path(plan)
                with plan._lock:
                    plan._state = "uncertain"
                raise PublishTransferUncertainError(str(error)) from error
            _remember_recovery_path(plan, response)
            if plan.recovery_path is not None:
                _cleanup_remote_publish_stage(plan, operation_deadline)
            reused = status == "reused"
            staged = PublishStagedGeneration(
                plan.manifest_digest,
                plan.target_config_digest,
                plan.generation_id,
                plan.generation_path,
                _result_files(plan),
                plan.manifest.total_bytes,
                reused,
            )
            with plan._lock:
                plan._state = "executed"
            _emit(progress, PublishTransferProgress("done", len(plan.manifest.files), len(plan.manifest.files), plan.manifest.total_bytes, plan.manifest.total_bytes))
            return staged

        _remember_recovery_path(plan, response)
        transfer_failure = process_failure_message(result, label="Publish transfer")
        if (
            response is not None
            and response.get("status") == "integrity_failure"
            and not result.cancelled
            and not result.timed_out
            and not result.output_limit_exceeded
        ):
            transfer_failure = str(
                response.get("error", "remote helper rejected the transfer")
            )

        recovery_deadline = min(operation_deadline, time.monotonic() + 30.0)
        try:
            status_result, status_response = _run_remote_request(
                plan,
                "status",
                deadline=recovery_deadline,
                cancel_event=None,
            )
        except BaseException as error:
            _retain_stage_recovery_path(plan)
            with plan._lock:
                plan._state = "uncertain"
            reason = transfer_failure or "Publish generation commit state is uncertain"
            raise PublishTransferUncertainError(
                f"{reason}; Publish generation commit state is uncertain"
            ) from error
        status_lifecycle_failure = _process_lifecycle_failure(
            status_result,
            label="Publish transfer status",
        )
        if status_lifecycle_failure is not None:
            _retain_stage_recovery_path(plan)
            with plan._lock:
                plan._state = "uncertain"
            reason = transfer_failure or status_lifecycle_failure
            raise PublishTransferUncertainError(
                f"{reason}; Publish generation commit state is uncertain"
            )
        if (
            status_result.succeeded
            and status_response is not None
            and status_response.get("ok") is True
        ):
            status = status_response.get("status")
            if status in {"not_committed", "committed"}:
                try:
                    _validate_committed_response(plan, status_response)
                except PublishTransferExecutionError as error:
                    _retain_stage_recovery_path(plan)
                    with plan._lock:
                        plan._state = "uncertain"
                    raise PublishTransferUncertainError(str(error)) from error
            if status == "not_committed":
                _remember_recovery_path(plan, status_response)
                _cleanup_remote_publish_stage(plan, operation_deadline)
                with plan._lock:
                    plan._state = "failed"
                raise PublishTransferExecutionError(
                    transfer_failure or "Publish transfer was not committed"
                )
            if status == "committed":
                _remember_recovery_path(plan, status_response)
                if plan.recovery_path is not None:
                    _cleanup_remote_publish_stage(plan, operation_deadline)
                with plan._lock:
                    plan._state = "executed"
                return PublishStagedGeneration(
                    plan.manifest_digest,
                    plan.target_config_digest,
                    plan.generation_id,
                    plan.generation_path,
                    _result_files(plan),
                    plan.manifest.total_bytes,
                    False,
                )
        with plan._lock:
            plan._state = "uncertain"
        _retain_stage_recovery_path(plan)
        reason = transfer_failure or process_failure_message(
            status_result,
            label="Publish transfer status",
        )
        if reason is None:
            reason = "Publish generation commit state is uncertain"
        else:
            reason = f"{reason}; Publish generation commit state is uncertain"
        raise PublishTransferUncertainError(reason)
    except BaseException:
        with plan._lock:
            if plan._state == "executing":
                plan._state = "failed"
        raise
