"""Pure models and validation for Pack migration conflict resolution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pack_migration_roots import PackMigrationRoot
from provider_identity import (
    ProviderIdentityError,
    canonical_identity,
    canonical_provider,
)

if TYPE_CHECKING:
    from pack_migration import PackMigrationPlan
    from pack_migration_resolution import (
        PackMigrationResolutionPlan,
        PackMigrationUnresolvedRoot,
    )


ResolutionAction = Literal["remove", "replace"]


class PackMigrationConflictResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class PackMigrationRootResolution:
    source_identity: str
    action: ResolutionAction
    replacement_provider: str | None = None
    replacement_project_id: str | None = None


@dataclass(frozen=True)
class PackMigrationResolutionRequest:
    plan_identity: int
    source_snapshot_digest: str
    resolution_snapshot_digest: str
    resolutions: tuple[PackMigrationRootResolution, ...]


@dataclass(frozen=True)
class PackMigrationRemovedRoot:
    source_root: PackMigrationRoot
    reason_code: str
    removed_dependencies: tuple[str, ...]


@dataclass(frozen=True)
class PackMigrationReplacedRoot:
    source_root: PackMigrationRoot
    replacement_root: PackMigrationRoot
    old_identity: str
    new_identity: str
    provider_changed: bool


@dataclass(frozen=True)
class PackMigrationConflictResolutionResult:
    resolution_plan: "PackMigrationResolutionPlan"
    removed_roots: tuple[PackMigrationRemovedRoot, ...]
    replaced_roots: tuple[PackMigrationReplacedRoot, ...]
    remaining_unresolved: tuple["PackMigrationUnresolvedRoot", ...]
    attempt_number: int
    state: Literal["resolved", "resolution-required"]


@dataclass(frozen=True)
class ValidatedPackMigrationResolution:
    effective_roots: tuple[PackMigrationRoot, ...]
    removed_roots: tuple[PackMigrationRemovedRoot, ...]
    replaced_roots: tuple[PackMigrationReplacedRoot, ...]


def _unresolved_identity(unresolved: object) -> str:
    source = getattr(unresolved, "source_root", None)
    identity = getattr(source, "canonical_identity", None)
    if isinstance(identity, str) and identity:
        return identity
    relative = getattr(source, "source_metadata_path", None)
    return f"candidate:{relative.as_posix() if hasattr(relative, 'as_posix') else ''}"


def resolution_snapshot_digest(resolution: object, attempt_number: int) -> str:
    """Bind choices to the exact unresolved set and target resolution attempt."""
    target = getattr(resolution, "target", None)
    target_tuple = (
        getattr(target, "minecraft_version", None),
        getattr(target, "loader", None),
        getattr(target, "loader_version", None),
    )
    records = [
        {
            "source_identity": _unresolved_identity(item),
            "reason_code": getattr(item, "reason_code", None),
            "replacement_supported": getattr(item, "replacement_supported", None),
            "current_target": target_tuple,
            "attempt": attempt_number,
        }
        for item in getattr(resolution, "unresolved_roots", ())
    ]
    payload = json.dumps(
        sorted(records, key=lambda item: str(item["source_identity"])),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_pack_migration_resolution_request(
    plan: "PackMigrationPlan",
    resolutions: tuple[PackMigrationRootResolution, ...],
) -> PackMigrationResolutionRequest:
    """Bind caller choices to the current unresolved plan without exposing digests."""
    from pack_migration_resolution import PackMigrationResolutionPlan

    with plan._lock:
        current = plan.resolution
        if plan.state != "resolution-required" or not isinstance(
            current, PackMigrationResolutionPlan
        ):
            raise PackMigrationConflictResolutionError(
                "Pack migration is not awaiting conflict resolution"
            )
        request = PackMigrationResolutionRequest(
            id(plan),
            plan.source_snapshot.snapshot_digest,
            resolution_snapshot_digest(
                current, int(getattr(plan, "_resolution_attempt", 0))
            ),
            tuple(resolutions),
        )
        validate_resolution_request(plan, request)
        return request


def _strict_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PackMigrationConflictResolutionError(
            f"{field} must be a non-empty canonical string"
        )
    return value


def _canonical_source_identity(root: PackMigrationRoot) -> str:
    try:
        identity = canonical_identity(root.provider, root.project_id)
    except ProviderIdentityError as error:
        raise PackMigrationConflictResolutionError(
            "Unresolved root has a malformed canonical identity"
        ) from error
    if identity != root.canonical_identity:
        raise PackMigrationConflictResolutionError(
            "Unresolved root has a malformed canonical identity"
        )
    return identity


def validate_resolution_request(
    plan: "PackMigrationPlan",
    request: PackMigrationResolutionRequest,
) -> ValidatedPackMigrationResolution:
    """Validate and derive effective roots without filesystem or network access."""
    from pack_migration_resolution import PackMigrationResolutionPlan

    if not isinstance(request, PackMigrationResolutionRequest):
        raise PackMigrationConflictResolutionError("Invalid resolution request")
    resolution = plan.resolution
    if plan.state != "resolution-required" or not isinstance(
        resolution, PackMigrationResolutionPlan
    ):
        raise PackMigrationConflictResolutionError(
            "Pack migration is not awaiting conflict resolution"
        )
    if set(plan._lock_set.owned_keys) != {
        plan.source_key,
        f"pack:{plan.target.target_id}",
    }:
        raise PackMigrationConflictResolutionError(
            "Pack migration locks are not fully owned"
        )
    if request.plan_identity != id(plan):
        raise PackMigrationConflictResolutionError(
            "Resolution request belongs to another plan"
        )
    if request.source_snapshot_digest != plan.source_snapshot.snapshot_digest:
        raise PackMigrationConflictResolutionError("Source snapshot digest is stale")
    attempt = int(getattr(plan, "_resolution_attempt", 0))
    if request.resolution_snapshot_digest != resolution_snapshot_digest(
        resolution, attempt
    ):
        raise PackMigrationConflictResolutionError("Unresolved root snapshot is stale")
    if resolution.provenance_required or resolution.root_candidates or any(
        item.reason_code == "root-provenance-required"
        for item in resolution.unresolved_roots
    ):
        raise PackMigrationConflictResolutionError(
            "Root provenance must be selected through the provenance API"
        )
    if any(
        issue.owner_identity is None or issue.identity != issue.owner_identity
        for issue in resolution.version_intent_issues
    ):
        raise PackMigrationConflictResolutionError(
            "Dependency version intent must be changed at its source Authority "
            "before migration conflict resolution"
        )

    unresolved: dict[str, object] = {}
    for item in resolution.unresolved_roots:
        root = item.source_root
        if not isinstance(root, PackMigrationRoot):
            raise PackMigrationConflictResolutionError(
                "Conflict resolution requires explicit root provenance"
            )
        identity = _canonical_source_identity(root)
        if identity in unresolved:
            raise PackMigrationConflictResolutionError(
                f"Duplicate unresolved root identity: {identity}"
            )
        unresolved[identity] = item

    choices: dict[str, PackMigrationRootResolution] = {}
    for choice in request.resolutions:
        if not isinstance(choice, PackMigrationRootResolution):
            raise PackMigrationConflictResolutionError("Invalid root resolution")
        identity = _strict_text(choice.source_identity, "Source identity")
        if identity in choices:
            raise PackMigrationConflictResolutionError(
                f"Duplicate root resolution: {identity}"
            )
        if identity not in unresolved:
            raise PackMigrationConflictResolutionError(
                f"Unknown unresolved root: {identity}"
            )
        if choice.action == "remove":
            if (
                choice.replacement_provider is not None
                or choice.replacement_project_id is not None
            ):
                raise PackMigrationConflictResolutionError(
                    "Remove resolution cannot include replacement fields"
                )
        elif choice.action == "replace":
            if not unresolved[identity].replacement_supported:
                raise PackMigrationConflictResolutionError(
                    f"Replacement is not supported for {identity}"
                )
            provider_value = _strict_text(
                choice.replacement_provider, "Replacement provider"
            )
            project_id = _strict_text(
                choice.replacement_project_id, "Replacement project ID"
            )
            try:
                provider = canonical_provider(provider_value)
                replacement_identity = canonical_identity(provider, project_id)
            except ProviderIdentityError as error:
                raise PackMigrationConflictResolutionError(str(error)) from error
            if provider_value != provider or provider not in {
                "modrinth",
                "curseforge",
            }:
                raise PackMigrationConflictResolutionError(
                    "Replacement provider must be modrinth or curseforge"
                )
            if provider == "curseforge" and (
                not project_id.isdecimal()
                or int(project_id) <= 0
                or str(int(project_id)) != project_id
            ):
                raise PackMigrationConflictResolutionError(
                    "CurseForge replacement project ID must be a positive canonical integer"
                )
            if replacement_identity == identity:
                raise PackMigrationConflictResolutionError(
                    "Replacement must use a different canonical identity"
                )
        else:
            raise PackMigrationConflictResolutionError(
                f"Unsupported resolution action: {choice.action}"
            )
        choices[identity] = choice

    if set(choices) != set(unresolved):
        raise PackMigrationConflictResolutionError(
            "Every unresolved root requires exactly one resolution"
        )

    roots_by_identity = {
        root.canonical_identity: root for root in resolution.roots
    }
    if len(roots_by_identity) != len(resolution.roots):
        raise PackMigrationConflictResolutionError(
            "Current effective root identities are ambiguous"
        )
    removed: list[PackMigrationRemovedRoot] = []
    replaced: list[PackMigrationReplacedRoot] = []
    for identity in sorted(unresolved):
        choice = choices[identity]
        source_root = unresolved[identity].source_root
        roots_by_identity.pop(identity, None)
        if choice.action == "remove":
            removed.append(
                PackMigrationRemovedRoot(
                    source_root,
                    unresolved[identity].reason_code,
                    (),
                )
            )
            continue
        provider = canonical_provider(choice.replacement_provider)
        project_id = choice.replacement_project_id
        replacement_identity = canonical_identity(provider, project_id)
        if replacement_identity in roots_by_identity:
            raise PackMigrationConflictResolutionError(
                f"Replacement root identity collision: {replacement_identity}"
            )
        replacement_root = PackMigrationRoot(
            replacement_identity,
            provider,
            project_id,
            None,
            None,
            source_root.source_side,
            source_root.source_metadata_path,
            source_root.source_filename,
            True,
            None,
        )
        roots_by_identity[replacement_identity] = replacement_root
        replaced.append(
            PackMigrationReplacedRoot(
                source_root,
                replacement_root,
                identity,
                replacement_identity,
                source_root.provider != provider,
            )
        )

    return ValidatedPackMigrationResolution(
        tuple(roots_by_identity[key] for key in sorted(roots_by_identity)),
        tuple(removed),
        tuple(replaced),
    )


__all__ = [
    "PackMigrationConflictResolutionError",
    "PackMigrationConflictResolutionResult",
    "PackMigrationRemovedRoot",
    "PackMigrationReplacedRoot",
    "PackMigrationResolutionRequest",
    "PackMigrationRootResolution",
    "ResolutionAction",
    "ValidatedPackMigrationResolution",
    "create_pack_migration_resolution_request",
    "resolution_snapshot_digest",
    "validate_resolution_request",
]
