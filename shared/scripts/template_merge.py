from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence


VALID_SIDES = frozenset({"client", "server", "both"})


class TemplateMergeError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateModEntry:
    template_id: str
    name: str
    provider: str
    project_id: str
    side: str
    url: str | None = None
    max_url_jar_size_bytes: int | None = None
    url_allow_private_networks: bool = False


@dataclass(frozen=True)
class MergedTemplateMod:
    candidate_key: str
    name: str
    provider: str
    project_id: str
    side: str
    template_ids: tuple[str, ...]
    url: str | None = None
    order: int = 0
    name_aliases: tuple[str, ...] = ()
    max_url_jar_size_bytes: int | None = None
    url_allow_private_networks: bool = False


@dataclass(frozen=True)
class TemplateConflict:
    key: str
    name: str
    candidates: tuple[MergedTemplateMod, ...]


@dataclass(frozen=True)
class TemplateComposition:
    template_ids: tuple[str, ...]
    mods: tuple[MergedTemplateMod, ...]
    conflicts: tuple[TemplateConflict, ...]


@dataclass(frozen=True)
class ConflictResolution:
    candidate_keys: tuple[str, ...]
    acknowledge_duplicate_risk: bool = False


@dataclass(frozen=True)
class ConflictSelection:
    conflict_key: str
    name: str
    candidate_keys: tuple[str, ...]
    candidate_labels: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedTemplateComposition:
    template_ids: tuple[str, ...]
    mods: tuple[MergedTemplateMod, ...]
    conflict_selections: tuple[ConflictSelection, ...]
    warnings: tuple[str, ...]


def normalize_name(name: str) -> str:
    return unicodedata.normalize("NFC", name.strip()).casefold()


def union_side(first: str, second: str) -> str:
    if first not in VALID_SIDES or second not in VALID_SIDES:
        raise TemplateMergeError(f"Invalid template side: {first!r} or {second!r}")
    if first == second:
        return first
    return "both"


def _candidate_key(entry: TemplateModEntry) -> str:
    base = f"{entry.provider}:{entry.project_id}"
    if entry.provider == "url":
        return f"{base}@{entry.url or ''}"
    return base


def _identity(entry: TemplateModEntry) -> tuple[str, str, str | None]:
    # A URL's project ID is its logical identity, but a changed selector cannot
    # be silently folded into the first occurrence.
    return (
        entry.provider,
        entry.project_id,
        entry.url if entry.provider == "url" else None,
    )


