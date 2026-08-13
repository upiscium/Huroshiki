from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal

from pack_migration_roots import (
    PackMigrationRootError,
    read_pack_control_file,
    write_pack_control_file,
)
from pack_tree_policy import scan_pack_migration_source


VERSION_OVERRIDE_MANIFEST_NAME = ".huroshiki-version-overrides.json"
VERSION_OVERRIDE_MANIFEST_PATH = Path(VERSION_OVERRIDE_MANIFEST_NAME)
VERSION_OVERRIDE_MANIFEST_MAX_BYTES = 1024 * 1024
VERSION_OVERRIDE_IGNORE_ENTRY = f"/{VERSION_OVERRIDE_MANIFEST_NAME}"


class ModVersionOverrideError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModVersionOverride:
    provider: Literal["curseforge", "modrinth"]
    project_id: str
    artifact_id: str
    locked: bool = False
    reason: str | None = None

    @property
    def canonical_identity(self) -> str:
        return f"{self.provider}:{self.project_id}"


@dataclass(frozen=True)
class ModVersionOverrideManifest:
    entries: tuple[ModVersionOverride, ...]


@dataclass(frozen=True)
class ModVersionOverrideStatus:
    override: ModVersionOverride
    status: Literal["active", "drifted", "stale"]
    installed_artifact_id: str | None


def _selection(
    provider: object,
    project_id: object,
    artifact_id: object,
):
    if not all(isinstance(value, str) for value in (provider, project_id, artifact_id)):
        raise ModVersionOverrideError("Version override IDs must be strings")
    if provider not in {"curseforge", "modrinth"}:
        raise ModVersionOverrideError("Version override provider is not canonical")
    try:
        from huroshiki_core import ExactModArtifactSelection, canonical_modrinth_id

        if provider == "modrinth":
            return ExactModArtifactSelection(
                provider,
                canonical_modrinth_id(project_id, "Modrinth project ID"),
                canonical_modrinth_id(artifact_id, "Modrinth version ID"),
            )
        return ExactModArtifactSelection(provider, project_id, artifact_id)
    except Exception as error:
        raise ModVersionOverrideError(str(error)) from error


