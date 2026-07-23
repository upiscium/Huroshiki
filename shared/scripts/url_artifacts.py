from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
import zipfile

import tomlkit


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

                    # Read to EOF so an understated Content-Length cannot hide bytes.
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
    root = source.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise HuroshikiError(f"Path escaped root: {relative_path}")
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
