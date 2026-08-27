from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
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
_CANONICAL_DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")
_IMMUTABLE_MODRINTH_ID = re.compile(r"^[A-Za-z0-9]{8}$")


@dataclass(frozen=True)
class TemplateVersionConstraint:
    """A version/artifact requirement contributed by one Template."""

    template_id: str
    provider: str
    project_id: str
    artifact_id: str
    scope: Literal["root", "dependency"]


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
    # A merged selector can be supplied by more than one Template.  Keep this
    # separate from origin_id: the latter is intentionally the stable display
    # and selection identity of the first contributor.
    template_origin_ids: tuple[str, ...] = ()

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
class ImportSelectionOption:
    option_key: str
    candidates: tuple[ModCandidate, ...]

    @property
    def selection_keys(self) -> tuple[str, ...]:
        return tuple(candidate.selection_key for candidate in self.candidates)

    @property
    def candidate_keys(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_key for candidate in self.candidates)

    @property
    def selector_identity(self) -> tuple[str, str, str | None]:
        identities = {candidate.selector_identity for candidate in self.candidates}
        if len(identities) != 1:
            raise TemplateMergeError("Selection option contains multiple selectors")
        return next(iter(identities))

    @property
    def actual_identity(self) -> tuple[str, str] | None:
        identities = {candidate.actual_identity for candidate in self.candidates}
        if len(identities) != 1:
            raise TemplateMergeError(
                "Selection option contains multiple actual identities"
            )
        return next(iter(identities))


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
    options: tuple[ImportSelectionOption, ...]

    @property
    def candidates(self) -> tuple[ModCandidate, ...]:
        return tuple(candidate for option in self.options for candidate in option.candidates)


@dataclass(frozen=True)
class UrlSelectorConflict:
    logical_identity: tuple[str, str]
    options: tuple[ImportSelectionOption, ...]

    @property
    def key(self) -> str:
        return f"{self.logical_identity[0]}:{self.logical_identity[1]}"

    @property
    def candidates(self) -> tuple[ModCandidate, ...]:
        return tuple(candidate for option in self.options for candidate in option.candidates)


@dataclass(frozen=True)
class LogicalIdentityConflict:
    logical_identity: tuple[str, str]
    options: tuple[ImportSelectionOption, ...]

    @property
    def key(self) -> str:
        return f"{self.logical_identity[0]}:{self.logical_identity[1]}"

    @property
    def candidates(self) -> tuple[ModCandidate, ...]:
        return tuple(candidate for option in self.options for candidate in option.candidates)

    @property
    def pack_candidate(self) -> ModCandidate:
        return next(candidate for candidate in self.candidates if candidate.origin_kind == "pack")

    @property
    def template_candidates(self) -> tuple[ModCandidate, ...]:
        return tuple(
            candidate for candidate in self.candidates if candidate.origin_kind == "template"
        )


@dataclass(frozen=True)
class ActualIdentityConflict:
    actual_identity: tuple[str, str]
    options: tuple[ImportSelectionOption, ...]

    @property
    def key(self) -> str:
        return f"{self.actual_identity[0]}:{self.actual_identity[1]}"

    @property
    def candidates(self) -> tuple[ModCandidate, ...]:
        return tuple(candidate for option in self.options for candidate in option.candidates)

    @property
    def pack_candidate(self) -> ModCandidate | None:
        return next(
            (candidate for candidate in self.candidates if candidate.origin_kind == "pack"),
            None,
        )

    @property
    def template_candidates(self) -> tuple[ModCandidate, ...]:
        return tuple(
            candidate for candidate in self.candidates if candidate.origin_kind == "template"
        )


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
    option_keys: tuple[str, ...]
    acknowledge_duplicate_risk: bool = False