def compose_templates(
    template_ids: Sequence[str],
    entries: Iterable[TemplateModEntry],
) -> TemplateComposition:
    ordered_ids = tuple(template_ids)
    if not ordered_ids:
        raise TemplateMergeError("At least one template must be selected")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise TemplateMergeError("Template selection contains duplicate IDs")

    merged: list[MergedTemplateMod] = []
    identity_indexes: dict[tuple[str, str, str | None], int] = {}
    for entry in entries:
        if entry.template_id not in ordered_ids:
            raise TemplateMergeError(
                f"Entry references unselected template: {entry.template_id}"
            )
        identity = _identity(entry)
        existing_index = identity_indexes.get(identity)
        if existing_index is not None:
            existing = merged[existing_index]
            origins = existing.template_ids
            if entry.template_id not in origins:
                origins += (entry.template_id,)
            alias = normalize_name(entry.name)
            aliases = existing.name_aliases
            if alias not in aliases:
                aliases += (alias,)
            limits = tuple(
                limit
                for limit in (
                    existing.max_url_jar_size_bytes,
                    entry.max_url_jar_size_bytes,
                )
                if limit is not None
            )
            merged[existing_index] = replace(
                existing,
                side=union_side(existing.side, entry.side),
                template_ids=origins,
                name_aliases=aliases,
                max_url_jar_size_bytes=min(limits) if limits else None,
                url_allow_private_networks=(
                    existing.url_allow_private_networks
                    and entry.url_allow_private_networks
                ),
            )
            continue
        identity_indexes[identity] = len(merged)
        merged.append(
            MergedTemplateMod(
                candidate_key=_candidate_key(entry),
                name=entry.name.strip(),
                provider=entry.provider,
                project_id=entry.project_id,
                url=entry.url,
                side=entry.side,
                template_ids=(entry.template_id,),
                order=len(merged),
                name_aliases=(normalize_name(entry.name),),
                max_url_jar_size_bytes=entry.max_url_jar_size_bytes,
                url_allow_private_networks=entry.url_allow_private_networks,
            )
        )

    parents = list(range(len(merged)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(first: int, second: int) -> None:
        first_root = root(first)
        second_root = root(second)
        if first_root != second_root:
            parents[second_root] = first_root

    name_indexes: dict[str, int] = {}
    url_indexes: dict[tuple[str, str], int] = {}
    for index, mod in enumerate(merged):
        for normalized_name in mod.name_aliases:
            if normalized_name in name_indexes:
                join(name_indexes[normalized_name], index)
            else:
                name_indexes[normalized_name] = index
        if mod.provider == "url":
            logical_id = (mod.provider, mod.project_id)
            if logical_id in url_indexes:
                join(url_indexes[logical_id], index)
            else:
                url_indexes[logical_id] = index

    groups: dict[int, list[MergedTemplateMod]] = {}
    for index, mod in enumerate(merged):
        groups.setdefault(root(index), []).append(mod)
    conflicts = tuple(
        TemplateConflict(
            normalize_name(candidates[0].name),
            candidates[0].name,
            tuple(candidates),
        )
        for candidates in groups.values()
        if len(candidates) > 1
    )
    return TemplateComposition(ordered_ids, tuple(merged), conflicts)


def resolve_composition(
    composition: TemplateComposition,
    resolutions: Mapping[str, ConflictResolution] | None,
) -> ResolvedTemplateComposition:
    supplied = {} if resolutions is None else dict(resolutions)
    expected_keys = {conflict.key for conflict in composition.conflicts}
    unknown = set(supplied) - expected_keys
    if unknown:
        raise TemplateMergeError(
            "Unknown or stale conflict resolution(s): " + ", ".join(sorted(unknown))
        )
    missing = expected_keys - set(supplied)
    if missing:
        raise TemplateMergeError(
            "Unresolved template conflict(s): " + ", ".join(sorted(missing))
        )

    selected_keys: set[str] = set()
    selections: list[ConflictSelection] = []
    warnings: list[str] = []
    for conflict in composition.conflicts:
        resolution = supplied[conflict.key]
        requested = tuple(resolution.candidate_keys)
        if not requested:
            raise TemplateMergeError(
                f"Conflict {conflict.name!r} must retain at least one candidate"
            )
        if len(set(requested)) != len(requested):
            raise TemplateMergeError(
                f"Conflict {conflict.name!r} contains duplicate candidate keys"
            )
        candidates = {
            candidate.candidate_key: candidate
            for candidate in conflict.candidates
        }
        stale = set(requested) - set(candidates)
        if stale:
            raise TemplateMergeError(
                f"Conflict {conflict.name!r} has unknown or stale candidate(s): "
                + ", ".join(sorted(stale))
            )
        if len(requested) > 1 and not resolution.acknowledge_duplicate_risk:
            raise TemplateMergeError(
                f"Conflict {conflict.name!r} retains multiple candidates without "
                "acknowledging duplicate MOD risk"
            )
        ordered = tuple(
            candidate for candidate in conflict.candidates
            if candidate.candidate_key in requested
        )
        selected_keys.update(candidate.candidate_key for candidate in ordered)
        selections.append(
            ConflictSelection(
                conflict.key,
                conflict.name,
                tuple(candidate.candidate_key for candidate in ordered),
                tuple(
                    (
                        f"{candidate.provider}:{candidate.project_id} ({candidate.url})"
                        if candidate.provider == "url"
                        else f"{candidate.provider}:{candidate.project_id}"
                    )
                    for candidate in ordered
                ),
            )
        )
        if len(ordered) > 1:
            warnings.append(
                f"{conflict.name}: multiple sources retained; duplicate MOD IDs or "
                "overlapping functionality may prevent Minecraft from starting."
            )

    conflict_candidates = {
        candidate.candidate_key
        for conflict in composition.conflicts
        for candidate in conflict.candidates
    }
    mods = tuple(
        mod for mod in composition.mods
        if mod.candidate_key not in conflict_candidates or mod.candidate_key in selected_keys
    )
    return ResolvedTemplateComposition(
        composition.template_ids,
        mods,
        tuple(selections),
        tuple(warnings),
    )
