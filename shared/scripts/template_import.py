from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Literal, Mapping, Sequence

from template_merge import (
    ConflictResolution,
    TemplateMergeError,
    TemplateModEntry,
    normalize_name,
    union_side,
)


SideDecision = Literal["keep_pack", "use_template", "union"]


@dataclass(frozen=True)
class ModCandidate:
    origin_kind: Literal["pack", "template"]
    origin_id: str
    name: str
    provider: str
    project_id: str
    side: str
    metadata_path: Path | None = None
    filename: str | None = None
    url: str | None = None
    url_max_jar_size_bytes: int | None = None
    url_allow_private_networks: bool = False
    actual_provider: str | None = None
    actual_project_id: str | None = None

    @property
    def logical_identity(self) -> tuple[str, str]:
        return self.provider, self.project_id

    @property
    def selector_identity(self) -> tuple[str, str, str | None]:
        return (
            self.provider,
            self.project_id,
            self.url if self.provider == "url" else None,
        )

    @property
    def actual_identity(self) -> tuple[str, str] | None:
        if self.actual_provider is None or self.actual_project_id is None:
            return None
        return self.actual_provider, self.actual_project_id

    @property
    def candidate_key(self) -> str:
        base = f"{self.provider}:{self.project_id}"
        return f"{base}@{self.url}" if self.provider == "url" else base


@dataclass(frozen=True)
class TemplateCompatibility:
    template_id: str
    minecraft: str
    loader: str


@dataclass(frozen=True)
class IdentitySideConflict:
    identity: tuple[str, str]
    pack_side: str
    template_side: str


@dataclass(frozen=True)
class CandidateNameConflict:
    key: str
    name: str
    candidates: tuple[ModCandidate, ...]


@dataclass(frozen=True)
class UrlSelectorConflict:
    logical_identity: tuple[str, str]
    candidates: tuple[ModCandidate, ...]

    @property
    def key(self) -> str:
        return f"{self.logical_identity[0]}:{self.logical_identity[1]}"


@dataclass(frozen=True)
class TemplateImportPlan:
    pack_key: str
    template_ids: tuple[str, ...]
    new_roots: tuple[ModCandidate, ...]
    existing_identities: tuple[ModCandidate, ...]
    side_conflicts: tuple[IdentitySideConflict, ...]
    name_conflicts: tuple[CandidateNameConflict, ...]
    url_selector_conflicts: tuple[UrlSelectorConflict, ...]
    pack_candidates: tuple[ModCandidate, ...]
    plan_digest: str

    @property
    def requires_resolution(self) -> bool:
        return bool(self.name_conflicts or self.url_selector_conflicts)


@dataclass(frozen=True)
class ResolvedTemplateImportPlan:
    plan_digest: str
    selected_new_roots: tuple[ModCandidate, ...]
    removed_pack_candidates: tuple[ModCandidate, ...]
    side_changes: tuple[tuple[tuple[str, str], str, str], ...]
    warnings: tuple[str, ...]


def template_candidate(
    template_id: str,
    *,
    name: str,
    provider: str,
    project_id: str,
    side: str,
    url: str | None = None,
    url_max_jar_size_bytes: int | None = None,
    url_allow_private_networks: bool = False,
    actual_provider: str | None = None,
    actual_project_id: str | None = None,
) -> ModCandidate:
    return ModCandidate(
        "template",
        template_id,
        name,
        provider,
        project_id,
        side,
        url=url,
        url_max_jar_size_bytes=url_max_jar_size_bytes,
        url_allow_private_networks=url_allow_private_networks,
        actual_provider=actual_provider,
        actual_project_id=actual_project_id,
    )


def candidate_from_template_entry(entry: TemplateModEntry) -> ModCandidate:
    return template_candidate(
        entry.template_id,
        name=entry.name,
        provider=entry.provider,
        project_id=entry.project_id,
        side=entry.side,
        url=entry.url,
        url_max_jar_size_bytes=entry.max_url_jar_size_bytes,
        url_allow_private_networks=entry.url_allow_private_networks,
    )


def _merged_template_identities(
    candidates: Sequence[ModCandidate],
) -> tuple[ModCandidate, ...]:
    merged: list[ModCandidate] = []
    indexes: dict[tuple[str, str, str | None], int] = {}
    for candidate in candidates:
        index = indexes.get(candidate.selector_identity)
        if index is None:
            indexes[candidate.selector_identity] = len(merged)
            merged.append(candidate)
        else:
            current = merged[index]
            limits = tuple(
                limit
                for limit in (
                    current.url_max_jar_size_bytes,
                    candidate.url_max_jar_size_bytes,
                )
                if limit is not None
            )
            merged[index] = replace(
                current,
                side=union_side(current.side, candidate.side),
                url_max_jar_size_bytes=min(limits) if limits else None,
                url_allow_private_networks=(
                    current.url_allow_private_networks
                    and candidate.url_allow_private_networks
                ),
            )
    return tuple(merged)