@dataclass(frozen=True)
class TemplateImportPlan:
    pack_key: str
    template_ids: tuple[str, ...]
    template_candidates: tuple[ModCandidate, ...]
    pack_candidates: tuple[ModCandidate, ...]
    selection_options: tuple[ImportSelectionOption, ...]
    new_roots: tuple[ModCandidate, ...]
    existing_identities: tuple[ModCandidate, ...]
    side_conflicts: tuple[IdentitySideConflict, ...]
    name_conflicts: tuple[CandidateNameConflict, ...]
    url_selector_conflicts: tuple[UrlSelectorConflict, ...]
    logical_identity_conflicts: tuple[LogicalIdentityConflict, ...]
    actual_identity_conflicts: tuple[ActualIdentityConflict, ...]
    verifications: tuple[ImportCandidateVerification, ...]
    plan_digest: str
    version_constraints: tuple[TemplateVersionConstraint, ...] = ()

    @property
    def requires_resolution(self) -> bool:
        return bool(
            self.name_conflicts
            or self.url_selector_conflicts
            or self.logical_identity_conflicts
            or self.actual_identity_conflicts
        )

    def active_version_constraints(
        self,
        selected_template_candidates: Sequence[ModCandidate] | None = None,
    ) -> tuple[TemplateVersionConstraint, ...]:
        candidates = (
            self.template_candidates
            if selected_template_candidates is None
            else selected_template_candidates
        )
        return _active_version_constraints(self, candidates)


@dataclass(frozen=True)
class ResolvedTemplateImportPlan:
    plan_digest: str
    selected_option_keys: tuple[str, ...]
    selected_template_candidates: tuple[ModCandidate, ...]
    retained_pack_candidates: tuple[ModCandidate, ...]
    selected_new_roots: tuple[ModCandidate, ...]
    removed_pack_candidates: tuple[ModCandidate, ...]
    side_changes: tuple[tuple[tuple[str, str], str, str], ...]
    warnings: tuple[str, ...]
    version_constraints: tuple[TemplateVersionConstraint, ...] = ()


def validate_resolved_template_import_plan(
    plan: TemplateImportPlan,
    resolved: ResolvedTemplateImportPlan,
) -> None:
    """Re-derive execution-bearing resolution fields from the fixed plan."""

    if resolved.plan_digest != plan.plan_digest:
        raise TemplateMergeError("Template import resolution has a stale plan digest")
    selected_option_keys = tuple(resolved.selected_option_keys)
    if len(selected_option_keys) != len(set(selected_option_keys)):
        raise TemplateMergeError("Template import resolution repeats an option key")
    selected_set = set(selected_option_keys)
    available = {option.option_key for option in plan.selection_options}
    if not selected_set <= available:
        raise TemplateMergeError("Template import resolution contains an unknown option")
    expected_order = tuple(
        option.option_key
        for option in plan.selection_options
        if option.option_key in selected_set
    )
    if selected_option_keys != expected_order:
        raise TemplateMergeError("Template import resolution option order is stale")
    for conflicts, exactly_one in (
        (plan.name_conflicts, False),
        (plan.url_selector_conflicts, False),
        (plan.logical_identity_conflicts, True),
        (plan.actual_identity_conflicts, True),
    ):
        for conflict in conflicts:
            count = sum(
                option.option_key in selected_set for option in conflict.options
            )
            if (exactly_one and count != 1) or (not exactly_one and count < 1):
                raise TemplateMergeError(
                    f"Template import resolution violates conflict {conflict.key!r}"
                )

    selected_candidate_keys = {
        candidate.selection_key
        for option in plan.selection_options
        if option.option_key in selected_set
        for candidate in option.candidates
    }
    expected_templates = tuple(
        candidate
        for candidate in plan.template_candidates
        if candidate.selection_key in selected_candidate_keys
    )
    expected_pack = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.selection_key in selected_candidate_keys
    )
    retained_actual = {candidate.actual_identity for candidate in expected_pack}
    expected_new = tuple(
        candidate
        for candidate in expected_templates
        if candidate.actual_identity is not None
        and candidate.actual_identity not in retained_actual
    )
    expected_removed = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.selection_key not in selected_candidate_keys
    )
    if (
        resolved.selected_template_candidates != expected_templates
        or resolved.retained_pack_candidates != expected_pack
        or resolved.selected_new_roots != expected_new
        or resolved.removed_pack_candidates != expected_removed
    ):
        raise TemplateMergeError(
            "Template import resolution candidate membership is stale"
        )
    verification_by_selector = {
        item.selector_identity: item for item in plan.verifications
    }
    if any(
        not verification_by_selector[candidate.selector_identity].succeeded
        for candidate in expected_templates
    ):
        raise TemplateMergeError(
            "Template import resolution selects an unverified candidate"
        )
    all_conflicts = (
        *plan.name_conflicts,
        *plan.url_selector_conflicts,
        *plan.logical_identity_conflicts,
        *plan.actual_identity_conflicts,
    )
    for removed in expected_removed:
        if not any(
            removed in conflict.candidates
            and any(
                option.option_key in selected_set
                and any(candidate in expected_new for candidate in option.candidates)
                for option in conflict.options
            )
            for conflict in all_conflicts
        ):
            raise TemplateMergeError(
                f"Removing {removed.candidate_key} leaves no selected replacement"
            )
    expected_constraints = _active_version_constraints(plan, expected_templates)
    if resolved.version_constraints != expected_constraints:
        raise TemplateMergeError(
            "Template import resolution version constraints are stale"
        )

    side_conflicts = {item.identity: item for item in plan.side_conflicts}
    seen_sides: set[tuple[str, str]] = set()
    selected_template_actual = {
        candidate.actual_identity for candidate in expected_templates
    }
    for identity, old_side, new_side in resolved.side_changes:
        conflict = side_conflicts.get(identity)
        if (
            conflict is None
            or identity in seen_sides
            or identity not in retained_actual
            or identity not in selected_template_actual
            or old_side != conflict.pack_side
            or new_side
            not in {
                conflict.template_side,
                union_side(conflict.pack_side, conflict.template_side),
            }
            or new_side == old_side
        ):
            raise TemplateMergeError(
                "Template import resolution side changes are stale"
            )
        seen_sides.add(identity)


