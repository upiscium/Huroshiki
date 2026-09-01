"""Pure, UI-neutral conflict choices for Template migration resolution.

This module deliberately uses duck typing for plans and results.  In particular it
does not import :mod:`template_migration` at import time; the operation module is
also a consumer of this API.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Literal

from provider_identity import ProviderIdentityError, canonical_identity

if TYPE_CHECKING:
    from template_migration import TemplateMigrationPlan, TemplateRootIntent

ResolutionAction = Literal["remove", "replace"]
_IMMUTABLE_MODRINTH_ID = re.compile(r"^[A-Za-z0-9]{8}$")


class TemplateMigrationConflictResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateMigrationRootResolution:
    source_index: int
    action: ResolutionAction
    replacement_provider: str | None = None
    replacement_project_id: str | None = None


@dataclass(frozen=True)
class TemplateMigrationResolutionRequest:
    plan_identity: int
    source_snapshot_digest: str
    plan_digest: str
    target: object
    resolution_attempt: int
    resolution_digest: str
    unresolved_digest: str
    version_intent_digest: str
    collision_digest: str
    resolutions: tuple[TemplateMigrationRootResolution, ...]


@dataclass(frozen=True)
class TemplateMigrationRemovedRoot:
    source_root: object
    reason_code: str
    abandoned_root_exact_constraints: tuple


@dataclass(frozen=True)
class TemplateMigrationReplacedRoot:
    source_root: object
    replacement_root: object
    old_identity: str
    new_identity: str | None
    provider_changed: bool


@dataclass(frozen=True)
class TemplateMigrationConflictResolutionResult:
    resolution: object
    removed_roots: tuple[TemplateMigrationRemovedRoot, ...]
    replaced_roots: tuple[TemplateMigrationReplacedRoot, ...]
    attempt_number: int
    state: Literal["resolved", "resolution-required"]


@dataclass(frozen=True)
class ValidatedTemplateMigrationResolution:
    effective_roots: tuple
    effective_overrides: tuple
    removed_roots: tuple[TemplateMigrationRemovedRoot, ...]
    replaced_roots: tuple[TemplateMigrationReplacedRoot, ...]
    request_digest: str


def _wire(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _wire(v) for k, v in asdict(value).items()}
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(k): _wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _current(plan: object) -> object:
    state = getattr(plan, "_state", None)
    result = getattr(state, "resolution", None)
    if result is None:
        result = getattr(plan, "resolution", None)
    if result is None or getattr(result, "status", None) != "resolution-required":
        raise TemplateMigrationConflictResolutionError("Template migration is not awaiting resolution")
    return result


def template_migration_unresolved_digest(resolution: object) -> str:
    return _digest(tuple(getattr(resolution, "unresolved", ())))


def template_migration_version_intent_digest(resolution: object) -> str:
    return _digest((getattr(resolution, "version_intent_facts", ()), getattr(resolution, "version_intent_issues", ())))


def template_migration_collision_digest(resolution: object) -> str:
    return _digest((getattr(resolution, "collisions", ()), getattr(resolution, "identity_collisions", ()), getattr(resolution, "path_collisions", ()), getattr(resolution, "filename_collisions", ())))


def template_migration_resolution_digest(resolution: object) -> str:
    value = getattr(resolution, "digest", None)
    if isinstance(value, str) and value:
        return value
    return _digest(resolution)


def template_migration_request_digest(request: TemplateMigrationResolutionRequest) -> str:
    return _digest(request)


# Short names are useful to callers and make the digest contract discoverable.
unresolved_digest = template_migration_unresolved_digest
version_intent_digest = template_migration_version_intent_digest
collision_digest = template_migration_collision_digest
resolution_digest = template_migration_resolution_digest
request_digest = template_migration_request_digest


def create_template_migration_resolution_request(plan: "TemplateMigrationPlan", resolutions: tuple[TemplateMigrationRootResolution, ...]) -> TemplateMigrationResolutionRequest:
    result = _current(plan)
    state = getattr(plan, "_state", None)
    attempt = int(getattr(result, "resolution_attempt", getattr(state, "attempt", 0)))
    request = TemplateMigrationResolutionRequest(
        id(plan), plan.source_snapshot_digest, plan.plan_digest, plan.target, attempt,
        resolution_digest(result), unresolved_digest(result), version_intent_digest(result), collision_digest(result), tuple(resolutions),
    )
    validate_template_migration_resolution_request(plan, request)
    return request


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TemplateMigrationConflictResolutionError(f"{field} must be non-empty and trimmed")
    return value


def _identity(provider: str, project: str) -> str:
    try:
        return canonical_identity(provider, project)
    except (ProviderIdentityError, AttributeError) as error:
        raise TemplateMigrationConflictResolutionError(str(error)) from error


def _root_identity(root: object) -> str | None:
    provider, project = getattr(root, "provider", None), getattr(root, "project_id", None)
    if not isinstance(provider, str) or not isinstance(project, str) or getattr(root, "url", None) is not None:
        return None
    try:
        return _identity(provider, project)
    except TemplateMigrationConflictResolutionError:
        return None


def validate_template_migration_resolution_request(plan: "TemplateMigrationPlan", request: TemplateMigrationResolutionRequest) -> ValidatedTemplateMigrationResolution:
    result = _current(plan)
    if not isinstance(request, TemplateMigrationResolutionRequest):
        raise TemplateMigrationConflictResolutionError("Invalid Template migration resolution request")
    checks = ((request.plan_identity, id(plan), "plan identity"), (request.source_snapshot_digest, plan.source_snapshot_digest, "source snapshot digest"), (request.plan_digest, plan.plan_digest, "plan digest"), (request.target, plan.target, "target"), (request.resolution_attempt, getattr(result, "resolution_attempt", 0), "resolution attempt"), (request.resolution_digest, resolution_digest(result), "resolution digest"), (request.unresolved_digest, unresolved_digest(result), "unresolved digest"), (request.version_intent_digest, version_intent_digest(result), "version intent digest"), (request.collision_digest, collision_digest(result), "collision digest"))
    for actual, expected, label in checks:
        if actual != expected:
            raise TemplateMigrationConflictResolutionError(f"Stale {label}")
    if not isinstance(request.resolution_attempt, int) or isinstance(request.resolution_attempt, bool) or request.resolution_attempt < 0:
        raise TemplateMigrationConflictResolutionError("Malformed resolution attempt")
    consumed = getattr(getattr(plan, "_state", None), "consumed_resolution_requests", ())
    if request_digest(request) in consumed or request in consumed:
        raise TemplateMigrationConflictResolutionError("Resolution request has already been consumed")
    if not request.resolutions:
        raise TemplateMigrationConflictResolutionError("At least one resolution choice is required")

    unresolved_items = tuple(getattr(result, "unresolved", ()))
    if any(not isinstance(getattr(x, "source_index", None), int) or isinstance(getattr(x, "source_index", None), bool) for x in unresolved_items):
        raise TemplateMigrationConflictResolutionError("Malformed unresolved source index")
    unresolved = {x.source_index: x for x in unresolved_items}
    if len(unresolved) != len(unresolved_items):
        raise TemplateMigrationConflictResolutionError("Ambiguous unresolved source indices")
    state = getattr(plan, "_state", None)
    roots = tuple(getattr(state, "effective_roots", getattr(result, "ordered_roots", getattr(plan, "roots", ()))))
    if any(not isinstance(getattr(x, "source_index", None), int) or isinstance(getattr(x, "source_index", None), bool) for x in roots):
        raise TemplateMigrationConflictResolutionError("Malformed effective root source index")
    if len({getattr(x, "source_index", None) for x in roots}) != len(roots):
        raise TemplateMigrationConflictResolutionError("Ambiguous effective root source indices")
    choices: dict[int, TemplateMigrationRootResolution] = {}
    for choice in request.resolutions:
        if not isinstance(choice, TemplateMigrationRootResolution) or not isinstance(choice.source_index, int) or isinstance(choice.source_index, bool) or choice.source_index in choices:
            raise TemplateMigrationConflictResolutionError("Duplicate or malformed root resolution")
        if choice.source_index not in unresolved:
            raise TemplateMigrationConflictResolutionError("Unknown or already resolved source index")
        if choice.action == "remove":
            if choice.replacement_provider is not None or choice.replacement_project_id is not None:
                raise TemplateMigrationConflictResolutionError("Remove resolution cannot include replacement fields")
        elif choice.action == "replace":
            item = unresolved[choice.source_index]
            if not getattr(item, "replacement_supported", True) or getattr(item, "version_issue", None) is not None:
                raise TemplateMigrationConflictResolutionError("Replacement is blocked for this root")
            provider = _text(choice.replacement_provider, "Replacement provider")
            project = _text(choice.replacement_project_id, "Replacement project ID")
            if provider != provider.lower() or provider not in {"modrinth", "curseforge"}:
                raise TemplateMigrationConflictResolutionError("Replacement provider must be canonical")
            if provider == "curseforge" and (not project.isdecimal() or int(project) <= 0 or str(int(project)) != project):
                raise TemplateMigrationConflictResolutionError("CurseForge project ID must be canonical positive decimal")
            if provider == "modrinth" and _IMMUTABLE_MODRINTH_ID.fullmatch(project) is None:
                raise TemplateMigrationConflictResolutionError("Modrinth project ID must be an 8-character immutable ID")
            selector = getattr(item, "source_selector", "")
            if not isinstance(selector, str):
                raise TemplateMigrationConflictResolutionError("Malformed root selector")
            if selector.startswith(("http://", "https://")):
                raise TemplateMigrationConflictResolutionError("URL roots cannot be replaced")
            root = next((value for value in getattr(state, "effective_roots", getattr(result, "ordered_roots", getattr(plan, "roots", ()))) if getattr(value, "source_index", None) == choice.source_index), None)
            old = getattr(item, "canonical_identity", None) or _root_identity(root)
            if old and _identity(provider, project) == old:
                raise TemplateMigrationConflictResolutionError("Replacement must change identity")
        else:
            raise TemplateMigrationConflictResolutionError("Unsupported resolution action")
        choices[choice.source_index] = choice

    if set(choices) != set(unresolved):
        raise TemplateMigrationConflictResolutionError(
            "Every unresolved root requires exactly one resolution"
        )

    overrides = list(getattr(state, "effective_overrides", getattr(getattr(state, "snapshot", None), "overrides", ())))
    removed: list[TemplateMigrationRemovedRoot] = []
    replaced: list[TemplateMigrationReplacedRoot] = []
    from template_migration import TemplateRootIntent
    effective = list(roots)
    root_by_index = {getattr(root, "source_index"): position for position, root in enumerate(effective)}
    identities: dict[str, int] = {}
    for fact in getattr(result, "ordered_root_facts", ()):
        identity = getattr(fact, "source_canonical_identity", None)
        if identity:
            if identity in identities and identities[identity] != fact.source_index:
                raise TemplateMigrationConflictResolutionError("Root identity ownership is ambiguous")
            identities[identity] = fact.source_index
    for item in getattr(result, "unresolved", ()):
        if item.canonical_identity:
            if item.canonical_identity in identities and identities[item.canonical_identity] != item.source_index:
                raise TemplateMigrationConflictResolutionError("Root identity ownership is ambiguous")
            identities[item.canonical_identity] = item.source_index
    for index, choice in choices.items():
        if index not in root_by_index:
            raise TemplateMigrationConflictResolutionError("Unresolved source index has no effective root")
        position = root_by_index[index]
        source = effective[position]
        old = getattr(unresolved[index], "canonical_identity", None) or _root_identity(source)
        root_constraints = tuple(c for c in overrides if getattr(c, "scope", None) == "root")
        if old is None and root_constraints:
            raise TemplateMigrationConflictResolutionError("Root exact-constraint ownership is ambiguous")
        owned = tuple(c for c in root_constraints if _root_identity(c) == old)
        if choice.action == "remove":
            overrides = [c for c in overrides if c not in owned]
            removed.append(TemplateMigrationRemovedRoot(source, getattr(unresolved[index], "code", "unresolved"), owned))
            effective[position] = None
        else:
            if owned:
                raise TemplateMigrationConflictResolutionError("Replacement cannot transfer root exact constraints")
            provider, project = choice.replacement_provider, choice.replacement_project_id
            new = _identity(provider, project)
            if any(_root_identity(r) == new for r in effective if r is not source):
                raise TemplateMigrationConflictResolutionError("Replacement root identity collision")
            replacement = TemplateRootIntent(index, source.name, provider, project, source.side, None)
            effective[position] = replacement
            replaced.append(TemplateMigrationReplacedRoot(source, replacement, old or "", new, provider != source.provider))
    return ValidatedTemplateMigrationResolution(tuple(root for root in effective if root is not None), tuple(overrides), tuple(removed), tuple(replaced), request_digest(request))


__all__ = [name for name in ("ResolutionAction", "TemplateMigrationConflictResolutionError", "TemplateMigrationRootResolution", "TemplateMigrationResolutionRequest", "TemplateMigrationRemovedRoot", "TemplateMigrationReplacedRoot", "TemplateMigrationConflictResolutionResult", "ValidatedTemplateMigrationResolution", "template_migration_unresolved_digest", "template_migration_version_intent_digest", "template_migration_collision_digest", "template_migration_resolution_digest", "template_migration_request_digest", "unresolved_digest", "version_intent_digest", "collision_digest", "resolution_digest", "request_digest", "create_template_migration_resolution_request", "validate_template_migration_resolution_request")]
