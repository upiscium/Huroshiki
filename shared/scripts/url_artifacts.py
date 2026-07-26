from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import queue
import re
import socket
import ssl
import struct
import tempfile
import threading
import time
import tomllib
from urllib.parse import unquote, urljoin, urlparse
from uuid import uuid4
import zipfile

import tomlkit

from portable_paths import PortablePathError, portable_basename, portable_relative_path
from portable_paths import portable_basename_key, portable_relative_path_key


class HuroshikiError(RuntimeError):
    pass


@dataclass(frozen=True)
class UrlArtifact:
    name: str
    mod_id: str
    version: str
    filename: str
    url: str
    sha256: str
    loaders: tuple[str, ...]


URL_USER_AGENT = "huroshiki/1 self-hosted-mod-fetcher"
URL_CHUNK_SIZE = 1024 * 1024
DEFAULT_URL_MAX_JAR_SIZE_BYTES = 256 * 1024 * 1024
DEFAULT_URL_TOTAL_TIMEOUT_SECONDS = 120.0
MAX_URL_REDIRECTS = 10
MAX_ZIP_ENTRIES = 10_000
MAX_METADATA_ENTRY_SIZE_BYTES = 1024 * 1024
ZIP_EOCD_SIZE = 22
ZIP_MAX_COMMENT_SIZE = 65_535
_ALWAYS_PROHIBITED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "2001:db8::/32",
        "3fff::/20",
    )
)


def _preflight_zip_entry_count(path: Path) -> None:
    with path.open("rb") as archive:
        archive.seek(0, 2)
        archive_size = archive.tell()
        tail_size = min(archive_size, ZIP_EOCD_SIZE + ZIP_MAX_COMMENT_SIZE)
        archive.seek(-tail_size, 2)
        tail = archive.read(tail_size)

    signature = b"PK\x05\x06"
    offset = tail.rfind(signature)
    while offset >= 0:
        if offset + ZIP_EOCD_SIZE <= len(tail):
            comment_size = int.from_bytes(tail[offset + 20 : offset + 22], "little")
            if offset + ZIP_EOCD_SIZE + comment_size == len(tail):
                break
        offset = tail.rfind(signature, 0, offset)
    if offset < 0:
        return

    (
        _signature,
        _disk_number,
        _central_directory_disk,
        entries_on_disk,
        total_entries,
        central_directory_size,
        central_directory_offset,
        _comment_size,
    ) = struct.unpack_from("<4s4H2LH", tail, offset)
    if (
        entries_on_disk == 0xFFFF
        or total_entries == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
    ):
        raise HuroshikiError("Downloaded JAR uses unsupported ZIP64 metadata")
    if max(entries_on_disk, total_entries) > MAX_ZIP_ENTRIES:
        raise HuroshikiError(
            f"Downloaded JAR contains more than {MAX_ZIP_ENTRIES} entries"
        )


