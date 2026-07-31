from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Literal

from portable_paths import portable_basename, portable_relative_path


ProviderName = Literal["modrinth", "curseforge", "url"]
Side = Literal["client", "server", "both"]


class ProviderIdentityError(ValueError):
    pass


def canonical_provider(provider: str) -> ProviderName:
    normalized = provider.strip().lower()
    aliases = {
        "mr": "modrinth",
        "modrinth": "modrinth",
        "cf": "curseforge",
        "curseforge": "curseforge",
        "u": "url",
        "url": "url",
        "selfhost": "url",
        "self-hosted": "url",
    }
    try:
        return aliases[normalized]  # type: ignore[return-value]
    except KeyError as error:
        raise ProviderIdentityError(f"Unsupported provider: {provider}") from error


def canonical_identity(provider: str, project_id: str) -> str:
    normalized = canonical_provider(provider)
    value = project_id.strip()
    if not value or any(ord(character) < 32 for character in value):
        raise ProviderIdentityError("Provider project ID must be a non-empty string")
    if normalized == "curseforge" and not value.isdecimal():
        raise ProviderIdentityError("CurseForge project ID must be numeric")
    return f"{normalized}:{value}"


@dataclass(frozen=True)
class ProviderMetadataIdentity:
    canonical_identity: str
    provider: ProviderName
    project_id: str
    file_id: str | None
    version: str | None
    side: Side
    metadata_path: Path
    filename: str
    download_url: str | None


@dataclass(frozen=True)
class ProviderMetadataCandidate:
    canonical_identity: str | None
    provider: ProviderName
    project_id: str | None
    file_id: str | None
    version: str | None
    side: Side
    metadata_path: Path
    filename: str
    download_url: str | None


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProviderIdentityError(f"{context} must be a mapping")
    return value


def _optional_scalar(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    raise ProviderIdentityError("Provider file identity must be a string or integer")


def parse_provider_metadata_candidate(
    relative_path: Path,
    contents: bytes,
) -> ProviderMetadataCandidate:
    try:
        relative = portable_relative_path(relative_path, context="Provider metadata path")
        document = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        raise ProviderIdentityError(f"Invalid provider metadata {relative_path}: {error}") from error
    side = document.get("side")
    if side not in {"client", "server", "both"}:
        raise ProviderIdentityError(f"Provider metadata {relative} has invalid side")
    filename = portable_basename(
        str(document.get("filename", "")), context="Metadata filename"
    )
    update = document.get("update", {})
    if not isinstance(update, dict):
        raise ProviderIdentityError(f"Provider metadata {relative} has invalid update data")
    known_updates = {key for key in ("modrinth", "curseforge") if key in update}
    if len(known_updates) > 1:
        raise ProviderIdentityError(
            f"Provider metadata {relative} has ambiguous provider identity"
        )
    if update and not known_updates:
        raise ProviderIdentityError(
            f"Provider metadata {relative} has unsupported update data"
        )
    provider: ProviderName
    project_id: str
    file_id: str | None
    version: str | None
    if "modrinth" in update:
        provider = "modrinth"
        provider_data = _mapping(update["modrinth"], "update.modrinth")
        project_id = str(provider_data.get("mod-id", "")).strip()
        file_id = _optional_scalar(
            provider_data.get("version", provider_data.get("version-id"))
        )
        version = file_id
    elif "curseforge" in update:
        provider = "curseforge"
        provider_data = _mapping(update["curseforge"], "update.curseforge")
        project_id = str(provider_data.get("project-id", "")).strip()
        file_id = _optional_scalar(
            provider_data.get("file-id", provider_data.get("fileId"))
        )
        version = file_id
    else:
        provider = "url"
        huroshiki = _mapping(document.get("huroshiki", {}), "huroshiki")
        project_id = str(huroshiki.get("project-id", "")).strip()
        file_id = None
        version = _optional_scalar(huroshiki.get("version"))
    if provider != "url" and not project_id:
        raise ProviderIdentityError(
            f"Provider metadata {relative} has no canonical project ID"
        )
    identity = canonical_identity(provider, project_id) if project_id else None
    download = document.get("download", {})
    download_url = (
        str(download.get("url", "")).strip()
        if isinstance(download, dict) and download.get("url") is not None
        else None
    )
    if provider == "url" and not download_url:
        raise ProviderIdentityError(f"URL metadata {relative} has no download URL")
    return ProviderMetadataCandidate(
        identity,
        provider,
        project_id or None,
        file_id,
        version,
        side,  # type: ignore[arg-type]
        relative,
        filename,
        download_url,
    )


def parse_provider_metadata(
    relative_path: Path,
    contents: bytes,
) -> ProviderMetadataIdentity:
    candidate = parse_provider_metadata_candidate(relative_path, contents)
    if candidate.canonical_identity is None or candidate.project_id is None:
        raise ProviderIdentityError(
            f"Provider metadata {candidate.metadata_path} has no canonical project ID"
        )
    return ProviderMetadataIdentity(
        candidate.canonical_identity,
        candidate.provider,
        candidate.project_id,
        candidate.file_id,
        candidate.version,
        candidate.side,
        candidate.metadata_path,
        candidate.filename,
        candidate.download_url,
    )
