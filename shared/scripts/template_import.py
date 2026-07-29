from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

if TYPE_CHECKING:
    from huroshiki_core import ResolvedModClosure

from template_merge import (
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

    @property
    def selection_key(self) -> str:
        if self.origin_kind == "pack":
            if self.metadata_path is None:
                raise TemplateMergeError(
                    "Pack selection candidate requires a metadata path"
                )
            return f"pack:{self.origin_id}:{self.metadata_path.as_posix()}"
        return f"template:{self.candidate_key}"


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
class LogicalIdentityConflict:
    logical_identity: tuple[str, str]
    pack_candidate: ModCandidate
    template_candidates: tuple[ModCandidate, ...]

    @property
    def key(self) -> str:
        return f"{self.logical_identity[0]}:{self.logical_identity[1]}"

    @property
    def candidates(self) -> tuple[ModCandidate, ...]:
        return (self.pack_candidate, *self.template_candidates)


@dataclass(frozen=True)
class ActualIdentityConflict:
    actual_identity: tuple[str, str]
    pack_candidate: ModCandidate | None
    template_candidates: tuple[ModCandidate, ...]

    @property
    def key(self) -> str:
        return f"{self.actual_identity[0]}:{self.actual_identity[1]}"

    @property
    def candidates(self) -> tuple[ModCandidate, ...]:
        if self.pack_candidate is None:
            return self.template_candidates
        return (self.pack_candidate, *self.template_candidates)


@dataclass(frozen=True)
class ImportCandidateVerification:
    selector_identity: tuple[str, str, str | None]
    actual_identity: tuple[str, str] | None
    metadata_path: Path | None
    filename: str | None
    closure_fingerprint: str | None
    error: str | None
    cached_closure: ResolvedModClosure | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.actual_identity is not None


@dataclass(frozen=True)
class ImportConflictResolution:
    selection_keys: tuple[str, ...]
    acknowledge_duplicate_risk: bool = False


@dataclass(frozen=True)
class TemplateImportPlan:
    pack_key: str
    template_ids: tuple[str, ...]
    template_candidates: tuple[ModCandidate, ...]
    pack_candidates: tuple[ModCandidate, ...]
    new_roots: tuple[ModCandidate, ...]
    existing_identities: tuple[ModCandidate, ...]
    side_conflicts: tuple[IdentitySideConflict, ...]
    name_conflicts: tuple[CandidateNameConflict, ...]
    url_selector_conflicts: tuple[UrlSelectorConflict, ...]
    logical_identity_conflicts: tuple[LogicalIdentityConflict, ...]
    actual_identity_conflicts: tuple[ActualIdentityConflict, ...]
    verifications: tuple[ImportCandidateVerification, ...]
    plan_digest: str

    @property
    def requires_resolution(self) -> bool:
        return bool(
            self.name_conflicts
            or self.url_selector_conflicts
            or self.logical_identity_conflicts
            or self.actual_identity_conflicts
        )


@dataclass(frozen=True)
class ResolvedTemplateImportPlan:
    plan_digest: str
    selected_template_candidates: tuple[ModCandidate, ...]
    retained_pack_candidates: tuple[ModCandidate, ...]
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


def merge_template_import_candidates(
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


def _logical_identity_conflicts(
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
) -> tuple[LogicalIdentityConflict, ...]:
    pack_by_logical = {
        candidate.logical_identity: candidate for candidate in pack_candidates
    }
    grouped: dict[tuple[str, str], list[ModCandidate]] = {}
    for candidate in template_candidates:
        pack_candidate = pack_by_logical.get(candidate.logical_identity)
        if (
            pack_candidate is not None
            and candidate.actual_identity is not None
            and candidate.actual_identity != pack_candidate.actual_identity
        ):
            grouped.setdefault(candidate.logical_identity, []).append(candidate)
    return tuple(
        LogicalIdentityConflict(identity, pack_by_logical[identity], tuple(candidates))
        for identity, candidates in grouped.items()
    )


def _actual_identity_conflicts(
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
) -> tuple[ActualIdentityConflict, ...]:
    pack_by_identity = {
        candidate.actual_identity: candidate
        for candidate in pack_candidates
        if candidate.actual_identity is not None
    }
    grouped: dict[tuple[str, str], list[ModCandidate]] = {}
    for candidate in template_candidates:
        if candidate.actual_identity is not None:
            grouped.setdefault(candidate.actual_identity, []).append(candidate)
    conflicts: list[ActualIdentityConflict] = []
    for identity, candidates in grouped.items():
        pack_candidate = pack_by_identity.get(identity)
        selector_collision = len(
            {candidate.selector_identity for candidate in candidates}
        ) > 1
        differs_from_pack_selector = pack_candidate is not None and any(
            candidate.selector_identity != pack_candidate.selector_identity
            for candidate in candidates
        )
        if differs_from_pack_selector or selector_collision:
            conflicts.append(
                ActualIdentityConflict(identity, pack_candidate, tuple(candidates))
            )
    return tuple(conflicts)


def _plan_digest_payload(
    pack_key: str,
    template_ids: tuple[str, ...],
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
    verifications: Sequence[ImportCandidateVerification],
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
            "candidate_key": candidate.candidate_key,
            "selection_key": candidate.selection_key,
        }

    payload = {
        "version": 2,
        "pack_key": pack_key,
        "template_ids": template_ids,
        "pack_candidates": [record(item) for item in pack_candidates],
        "template_candidates": [record(item) for item in template_candidates],
        "verifications": [
            {
                "selector_identity": item.selector_identity,
                "actual_identity": item.actual_identity,
                "metadata_path": (
                    item.metadata_path.as_posix()
                    if item.metadata_path is not None
                    else None
                ),
                "filename": item.filename,
                "closure_fingerprint": item.closure_fingerprint,
                "error": item.error,
            }
            for item in verifications
        ],
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
    verifications: Sequence[ImportCandidateVerification],
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
        if candidate.actual_identity is None:
            raise TemplateMergeError("Pack candidates require an actual identity")
    for candidate in (*pack_candidates, *template_candidates):
        if candidate.side not in {"client", "server", "both"}:
            raise TemplateMergeError(f"Invalid candidate side: {candidate.side}")
        if not candidate.name.strip() or not candidate.provider or not candidate.project_id:
            raise TemplateMergeError("Import candidates require name, provider, and project ID")
        if candidate.provider == "url" and not candidate.url:
            raise TemplateMergeError("URL import candidates require a URL selector")
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
    merged_templates = merge_template_import_candidates(ordered_templates)
    selection_keys = [
        candidate.selection_key
        for candidate in (*pack_candidates, *merged_templates)
    ]
    if len(selection_keys) != len(set(selection_keys)):
        raise TemplateMergeError(
            "Template import candidates contain duplicate selection keys"
        )
    verification_by_selector = {
        item.selector_identity: item for item in verifications
    }
    if len(verification_by_selector) != len(verifications) or set(
        verification_by_selector
    ) != {candidate.selector_identity for candidate in merged_templates}:
        raise TemplateMergeError("Template candidate verifications are incomplete or stale")
    for candidate in merged_templates:
        verification = verification_by_selector[candidate.selector_identity]
        if candidate.actual_identity != verification.actual_identity:
            raise TemplateMergeError("Template candidate verification identity mismatch")
    pack_by_actual = {
        candidate.actual_identity: candidate for candidate in pack_candidates
    }
    new_roots = tuple(
        candidate
        for candidate in merged_templates
        if candidate.actual_identity is not None
        and candidate.actual_identity not in pack_by_actual
    )
    existing_actual = {
        candidate.actual_identity
        for candidate in merged_templates
        if candidate.actual_identity in pack_by_actual
    }
    existing = tuple(
        candidate
        for candidate in pack_candidates
        if candidate.actual_identity in existing_actual
    )
    side_conflicts = tuple(
        IdentitySideConflict(
            candidate.actual_identity,
            pack_by_actual[candidate.actual_identity].side,
            candidate.side,
        )
        for candidate in merged_templates
        if candidate.actual_identity in pack_by_actual
        and candidate.selector_identity
        == pack_by_actual[candidate.actual_identity].selector_identity
        and pack_by_actual[candidate.actual_identity].side != candidate.side
    )
    name_conflicts = _name_conflicts((*pack_candidates, *merged_templates))
    url_selector_conflicts = _url_selector_conflicts(merged_templates)
    logical_identity_conflicts = _logical_identity_conflicts(
        pack_candidates, merged_templates
    )
    actual_identity_conflicts = _actual_identity_conflicts(
        pack_candidates, merged_templates
    )
    return TemplateImportPlan(
        pack_key,
        ordered_ids,
        merged_templates,
        tuple(pack_candidates),
        new_roots,
        existing,
        side_conflicts,
        name_conflicts,
        url_selector_conflicts,
        logical_identity_conflicts,
        actual_identity_conflicts,
        tuple(verifications),
        _plan_digest_payload(
            pack_key,
            ordered_ids,
            pack_candidates,
            ordered_templates,
            verifications,
        ),
    )


def resolve_template_import_plan(
    plan: TemplateImportPlan,
    *,
    name_resolutions: Mapping[str, ImportConflictResolution] | None = None,
    url_selector_resolutions: Mapping[str, ImportConflictResolution] | None = None,
    logical_identity_resolutions: Mapping[str, ImportConflictResolution] | None = None,
    actual_identity_resolutions: Mapping[str, ImportConflictResolution] | None = None,
    side_decisions: Mapping[tuple[str, str], SideDecision] | None = None,
) -> ResolvedTemplateImportPlan:
    requirements: dict[str, bool] = {}
    sources: dict[str, list[str]] = {}
    warnings: list[str] = []

    def checked_resolutions(
        kind: str,
        conflicts: Sequence[object],
        supplied: Mapping[str, ImportConflictResolution] | None,
    ) -> dict[str, ImportConflictResolution]:
        values = dict(supplied or {})
        expected = {getattr(conflict, "key") for conflict in conflicts}
        if set(values) != expected:
            details = sorted((expected - set(values)) | (set(values) - expected))
            raise TemplateMergeError(
                f"Unresolved or stale {kind} conflict(s): " + ", ".join(details)
            )
        return values

    def apply_constraints(
        *,
        kind: str,
        key: str,
        candidates: Sequence[ModCandidate],
        resolution: ImportConflictResolution,
        cardinality: Literal["one-or-more", "exactly-one"],
    ) -> None:
        keys = tuple(resolution.selection_keys)
        available_keys = tuple(candidate.selection_key for candidate in candidates)
        available = set(available_keys)
        if len(available) != len(available_keys):
            raise TemplateMergeError(f"Conflict {kind} {key!r} has duplicate candidate keys")
        if len(set(keys)) != len(keys) or not set(keys) <= available:
            raise TemplateMergeError(f"Invalid resolution for {kind} conflict {key!r}")
        if cardinality == "exactly-one" and len(keys) != 1:
            raise TemplateMergeError(
                f"{kind.capitalize()} conflict {key!r} requires exactly one candidate"
            )
        if cardinality == "one-or-more" and not keys:
            raise TemplateMergeError(
                f"{kind.capitalize()} conflict {key!r} requires at least one candidate"
            )
        selected = set(keys)
        source = f'{kind} conflict "{key}"'
        candidate_by_selection = {
            candidate.selection_key: candidate for candidate in candidates
        }
        for selection_key in available_keys:
            required = selection_key in selected
            previous = requirements.get(selection_key)
            if previous is not None and previous != required:
                previous_source = sources[selection_key][-1]
                action = "selected" if previous else "rejected"
                opposite = "selected" if required else "rejected"
                candidate = candidate_by_selection[selection_key]
                raise TemplateMergeError(
                    f"Selection {selection_key} ({candidate.candidate_key}) is "
                    f"{action} by {previous_source} but {opposite} by {source}"
                )
            requirements[selection_key] = required
            sources.setdefault(selection_key, []).append(source)

    conflict_groups = (
        (
            "name",
            plan.name_conflicts,
            checked_resolutions("name", plan.name_conflicts, name_resolutions),
            "one-or-more",
        ),
        (
            "URL selector",
            plan.url_selector_conflicts,
            checked_resolutions(
                "URL selector", plan.url_selector_conflicts, url_selector_resolutions
            ),
            "one-or-more",
        ),
        (
            "logical identity",
            plan.logical_identity_conflicts,
            checked_resolutions(
                "logical identity",
                plan.logical_identity_conflicts,
                logical_identity_resolutions,
            ),
            "exactly-one",
        ),
        (
            "actual identity",
            plan.actual_identity_conflicts,
            checked_resolutions(
                "actual identity",
                plan.actual_identity_conflicts,
                actual_identity_resolutions,
            ),
            "exactly-one",
        ),
    )
    for kind, conflicts, resolutions, cardinality in conflict_groups:
        for conflict in conflicts:
            apply_constraints(
                kind=kind,
                key=conflict.key,
                candidates=conflict.candidates,
                resolution=resolutions[conflict.key],
                cardinality=cardinality,
            )

    selected_keys = {
        candidate.selection_key
        for candidate in (*plan.pack_candidates, *plan.template_candidates)
        if requirements.get(candidate.selection_key, True)
    }
    for kind, conflicts, resolutions, cardinality in conflict_groups:
        for conflict in conflicts:
            final_keys = tuple(
                candidate.selection_key
                for candidate in conflict.candidates
                if candidate.selection_key in selected_keys
            )
            if cardinality == "exactly-one" and len(final_keys) != 1:
                raise TemplateMergeError(
                    f"{kind.capitalize()} conflict {conflict.key!r} must retain exactly one candidate"
                )
            if cardinality == "one-or-more" and not final_keys:
                raise TemplateMergeError(
                    f"{kind.capitalize()} conflict {conflict.key!r} must retain a candidate"
                )
            resolution = resolutions[conflict.key]
            if len(final_keys) > 1 and not resolution.acknowledge_duplicate_risk:
                raise TemplateMergeError(
                    f"Conflict {conflict.key!r} retains multiple candidates without "
                    "acknowledging duplicate MOD risk"
                )
            if len(final_keys) > 1:
                warnings.append(
                    f"{conflict.key}: multiple sources retained; duplicate MOD risk acknowledged"
                )

    selected_templates = tuple(
        candidate
        for candidate in plan.template_candidates
        if candidate.selection_key in selected_keys
    )
    retained_pack = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.selection_key in selected_keys
    )
    verification_by_selector = {
        item.selector_identity: item for item in plan.verifications
    }
    for candidate in selected_templates:
        verification = verification_by_selector[candidate.selector_identity]
        if not verification.succeeded:
            raise TemplateMergeError(
                f"Selected candidate {candidate.candidate_key} could not be verified: "
                f"{verification.error}"
            )

    retained_actual = {
        candidate.actual_identity for candidate in retained_pack
    }
    selected_new = tuple(
        candidate
        for candidate in selected_templates
        if candidate.actual_identity not in retained_actual
    )
    removed_pack = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.selection_key not in selected_keys
    )
    all_conflicts = tuple(
        conflict
        for _kind, conflicts, _resolutions, _cardinality in conflict_groups
        for conflict in conflicts
    )
    for removed in removed_pack:
        replacing = any(
            removed in conflict.candidates
            and any(candidate in selected_new for candidate in conflict.candidates)
            for conflict in all_conflicts
        )
        if not replacing:
            raise TemplateMergeError(
                f"Removing {removed.candidate_key} leaves no selected replacement"
            )

    supplied_sides = dict(side_decisions or {})
    expected_sides = {conflict.identity for conflict in plan.side_conflicts}
    stale_sides = set(supplied_sides) - expected_sides
    if stale_sides:
        raise TemplateMergeError("Unknown or stale side conflict decision")
    side_changes: list[tuple[tuple[str, str], str, str]] = []
    selected_template_actual = {
        candidate.actual_identity for candidate in selected_templates
    }
    for conflict in plan.side_conflicts:
        if (
            conflict.identity not in retained_actual
            or conflict.identity not in selected_template_actual
        ):
            continue
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
        selected_templates,
        retained_pack,
        selected_new,
        removed_pack,
        tuple(side_changes),
        tuple(warnings),
    )