def _read_metadata_entry(jar: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_METADATA_ENTRY_SIZE_BYTES:
        raise HuroshikiError(
            f"JAR metadata entry {info.filename} exceeds "
            f"{MAX_METADATA_ENTRY_SIZE_BYTES} bytes"
        )
    with jar.open(info) as stream:
        data = stream.read(MAX_METADATA_ENTRY_SIZE_BYTES + 1)
    if len(data) > MAX_METADATA_ENTRY_SIZE_BYTES:
        raise HuroshikiError(
            f"JAR metadata entry {info.filename} exceeds "
            f"{MAX_METADATA_ENTRY_SIZE_BYTES} bytes"
        )
    return data


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HuroshikiError("URL provider requires an http:// or https:// public URL")
    filename = unquote(parsed.path.rsplit("/", 1)[-1])
    try:
        filename = portable_basename(filename, context="URL filename")
    except PortablePathError as error:
        raise HuroshikiError(str(error)) from error
    if not filename.lower().endswith(".jar"):
        raise HuroshikiError("The self-hosted MOD URL must point to a .jar file")


def _approved_addresses(
    hostname: str,
    port: int,
    *,
    allow_private_networks: bool,
) -> tuple[str, ...]:
    try:
        addresses = tuple(
            dict.fromkeys(
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname, port, type=socket.SOCK_STREAM
                )
            )
        )
    except socket.gaierror as error:
        raise HuroshikiError(f"Could not resolve self-hosted URL host {hostname}: {error}") from error
    if not addresses:
        raise HuroshikiError(f"Could not resolve self-hosted URL host {hostname}")
    nat64_networks = (
        ipaddress.ip_network("64:ff9b::/96"),
        ipaddress.ip_network("64:ff9b:1::/48"),
    )
    prohibited: list[str] = []
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        is_nat64 = isinstance(parsed, ipaddress.IPv6Address) and any(
            parsed in network for network in nat64_networks
        )
        always_prohibited = (
            parsed.is_unspecified
            or parsed.is_multicast
            or (parsed.is_reserved and not is_nat64)
            or any(parsed in network for network in _ALWAYS_PROHIBITED_NETWORKS)
        )
        if always_prohibited or (not allow_private_networks and (not parsed.is_global or is_nat64)):
            prohibited.append(address)
    if prohibited:
        raise HuroshikiError(
            f"Self-hosted URL host {hostname} resolves to prohibited address "
            f"{prohibited[0]}; set url_allow_private_networks: true in machine-local "
            "configuration only when this access is intended"
        )
    return addresses


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HuroshikiError("URL download deadline exceeded")
    return remaining


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float) -> None:
        super().__init__(
            hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


@dataclass
class _OpenedUrl:
    response: object
    transport_socket: socket.socket | None
    connection: http.client.HTTPConnection | None = None

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            if self.connection is not None:
                self.connection.close()


def _open_validated_url(
    url: str,
    *,
    deadline: float | None = None,
    timeout: float | None = None,
    allow_private_networks: bool,
) -> _OpenedUrl:
    if deadline is None:
        if timeout is None:
            raise TypeError("deadline or timeout is required")
        deadline = time.monotonic() + timeout
    current = url
    for redirect_count in range(MAX_URL_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HuroshikiError("Redirect target must be an http:// or https:// URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        _remaining_timeout(deadline)
        address = _approved_addresses(
            parsed.hostname,
            port,
            allow_private_networks=allow_private_networks,
        )[0]
        connection_type = (
            _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        )
        connection = connection_type(
            parsed.hostname, port, address, _remaining_timeout(deadline)
        )
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        host = (
            f"[{parsed.hostname}]"
            if ":" in parsed.hostname
            else parsed.hostname
        )
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        try:
            connection.timeout = _remaining_timeout(deadline)
            connection.request(
                "GET",
                target,
                headers={
                    "Host": host,
                    "User-Agent": URL_USER_AGENT,
                    "Connection": "close",
                },
            )
            transport_socket = getattr(connection, "sock", None)
            if transport_socket is not None:
                transport_socket.settimeout(_remaining_timeout(deadline))
            response = connection.getresponse()
        except BaseException:
            connection.close()
            if time.monotonic() >= deadline:
                raise HuroshikiError("URL download deadline exceeded")
            raise
        if response.status not in {301, 302, 303, 307, 308}:
            if response.status >= 400:
                response.close()
                connection.close()
                raise HuroshikiError(
                    f"Self-hosted URL returned HTTP {response.status}: {current}"
                )
            return _OpenedUrl(response, getattr(connection, "sock", None), connection)
        location = response.headers.get("Location")
        response.close()
        connection.close()
        if not location:
            raise HuroshikiError("Self-hosted URL redirect omitted Location")
        if redirect_count == MAX_URL_REDIRECTS:
            raise HuroshikiError("Self-hosted URL exceeded redirect limit")
        current = urljoin(current, location)


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
        _preflight_zip_entry_count(path)
        with zipfile.ZipFile(path) as jar:
            infos = jar.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise HuroshikiError(
                    f"Downloaded JAR contains more than {MAX_ZIP_ENTRIES} entries"
                )
            entries = {info.filename: info for info in infos}
            for metadata_name, loader in (
                ("META-INF/neoforge.mods.toml", "neoforge"),
                ("META-INF/mods.toml", "forge"),
            ):
                if metadata_name not in entries:
                    continue
                try:
                    data = tomllib.loads(
                        _read_metadata_entry(jar, entries[metadata_name]).decode("utf-8")
                    )
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

            if "fabric.mod.json" in entries:
                try:
                    data = json.loads(
                        _read_metadata_entry(jar, entries["fabric.mod.json"]).decode(
                            "utf-8"
                        )
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None
                if isinstance(data, dict):
                    raw_mod_id = str(data.get("id", "")).strip()
                    if raw_mod_id:
                        mod_id = sanitize_mod_id(raw_mod_id)
                        name = str(data.get("name", mod_id)).strip() or mod_id
                        version = str(data.get("version", "")).strip()
                        identities.append((mod_id, name, version, "fabric"))

            if "quilt.mod.json" in entries:
                try:
                    data = json.loads(
                        _read_metadata_entry(jar, entries["quilt.mod.json"]).decode(
                            "utf-8"
                        )
                    )
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
    *,
    total_timeout_seconds: float = DEFAULT_URL_TOTAL_TIMEOUT_SECONDS,
    allow_private_networks: bool = False,
) -> UrlArtifact:
    validate_public_url(url)
    if (
        isinstance(max_size_bytes, bool)
        or not isinstance(max_size_bytes, int)
        or max_size_bytes <= 0
    ):
        raise HuroshikiError("URL JAR size limit must be a positive integer")
    if total_timeout_seconds <= 0:
        raise HuroshikiError("URL download deadline must be positive")
    deadline = time.monotonic() + total_timeout_seconds

    def check_cancel_deadline() -> None:
        if cancel_event.is_set():
            raise HuroshikiError("URL download cancelled")
        if time.monotonic() >= deadline:
            raise HuroshikiError("URL download deadline exceeded")

    filename = portable_basename(
        unquote(urlparse(url).path.rsplit("/", 1)[-1]),
        context="URL filename",
    )
    append_url_log(log_dir, f"Downloading {url}")
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
                open_results: queue.Queue[tuple[object | None, BaseException | None]] = (
                    queue.Queue(maxsize=1)
                )
                open_ready = threading.Event()
                open_claimed = threading.Event()
                open_abandoned = threading.Event()

                def open_request() -> None:
                    try:
                        opened = _open_validated_url(
                            url,
                            deadline=deadline,
                            allow_private_networks=allow_private_networks,
                        )
                    except BaseException as error:
                        if not open_abandoned.is_set():
                            open_results.put((None, error))
                            open_ready.set()
                        return

                    if open_abandoned.is_set():
                        opened.close()
                        return
                    open_results.put((opened, None))
                    open_ready.set()
                    while not open_claimed.wait(0.1):
                        if open_abandoned.is_set():
                            opened.close()
                            return

                opener = threading.Thread(
                    target=open_request,
                    daemon=True,
                    name="huroshiki-url-opener",
                )
                check_cancel_deadline()
                opener.start()
                while not open_ready.is_set():
                    try:
                        check_cancel_deadline()
                    except HuroshikiError:
                        open_abandoned.set()
                        raise
                    open_ready.wait(min(max(deadline - time.monotonic(), 0), 0.05))
                try:
                    check_cancel_deadline()
                except HuroshikiError:
                    open_abandoned.set()
                    raise
                response_result, open_error = open_results.get_nowait()
                if open_error is not None:
                    raise open_error
                open_claimed.set()
                opened = response_result
                if not isinstance(opened, _OpenedUrl):
                    opened = _OpenedUrl(opened, None)
                response = opened.response

                try:
                    watcher_stop = threading.Event()
                    deadline_reached = threading.Event()

                    def close_on_cancel_or_deadline() -> None:
                        while not watcher_stop.is_set():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                deadline_reached.set()
                                if opened.transport_socket is not None:
                                    try:
                                        opened.transport_socket.shutdown(socket.SHUT_RDWR)
                                    except OSError:
                                        pass
                                else:
                                    threading.Thread(
                                        target=response.close,
                                        daemon=True,
                                        name="huroshiki-url-mock-close",
                                    ).start()
                                return
                            if cancel_event.wait(min(remaining, 0.1)):
                                if opened.transport_socket is not None:
                                    try:
                                        opened.transport_socket.shutdown(socket.SHUT_RDWR)
                                    except OSError:
                                        pass
                                else:
                                    threading.Thread(
                                        target=response.close,
                                        daemon=True,
                                        name="huroshiki-url-mock-close",
                                    ).start()
                                return

                    watcher = threading.Thread(
                        target=close_on_cancel_or_deadline,
                        daemon=True,
                        name="huroshiki-url-deadline",
                    )
                    watcher.start()
                    check_cancel_deadline()
                    try:
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

                        # Read to EOF so an understated Content-Length cannot hide bytes.
                        if declared_size is not None and not response.chunked:
                            response.length = None
                        received_size = 0
                        while True:
                            check_cancel_deadline()
                            if deadline_reached.is_set():
                                raise HuroshikiError("URL download deadline exceeded")
                            try:
                                chunk = response.read(
                                    min(
                                        URL_CHUNK_SIZE,
                                        max_size_bytes - received_size + 1,
                                    )
                                )
                            except (OSError, ValueError) as error:
                                if cancel_event.is_set():
                                    raise HuroshikiError("URL download cancelled") from error
                                if deadline_reached.is_set() or time.monotonic() >= deadline:
                                    raise HuroshikiError(
                                        "URL download deadline exceeded"
                                    ) from error
                                raise
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
                    finally:
                        watcher_stop.set()
                        watcher.join(timeout=1)
                finally:
                    opened.close()
            except (OSError, http.client.HTTPException) as error:
                raise HuroshikiError(
                    f"Could not download self-hosted MOD: {error}"
                ) from error

        check_cancel_deadline()
        mod_id, name, version, loaders = parse_jar_identity(
            temporary_path, target_loader
        )
        check_cancel_deadline()
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
    try:
        relative_path = portable_relative_path(relative_path)
        portable_basename(artifact.filename, context="Metadata filename")
    except PortablePathError as error:
        raise HuroshikiError(str(error)) from error
    root = source.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise HuroshikiError(f"Path escaped root: {relative_path}")
    incoming_filename_key = portable_basename_key(
        artifact.filename, context="Metadata filename"
    )
    incoming_path_key = portable_relative_path_key(relative_path)
    for existing in sorted(source.rglob("*.pw.toml")):
        existing_relative = existing.relative_to(source)
        try:
            data = tomllib.loads(existing.read_text(encoding="utf-8"))
            existing_path_key = portable_relative_path_key(existing_relative)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, PortablePathError) as error:
            raise HuroshikiError(f"Cannot inspect existing metadata {existing_relative}: {error}") from error
        update = data.get("update", {})
        has_provider_update = isinstance(update, dict) and any(
            isinstance(update.get(provider), dict)
            for provider in ("modrinth", "curseforge")
        )
        existing_identity = (
            "url",
            existing_relative.name.removesuffix(".pw.toml"),
        ) if not has_provider_update else None
        if (
            existing_path_key == incoming_path_key
            and existing_identity == ("url", artifact.mod_id)
        ):
            continue
        try:
            existing_filename = str(data.get("filename", ""))
            existing_filename_key = portable_basename_key(
                existing_filename, context="Metadata filename"
            )
        except PortablePathError as error:
            raise HuroshikiError(
                f"Cannot inspect existing metadata {existing_relative}: {error}"
            ) from error
        if existing_filename_key == incoming_filename_key:
            raise HuroshikiError(
                f"Portable filename collision for {artifact.filename!r}: "
                f"{existing_relative} already uses {existing_filename!r}"
            )
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
