from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import tomllib
from typing import Callable, Literal

from mod_version_overrides import (
    ModVersionOverride,
    ModVersionOverrideError,
    read_mod_version_overrides,
    require_mod_version_overrides_ignored,
    serialize_mod_version_overrides,
)
from provider_identity import parse_provider_metadata


@dataclass(frozen=True)
class PackMigrationVersionIntentIssue:
    """A valid source intent which cannot be satisfied by a target closure."""

    identity: str
    owner_identity: str | None
    requested_artifact_id: str
    reason_code: Literal["version-intent-blocked"]
    message: str


@dataclass(frozen=True)
class PackMigrationVersionIntentFacts:
    overrides: tuple[ModVersionOverride, ...] = ()
    automatic_identities: tuple[str, ...] = ()
    digest: str = ""


@dataclass(frozen=True)
class DetachedVersionIntentMetadata:
    relative_path: Path
    contents: bytes


class PackMigrationVersionIntentError(RuntimeError):
    """Malformed, stale, or otherwise unauthoritative detached intent."""


def _digest(overrides: tuple[ModVersionOverride, ...]) -> str:
    return hashlib.sha256(serialize_mod_version_overrides(overrides)).hexdigest()


def read_detached_version_intent(
    source: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[ModVersionOverride, ...]:
    try:
        return read_mod_version_overrides(source, checkpoint=checkpoint)
    except (ModVersionOverrideError, OSError) as error:
        raise PackMigrationVersionIntentError(
            f"Detached version override authority is invalid: {error}"
        ) from error


def validate_detached_version_intent(
    source: Path,
    *,
    metadata: tuple[DetachedVersionIntentMetadata, ...],
    checkpoint: Callable[[], None],
    overrides: tuple[ModVersionOverride, ...] | None = None,
) -> PackMigrationVersionIntentFacts:
    """Validate authority against the fixed detached metadata, before resolving."""
    overrides = (
        read_detached_version_intent(source, checkpoint=checkpoint)
        if overrides is None
        else overrides
    )
    if overrides:
        try:
            require_mod_version_overrides_ignored(source, checkpoint=checkpoint)
        except (ModVersionOverrideError, OSError) as error:
            raise PackMigrationVersionIntentError(
                f"Detached version override authority is invalid: {error}"
            ) from error
    records: dict[str, list[object]] = {}
    for item in metadata:
        checkpoint()
        try:
            parsed = parse_provider_metadata(item.relative_path, item.contents)
        except Exception as error:
            try:
                document = tomllib.loads(item.contents.decode("utf-8"))
            except (UnicodeError, tomllib.TOMLDecodeError):
                document = {}
            update = document.get("update")
            if not isinstance(update, dict) or not (
                {"modrinth", "curseforge"} & set(update)
            ):
                # URL metadata cannot own provider exact-version intent.
                continue
            raise PackMigrationVersionIntentError(
                f"Detached provider metadata is invalid: {error}"
            ) from error
        records.setdefault(parsed.canonical_identity, []).append(parsed)
    for override in overrides:
        checkpoint()
        matches = records.get(override.canonical_identity, [])
        if len(matches) != 1:
            raise PackMigrationVersionIntentError(
                "Detached version override is stale or orphaned: "
                f"{override.canonical_identity}"
            )
        actual = matches[0].file_id
        if actual != override.artifact_id:
            raise PackMigrationVersionIntentError(
                "Detached version override artifact does not match installed metadata: "
                f"{override.canonical_identity} expected {override.artifact_id}, "
                f"found {actual or '<missing>'}"
            )
    identities = tuple(sorted(records))
    return PackMigrationVersionIntentFacts(
        overrides=overrides,
        automatic_identities=tuple(
            identity for identity in identities
            if identity not in {item.canonical_identity for item in overrides}
        ),
        digest=_digest(overrides),
    )


def intent_by_identity(
    facts: PackMigrationVersionIntentFacts,
) -> dict[str, ModVersionOverride]:
    return {item.canonical_identity: item for item in facts.overrides}


__all__ = [
    "PackMigrationVersionIntentError",
    "PackMigrationVersionIntentFacts",
    "PackMigrationVersionIntentIssue",
    "DetachedVersionIntentMetadata",
    "intent_by_identity",
    "read_detached_version_intent",
    "validate_detached_version_intent",
]