def _active_version_constraints(
    plan: TemplateImportPlan,
    selected_candidates: Sequence[ModCandidate],
) -> tuple[TemplateVersionConstraint, ...]:
    active: list[TemplateVersionConstraint] = []
    for constraint in plan.version_constraints:
        if constraint.scope == "dependency":
            active.append(constraint)
            continue
        if any(
            constraint.template_id in candidate.template_origin_ids
            and candidate.logical_identity
            == (constraint.provider, constraint.project_id)
            for candidate in selected_candidates
        ):
            active.append(constraint)

    # Constraints are ordered input, but consistency is independent of that
    # order.  Never silently let a later Template replace an earlier intent.
    artifacts: dict[tuple[str, str], str] = {}
    roles: dict[tuple[str, str, str], str] = {}
    for constraint in active:
        identity = (constraint.provider, constraint.project_id)
        previous_artifact = artifacts.get(identity)
        if previous_artifact is not None and previous_artifact != constraint.artifact_id:
            raise TemplateMergeError(
                "Conflicting active Template version artifacts for "
                f"{identity[0]}:{identity[1]}"
            )
        artifacts[identity] = constraint.artifact_id
        role_key = (*identity, constraint.artifact_id)
        previous_scope = roles.get(role_key)
        if previous_scope is not None and previous_scope != constraint.scope:
            raise TemplateMergeError(
                "Conflicting active Template version constraint roles for "
                f"{identity[0]}:{identity[1]}"
            )
        roles[role_key] = constraint.scope
    return tuple(active)


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
        template_origin_ids=(template_id,),
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
        if candidate.origin_kind == "template" and not candidate.template_origin_ids:
            candidate = replace(candidate, template_origin_ids=(candidate.origin_id,))
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
                template_origin_ids=tuple(
                    dict.fromkeys(
                        (*current.template_origin_ids, *candidate.template_origin_ids)
                    )
                ),
            )
    return tuple(merged)