def _name_conflicts(candidates: Sequence[ModCandidate]) -> tuple[CandidateNameConflict, ...]:
    identity_order: list[tuple[str, str, str | None]] = []
    identity_candidates: dict[tuple[str, str, str | None], ModCandidate] = {}
    aliases: dict[tuple[str, str, str | None], set[str]] = {}
    for candidate in candidates:
        identity = candidate.selector_identity
        if identity not in identity_candidates:
            identity_order.append(identity)
            identity_candidates[identity] = candidate
            aliases[identity] = set()
        aliases[identity].add(normalize_name(candidate.name))

    parents = list(range(len(identity_order)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    first_by_name: dict[str, int] = {}
    for index, identity in enumerate(identity_order):
        for alias in sorted(aliases[identity]):
            previous = first_by_name.get(alias)
            if previous is None:
                first_by_name[alias] = index
            else:
                parents[root(index)] = root(previous)

    groups: dict[int, list[ModCandidate]] = {}
    for index, identity in enumerate(identity_order):
        groups.setdefault(root(index), []).append(identity_candidates[identity])
    return tuple(
        CandidateNameConflict(
            normalize_name(group[0].name),
            group[0].name.strip(),
            tuple(group),
        )
        for group in groups.values()
        if len(group) > 1
    )


def _url_selector_conflicts(
    candidates: Sequence[ModCandidate],
) -> tuple[UrlSelectorConflict, ...]:
    grouped: dict[tuple[str, str], list[ModCandidate]] = {}
    for candidate in candidates:
        if candidate.provider == "url":
            grouped.setdefault(candidate.logical_identity, []).append(candidate)
    return tuple(
        UrlSelectorConflict(identity, tuple(group))
        for identity, group in grouped.items()
        if len({candidate.selector_identity for candidate in group}) > 1
    )


def _plan_digest_payload(
    pack_key: str,
    template_ids: tuple[str, ...],
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
) -> str:
    def record(candidate: ModCandidate) -> dict[str, object]:
        return {
            "origin_kind": candidate.origin_kind,
            "origin_id": candidate.origin_id,
            "name": candidate.name,
            "provider": candidate.provider,
            "project_id": candidate.project_id,
            "side": candidate.side,
            "metadata_path": (
                candidate.metadata_path.as_posix()
                if candidate.metadata_path is not None
                else None
            ),
            "filename": candidate.filename,
            "url": candidate.url,
            "url_max_jar_size_bytes": candidate.url_max_jar_size_bytes,
            "url_allow_private_networks": candidate.url_allow_private_networks,
            "actual_provider": candidate.actual_provider,
            "actual_project_id": candidate.actual_project_id,
        }

    payload = {
        "version": 1,
        "pack_key": pack_key,
        "template_ids": template_ids,
        "pack_candidates": [record(item) for item in pack_candidates],
        "template_candidates": [record(item) for item in template_candidates],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_template_import_plan(
    *,
    pack_key: str,
    pack_minecraft: str,
    pack_loader: str,
    template_ids: Sequence[str],
    compatibilities: Mapping[str, TemplateCompatibility],
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
) -> TemplateImportPlan:
    ordered_ids = tuple(template_ids)
    if not ordered_ids:
        raise TemplateMergeError("At least one template must be selected")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise TemplateMergeError("Template selection contains duplicate IDs")
    for template_id in ordered_ids:
        compatibility = compatibilities.get(template_id)
        if compatibility is None:
            raise TemplateMergeError(f"Missing compatibility data for {template_id}")
        if compatibility.minecraft != pack_minecraft or compatibility.loader != pack_loader:
            raise TemplateMergeError(
                f"Template {template_id} is incompatible with {pack_minecraft}/{pack_loader}"
            )
    for candidate in pack_candidates:
        if candidate.origin_kind != "pack":
            raise TemplateMergeError("Pack candidates must have origin_kind='pack'")
    for candidate in (*pack_candidates, *template_candidates):
        if candidate.side not in {"client", "server", "both"}:
            raise TemplateMergeError(f"Invalid candidate side: {candidate.side}")
        if not candidate.name.strip() or not candidate.provider or not candidate.project_id:
            raise TemplateMergeError("Import candidates require name, provider, and project ID")
    if len({candidate.logical_identity for candidate in pack_candidates}) != len(
        pack_candidates
    ):
        raise TemplateMergeError("Pack candidates contain duplicate identities")
    ordered_templates = [
        candidate
        for template_id in ordered_ids
        for candidate in template_candidates
        if candidate.origin_kind == "template" and candidate.origin_id == template_id
    ]
    if len(ordered_templates) != len(template_candidates):
        raise TemplateMergeError("Template candidate references an unselected template")
    merged_templates = _merged_template_identities(ordered_templates)
    pack_by_identity = {
        candidate.logical_identity: candidate for candidate in pack_candidates
    }
    new_roots = tuple(
        candidate
        for candidate in merged_templates
        if candidate.logical_identity not in pack_by_identity
    )
    existing = tuple(
        pack_by_identity[candidate.logical_identity]
        for candidate in merged_templates
        if candidate.logical_identity in pack_by_identity
    )
    side_conflicts = tuple(
        IdentitySideConflict(
            candidate.logical_identity,
            pack_by_identity[candidate.logical_identity].side,
            candidate.side,
        )
        for candidate in merged_templates
        if candidate.logical_identity in pack_by_identity
        and pack_by_identity[candidate.logical_identity].side != candidate.side
    )
    name_conflicts = _name_conflicts((*pack_candidates, *merged_templates))
    url_selector_conflicts = _url_selector_conflicts(merged_templates)
    return TemplateImportPlan(
        pack_key,
        ordered_ids,
        new_roots,
        existing,
        side_conflicts,
        name_conflicts,
        url_selector_conflicts,
        tuple(pack_candidates),
        _plan_digest_payload(
            pack_key,
            ordered_ids,
            pack_candidates,
            ordered_templates,
        ),
    )


def resolve_template_import_plan(
    plan: TemplateImportPlan,
    *,
    name_resolutions: Mapping[str, ConflictResolution] | None = None,
    url_selector_resolutions: Mapping[str, ConflictResolution] | None = None,
    side_decisions: Mapping[tuple[str, str], SideDecision] | None = None,
) -> ResolvedTemplateImportPlan:
    supplied_names = dict(name_resolutions or {})
    expected_names = {conflict.key for conflict in plan.name_conflicts}
    if set(supplied_names) != expected_names:
        missing = expected_names - set(supplied_names)
        stale = set(supplied_names) - expected_names
        details = sorted(missing | stale)
        raise TemplateMergeError(
            "Unresolved or stale import name conflict(s): " + ", ".join(details)
        )
    selected_keys = {
        candidate.candidate_key
        for candidate in (*plan.pack_candidates, *plan.new_roots)
    }
    warnings: list[str] = []
    for conflict in plan.name_conflicts:
        resolution = supplied_names[conflict.key]
        keys = tuple(resolution.candidate_keys)
        available = {candidate.candidate_key for candidate in conflict.candidates}
        if not keys or len(set(keys)) != len(keys) or not set(keys) <= available:
            raise TemplateMergeError(f"Invalid resolution for {conflict.name}")
        if len(keys) > 1 and not resolution.acknowledge_duplicate_risk:
            raise TemplateMergeError(
                f"Conflict {conflict.name!r} retains multiple candidates without "
                "acknowledging duplicate MOD risk"
            )
        selected_keys.difference_update(available - set(keys))
        if len(keys) > 1:
            warnings.append(
                f"{conflict.name}: multiple sources retained; duplicate MOD risk acknowledged"
            )

    conflict_keys = {
        candidate.candidate_key
        for conflict in plan.name_conflicts
        for candidate in conflict.candidates
    }
    supplied_urls = dict(url_selector_resolutions or {})
    expected_urls = {conflict.key for conflict in plan.url_selector_conflicts}
    if set(supplied_urls) != expected_urls:
        details = sorted(
            (expected_urls - set(supplied_urls))
            | (set(supplied_urls) - expected_urls)
        )
        raise TemplateMergeError(
            "Unresolved or stale URL selector conflict(s): " + ", ".join(details)
        )
    for conflict in plan.url_selector_conflicts:
        resolution = supplied_urls[conflict.key]
        keys = tuple(resolution.candidate_keys)
        available = {candidate.candidate_key for candidate in conflict.candidates}
        if not keys or len(set(keys)) != len(keys) or not set(keys) <= available:
            raise TemplateMergeError(f"Invalid resolution for {conflict.key}")
        if len(keys) > 1 and not resolution.acknowledge_duplicate_risk:
            raise TemplateMergeError(
                f"Conflict {conflict.key!r} retains multiple URL selectors without "
                "acknowledging duplicate MOD risk"
            )
        selected_keys.difference_update(available - set(keys))
        conflict_keys.update(available)
        if len(keys) > 1:
            warnings.append(
                f"{conflict.key}: multiple URL selectors retained; duplicate MOD risk acknowledged"
            )
    selected_new = tuple(
        candidate
        for candidate in plan.new_roots
        if candidate.candidate_key not in conflict_keys
        or candidate.candidate_key in selected_keys
    )
    removed_pack = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.candidate_key in conflict_keys
        and candidate.candidate_key not in selected_keys
    )

    supplied_sides = dict(side_decisions or {})
    expected_sides = {conflict.identity for conflict in plan.side_conflicts}
    stale_sides = set(supplied_sides) - expected_sides
    if stale_sides:
        raise TemplateMergeError("Unknown or stale side conflict decision")
    side_changes: list[tuple[tuple[str, str], str, str]] = []
    for conflict in plan.side_conflicts:
        decision = supplied_sides.get(conflict.identity, "keep_pack")
        if decision == "keep_pack":
            result = conflict.pack_side
        elif decision == "use_template":
            result = conflict.template_side
        elif decision == "union":
            result = union_side(conflict.pack_side, conflict.template_side)
        else:
            raise TemplateMergeError(f"Invalid side decision: {decision}")
        if result != conflict.pack_side:
            side_changes.append((conflict.identity, conflict.pack_side, result))
    return ResolvedTemplateImportPlan(
        plan.plan_digest,
        selected_new,
        removed_pack,
        tuple(side_changes),
        tuple(warnings),
    )