def _reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModVersionOverrideError("Version override reason must be a string")
    if not value or value != value.strip():
        raise ModVersionOverrideError(
            "Version override reason must be a non-empty trimmed string"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ModVersionOverrideError("Version override reason contains control characters")
    return value


def parse_mod_version_overrides(contents: bytes) -> ModVersionOverrideManifest:
    if len(contents) > VERSION_OVERRIDE_MANIFEST_MAX_BYTES:
        raise ModVersionOverrideError("Version override manifest exceeds size limit")
    try:
        value = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ModVersionOverrideError("Version override manifest contains invalid JSON") from error
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1:
        raise ModVersionOverrideError("Version override manifest schema must be 1")
    if set(value) != {"schema", "mods"}:
        raise ModVersionOverrideError("Version override manifest contains unknown fields")
    mods = value.get("mods")
    if not isinstance(mods, dict):
        raise ModVersionOverrideError("Version override manifest mods must be an object")
    entries: list[ModVersionOverride] = []
    for identity, raw in mods.items():
        if not isinstance(identity, str) or not isinstance(raw, dict):
            raise ModVersionOverrideError("Version override entries must be objects")
        allowed = {"artifact_id", "selection", "locked", "reason"}
        if set(raw) - allowed or not {"artifact_id", "selection", "locked"} <= set(raw):
            raise ModVersionOverrideError(f"Version override {identity} contains unknown fields")
        if identity.count(":") != 1:
            raise ModVersionOverrideError(f"Invalid version override identity: {identity}")
        provider, project_id = identity.split(":", 1)
        selection = _selection(provider, project_id, raw.get("artifact_id"))
        if selection.identity_label != identity:
            raise ModVersionOverrideError(f"Version override identity is not canonical: {identity}")
        if raw.get("selection") != "user":
            raise ModVersionOverrideError("Version override selection must be user")
        locked = raw.get("locked")
        if type(locked) is not bool:
            raise ModVersionOverrideError("Version override locked must be a boolean")
        entries.append(
            ModVersionOverride(
                provider,
                str(selection.project_id),
                str(selection.artifact_id),
                locked,
                _reason(raw.get("reason")),
            )
        )
    return ModVersionOverrideManifest(
        tuple(sorted(entries, key=lambda item: item.canonical_identity))
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ModVersionOverrideError(f"Duplicate JSON field: {key}")
        value[key] = item
    return value


def serialize_mod_version_overrides(entries: tuple[ModVersionOverride, ...]) -> bytes:
    ordered: dict[str, dict[str, object]] = {}
    for entry in sorted(entries, key=lambda item: item.canonical_identity):
        selection = _selection(entry.provider, entry.project_id, entry.artifact_id)
        identity = selection.identity_label
        if identity in ordered:
            raise ModVersionOverrideError(f"Duplicate version override identity: {identity}")
        item: dict[str, object] = {
            "artifact_id": str(selection.artifact_id),
            "selection": "user",
            "locked": entry.locked,
        }
        reason = _reason(entry.reason)
        if reason is not None:
            item["reason"] = reason
        ordered[identity] = item
    return (json.dumps({"schema": 1, "mods": ordered}, indent=2) + "\n").encode("utf-8")


def read_mod_version_overrides(source: Path) -> tuple[ModVersionOverride, ...]:
    scan = scan_pack_migration_source(source, checkpoint=lambda: None)
    entry = next(
        (item for item in scan.entries if item.relative_path == VERSION_OVERRIDE_MANIFEST_PATH),
        None,
    )
    if entry is None:
        return ()
    try:
        contents = read_pack_control_file(
            source,
            scan,
            VERSION_OVERRIDE_MANIFEST_PATH,
            max_bytes=VERSION_OVERRIDE_MANIFEST_MAX_BYTES,
        )
    except PackMigrationRootError as error:
        raise ModVersionOverrideError(str(error)) from error
    return parse_mod_version_overrides(contents).entries


def get_mod_version_override(source: Path, identity: str) -> ModVersionOverride | None:
    return next(
        (entry for entry in read_mod_version_overrides(source) if entry.canonical_identity == identity),
        None,
    )


def write_mod_version_overrides(
    source: Path, entries: tuple[ModVersionOverride, ...]
) -> None:
    try:
        scan = scan_pack_migration_source(source, checkpoint=lambda: None)
        write_pack_control_file(
            source,
            VERSION_OVERRIDE_MANIFEST_PATH,
            serialize_mod_version_overrides(entries),
            expected_root_identity=scan.root_identity,
        )
    except PackMigrationRootError as error:
        raise ModVersionOverrideError(str(error)) from error


def set_mod_version_override(
    source: Path, override: ModVersionOverride
) -> tuple[ModVersionOverride, ...]:
    entries = {item.canonical_identity: item for item in read_mod_version_overrides(source)}
    entries[override.canonical_identity] = override
    result = tuple(entries.values())
    write_mod_version_overrides(source, result)
    return tuple(sorted(result, key=lambda item: item.canonical_identity))


def remove_mod_version_override(
    source: Path, identity: str
) -> tuple[ModVersionOverride, ...]:
    entries = {item.canonical_identity: item for item in read_mod_version_overrides(source)}
    entries.pop(identity, None)
    result = tuple(entries.values())
    write_mod_version_overrides(source, result)
    return tuple(sorted(result, key=lambda item: item.canonical_identity))


def ensure_mod_version_overrides_ignored(source: Path) -> None:
    try:
        scan = scan_pack_migration_source(source, checkpoint=lambda: None)
        ignore = Path(".packwizignore")
        entry = next((item for item in scan.entries if item.relative_path == ignore), None)
        contents = (
            b""
            if entry is None
            else read_pack_control_file(
                source, scan, ignore, max_bytes=1024 * 1024
            )
        )
    except PackMigrationRootError as error:
        raise ModVersionOverrideError(str(error)) from error
    try:
        text = contents.decode("utf-8")
    except UnicodeError as error:
        raise ModVersionOverrideError(".packwizignore is not valid UTF-8") from error
    lines = {line.strip() for line in text.splitlines()}
    additions = [
        item
        for item in ("/.huroshiki-roots.json", VERSION_OVERRIDE_IGNORE_ENTRY)
        if item not in lines
    ]
    if not additions:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    try:
        write_pack_control_file(
            source,
            ignore,
            (text + "".join(f"{item}\n" for item in additions)).encode("utf-8"),
            expected_root_identity=scan.root_identity,
        )
    except PackMigrationRootError as error:
        raise ModVersionOverrideError(str(error)) from error