def _canonical_version_constraints(
    constraints: Sequence[TemplateVersionConstraint | Mapping[str, object]],
) -> tuple[TemplateVersionConstraint, ...]:
    """Normalize the public input without trusting mutable/malformed records."""
    normalized: list[TemplateVersionConstraint] = []
    for item in constraints:
        if isinstance(item, TemplateVersionConstraint):
            values = {
                "template_id": item.template_id,
                "provider": item.provider,
                "project_id": item.project_id,
                "artifact_id": item.artifact_id,
                "scope": item.scope,
            }
        elif isinstance(item, Mapping):
            if set(item) != {
                "template_id",
                "provider",
                "project_id",
                "artifact_id",
                "scope",
            }:
                raise TemplateMergeError("Malformed Template version constraint")
            values = dict(item)
        else:
            raise TemplateMergeError("Template version constraints must be records")
        if any(not isinstance(values[key], str) for key in values):
            raise TemplateMergeError("Template version constraint fields must be strings")
        template_id = values["template_id"].strip()
        provider = values["provider"].strip().lower()
        project_id = values["project_id"].strip()
        artifact_id = values["artifact_id"].strip()
        scope = values["scope"].strip().lower()
        if not template_id or not provider or not project_id or not artifact_id:
            raise TemplateMergeError("Template version constraint fields cannot be empty")
        if provider == "curseforge":
            if (
                _CANONICAL_DECIMAL_ID.fullmatch(project_id) is None
                or _CANONICAL_DECIMAL_ID.fullmatch(artifact_id) is None
            ):
                raise TemplateMergeError(
                    "CurseForge Template version constraints require canonical "
                    "positive decimal IDs"
                )
        elif provider == "modrinth":
            if (
                _IMMUTABLE_MODRINTH_ID.fullmatch(project_id) is None
                or _IMMUTABLE_MODRINTH_ID.fullmatch(artifact_id) is None
            ):
                raise TemplateMergeError(
                    "Modrinth Template version constraints require immutable IDs"
                )
        else:
            raise TemplateMergeError(
                "Template version constraints support only CurseForge or Modrinth"
            )
        if scope not in {"root", "dependency"}:
            raise TemplateMergeError(f"Invalid Template version constraint scope: {scope}")
        normalized.append(
            TemplateVersionConstraint(
                template_id, provider, project_id, artifact_id, scope  # type: ignore[arg-type]
            )
        )
    return tuple(normalized)


def _selection_option_key(candidates: Sequence[ModCandidate]) -> str:
    if len(candidates) == 1:
        return candidates[0].selection_key
    payload = {
        "version": 1,
        "selection_keys": sorted(candidate.selection_key for candidate in candidates),
        "selector_identity": candidates[0].selector_identity,
        "actual_identity": candidates[0].actual_identity,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "group:" + hashlib.sha256(serialized).hexdigest()


def import_selection_options(
    candidates: Sequence[ModCandidate],
) -> tuple[ImportSelectionOption, ...]:
    groups: dict[tuple[object, ...], list[ModCandidate]] = {}
    for candidate in candidates:
        if candidate.actual_identity is None:
            key: tuple[object, ...] = ("candidate", candidate.selection_key)
        else:
            key = (
                "source",
                candidate.selector_identity,
                candidate.actual_identity,
            )
        groups.setdefault(key, []).append(candidate)
    options = tuple(
        ImportSelectionOption(_selection_option_key(group), tuple(group))
        for group in groups.values()
    )
    option_keys = [option.option_key for option in options]
    if len(option_keys) != len(set(option_keys)):
        raise TemplateMergeError("Template import contains duplicate option keys")
    return options


def _name_conflicts(
    options: Sequence[ImportSelectionOption],
) -> tuple[CandidateNameConflict, ...]:
    aliases = {
        option.option_key: {
            normalize_name(candidate.name) for candidate in option.candidates
        }
        for option in options
    }

    parents = list(range(len(options)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    first_by_name: dict[str, int] = {}
    for index, option in enumerate(options):
        for alias in sorted(aliases[option.option_key]):
            previous = first_by_name.get(alias)
            if previous is None:
                first_by_name[alias] = index
            else:
                parents[root(index)] = root(previous)

    groups: dict[int, list[ImportSelectionOption]] = {}
    for index, option in enumerate(options):
        groups.setdefault(root(index), []).append(option)
    return tuple(
        CandidateNameConflict(
            normalize_name(group[0].candidates[0].name),
            group[0].candidates[0].name.strip(),
            tuple(group),
        )
        for group in groups.values()
        if len(group) > 1
    )


def _url_selector_conflicts(
    options: Sequence[ImportSelectionOption],
) -> tuple[UrlSelectorConflict, ...]:
    grouped: dict[tuple[str, str], list[ImportSelectionOption]] = {}
    for option in options:
        if option.selector_identity[0] == "url" and not any(
            candidate.origin_kind == "pack" for candidate in option.candidates
        ):
            identity = option.selector_identity[:2]
            grouped.setdefault(identity, []).append(option)
    return tuple(
        UrlSelectorConflict(identity, tuple(group))
        for identity, group in grouped.items()
        if len({option.selector_identity for option in group}) > 1
    )


def _logical_identity_conflicts(
    options: Sequence[ImportSelectionOption],
) -> tuple[LogicalIdentityConflict, ...]:
    grouped: dict[tuple[str, str], list[ImportSelectionOption]] = {}
    for option in options:
        grouped.setdefault(option.selector_identity[:2], []).append(option)
    conflicts: list[LogicalIdentityConflict] = []
    for identity, group in grouped.items():
        pack_option = next(
            (
                option
                for option in group
                if any(candidate.origin_kind == "pack" for candidate in option.candidates)
            ),
            None,
        )
        if pack_option is None:
            continue
        if len(group) > 1:
            conflicts.append(LogicalIdentityConflict(identity, tuple(group)))
    return tuple(conflicts)


def _actual_identity_conflicts(
    options: Sequence[ImportSelectionOption],
) -> tuple[ActualIdentityConflict, ...]:
    grouped: dict[tuple[str, str], list[ImportSelectionOption]] = {}
    for option in options:
        if option.actual_identity is not None:
            grouped.setdefault(option.actual_identity, []).append(option)
    return tuple(
        ActualIdentityConflict(identity, tuple(group))
        for identity, group in grouped.items()
        if len(group) > 1
    )


def _plan_digest_payload(
    pack_key: str,
    template_ids: tuple[str, ...],
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
    selection_options: Sequence[ImportSelectionOption],
    verifications: Sequence[ImportCandidateVerification],
    version_constraints: Sequence[TemplateVersionConstraint],
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
            "template_origin_ids": candidate.template_origin_ids,
            "candidate_key": candidate.candidate_key,
            "selection_key": candidate.selection_key,
        }

    payload = {
        "version": 5,
        "pack_key": pack_key,
        "template_ids": template_ids,
        "pack_candidates": [record(item) for item in pack_candidates],
        "template_candidates": [record(item) for item in template_candidates],
        "selection_options": [
            {
                "option_key": option.option_key,
                "selection_keys": option.selection_keys,
                "selector_identity": option.selector_identity,
                "actual_identity": option.actual_identity,
            }
            for option in selection_options
        ],
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
        "version_constraints": [
            {
                "template_id": item.template_id,
                "provider": item.provider,
                "project_id": item.project_id,
                "artifact_id": item.artifact_id,
                "scope": item.scope,
            }
            for item in version_constraints
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
    constraints: Sequence[
        TemplateVersionConstraint | Mapping[str, object]
    ] = (),
) -> TemplateImportPlan:
    ordered_ids = tuple(template_ids)
    if not ordered_ids:
        raise TemplateMergeError("At least one template must be selected")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise TemplateMergeError("Template selection contains duplicate IDs")
    normalized_constraints = _canonical_version_constraints(constraints)
    selected_id_set = set(ordered_ids)
    if any(item.template_id not in selected_id_set for item in normalized_constraints):
        raise TemplateMergeError("Template version constraint references an unselected template")
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
    constraints_by_template_identity: dict[
        tuple[str, str, str], TemplateVersionConstraint
    ] = {}
    for constraint in normalized_constraints:
        key = (
            constraint.template_id,
            constraint.provider,
            constraint.project_id,
        )
        previous = constraints_by_template_identity.get(key)
        if previous is not None and (
            previous.artifact_id != constraint.artifact_id
            or previous.scope != constraint.scope
        ):
            raise TemplateMergeError(
                "One Template requests conflicting version intent for "
                f"{constraint.provider}:{constraint.project_id}"
            )
        constraints_by_template_identity[key] = constraint
        matching_roots = [
            candidate
            for candidate in ordered_templates
            if candidate.origin_id == constraint.template_id
            and candidate.logical_identity
            == (constraint.provider, constraint.project_id)
        ]
        if constraint.scope == "root" and len(matching_roots) != 1:
            raise TemplateMergeError(
                "Template root version constraint must match exactly one root"
            )
        if constraint.scope == "dependency" and matching_roots:
            raise TemplateMergeError(
                "Template dependency version constraint cannot also be a root"
            )
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
    selection_options = import_selection_options(
        (*pack_candidates, *merged_templates)
    )
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
    name_conflicts = _name_conflicts(selection_options)
    logical_identity_conflicts = _logical_identity_conflicts(selection_options)
    logical_conflict_identities = {
        conflict.logical_identity for conflict in logical_identity_conflicts
    }
    url_selector_conflicts = tuple(
        conflict
        for conflict in _url_selector_conflicts(selection_options)
        if conflict.logical_identity not in logical_conflict_identities
    )
    logical_option_sets = [
        {option.option_key for option in conflict.options}
        for conflict in logical_identity_conflicts
    ]
    actual_identity_conflicts = tuple(
        conflict
        for conflict in _actual_identity_conflicts(selection_options)
        if not any(
            {option.option_key for option in conflict.options} <= logical_options
            for logical_options in logical_option_sets
        )
    )
    return TemplateImportPlan(
        pack_key,
        ordered_ids,
        merged_templates,
        tuple(pack_candidates),
        selection_options,
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
            merged_templates,
            selection_options,
            verifications,
            normalized_constraints,
        ),
        normalized_constraints,
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
        options: Sequence[ImportSelectionOption],
        resolution: ImportConflictResolution,
        cardinality: Literal["one-or-more", "exactly-one"],
    ) -> None:
        keys = tuple(resolution.option_keys)
        available_keys = tuple(option.option_key for option in options)
        available = set(available_keys)
        if len(available) != len(available_keys):
            raise TemplateMergeError(f"Conflict {kind} {key!r} has duplicate option keys")
        if len(set(keys)) != len(keys) or not set(keys) <= available:
            raise TemplateMergeError(f"Invalid resolution for {kind} conflict {key!r}")
        if cardinality == "exactly-one" and len(keys) != 1:
            raise TemplateMergeError(
                f"{kind.capitalize()} conflict {key!r} requires exactly one option"
            )
        if cardinality == "one-or-more" and not keys:
            raise TemplateMergeError(
                f"{kind.capitalize()} conflict {key!r} requires at least one option"
            )
        selected = set(keys)
        source = f'{kind} conflict "{key}"'
        for option_key in available_keys:
            required = option_key in selected
            previous = requirements.get(option_key)
            if previous is not None and previous != required:
                previous_source = sources[option_key][-1]
                action = "selected" if previous else "rejected"
                opposite = "selected" if required else "rejected"
                raise TemplateMergeError(
                    f"Option {option_key} is {action} by {previous_source} "
                    f"but {opposite} by {source}"
                )
            requirements[option_key] = required
            sources.setdefault(option_key, []).append(source)

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
                options=conflict.options,
                resolution=resolutions[conflict.key],
                cardinality=cardinality,
            )

    selected_option_keys = {
        option.option_key
        for option in plan.selection_options
        if requirements.get(option.option_key, True)
    }
    for kind, conflicts, resolutions, cardinality in conflict_groups:
        for conflict in conflicts:
            final_keys = tuple(
                option.option_key
                for option in conflict.options
                if option.option_key in selected_option_keys
            )
            if cardinality == "exactly-one" and len(final_keys) != 1:
                raise TemplateMergeError(
                    f"{kind.capitalize()} conflict {conflict.key!r} must retain exactly one option"
                )
            if cardinality == "one-or-more" and not final_keys:
                raise TemplateMergeError(
                    f"{kind.capitalize()} conflict {conflict.key!r} must retain an option"
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

    selected_candidate_keys = {
        candidate.selection_key
        for option in plan.selection_options
        if option.option_key in selected_option_keys
        for candidate in option.candidates
    }

    selected_templates = tuple(
        candidate
        for candidate in plan.template_candidates
        if candidate.selection_key in selected_candidate_keys
    )
    retained_pack = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.selection_key in selected_candidate_keys
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
        if candidate.actual_identity is not None
        and candidate.actual_identity not in retained_actual
    )
    removed_pack = tuple(
        candidate
        for candidate in plan.pack_candidates
        if candidate.selection_key not in selected_candidate_keys
    )
    all_conflicts = tuple(
        conflict
        for _kind, conflicts, _resolutions, _cardinality in conflict_groups
        for conflict in conflicts
    )
    for removed in removed_pack:
        replacing = any(
            removed in conflict.candidates
            and any(
                option.option_key in selected_option_keys
                and any(candidate in selected_new for candidate in option.candidates)
                for option in conflict.options
            )
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
    active_version_constraints = _active_version_constraints(plan, selected_templates)
    resolved = ResolvedTemplateImportPlan(
        plan.plan_digest,
        tuple(
            option.option_key
            for option in plan.selection_options
            if option.option_key in selected_option_keys
        ),
        selected_templates,
        retained_pack,
        selected_new,
        removed_pack,
        tuple(side_changes),
        tuple(warnings),
        active_version_constraints,
    )
    validate_resolved_template_import_plan(plan, resolved)
    return resolved
