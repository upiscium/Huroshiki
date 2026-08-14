#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Iterable, Literal, Mapping, Sequence
from urllib.parse import unquote, urlparse
from uuid import uuid4


import tomlkit

import packctl
from dependency_equivalence import (
    DependencyCandidate,
    EQUIVALENCE_POLICY_VERSION,
    LoaderDependencyRequirement,
    version_satisfies_requirement,
    EquivalenceContext,
    EquivalenceError,
    MaterializedArtifact,
    SemanticJarIdentity,
    Provenance,
    verify_equivalence,
)
from pack_migration_roots import (
    PackRootRecord,
    PackMigrationRoot,
    PackMigrationRootError,
    ROOT_MANIFEST_PATH,
    extract_pack_migration_roots,
    ensure_pack_root_manifest_ignored,
    identify_pack_metadata_by_slug,
    read_pack_control_file,
    read_pack_root_manifest,
    record_pack_root,
    remove_pack_root,
    write_pack_control_file,
    write_pack_root_manifest,
)
from pack_tree_policy import PackTreePolicyError, PackTreeScan, scan_pack_migration_source
from provider_identity import parse_provider_metadata
from provider_artifacts import materialize_provider_artifact
from mod_version_overrides import (
    ModVersionOverride,
    ModVersionOverrideError,
    ModVersionOverrideStatus,
    VERSION_OVERRIDE_MANIFEST_MAX_BYTES,
    VERSION_OVERRIDE_MANIFEST_PATH,
    ensure_mod_version_overrides_ignored,
    get_mod_version_override,
    parse_mod_version_overrides,
    read_mod_version_overrides,
    remove_mod_version_override,
    require_mod_version_overrides_ignored,
    set_mod_version_override,
)

if TYPE_CHECKING:
    from pack_migration_conflicts import (
        PackMigrationConflictResolutionResult,
        PackMigrationResolutionRequest,
    )
    from pack_migration_resolution import (
        PackMigrationProgress,
        PackMigrationResolutionPlan,
    )
    from pack_migration_roots import PackMigrationRootSelection
from content_operations import (
    CONTENT_EDITOR_MAX_BYTES,
    ContentBrowseResult,
    ContentChange,
    ContentChangePlan,
    ContentCleanupError,
    ContentConflict,
    ContentCreateDirectory,
    ContentCreateFile,
    ContentDeleteDirectory,
    ContentDeleteFile,
    ContentDiscardOperation,
    ContentEntry,
    ContentFile,
    ContentImportRequest,
    ContentImportSourceEntry,
    ContentImportSourceSnapshot,
    ContentImportSummary,
    ContentMove,
    ContentPathInfo,
    ContentOperation,
    ContentOperationCancelled,
    ContentOperationDeadlineExceeded,
    ContentOperationError,
    ContentPlanStale,
    ContentReplaceFile,
    ContentSnapshot,
    ContentSnapshotEntry,
    ContentTextDocument,
    PathIdentity,
    apply_content_changes as _apply_content_changes,
    analyze_content_conflicts,
    content_snapshot_at,
    discard_content_plan as _discard_content_plan,
    list_content_entries_at,
    load_content_browser_at,
    load_content_text_document_at,
    encode_content_editor_text,
    inspect_content_import_source_at,
    plan_content_import_at,
    plan_content_changes_at,
    read_content_file_at,
    resolve_content_path_info_at,
)
from process_runner import (
    BoundedProcessResult,
    PACKWIZ_OPERATION_TIMEOUT_SECONDS,
    PACKWIZ_PROCESS_TIMEOUT_SECONDS,
    PROCESS_KILL_GRACE_SECONDS,
    PROCESS_POLL_SECONDS,
    PROCESS_REAP_GRACE_SECONDS,
    PROCESS_TERMINATE_GRACE_SECONDS,
    ProcessGroupMember,
    ProcessTerminationResult,
    live_process_group_members,
    process_failure_message,
    run_bounded_process,
    stop_process_group,
)
from overlay_policy import (
    OVERLAY_TARGETS,
    OverlayPolicyError,
    create_overlay_file,
    delete_overlay_file,
    normalize_overlay_relative_path,
    read_overlay_text,
    safe_overlay_child,
    scan_content_overlays,
    write_overlay_text,
)
from template_merge import (
    ConflictResolution,
    ConflictSelection,
    MergedTemplateMod,
    ResolvedTemplateComposition,
    TemplateComposition,
    TemplateConflict,
    TemplateMergeError,
    TemplateModEntry,
    compose_templates,
    resolve_composition,
    union_side,
)
from template_import import (
    CandidateNameConflict,
    ImportCandidateVerification,
    ModCandidate,
    ResolvedTemplateImportPlan,
    SideDecision,
    TemplateCompatibility,
    TemplateImportPlan,
    build_template_import_plan,
    merge_template_import_candidates,
    resolve_template_import_plan,
    template_candidate,
)
from packwiz_parser import ParserEvent
from packwiz_pty import PackwizPtySession, PtyResult
from url_artifacts import (
    DEFAULT_URL_MAX_JAR_SIZE_BYTES,
    DEFAULT_URL_TOTAL_TIMEOUT_SECONDS,
    HuroshikiError,
    URL_CHUNK_SIZE,
    URL_USER_AGENT,
    UrlArtifact,
    append_url_log,
    download_url_artifact,
    ensure_url_error_log,
    parse_jar_identity,
    sanitize_mod_id,
    url_log_paths,
    validate_public_url,
    write_url_metadata,
)
from portable_paths import (
    PortablePathError,
    portable_basename,
    portable_basename_key,
    portable_relative_path,
    portable_relative_path_key,
)
from pack_migration import (
    PackMigrationPlan,
    PackMigrationPublicationPlan,
    PackMigrationSourceSnapshot,
    PackMigrationTarget,
    apply_pack_copy_migration_at as _apply_pack_copy_migration_at,
    apply_pack_migration_publication as _apply_pack_migration_publication,
    discard_pack_migration_plan as _discard_pack_migration_plan,
    plan_pack_copy_migration_at,
    snapshot_pack_migration_source_at,
    prepare_pack_migration_publication as _prepare_pack_migration_publication,
    retry_pack_migration_cleanup as _retry_pack_migration_cleanup,
)
from pack_publish import (
    PackPublishCancelled,
    PackPublishDeadlineExceeded,
    PackPublishError,
    PackPublishManifest,
    PublishFileEntry,
    PublishWarning,
    plan_pack_publish_manifest,
)
from publish_target import (
    LEGACY_SERVER_ID,
    PUBLISH_RESERVED_NAMES,
    PUBLISH_RESERVED_PREFIX,
    PublishRemoteTarget,
    PublishRestartTarget,
    PublishSshEndpoint,
    PublishTargetError,
    compute_publish_remote_target_digest,
    is_publish_reserved_child,
    parse_publish_ssh_endpoint,
    publish_remote_target_from_legacy_settings,
    validate_publish_remote_path,
    validate_publish_ssh_port,
)
from publish_transfer import (
    PublishStagedFile,
    PublishStagedGeneration,
    PublishTransferCleanupError,
    PublishTransferError,
    PublishTransferExecutionError,
    PublishTransferPlan,
    PublishTransferPlanningError,
    PublishTransferProgress,
    PublishTransferUncertainError,
    compute_publish_generation_id,
    discard_publish_transfer_plan,
    execute_publish_transfer,
    prepare_publish_transfer,
    retry_discard_publish_transfer_plan,
)
from publish_activation import (
    PublishActivatedGeneration,
    PublishActivationCleanupError,
    PublishActivationError,
    PublishActivationUncertainError,
    PublishSemanticVerification,
    PublishSemanticVerificationError,
    PublishSemanticVerificationUncertainError,
    activate_publish_generation,
    retry_publish_activation_cleanup,
    verify_publish_generation,
)

ROOT = packctl.ROOT
PACKS = packctl.PACKS
TEMPLATES = packctl.TEMPLATES
SCRIPTS = Path(__file__).resolve().parent
STATE_ROOT = ROOT / ".huroshiki"
TRANSACTION_ROOT = STATE_ROOT / "transactions"
LOG_ROOT = STATE_ROOT / "logs"
TRASH_ROOT = STATE_ROOT / "trash"


PROJECT_KINDS = ("pack", "template")
StateItem = packctl.StateItem
DeploymentSettings = packctl.DeploymentSettings
DeploymentSettingsSources = packctl.DeploymentSettingsSources
DeploymentSettingsBaseline = packctl.DeploymentSettingsBaseline
RsyncTargetParts = packctl.RsyncTargetParts
PublicPackUrlInfo = packctl.PublicPackUrlInfo
PublicPackUrlBaseline = packctl.PublicPackUrlBaseline


def project_key(kind: str, project_id: str) -> str:
    if kind not in PROJECT_KINDS:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    packctl.validate_project_id(project_id)
    return f"{kind}:{project_id}"


def split_project_key(key: str) -> tuple[str, str]:
    try:
        kind, project_id = key.split(":", 1)
    except ValueError as error:
        raise HuroshikiError(f"Invalid project key: {key}") from error
    if kind not in PROJECT_KINDS:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    packctl.validate_project_id(project_id)
    return kind, project_id


@dataclass(frozen=True)
class ProjectDeployPreview:
    project_key: str
    action: str
    target: str
    dist_digest: str
    changes: tuple[packctl.RsyncChange, ...]
    raw_lines: tuple[str, ...]
    restart_target: tuple[str, str, str] | None = None
    snapshot: Path | None = None

    @property
    def confirmation_lines(self) -> tuple[str, ...]:
        counts = {
            category: sum(change.category == category for change in self.changes)
            for category in ("added", "updated", "deleted")
        }
        lines = [
            f"Pack: {split_project_key(self.project_key)[1]}",
            f"Action: {self.action}",
            f"Rsync target: {self.target}",
            "Changes: "
            f"{counts['added']} added, {counts['updated']} updated, "
            f"{counts['deleted']} deleted",
        ]
        if self.restart_target is not None:
            host, stack, service = self.restart_target
            lines.extend(
                (
                    f"SSH target: {host}",
                    f"Stack directory: {stack}",
                    f"Compose service: {service}",
                )
            )
        if self.raw_lines:
            lines.extend(("", "Rsync detail:", *self.raw_lines))
        else:
            lines.append("No rsync changes.")
        return tuple(lines)


def project_root(key: str) -> Path:
    kind, project_id = split_project_key(key)
    return packctl.get_project_root(kind, project_id)


def project_config(key: str) -> dict[str, object]:
    kind, project_id = split_project_key(key)
    return packctl.load_project_config(kind, project_id)


def deployment_settings(key: str) -> DeploymentSettings:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Deployment settings are available only for packs")
    try:
        return packctl.deployment_settings(project_id)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def resolve_publish_remote_target(
    pack_id: str,
    *,
    server_id: str | None = None,
    remote_path: str | None = None,
) -> PublishRemoteTarget:
    """Resolve the current legacy Pack settings into a transport-neutral target."""

    if server_id is not None:
        raise HuroshikiError(
            "Named Publish server profiles are not implemented yet"
        )
    try:
        settings = packctl.deployment_settings(pack_id)
        return publish_remote_target_from_legacy_settings(
            rsync_target=settings.rsync_target,
            ssh_host=settings.ssh_host,
            stack_dir=settings.stack_dir,
            service=settings.service,
            server_id=LEGACY_SERVER_ID,
            remote_path=remote_path,
        )
    except (packctl.ConfigError, PublishTargetError) as error:
        raise HuroshikiError(str(error)) from error


def deployment_settings_baseline(key: str) -> DeploymentSettingsBaseline:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Deployment settings are available only for packs")
    try:
        return packctl.deployment_settings_baseline(project_id)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def deployment_settings_sources(key: str) -> DeploymentSettingsSources:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Deployment settings are available only for packs")
    try:
        return packctl.deployment_settings_sources(project_id)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def proposed_deployment_settings(
    *,
    ssh_host: str,
    stack_dir: str,
    service: str,
    rsync_host: str,
    rsync_path: str,
) -> DeploymentSettings:
    try:
        return DeploymentSettings(
            packctl.join_rsync_target(rsync_host, rsync_path),
            packctl.validate_ssh_target(ssh_host),
            packctl.validate_remote_stack_dir(stack_dir),
            packctl.validate_compose_service(service),
        )
    except (packctl.ConfigError, ValueError) as error:
        raise HuroshikiError(str(error)) from error


def split_rsync_target(value: str) -> RsyncTargetParts:
    try:
        return packctl.split_rsync_target(value)
    except ValueError as error:
        raise HuroshikiError(str(error)) from error


def update_deployment_settings(
    key: str,
    settings: DeploymentSettings,
    *,
    expected_baseline: DeploymentSettingsBaseline | None = None,
) -> DeploymentSettings:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Deployment settings are available only for packs")
    try:
        current = expected_baseline.settings if expected_baseline is not None else None
        return packctl.update_deployment_settings(
            project_id,
            rsync_target=(
                settings.rsync_target
                if current is None or settings.rsync_target != current.rsync_target
                else packctl.UNSET
            ),
            ssh_host=(
                settings.ssh_host
                if current is None or settings.ssh_host != current.ssh_host
                else packctl.UNSET
            ),
            stack_dir=(
                settings.stack_dir
                if current is None or settings.stack_dir != current.stack_dir
                else packctl.UNSET
            ),
            service=(
                settings.service
                if current is None or settings.service != current.service
                else packctl.UNSET
            ),
            expected_baseline=(
                expected_baseline.snapshot if expected_baseline is not None else None
            ),
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def public_pack_url_baseline(key: str) -> PublicPackUrlBaseline:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Public Pack URLs are available only for packs")
    try:
        return packctl.public_pack_url_baseline(project_id)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def validate_public_pack_url(value: str) -> str:
    try:
        return packctl.validate_public_pack_url(value)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def set_public_pack_url(
    key: str,
    url: str,
    *,
    expected_baseline: PublicPackUrlBaseline | None = None,
) -> PublicPackUrlInfo:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Public Pack URLs are available only for packs")
    try:
        return packctl.set_public_pack_url(
            project_id,
            url,
            expected_baseline=expected_baseline,
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def clear_local_public_pack_url(
    key: str,
    *,
    expected_baseline: PublicPackUrlBaseline | None = None,
) -> PublicPackUrlInfo:
    kind, project_id = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Public Pack URLs are available only for packs")
    try:
        return packctl.clear_local_public_pack_url(
            project_id,
            expected_baseline=expected_baseline,
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def project_source(key: str) -> Path:
    kind, _ = split_project_key(key)
    if kind != "pack":
        raise HuroshikiError("Template manifests do not have a persistent Packwiz source")
    return project_root(key) / "source"


@dataclass(frozen=True)
class ProjectInfo:
    kind: str
    project_id: str
    display_name: str
    minecraft: str
    loader: str
    loader_version: str
    enabled: bool
    error: str | None = None
    mod_count: int | None = None

    @property
    def key(self) -> str:
        return project_key(self.kind, self.project_id)

    @property
    def type_label(self) -> str:
        return "MODPACK" if self.kind == "pack" else "TEMPLATE"

    @property
    def manifest_path(self) -> Path:
        name = "pack.yaml" if self.kind == "pack" else "template.yaml"
        parent = PACKS if self.kind == "pack" else TEMPLATES
        return parent / self.project_id / name


@dataclass
class ModInfo:
    relative_path: Path
    slug: str
    name: str
    provider: str
    project_id: str
    filename: str
    client: bool
    server: bool
    source_url: str = ""
    selected: bool = False
    side_error: str | None = None

    @property
    def side(self) -> str:
        if self.side_error is not None:
            return "invalid"
        return side_from_flags(self.client, self.server)

    @property
    def side_label(self) -> str:
        if self.side_error is not None:
            return "[?] [?]"
        return f"[{'x' if self.client else ' '}] [{'x' if self.server else ' '}]"


@dataclass(frozen=True)
class UpdateChange:
    relative_path: Path
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True)
class LoaderMigrationPreview:
    project_key: str
    minecraft: str
    loader: str
    old_version: str
    new_version: str
    changes: tuple[UpdateChange, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class UpdateCandidate:
    key: str
    root: Path
    slug: str
    name: str
    provider: str
    current_version: str
    new_version: str
    status: str
    changes: tuple[UpdateChange, ...] = ()
    current_file_id: str = "-"
    new_file_id: str = "-"
    added_dependencies: int = 0
    error: str | None = None
    error_returncode: int | None = None
    error_kind: str | None = None

    @property
    def relative_path(self) -> Path:
        return self.root

    @property
    def available(self) -> bool:
        return self.status == "update"

    @property
    def file_count(self) -> int:
        return len(self.changes)


_EXACT_DECIMAL_ID_RE = re.compile(r"^[0-9]+$")
_CANONICAL_MODRINTH_ID_TOKEN = object()
_MODRINTH_IMMUTABLE_ID_RE = re.compile(r"^[A-Za-z0-9]{8}$")


class CanonicalModrinthId(str):
    """Opaque ID branded from an authoritative Modrinth result."""

    __slots__ = ()

    def __new__(cls, value: str, _token: object) -> "CanonicalModrinthId":
        if _token is not _CANONICAL_MODRINTH_ID_TOKEN:
            raise TypeError("Canonical Modrinth IDs must be created with canonical_modrinth_id()")
        return super().__new__(cls, value)


def _validate_canonical_modrinth_id(value: str, context: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _MODRINTH_IMMUTABLE_ID_RE.fullmatch(value) is None
    ):
        raise HuroshikiError(f"{context} must be an 8-character immutable Modrinth ID")
    return value


def canonical_modrinth_id(value: str, context: str | None = None) -> CanonicalModrinthId:
    """Brand an already-resolved Modrinth project or version ID.

    This pure factory validates provider-output syntax only; it never resolves
    selectors or performs network, process, or filesystem work.  Callers must
    pass the provider's authoritative ``id`` field, not a slug, URL, or label.
    """

    return CanonicalModrinthId(
        _validate_canonical_modrinth_id(
            value,
            context or "Modrinth ID",
        ),
        _CANONICAL_MODRINTH_ID_TOKEN,
    )


def _require_canonical_modrinth_id(
    value: str | CanonicalModrinthId,
    context: str,
) -> CanonicalModrinthId:
    if type(value) is not CanonicalModrinthId:
        raise HuroshikiError(f"{context} must be a canonical Modrinth ID")
    return value


def _exact_positive_decimal(value: str, context: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _EXACT_DECIMAL_ID_RE.fullmatch(value) is None
        or value == "0"
        or value.startswith("0")
    ):
        raise HuroshikiError(f"{context} must be a canonical positive decimal integer")
    return value


@dataclass(frozen=True)
class ExactModArtifactSelection:
    """Exact provider artifact selected using authoritative immutable IDs.

    Modrinth fields must be ``CanonicalModrinthId`` values produced by the
    provider lookup/search boundary.  Raw selectors, URLs, slugs, and display
    labels are intentionally not accepted here.
    """

    provider: Literal["curseforge", "modrinth"]
    project_id: str | CanonicalModrinthId
    artifact_id: str | CanonicalModrinthId

    def __post_init__(self) -> None:
        if self.provider not in {"curseforge", "modrinth"}:
            raise HuroshikiError(
                "Exact MOD artifact selection supports only CurseForge or Modrinth"
            )
        if self.provider == "curseforge":
            project_id = _exact_positive_decimal(
                self.project_id, "CurseForge project ID"
            )
            artifact_id = _exact_positive_decimal(
                self.artifact_id, "CurseForge file ID"
            )
        else:
            project_id = _require_canonical_modrinth_id(
                self.project_id, "Modrinth project ID"
            )
            artifact_id = _require_canonical_modrinth_id(
                self.artifact_id, "Modrinth version ID"
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "artifact_id", artifact_id)

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.project_id

    @property
    def identity_label(self) -> str:
        return f"{self.provider}:{self.project_id}"


@dataclass(frozen=True)
class ModVersionSelectionPreview:
    identity: str
    relative_path: Path
    name: str
    provider: str
    old_version: str
    old_artifact_id: str
    new_version: str
    new_artifact_id: str
    changes: tuple[UpdateChange, ...]
    added_dependencies: int
    removed_dependencies: int
    added_dependency_identities: tuple[str, ...] = ()
    removed_dependency_identities: tuple[str, ...] = ()
    override_identity: str | None = None
    override_artifact_id: str | None = None
    override_locked: bool | None = None
    diagnostic_messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModVersionIntentStatus:
    identity: str
    selection: Literal["automatic", "user"]
    installed_artifact_id: str | None
    selected_artifact_id: str | None
    locked: bool | None
    reason: str | None
    override_status: Literal["active", "drifted", "stale"] | None


@dataclass(frozen=True)
class ModVersionCandidate:
    provider: str
    project_id: str | CanonicalModrinthId
    artifact_id: str | CanonicalModrinthId
    version: str
    filename: str
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    release_type: Literal["release", "beta", "alpha"]
    published_at: str

    def as_exact_selection(self) -> ExactModArtifactSelection:
        """Convert provider output into the existing exact-selection authority."""
        return ExactModArtifactSelection(
            self.provider,
            self.project_id,
            self.artifact_id,
        )


@dataclass(frozen=True)
class ModVersionCandidateView:
    candidate: ModVersionCandidate
    current: bool
    selected: bool
    pinned: bool
    compatible: bool
    compatibility_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModVersionCandidateCatalog:
    identity: str
    minecraft: str
    loader: str
    candidates: tuple[ModVersionCandidateView, ...]
    intent_status: ModVersionIntentStatus
    selected_candidate_missing: bool


@dataclass(frozen=True)
class ModVersionIntentPreview:
    identity: str
    installed_artifact_id: str | None
    selected_artifact_id: str | None
    old_selection: Literal["automatic", "user"]
    new_selection: Literal["automatic", "user"]
    old_locked: bool | None
    new_locked: bool | None
    reason: str | None
    override_status: Literal["active", "drifted", "stale"] | None
    changes: tuple[UpdateChange, ...]


@dataclass(frozen=True)
class ExactArtifactVerification:
    identity: tuple[str, str]
    artifact_id: str
    sha256: str
    semantic_identity: SemanticJarIdentity
    dependency_requirements: tuple[LoaderDependencyRequirement, ...] | None


@dataclass(frozen=True)
class ExactDependencyEdge:
    parent_identity: tuple[str, str]
    child_identity: tuple[str, str]
    required_mod_id: str
    version_range: str


@dataclass(frozen=True)
class ExactDependencyGraph:
    semantic_bindings: tuple[tuple[str, tuple[str, str]], ...]
    edges: tuple[ExactDependencyEdge, ...]
    root_reachability: tuple[
        tuple[tuple[str, str], frozenset[tuple[str, str]]], ...
    ]

    def reachable_roots(self, identity: tuple[str, str]) -> frozenset[tuple[str, str]]:
        return dict(self.root_reachability).get(identity, frozenset())


@dataclass(frozen=True)
class ModVersionSelectionProgress:
    phase: Literal[
        "validating",
        "checkpointing",
        "resolving",
        "verifying-root",
        "verifying-dependencies",
        "materializing",
        "merging",
        "complete",
    ]
    message: str = ""


class ExactModVersionCancelled(HuroshikiError):
    pass


class ExactModVersionDeadlineExceeded(HuroshikiError):
    pass


@dataclass(frozen=True)
class UpdateRunReport:
    candidates: tuple[UpdateCandidate, ...]
    selected: tuple[UpdateCandidate, ...]
    failures: tuple[UpdateCandidate, ...]
    applied: bool
    partial: bool


@dataclass(frozen=True)
class UpdateProgress:
    phase: Literal["normalizing", "resolving", "complete", "failed", "cancelled"]
    completed: int
    total: int
    mod_name: str = ""
    provider: str = ""
    message: str = ""


class UpdatePreparationCancelled(HuroshikiError):
    pass


class UpdatePreparationDeadlineExceeded(HuroshikiError):
    pass


class UpdatePreparationOperation:
    def __init__(
        self,
        project_key: str,
        *,
        deadline: float | None = None,
    ) -> None:
        self.project_key = project_key
        self.transaction: PackTransaction | None = None
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.candidates: tuple[UpdateCandidate, ...] = ()
        self.error: BaseException | None = None
        self.cancelled = False
        self.deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + UPDATE_OPERATION_TIMEOUT_SECONDS
        )
        self._progress: queue.SimpleQueue[UpdateProgress] = queue.SimpleQueue()
        self._discarded = False
        self._claimed = False
        self._started = False
        self._state_lock = threading.Lock()

    def run(self) -> None:
        with self._state_lock:
            if self.done.is_set():
                return
            self._started = True
        try:
            self.transaction = PackTransaction.create(
                self.project_key,
                checkpoint=self._checkpoint,
            )
            self.candidates = tuple(
                self.transaction.prepare_updates(
                    cancel_event=self.cancel_event,
                    deadline=self.deadline,
                    on_progress=self._progress.put,
                )
            )
            if self.cancel_event.is_set():
                self.cancelled = True
        except UpdatePreparationCancelled:
            self.cancelled = True
        except BaseException as error:
            self.error = error
        finally:
            with self._state_lock:
                if self.cancelled or self.error is not None or self.cancel_event.is_set():
                    self.cancelled = self.cancelled or self.cancel_event.is_set()
                    self._discard_recording_error()
                self.done.set()

    def _checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise UpdatePreparationCancelled("Update preparation was cancelled")
        if time.monotonic() >= self.deadline:
            raise UpdatePreparationDeadlineExceeded(
                "Update preparation operation deadline exceeded"
            )

    def _discard_once(self) -> None:
        if self._discarded:
            return
        self._discarded = True
        if self.transaction is not None:
            self.transaction.discard()

    def _discard_recording_error(self) -> None:
        try:
            self._discard_once()
        except BaseException as error:
            if self.error is None:
                self.error = error

    def cancel(self) -> None:
        with self._state_lock:
            self.cancelled = True
            self.cancel_event.set()
            if (not self._started or self.done.is_set()) and not self._claimed:
                self._discard_recording_error()
                self.done.set()

    def claim_transaction(self) -> "PackTransaction":
        with self._state_lock:
            if (
                not self.done.is_set()
                or self.cancelled
                or self.error is not None
                or self._discarded
                or self.transaction is None
            ):
                raise HuroshikiError("Update preparation has no successful transaction")
            self._claimed = True
            return self.transaction

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def drain_progress(self) -> tuple[UpdateProgress, ...]:
        values: list[UpdateProgress] = []
        while True:
            try:
                values.append(self._progress.get_nowait())
            except queue.Empty:
                return tuple(values)


@dataclass(frozen=True)
class TemplateInfo:
    target: str
    relative_path: Path
    full_path: Path
    size: int
    error: str | None = None


@dataclass(frozen=True)
class TransactionBatch:
    provider: str
    query: str
    changed_files: tuple[Path, ...]
    root_identity: tuple[str, str] | None = None
    closure_identities: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StagedExactModTarget:
    mod: ModInfo
    role: Literal["root", "dependency"]
    required_by: tuple[str, ...] = ()
    required_by_complete: bool = False


@dataclass(frozen=True)
class ExactStageEvidence:
    selection: ExactModArtifactSelection
    source_digest: tuple[tuple[Path, str], ...]
    verification_digest: str
    verifications: tuple[ExactArtifactVerification, ...]
    manifest: bytes | None
    versions: tuple[str, str, str]
    metadata_identities: tuple[tuple[str, str], ...]
    reachability: tuple[
        tuple[tuple[str, str], tuple[str, ...]], ...
    ]
    accepted_identities: tuple[str, ...]
    mutation_generation: int
    checkpoint_digest: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class AcceptedExactStage:
    evidence: ExactStageEvidence
    checkpoint: Path
    prior_evidence: ExactStageEvidence | None
    prior_override_mutated: bool
    prior_verification_required: bool


@dataclass(frozen=True)
class AddOperationResult:
    returncode: int
    changed_files: tuple[Path, ...]
    raw_log: Path
    text_log: Path
    event_log: Path
    message: str
    cancelled: bool = False
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0 and bool(self.changed_files)


@dataclass(frozen=True)
class TemplateInstallFailure:
    name: str
    provider: str
    project_id: str
    reason: str


@dataclass(frozen=True)
class RetainedTemplateCandidate:
    candidate_key: str
    name: str
    requested_provider: str
    requested_project_id: str
    actual_provider: str
    actual_project_id: str
    relative_path: Path
    filename: str


@dataclass(frozen=True)
class TemplateCreationReport:
    pack_key: str
    template_ids: tuple[str, ...]
    conflict_selections: tuple[ConflictSelection, ...] = field(default_factory=tuple)
    conflict_warnings: tuple[str, ...] = field(default_factory=tuple)
    installed: tuple[str, ...] = field(default_factory=tuple)
    failed: tuple[TemplateInstallFailure, ...] = field(default_factory=tuple)
    retained: tuple[RetainedTemplateCandidate, ...] = field(default_factory=tuple)

    @property
    def template_id(self) -> str:
        return self.template_ids[0]

    @property
    def warning_lines(self) -> list[str]:
        lines = ["Applied templates: " + " -> ".join(self.template_ids)]
        if self.conflict_selections:
            lines.append("Conflict decisions:")
            lines.extend(
                f"- {item.name}: {', '.join(item.candidate_labels)}"
                for item in self.conflict_selections
            )
        lines.extend(f"Warning: {warning}" for warning in self.conflict_warnings)
        lines.append(f"Installed {len(self.installed)} MOD(s).")
        if self.retained:
            lines.append("Retained template candidates:")
            lines.extend(
                f"- {item.name}: {item.actual_provider}:{item.actual_project_id} "
                f"at {item.relative_path} ({item.filename})"
                for item in self.retained
            )
        if not self.failed:
            lines.append("No installation failures.")
            return lines
        lines.append(f"Could not install {len(self.failed)} MOD(s):")
        lines.extend(
            f"- {item.name} ({item.provider}:{item.project_id}): {item.reason}"
            for item in self.failed
        )
        return lines


@dataclass(frozen=True)
class ResolvedMetadata:
    identity: tuple[str, str]
    relative_path: Path
    filename: str
    contents: bytes
    provider: str
    project_id: str


@dataclass(frozen=True)
class ResolvedModClosure:
    root_identity: tuple[str, str]
    metadata: tuple[ResolvedMetadata, ...]


@dataclass(frozen=True)
class ResolvedSelector:
    provider: str
    original: str
    canonical_project_id: str | CanonicalModrinthId | None
    display_label: str | None = None

    def __post_init__(self) -> None:
        if self.canonical_project_id is None:
            return
        provider = canonical_provider(self.provider)
        if provider == "curseforge":
            object.__setattr__(
                self,
                "canonical_project_id",
                canonical_curseforge_project_id(self.canonical_project_id),
            )


@dataclass(frozen=True)
class ProviderProject:
    provider: str
    project_id: str | CanonicalModrinthId
    slug: str
    title: str
    description: str = ""
    author: str = ""

    def __post_init__(self) -> None:
        provider = canonical_provider(self.provider)
        if provider == "modrinth":
            object.__setattr__(
                self,
                "project_id",
                canonical_modrinth_id(self.project_id, "Modrinth project ID"),
            )
        elif provider == "curseforge":
            object.__setattr__(
                self,
                "project_id",
                canonical_curseforge_project_id(self.project_id),
            )


@dataclass(frozen=True)
class InstallSearchResult:
    provider: str
    project_id: str
    title: str
    subtitle: str


ResolverProcessResult = BoundedProcessResult
ResolverTerminationResult = ProcessTerminationResult
def run_resolver_process(
    command: Sequence[str],
    **kwargs: object,
) -> ResolverProcessResult:
    kwargs.setdefault("max_output_bytes", packctl.PACKWIZ_OUTPUT_MAX_BYTES)
    return run_bounded_process(command, **kwargs)  # type: ignore[arg-type]
stop_resolver_process_group = stop_process_group


class ProviderSearchOperation:
    def __init__(
        self,
        *,
        provider: str,
        query: str,
        minecraft: str,
        loader: str,
        deadline: float | None = None,
    ) -> None:
        if canonical_provider(provider) != "modrinth":
            raise HuroshikiError("Provider API search is available only for Modrinth")
        self.provider = provider
        self.query = query
        self.minecraft = minecraft
        self.loader = loader
        self.deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + PROVIDER_LOOKUP_TIMEOUT_SECONDS
        )
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.cancelled = False
        self.results: tuple[ProviderProject, ...] = ()
        self.error: str | None = None

    def run(self) -> tuple[ProviderProject, ...]:
        try:
            self.results = search_provider_projects(
                self.provider,
                self.query,
                minecraft=self.minecraft,
                loader=self.loader,
                cancel_event=self.cancel_event,
                deadline=self.deadline,
            )
        except Exception as error:
            self.error = str(error)
        finally:
            self.done.set()
        return self.results

    def cancel(self) -> None:
        self.cancelled = True
        self.cancel_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)


class AddOperationCancelled(HuroshikiError):
    pass


class AddOperationDeadlineExceeded(HuroshikiError):
    pass


class _AddOperationLifecycle:
    def __init__(
        self,
        transaction: "PackTransaction",
        *,
        deadline: float | None,
    ) -> None:
        self.transaction = transaction
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.cancelled = False
        self.deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
        )
        self.result: AddOperationResult | None = None
        self.cleanup_error: BaseException | None = None
        self.resolver_process_result: BoundedProcessResult | None = None
        self.resolver_termination_result: ProcessTerminationResult | None = None
        self.resolver_termination_incomplete = False
        self.packwiz_diagnostic_messages: list[str] = []
        self._checkpoint_complete = False
        self._pending_batch: TransactionBatch | None = None
        self._state: Literal["created", "running", "done"] = "created"
        self._state_lock = threading.Lock()

    @property
    def state(self) -> Literal["created", "running", "done"]:
        with self._state_lock:
            return self._state

    def _mark_started(self) -> bool:
        with self._state_lock:
            if self._state == "done":
                return False
            if self._state != "created":
                raise HuroshikiError("Add operation is already running")
            self._state = "running"
            return True

    def _claim_prestart_abort(self) -> bool:
        with self._state_lock:
            if self._state != "created":
                return False
            self._state = "running"
            return True

    def _complete(self, result: AddOperationResult) -> AddOperationResult:
        with self._state_lock:
            if self._state == "done":
                assert self.result is not None
                return self.result
            self.result = result
            self._state = "done"
            self.done.set()
            return result

    def _checkpoint(self) -> None:
        if self.cancel_event.is_set():
            self.cancelled = True
            raise AddOperationCancelled("Install operation was cancelled")
        with self._state_lock:
            deadline = self.deadline
        if time.monotonic() >= deadline:
            raise AddOperationDeadlineExceeded(
                "Install operation deadline exceeded"
            )

    def _request_cancel(self, *, deadline: float | None) -> float:
        with self._state_lock:
            self.cancelled = True
            self.cancel_event.set()
            if deadline is not None:
                self.deadline = min(self.deadline, deadline)
            effective_deadline = self.deadline
            abort_before_start = self._state == "created"
        if abort_before_start:
            self.abort_before_start(
                AddOperationCancelled("Install operation was cancelled"),
                cancelled=True,
            )
        return effective_deadline

    def _normalized_error(self, error: BaseException) -> BaseException:
        if self.cancelled or self.cancel_event.is_set():
            self.cancelled = True
            return AddOperationCancelled("Install operation was cancelled")
        with self._state_lock:
            deadline = self.deadline
        if isinstance(error, AddOperationDeadlineExceeded) or time.monotonic() >= deadline:
            return AddOperationDeadlineExceeded(
                "Install operation deadline exceeded"
            )
        return error

    def _error_result(self, error: BaseException) -> AddOperationResult:
        raw_log, text_log, event_log = url_log_paths(self.log_dir)
        message = str(error)
        if self.cleanup_error is not None:
            message = f"Add operation cleanup failed: {self.cleanup_error}"
        return AddOperationResult(
            returncode=130 if self.cancelled else 1,
            changed_files=(),
            raw_log=raw_log,
            text_log=text_log,
            event_log=event_log,
            message=message,
            cancelled=self.cancelled,
            timed_out=isinstance(error, AddOperationDeadlineExceeded),
        )

    def abort_before_start(
        self,
        error: BaseException,
        *,
        cancelled: bool = False,
    ) -> bool:
        return self.transaction.fail_operation_start(
            self,
            error,
            cancelled=cancelled,
        )

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def _record_resolver_process_result(self, result: BoundedProcessResult) -> None:
        self.resolver_process_result = result
        if result.termination_incomplete:
            self.resolver_termination_incomplete = True

    def _record_packwiz_diagnostic(self, message: str) -> None:
        self.packwiz_diagnostic_messages.append(message)

    def _retry_resolver_cleanup(self, deadline: float | None) -> None:
        process = self.resolver_process_result
        if (
            deadline is None
            or not self.resolver_termination_incomplete
            or process is None
            or process.process_group is None
        ):
            return
        self.resolver_termination_result = stop_resolver_process_group(
            process.process_group,
            parent=process.parent_process,
            cleanup_deadline=deadline,
        )
        self.resolver_termination_incomplete = not (
            self.resolver_termination_result.group_drained
            and self.resolver_termination_result.parent_reaped
        )


class ResolvedAddOperation(_AddOperationLifecycle):
    def __init__(
        self,
        transaction: "PackTransaction",
        *,
        provider: str,
        selector: str,
        canonical_project_id: str | None,
        side: str,
        deadline: float | None = None,
    ) -> None:
        super().__init__(transaction, deadline=deadline)
        self.provider, self.selector = normalize_add_selector(provider, selector)
        self.canonical_project_id = canonical_project_id
        self.side = packctl.normalize_side(side)
        operation_id = uuid4().hex
        self.checkpoint = transaction.root / f"checkpoint-{operation_id}"
        self.retained_checkpoint = (
            transaction.root / f"retained-add-checkpoint-{operation_id}"
        )
        self.resolver_root = transaction.root / f"resolved-{operation_id}"
        self.retained_resolver_root = (
            transaction.root / f"retained-add-resolver-{operation_id}"
        )
        self.retained_failed_source = (
            transaction.root / f"retained-failed-add-source-{operation_id}"
        )
        self.minecraft = ""
        self.loader = ""
        self.loader_version = ""
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_dir = (
            ROOT
            / ".huroshiki"
            / "logs"
            / transaction.project_key.replace(":", "-")
            / f"{timestamp}-{uuid4().hex[:8]}"
        )

    def run(self) -> AddOperationResult:
        if not self._mark_started():
            assert self.result is not None
            return self.result
        fatal_error: BaseException | None = None
        result: AddOperationResult | None = None
        raw_log, text_log, event_log = url_log_paths(self.log_dir)
        try:
            self._checkpoint()
            with self.transaction._lock:
                if not self.transaction.active or self.transaction._operation is not self:
                    raise HuroshikiError("Transaction was closed before MOD resolution started")
            self._checkpoint()
            copy_transaction_source(
                self.transaction.source,
                self.checkpoint,
                checkpoint=self._checkpoint,
                retained_destination=self.retained_checkpoint,
            )
            self._checkpoint_complete = True
            self._checkpoint()
            with self.transaction._lock:
                if not self.transaction.active or self.transaction._operation is not self:
                    raise HuroshikiError(
                        "Transaction was closed during MOD checkpoint preparation"
                    )
                self.minecraft, self.loader, self.loader_version = (
                    packctl.project_versions(self.transaction.source)
                )
                self.transaction._ensure_empty_pack_root_manifest()
                self._checkpoint()
            closure = resolve_mod_closure(
                provider=self.provider,
                selector=self.selector,
                minecraft=self.minecraft,
                loader=self.loader,
                loader_version=self.loader_version,
                canonical_project_id=self.canonical_project_id,
                cancel_event=self.cancel_event,
                deadline=self.deadline,
                resolver_root=self.resolver_root,
                process_result_callback=self._record_resolver_process_result,
                diagnostic_project_id=self.transaction.project_key.partition(":")[2],
                diagnostic_callback=self._record_packwiz_diagnostic,
            )
            self._checkpoint()
            with self.transaction._lock:
                if not self.transaction.active or self.transaction._operation is not self:
                    raise HuroshikiError(
                        "Transaction was closed before MOD resolution completed"
                    )
                self._checkpoint()
                overrides = self.transaction._validated_version_overrides()
                changed = merge_metadata_closure(
                    self.transaction.source,
                    closure,
                    requested_side=self.side,
                    cancel_event=self.cancel_event,
                    deadline=self.deadline,
                    equivalence_workspace=self.resolver_root / "equivalence",
                    process_result_callback=self._record_resolver_process_result,
                )
                self.transaction._assert_version_overrides_preserved(overrides)
                self._checkpoint()
                batch = TransactionBatch(
                    provider=self.provider,
                    query=self.selector,
                    changed_files=changed,
                    root_identity=closure.root_identity,
                    closure_identities=tuple(
                        sorted(item.identity for item in closure.metadata)
                    ),
                )
                message = (
                    f"Staged {self.provider}:{closure.root_identity[1]} and dependencies"
                )
                if self.packwiz_diagnostic_messages:
                    message += " " + " ".join(self.packwiz_diagnostic_messages)
                result = AddOperationResult(
                    0,
                    changed,
                    raw_log,
                    text_log,
                    event_log,
                    message,
                )
                self._pending_batch = batch
                self.transaction.batches.append(batch)
                self.transaction._retain_add_operation_paths(self)
                self._checkpoint_complete = False
                self._pending_batch = None
                self.transaction._operation = None
                self.transaction._record_source_mutation()
        except BaseException as error:
            if not isinstance(error, Exception):
                fatal_error = error
            error = self._normalized_error(error)
            try:
                self.transaction._rollback_add(self)
            except BaseException as cleanup_error:
                self.cleanup_error = cleanup_error
            result = self._error_result(error)
        if result is None:
            result = self._error_result(HuroshikiError("Add operation failed"))
        completed = self._complete(result)
        if fatal_error is not None:
            raise fatal_error
        return completed

    def cancel(self, *, deadline: float | None = None) -> None:
        self._request_cancel(deadline=deadline)
        if self.done.is_set():
            self._retry_resolver_cleanup(deadline)
            self.transaction._release_add_cleanup_ownership(self)


class PackwizAddOperation(_AddOperationLifecycle):
    def __init__(
        self,
        transaction: "PackTransaction",
        provider: str,
        query: str,
        *,
        client: bool,
        server: bool,
        on_event: Callable[[ParserEvent], None] | None = None,
        deadline: float | None = None,
    ) -> None:
        super().__init__(transaction, deadline=deadline)
        self.provider, self.query = normalize_add_selector(provider, query)
        if self.provider == "curseforge" and self.query.isdecimal():
            self.query = canonical_curseforge_project_id(self.query)
        self.client = client
        self.server = server
        self.on_event = on_event
        self.termination_result: ProcessTerminationResult | None = None
        self.termination_incomplete = False
        self.resolver_process_result: BoundedProcessResult | None = None
        self.resolver_termination_result: ProcessTerminationResult | None = None
        self.resolver_termination_incomplete = False
        self.menu_items: dict[int, str] = {}
        self.selection: str | None = None
        operation_id = uuid4().hex
        self.checkpoint = transaction.root / f"checkpoint-{operation_id}"
        self.retained_checkpoint = (
            transaction.root / f"retained-add-checkpoint-{operation_id}"
        )
        self.resolver_root = transaction.root / f"resolver-{operation_id}"
        self.retained_resolver_root = (
            transaction.root / f"retained-add-resolver-{operation_id}"
        )
        self.retained_failed_source = (
            transaction.root / f"retained-failed-add-source-{operation_id}"
        )
        self.resolver_source = self.resolver_root / "source"
        self.closure_resolver_root = self.resolver_root / "closure"
        self.minecraft = ""
        self.loader = ""
        self.loader_version = ""

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_dir = (
            ROOT / ".huroshiki" / "logs"
            / transaction.project_key.replace(":", "-")
            / f"{timestamp}-{uuid4().hex[:8]}"
        )
        self.session: PackwizPtySession | None = None

    def _prepare(self) -> None:
        self._checkpoint()
        with self.transaction._lock:
            if not self.transaction.active or self.transaction._operation is not self:
                raise HuroshikiError("Transaction was closed before add preparation started")
        self._checkpoint()
        copy_transaction_source(
            self.transaction.source,
            self.checkpoint,
            checkpoint=self._checkpoint,
            retained_destination=self.retained_checkpoint,
        )
        self._checkpoint_complete = True
        self._checkpoint()
        with self.transaction._lock:
            if not self.transaction.active or self.transaction._operation is not self:
                raise HuroshikiError(
                    "Transaction was closed during add checkpoint preparation"
                )
            self.minecraft, self.loader, self.loader_version = packctl.project_versions(
                self.transaction.source
            )
            self._checkpoint()
            create_resolver_source(
                self.resolver_source,
                display_name=f"Resolve {self.query}",
                minecraft=self.minecraft,
                loader=self.loader,
                loader_version=self.loader_version,
            )
            self._checkpoint()
            packctl.ensure_safe_state_path(
                self.log_dir,
                state_root=ROOT / ".huroshiki",
                repository_root=ROOT,
            )
            if self.provider != "url":
                def record_event(event: ParserEvent) -> None:
                    if event.kind == "search_results":
                        self.menu_items = {item.index: item.label for item in event.items}
                    if self.on_event is not None:
                        self.on_event(event)
                    if self.provider == "curseforge" and event.kind == "confirmation":
                        if self.session is None:
                            raise HuroshikiError("Packwiz PTY session was not initialized")
                        # The interactive pass is an identity probe. Dependencies are
                        # resolved only after the selected root's numeric ID is verified.
                        self.session.send_line("n")

                self.session = PackwizPtySession(
                    build_add_command(self.provider, self.query),
                    cwd=self.resolver_source,
                    log_dir=self.log_dir,
                    on_event=record_event,
                    cancel_event=self.cancel_event,
                )
            self._checkpoint()

    def run(self) -> AddOperationResult:
        if not self._mark_started():
            assert self.result is not None
            return self.result
        fatal_error: BaseException | None = None
        result: AddOperationResult | None = None
        try:
            self._prepare()
            self._checkpoint()
            if self.provider == "url":
                result = self.transaction._finish_url_add(self)
            else:
                if self.session is None:
                    raise HuroshikiError("Packwiz PTY session was not initialized")
                pty_result = self.session.run(deadline=self.deadline)
                self.termination_result = pty_result.termination_result
                self.termination_incomplete = pty_result.termination_incomplete
                if pty_result.timed_out:
                    raise AddOperationDeadlineExceeded(
                        "Install operation deadline exceeded"
                    )
                self._checkpoint()
                result = self.transaction._finish_add(self, pty_result)
        except BaseException as error:
            if not isinstance(error, Exception):
                fatal_error = error
            error = self._normalized_error(error)
            try:
                self.transaction._rollback_add(self)
            except BaseException as cleanup_error:
                self.cleanup_error = cleanup_error
            if self.provider == "url":
                ensure_url_error_log(self.log_dir, str(error))
            result = self._error_result(error)
        finally:
            if self.session is not None and self.session.termination_result is not None:
                self.termination_result = self.session.termination_result
                self.termination_incomplete = not (
                    self.termination_result.group_drained
                    and self.termination_result.parent_reaped
                ) or getattr(self, "resolver_termination_incomplete", False)
            if result is None:
                result = self._error_result(HuroshikiError("Add operation failed"))
        completed = self._complete(result)
        if fatal_error is not None:
            raise fatal_error
        return completed

    def send_selection(self, index: int) -> None:
        if self.session is None:
            raise HuroshikiError("URL additions do not expose search results")
        self.selection = self.menu_items.get(index)
        self.session.send_line(str(index))

    def confirm(self, accepted: bool = True) -> None:
        if self.session is not None:
            self.session.send_line("y" if accepted else "n")

    def cancel_menu(self) -> None:
        effective_deadline = self._request_cancel(deadline=None)
        if self.session is None:
            return
        try:
            self.session.send_line("0")
        except (OSError, RuntimeError):
            self.session.cancel(deadline=effective_deadline)

    def cancel(
        self,
        *,
        deadline: float | None = None,
    ) -> ProcessTerminationResult | None:
        termination = self.termination_result
        retrying_cleanup = self.done.is_set() and (
            self.termination_incomplete
            or (
                termination is not None
                and not (termination.group_drained and termination.parent_reaped)
            )
        )
        effective_deadline = self._request_cancel(deadline=deadline)
        resolver_process = getattr(self, "resolver_process_result", None)
        resolver_termination_incomplete = getattr(
            self, "resolver_termination_incomplete", False
        )
        if (
            retrying_cleanup
            and resolver_termination_incomplete
            and deadline is not None
            and resolver_process is not None
            and resolver_process.process_group is not None
        ):
            self.resolver_termination_result = stop_resolver_process_group(
                resolver_process.process_group,
                parent=resolver_process.parent_process,
                cleanup_deadline=deadline,
            )
            self.resolver_termination_incomplete = not (
                self.resolver_termination_result.group_drained
                and self.resolver_termination_result.parent_reaped
            )
        session = self.session
        if session is not None:
            cleanup_deadline = (
                deadline
                if retrying_cleanup and deadline is not None
                else effective_deadline
            )
            self.termination_result = session.cancel(deadline=cleanup_deadline)
            if self.termination_result is not None:
                self.termination_incomplete = not (
                    self.termination_result.group_drained
                    and self.termination_result.parent_reaped
                ) or getattr(self, "resolver_termination_incomplete", False)
        elif not getattr(self, "resolver_termination_incomplete", False):
            self.termination_incomplete = False
        if self.done.is_set():
            self.transaction._release_add_cleanup_ownership(self)
        return self.termination_result or self.resolver_termination_result

    def _record_resolver_process_result(self, result: BoundedProcessResult) -> None:
        self.resolver_process_result = result
        self.resolver_termination_incomplete = result.termination_incomplete
        self.termination_incomplete = (
            self.termination_incomplete or self.resolver_termination_incomplete
        )

    def resize(self, width: int, height: int) -> None:
        if self.session is not None:
            self.session.resize(width, height)

TRANSACTION_DISCARD_TIMEOUT_SECONDS = 10.0
_RETAINED_FAILED_TRANSACTIONS: dict[str, "PackTransaction"] = {}
_RETAINED_TEMPLATE_CREATIONS: dict[
    str, tuple[object, Path, list[BoundedProcessResult]]
] = {}


class TemplateCreationCleanupRequired(HuroshikiError):
    def __init__(
        self, workspace: Path, results: list[BoundedProcessResult]
    ) -> None:
        self.workspace = workspace
        self.results = results
        super().__init__(
            "Template creation process cleanup was incomplete; state retained at "
            f"{workspace}"
        )


def _cleanup_template_creation_processes(
    workspace: Path,
    results: list[BoundedProcessResult],
    *,
    deadline: float,
) -> None:
    remaining: list[BoundedProcessResult] = []
    for result in results:
        if result.process_group is None:
            remaining.append(result)
            continue
        cleanup = stop_resolver_process_group(
            result.process_group,
            parent=result.parent_process,
            cleanup_deadline=deadline,
        )
        if not (cleanup.group_drained and cleanup.parent_reaped):
            remaining.append(result)
    results[:] = remaining
    if remaining:
        raise TemplateCreationCleanupRequired(workspace, results)
    if workspace.exists():
        shutil.rmtree(workspace)


def retry_retained_template_creation_cleanup(
    project_key_value: str, *, deadline: float | None = None
) -> None:
    retained = _RETAINED_TEMPLATE_CREATIONS.get(project_key_value)
    if retained is None:
        return
    lock, workspace, results = retained
    _cleanup_template_creation_processes(
        workspace,
        results,
        deadline=(
            deadline
            if deadline is not None
            else time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
        ),
    )
    lock.release()
    _RETAINED_TEMPLATE_CREATIONS.pop(project_key_value, None)


def retry_all_retained_template_creation_cleanup(*, deadline: float) -> None:
    failures: list[str] = []
    for project_key_value in tuple(_RETAINED_TEMPLATE_CREATIONS):
        try:
            retry_retained_template_creation_cleanup(
                project_key_value, deadline=deadline
            )
        except BaseException as error:
            failures.append(f"{project_key_value}: {error}")
    if failures:
        raise HuroshikiError("; ".join(failures))


class TransactionDiscardError(HuroshikiError):
    pass


class TransactionDiscardTimeout(TransactionDiscardError):
    pass


class TransactionDiscardIntegrityError(TransactionDiscardError):
    pass


class TransactionDiscardOperation:
    def __init__(
        self,
        transaction: "PackTransaction",
        deadline: float,
    ) -> None:
        self.transaction = transaction
        self.deadline = deadline
        self.done = threading.Event()
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._started = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._execute,
                name=f"huroshiki-discard-{self.transaction.project_key}",
                daemon=False,
            )
            try:
                self._thread.start()
            except BaseException as error:
                self.error = error
                self.transaction._record_discard_failure(self, error)
                self.done.set()
                raise

    def run(self) -> None:
        with self._lock:
            owner = not self._started
            if owner:
                self._started = True
        if owner:
            self._execute()
            return
        self.wait(max(0.0, self.deadline - time.monotonic()))

    def _execute(self) -> None:
        try:
            self.transaction._run_discard_operation(self)
        except BaseException as error:
            self.error = error
            self.transaction._record_discard_failure(self, error)
        finally:
            self.done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def raise_for_error(self) -> None:
        if not self.done.is_set():
            raise TransactionDiscardTimeout("Transaction discard is still running")
        if self.error is not None:
            raise self.error


class ExactModVersionOperation:
    """Transaction-owned lifecycle for synchronous exact-selection workers."""

    def __init__(
        self,
        cancel_event: threading.Event,
        deadline: float,
    ) -> None:
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.done = threading.Event()
        self.cleanup_error: BaseException | None = None
        self.termination_incomplete = False

    def cancel(self, *, deadline: float | None = None) -> None:
        del deadline
        self.cancel_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)


@dataclass
class PackTransaction:
    project_key: str
    root: Path
    source: Path
    baseline: dict[Path, str]
    baseline_contents: dict[Path, bytes] = field(default_factory=dict)
    root_manifest_baseline: tuple[PackRootRecord, ...] = field(default_factory=tuple)
    real_source_baseline: dict[Path, str] = field(default_factory=dict)
    pack_config_baseline: dict[str, str] = field(default_factory=dict)
    template_config_baseline: dict[str, str] = field(default_factory=dict)
    template_manifest: list[object] | None = None
    batches: list[TransactionBatch] = field(default_factory=list)
    update_candidates: tuple[UpdateCandidate, ...] = field(default_factory=tuple)
    selected_update_changes: tuple[UpdateChange, ...] = field(default_factory=tuple)
    _exact_selection_prepared: bool = field(default=False, init=False, repr=False)
    _pending_exact_evidence: ExactStageEvidence | None = field(
        default=None, init=False, repr=False
    )
    _accepted_exact_evidence: ExactStageEvidence | None = field(
        default=None, init=False, repr=False
    )
    _accepted_exact_stages: list[AcceptedExactStage] = field(
        default_factory=list, init=False, repr=False
    )
    _exact_selection_checkpoint: Path | None = field(
        default=None, init=False, repr=False
    )
    _exact_selection_failed_source: Path | None = field(
        default=None, init=False, repr=False
    )
    _version_override_mutated: bool = field(default=False, init=False, repr=False)
    _exact_verification_required: bool = field(default=False, init=False, repr=False)
    _mutation_generation: int = field(default=0, init=False, repr=False)
    _rollback_source_digest: tuple[tuple[Path, str], ...] | None = field(
        default=None, init=False, repr=False
    )
    _intent_only_mutation: bool = field(default=False, init=False, repr=False)
    _source_mutation_recorded: bool = field(default=False, init=False, repr=False)
    _equivalence_process_results: list[BoundedProcessResult] = field(
        default_factory=list, init=False, repr=False
    )
    active: bool = True
    _project_lock: packctl.ProjectLock | None = field(default=None, init=False, repr=False)
    _operation: PackwizAddOperation | ResolvedAddOperation | ExactModVersionOperation | None = field(
        default=None, init=False, repr=False
    )
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _discard_operation: TransactionDiscardOperation | None = field(
        default=None, init=False, repr=False
    )
    _discard_finalized: bool = field(default=False, init=False, repr=False)
    _discard_error: BaseException | None = field(default=None, init=False, repr=False)
    _discard_state: Literal["active", "discarding", "discarded", "failed"] = field(
        default="active", init=False, repr=False
    )

    @classmethod
    def create(
        cls,
        project_key_value: str,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> "PackTransaction":
        kind, project_id = split_project_key(project_key_value)
        try:
            project_lock = packctl.ProjectLock(
                project_key_value, "transaction"
            ).acquire()
        except packctl.ConfigError as error:
            raise HuroshikiError(str(error)) from error
        tx_root: Path | None = None
        try:
            real_root = project_root(project_key_value)
            transaction_root = ROOT / ".huroshiki" / "transactions"
            packctl.make_state_directory(
                transaction_root,
                state_root=ROOT / ".huroshiki",
                repository_root=ROOT,
            )
            safe_prefix = project_key_value.replace(":", "-")
            tx_root = Path(
                tempfile.mkdtemp(
                    prefix=f"{safe_prefix}-",
                    dir=transaction_root,
                )
            )
            tx_source = tx_root / "source"

            if kind == "pack":
                real_source = real_root / "source"
                if not real_source.is_dir():
                    raise HuroshikiError(
                        f"Missing Packwiz source directory: {real_source}"
                    )
                ensure_safe_pack_source(real_source, checkpoint=checkpoint)
                verified_config = pack_config_snapshot(real_root)
                checkpoint_arguments = (
                    {"checkpoint": checkpoint} if checkpoint is not None else {}
                )
                verified_baseline = tree_digest_snapshot(
                    real_source,
                    **checkpoint_arguments,
                )
                copy_transaction_source(
                    real_source,
                    tx_source,
                    **checkpoint_arguments,
                )
                if (
                    tree_digest_snapshot(real_source, **checkpoint_arguments)
                    != verified_baseline
                    or tree_digest_snapshot(tx_source, **checkpoint_arguments)
                    != verified_baseline
                    or pack_config_snapshot(real_root) != verified_config
                ):
                    raise HuroshikiError(
                        "The pack source or configuration changed while the transaction "
                        "copy was being created; retry the operation."
                    )
                transaction = cls(
                    project_key=project_key_value,
                    root=tx_root,
                    source=tx_source,
                    baseline=metadata_digest_snapshot(
                        tx_source,
                        **checkpoint_arguments,
                    ),
                    baseline_contents=metadata_content_snapshot(
                        tx_source,
                        **checkpoint_arguments,
                    ),
                    root_manifest_baseline=(
                        read_pack_root_manifest(tx_source)
                        if (tx_source / ".huroshiki-roots.json").is_file()
                        and not (tx_source / ".huroshiki-roots.json").is_symlink()
                        else ()
                    ),
                    real_source_baseline=verified_baseline,
                    pack_config_baseline=verified_config,
                )
            else:
                verified_config = template_config_snapshot(real_root)
                config = packctl.load_template_config(project_id)
                minecraft, loader, loader_version = packctl.template_versions(
                    project_id
                )
                create_resolver_source(
                    tx_source,
                    display_name=str(config.get("display_name", project_id)),
                    minecraft=minecraft,
                    loader=loader,
                    loader_version=loader_version,
                )
                if template_config_snapshot(real_root) != verified_config:
                    raise HuroshikiError(
                        "The template configuration changed while the transaction "
                        "resolver was being created; retry the operation."
                    )
                transaction = cls(
                    project_key=project_key_value,
                    root=tx_root,
                    source=tx_source,
                    baseline={},
                    baseline_contents={},
                    template_config_baseline=verified_config,
                )
            transaction._project_lock = project_lock
            return transaction
        except BaseException:
            if tx_root is not None:
                shutil.rmtree(tx_root, ignore_errors=True)
            project_lock.release()
            raise

    def _finish_state(self) -> None:
        with self._lock:
            try:
                (self.root / ".completed").touch(exist_ok=True)
            except OSError:
                pass
            if self._project_lock is not None:
                self._project_lock.release()
                self._project_lock = None

    def _mark_lifecycle_completed(self) -> None:
        with self._lock:
            self.active = False
            self._discard_finalized = True
            self._discard_error = None
            self._discard_state = "discarded"

    def __del__(self) -> None:
        project_lock = getattr(self, "_project_lock", None)
        if project_lock is not None:
            try:
                root = getattr(self, "root", None)
                if isinstance(root, Path) and root.is_dir():
                    try:
                        (root / ".discard-integrity-error").write_text(
                            "Transaction owner was destroyed before bounded discard completed.\n",
                            encoding="utf-8",
                        )
                    except OSError:
                        pass
                print(
                    f"Transaction cleanup was not completed for {self.project_key}; "
                    f"retaining {self.root}",
                    file=sys.stderr,
                )
                project_lock.release()
                self._project_lock = None
            except BaseException:
                pass

    def ensure_active(self) -> None:
        if not self.active or not self.source.is_dir():
            raise HuroshikiError("This transaction is no longer active")

    def _ensure_exact_selection_not_prepared(self) -> None:
        if self._exact_selection_prepared:
            raise HuroshikiError(
                "An exact MOD version is prepared; apply or discard it before "
                "staging another transaction change"
            )

    @property
    def exact_selection_prepared(self) -> bool:
        return self._exact_selection_prepared

    @property
    def exact_selection_accepted(self) -> bool:
        return self._accepted_exact_evidence is not None

    @property
    def operation_active(self) -> bool:
        with self._lock:
            return self._operation is not None

    @property
    def process_cleanup_pending(self) -> bool:
        with self._lock:
            return bool(self._equivalence_process_results)

    def _ensure_empty_pack_root_manifest(self) -> None:
        manifest = self.source / ".huroshiki-roots.json"
        if manifest.exists():
            return
        if metadata_content_snapshot(self.source):
            return
        ensure_pack_root_manifest_ignored(self.source)
        write_pack_root_manifest(self.source, ())

    def _validated_version_overrides(self) -> tuple[ModVersionOverride, ...]:
        return _validate_mod_version_override_records(
            self.source,
            _exact_metadata_records(self.source),
        )

    def _assert_version_overrides_preserved(
        self, overrides: tuple[ModVersionOverride, ...]
    ) -> None:
        _validate_mod_version_override_records(
            self.source,
            _exact_metadata_records(self.source),
            overrides=overrides,
        )

    def _restore_version_intent_manifest(
        self, contents: bytes, *, deadline: float
    ) -> None:
        def checkpoint() -> None:
            if time.monotonic() >= deadline:
                raise TransactionDiscardIntegrityError(
                    "Version intent rollback cleanup deadline exceeded"
                )

        try:
            scan = scan_pack_migration_source(self.source, checkpoint=checkpoint)
            write_pack_control_file(
                self.source,
                VERSION_OVERRIDE_MANIFEST_PATH,
                contents,
                expected_root_identity=scan.root_identity,
                checkpoint=checkpoint,
            )
        except BaseException as error:
            raise HuroshikiError(
                "Could not restore version intent manifest; transaction retained: "
                f"{error}"
            ) from error

    def rollback_exact_mod_version(self, *, deadline: float | None = None) -> None:
        """Restore the source before the pending or latest accepted selection."""
        self._lifecycle_lock.acquire()
        try:
            with self._lock:
                self.ensure_active()
                if self._operation is not None:
                    raise HuroshikiError(
                        "Wait for the active exact version operation to finish"
                    )
                cleanup_was_pending = bool(self._equivalence_process_results)
                cleanup_deadline = (
                    deadline
                    if deadline is not None
                    else time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
                )
                self._retry_equivalence_process_cleanup(cleanup_deadline)
                if not self._exact_selection_prepared and not self._accepted_exact_stages:
                    if cleanup_was_pending:
                        return
                    raise HuroshikiError("No exact MOD version selection can be undone")
                if self._exact_selection_prepared:
                    self._restore_exact_selection_checkpoint()
                else:
                    stage = self._accepted_exact_stages[-1]
                    self._restore_exact_selection_source(
                        stage.checkpoint,
                        self.root / f"failed-accepted-exact-source-{uuid4().hex}",
                        expected_digest=stage.evidence.checkpoint_digest,
                    )
                    self._accepted_exact_stages.pop()
                    self._mutation_generation += 1
                    self._accepted_exact_evidence = (
                        None
                        if stage.prior_evidence is None
                        else replace(
                            stage.prior_evidence,
                            mutation_generation=self._mutation_generation,
                        )
                    )
                    if self._accepted_exact_stages and self._accepted_exact_evidence is not None:
                        previous = self._accepted_exact_stages[-1]
                        self._accepted_exact_stages[-1] = replace(
                            previous,
                            evidence=self._accepted_exact_evidence,
                        )
                    self._version_override_mutated = stage.prior_override_mutated
                    self._exact_verification_required = (
                        stage.prior_verification_required
                    )
        finally:
            self._lifecycle_lock.release()

    def prepare_mod_version_automatic(
        self,
        identity: str,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> ModVersionIntentPreview:
        """Stage removal of one user exact-selection intent without changing metadata."""
        def checkpoint() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise ExactModVersionCancelled(
                    "MOD version intent operation was cancelled"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise ExactModVersionDeadlineExceeded(
                    "MOD version intent operation deadline exceeded"
                )

        with self._lock:
            checkpoint()
            self.ensure_active()
            self._ensure_exact_selection_not_prepared()
            if self._operation is not None:
                raise HuroshikiError("Wait for the active transaction operation to finish")
            kind, _project_id = split_project_key(self.project_key)
            if kind != "pack":
                raise HuroshikiError(
                    "MOD version intent controls are available only for packs"
                )
            status = mod_version_intent_status(
                self.source, identity, checkpoint=checkpoint
            )
            before = _file_content_snapshot(self.source, checkpoint)
            if status.selection == "user":
                manifest_before = before.get(VERSION_OVERRIDE_MANIFEST_PATH)
                if manifest_before is None:
                    raise HuroshikiError(
                        "Version intent manifest disappeared before mutation"
                    )
                try:
                    require_mod_version_overrides_ignored(
                        self.source, checkpoint=checkpoint
                    )
                    remove_mod_version_override(
                        self.source, status.identity, checkpoint=checkpoint
                    )
                    after = _file_content_snapshot(self.source, checkpoint)
                    changes = _content_changes(before, after)
                    if any(
                        change.relative_path != VERSION_OVERRIDE_MANIFEST_PATH
                        for change in changes
                    ):
                        raise HuroshikiError(
                            "Automatic intent mutation changed non-intent Pack files"
                        )
                except BaseException as error:
                    cleanup_deadline = (
                        time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
                    )

                    def cleanup_checkpoint() -> None:
                        if time.monotonic() >= cleanup_deadline:
                            raise TransactionDiscardIntegrityError(
                                "Version intent rollback cleanup deadline exceeded"
                            )

                    try:
                        current = _file_content_snapshot(
                            self.source, cleanup_checkpoint
                        )
                        if current.get(VERSION_OVERRIDE_MANIFEST_PATH) != manifest_before:
                            self._restore_version_intent_manifest(
                                manifest_before, deadline=cleanup_deadline
                            )
                    except BaseException as restore_error:
                        raise HuroshikiError(
                            f"{error}; version intent rollback failed: {restore_error}"
                        ) from error
                    if isinstance(error, (ModVersionOverrideError, OSError)):
                        raise HuroshikiError(str(error)) from error
                    raise
            else:
                after = before
                changes = ()
            if changes:
                self._version_override_mutated = True
                self._record_source_mutation(intent_only=True)
            elif not self._source_mutation_recorded:
                self._intent_only_mutation = True
            return ModVersionIntentPreview(
                identity=status.identity,
                installed_artifact_id=status.installed_artifact_id,
                selected_artifact_id=status.selected_artifact_id,
                old_selection=status.selection,
                new_selection="automatic",
                old_locked=status.locked,
                new_locked=None,
                reason=status.reason,
                override_status=status.override_status,
                changes=changes,
            )

    def prepare_mod_version_pin(
        self,
        identity: str,
        *,
        locked: bool = True,
        reason: str | None = None,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> ModVersionIntentPreview:
        """Stage a lock-state change for one active user exact-selection intent."""
        def checkpoint() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise ExactModVersionCancelled(
                    "MOD version intent operation was cancelled"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise ExactModVersionDeadlineExceeded(
                    "MOD version intent operation deadline exceeded"
                )

        with self._lock:
            checkpoint()
            self.ensure_active()
            self._ensure_exact_selection_not_prepared()
            if self._operation is not None:
                raise HuroshikiError("Wait for the active transaction operation to finish")
            kind, _project_id = split_project_key(self.project_key)
            if kind != "pack":
                raise HuroshikiError("MOD version pins are available only for packs")
            if type(locked) is not bool:
                raise HuroshikiError("MOD version pin state must be a boolean")
            status = mod_version_intent_status(
                self.source, identity, checkpoint=checkpoint
            )
            if status.selection == "automatic":
                raise HuroshikiError(
                    "Cannot pin an automatically selected MOD; select an exact version first"
                )
            if status.override_status != "active":
                raise HuroshikiError(
                    f"Cannot change pin state for {status.identity}: version intent status "
                    f"is {status.override_status}; re-select the exact artifact or return "
                    "to Automatic"
                )
            before = _file_content_snapshot(self.source, checkpoint)
            manifest_before = before.get(VERSION_OVERRIDE_MANIFEST_PATH)
            if manifest_before is None:
                raise HuroshikiError(
                    "Version intent manifest disappeared before mutation"
                )
            try:
                existing = get_mod_version_override(
                    self.source, status.identity, checkpoint=checkpoint
                )
                if existing is None:
                    raise HuroshikiError("Version intent changed while preparing preview")
                updated = ModVersionOverride(
                    existing.provider,
                    existing.project_id,
                    existing.artifact_id,
                    locked,
                    existing.reason if reason is None else reason,
                )
                require_mod_version_overrides_ignored(
                    self.source, checkpoint=checkpoint
                )
                set_mod_version_override(
                    self.source, updated, checkpoint=checkpoint
                )
                after = _file_content_snapshot(self.source, checkpoint)
                changes = _content_changes(before, after)
                if any(
                    change.relative_path != VERSION_OVERRIDE_MANIFEST_PATH
                    for change in changes
                ):
                    raise HuroshikiError(
                        "Pin intent mutation changed non-intent Pack files"
                    )
            except BaseException as error:
                cleanup_deadline = (
                    time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
                )

                def cleanup_checkpoint() -> None:
                    if time.monotonic() >= cleanup_deadline:
                        raise TransactionDiscardIntegrityError(
                            "Version intent rollback cleanup deadline exceeded"
                        )

                try:
                    current = _file_content_snapshot(
                        self.source, cleanup_checkpoint
                    )
                    if current.get(VERSION_OVERRIDE_MANIFEST_PATH) != manifest_before:
                        self._restore_version_intent_manifest(
                            manifest_before, deadline=cleanup_deadline
                        )
                except BaseException as restore_error:
                    raise HuroshikiError(
                        f"{error}; version intent rollback failed: {restore_error}"
                    ) from error
                if isinstance(error, HuroshikiError):
                    raise
                if isinstance(error, (ModVersionOverrideError, OSError)):
                    raise HuroshikiError(str(error)) from error
                raise
            if changes:
                self._version_override_mutated = True
                self._record_source_mutation(intent_only=True)
            elif not self._source_mutation_recorded:
                self._intent_only_mutation = True
            return ModVersionIntentPreview(
                identity=status.identity,
                installed_artifact_id=status.installed_artifact_id,
                selected_artifact_id=updated.artifact_id,
                old_selection="user",
                new_selection="user",
                old_locked=status.locked,
                new_locked=updated.locked,
                reason=updated.reason,
                override_status=status.override_status,
                changes=changes,
            )

    def set_mod_version_pin(
        self,
        identity: str,
        *,
        locked: bool = True,
        reason: str | None = None,
    ) -> ModVersionOverride:
        """Compatibility API returning the resulting override record."""
        preview = self.prepare_mod_version_pin(
            identity, locked=locked, reason=reason
        )
        override = get_mod_version_override(self.source, preview.identity)
        if override is None:
            raise HuroshikiError("Version intent changed while preparing preview")
        return override

    def _record_source_mutation(self, *, intent_only: bool = False) -> None:
        """Invalidate accepted exact evidence after a successful staged mutation."""
        had_accepted = self._accepted_exact_evidence is not None
        self._mutation_generation += 1
        if had_accepted:
            self._exact_verification_required = True
        self._accepted_exact_evidence = None
        self._accepted_exact_stages.clear()
        self._rollback_source_digest = None
        self._intent_only_mutation = (
            intent_only
            if not self._source_mutation_recorded
            else self._intent_only_mutation and intent_only
        )
        self._source_mutation_recorded = True

    def _restore_exact_selection_source(
        self,
        checkpoint: Path,
        failed_source: Path,
        *,
        expected_digest: tuple[tuple[Path, str], ...],
    ) -> None:
        if not checkpoint.is_dir() or checkpoint.is_symlink():
            raise HuroshikiError(
                "Exact MOD selection checkpoint is missing; transaction state retained"
            )
        if failed_source.exists():
            raise HuroshikiError(
                "Exact MOD selection rollback path already exists; transaction state retained"
            )
        checkpoint_fd, checkpoint_metadata = _open_pinned_source(checkpoint)
        try:
            checkpoint_issues = packctl.pack_source_fd_entry_issues(checkpoint_fd)
            if checkpoint_issues:
                details = "; ".join(
                    f"{relative}: {message}"
                    for relative, message in checkpoint_issues
                )
                raise HuroshikiError(
                    f"Unsafe exact MOD selection checkpoint: {details}"
                )
            if tuple(sorted(_source_fd_snapshot(checkpoint_fd).items())) != expected_digest:
                raise HuroshikiError(
                    "Exact MOD selection checkpoint changed; transaction state retained"
                )
            current_checkpoint = os.stat(checkpoint, follow_symlinks=False)
            if not _same_entry(checkpoint_metadata, current_checkpoint):
                raise HuroshikiError(
                    "Exact MOD selection checkpoint was replaced; transaction state retained"
                )
            if self.source.exists():
                self.source.rename(failed_source)
            try:
                checkpoint.rename(self.source)
            except BaseException as error:
                if failed_source.exists() and not self.source.exists():
                    failed_source.rename(self.source)
                raise HuroshikiError(
                    "Could not restore exact MOD selection checkpoint; transaction retained"
                ) from error
            installed = os.stat(self.source, follow_symlinks=False)
            if not _same_entry(checkpoint_metadata, installed):
                suspicious = self.root / f"retained-replaced-exact-checkpoint-{uuid4().hex}"
                try:
                    self.source.rename(suspicious)
                    failed_source.rename(self.source)
                except BaseException as restore_error:
                    raise HuroshikiError(
                        "Exact MOD checkpoint identity changed during rollback and the "
                        f"original source could not be restored; state retained at {self.root}"
                    ) from restore_error
                raise HuroshikiError(
                    "Exact MOD checkpoint identity changed during rollback; original "
                    f"source restored and replacement retained at {suspicious}"
                )
            self._rollback_source_digest = expected_digest
        finally:
            os.close(checkpoint_fd)

    def _restore_exact_selection_checkpoint(self) -> None:
        checkpoint = self._exact_selection_checkpoint
        failed_source = self._exact_selection_failed_source
        if checkpoint is None:
            return
        if failed_source is None:
            raise HuroshikiError(
                "Exact MOD selection rollback path is missing; transaction state retained"
            )
        evidence = self._pending_exact_evidence
        if evidence is None:
            raise HuroshikiError(
                "Exact MOD selection rollback evidence is missing; transaction state retained"
            )
        self._restore_exact_selection_source(
            checkpoint,
            failed_source,
            expected_digest=evidence.checkpoint_digest,
        )
        self._exact_selection_prepared = False
        self._pending_exact_evidence = None
        self._exact_selection_checkpoint = None
        self._exact_selection_failed_source = None

    def _validate_exact_selection_stage(self, evidence: ExactStageEvidence) -> None:
        if evidence.mutation_generation != self._mutation_generation:
            raise HuroshikiError(
                "Exact MOD verification evidence is stale after a staged mutation"
            )
        ensure_safe_pack_source(self.source)
        if _exact_source_digest(self.source) != evidence.source_digest:
            raise HuroshikiError(
                "The exact MOD selection staging changed after preview; apply aborted"
            )
        if _exact_manifest_bytes(self.source) != evidence.manifest:
            raise HuroshikiError(
                "The exact MOD root manifest changed after preview; apply aborted"
            )
        if packctl.project_versions(self.source) != evidence.versions:
            raise HuroshikiError(
                "The exact MOD Minecraft or loader version changed after preview; "
                "apply aborted"
            )
        records = _exact_metadata_records(self.source)
        if _exact_metadata_identity_snapshot(records) != evidence.metadata_identities:
            raise HuroshikiError(
                "Exact MOD metadata identities changed after verification; apply aborted"
            )
        overrides = _validate_mod_version_override_records(self.source, records)
        if _exact_override_identity_snapshot(overrides) != evidence.accepted_identities:
            raise HuroshikiError(
                "Exact MOD accepted selections changed after verification; apply aborted"
            )
        selection = evidence.selection
        matches = records.get(selection.identity, ())
        if len(matches) != 1:
            raise HuroshikiError(
                f"Exact MOD selection target is no longer unique: "
                f"{selection.identity_label}"
            )
        relative, contents, _mod = matches[0]
        try:
            identity = parse_provider_metadata(relative, contents)
        except Exception as error:
            raise HuroshikiError(
                f"Exact MOD selection target metadata is invalid after preview: {error}"
            ) from error
        if (
            identity.provider != selection.provider
            or identity.project_id != selection.project_id
            or identity.file_id != selection.artifact_id
        ):
            raise HuroshikiError(
                f"Exact MOD selection target changed after preview: "
                f"{selection.identity_label} artifact "
                f"{identity.file_id or '<missing>'}"
            )
        _exact_assert_root_manifest_identities(self.source, records)
        if set(identity for identity, _owners in evidence.reachability) != set(records):
            raise HuroshikiError(
                "Exact MOD reachability evidence is incomplete; apply aborted"
            )
        if (
            _exact_verification_binding_digest(evidence.verifications, records)
            != evidence.verification_digest
        ):
            raise HuroshikiError(
                "Exact MOD semantic verification evidence changed after preview; apply aborted"
            )

    def accept_exact_mod_version(self) -> None:
        """Accept a pending exact preview without publishing the Pack transaction."""
        with self._lifecycle_lock:
            with self._lock:
                self.ensure_active()
                if self._operation is not None:
                    raise HuroshikiError(
                        "Wait for the active exact version operation to finish"
                    )
                evidence = self._pending_exact_evidence
                checkpoint = self._exact_selection_checkpoint
                if not self._exact_selection_prepared or evidence is None or checkpoint is None:
                    raise HuroshikiError("No exact MOD version preview is prepared")
                self._validate_exact_selection_stage(evidence)
                prior_evidence = self._accepted_exact_evidence
                prior_override_mutated = self._version_override_mutated
                prior_verification_required = self._exact_verification_required
                self._mutation_generation += 1
                accepted = replace(
                    evidence, mutation_generation=self._mutation_generation
                )
                self._accepted_exact_stages.append(
                    AcceptedExactStage(
                        accepted,
                        checkpoint,
                        prior_evidence,
                        prior_override_mutated,
                        prior_verification_required,
                    )
                )
                self._accepted_exact_evidence = accepted
                self._version_override_mutated = True
                self._exact_verification_required = False
                self._pending_exact_evidence = None
                self._exact_selection_prepared = False
                self._exact_selection_checkpoint = None
                self._exact_selection_failed_source = None

    def _record_equivalence_process_result(
        self, result: BoundedProcessResult
    ) -> None:
        if result.termination_incomplete:
            self._equivalence_process_results.append(result)

    def _retry_equivalence_process_cleanup(self, deadline: float) -> None:
        remaining: list[BoundedProcessResult] = []
        for result in self._equivalence_process_results:
            if result.process_group is None:
                remaining.append(result)
                continue
            cleanup = stop_resolver_process_group(
                result.process_group,
                parent=result.parent_process,
                cleanup_deadline=deadline,
            )
            if not (cleanup.group_drained and cleanup.parent_reaped):
                remaining.append(result)
        self._equivalence_process_results = remaining
        if remaining:
            raise TransactionDiscardIntegrityError(
                "Dependency-equivalence process-group cleanup was incomplete"
            )

    def begin_add(
        self,
        provider: str,
        query: str,
        *,
        client: bool,
        server: bool,
        on_event: Callable[[ParserEvent], None] | None = None,
        deadline: float | None = None,
    ) -> PackwizAddOperation:
        side_from_flags(client, server)
        with self._lock:
            self.ensure_active()
            self._ensure_exact_selection_not_prepared()
            if self._operation is not None:
                raise HuroshikiError("Another Packwiz search is already running")
            operation = PackwizAddOperation(
                self,
                provider,
                query,
                client=client,
                server=server,
                on_event=on_event,
                deadline=deadline,
            )
            self._operation = operation
            return operation

    def begin_resolved_add(
        self,
        *,
        provider: str,
        selector: str,
        canonical_project_id: str | None,
        side: str,
        deadline: float | None = None,
    ) -> ResolvedAddOperation:
        with self._lock:
            self.ensure_active()
            self._ensure_exact_selection_not_prepared()
            if self._operation is not None:
                raise HuroshikiError("Another add operation is already running")
            operation = ResolvedAddOperation(
                self,
                provider=provider,
                selector=selector,
                canonical_project_id=canonical_project_id,
                side=side,
                deadline=deadline,
            )
            self._operation = operation
            return operation

    def fail_operation_start(
        self,
        operation: _AddOperationLifecycle,
        error: BaseException,
        *,
        cancelled: bool = False,
    ) -> bool:
        if not operation._claim_prestart_abort():
            return False
        cleanup_errors: list[BaseException] = []
        with self._lock:
            if self._operation is operation:
                try:
                    self._retain_add_operation_paths(operation)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                self._operation = None
            else:
                cleanup_errors.append(
                    HuroshikiError("Add operation ownership changed before worker start")
                )
        operation.cancelled = operation.cancelled or cancelled
        if cleanup_errors:
            operation.cleanup_error = cleanup_errors[0]
        operation._complete(operation._error_result(error))
        return True

    @staticmethod
    def _retain_add_operation_paths(
        operation: PackwizAddOperation | ResolvedAddOperation,
    ) -> None:
        if operation.resolver_root.exists():
            operation.resolver_root.rename(operation.retained_resolver_root)
        if operation.checkpoint.exists():
            operation.checkpoint.rename(operation.retained_checkpoint)

    @staticmethod
    def _add_termination_incomplete(
        operation: PackwizAddOperation | ResolvedAddOperation,
    ) -> bool:
        termination = getattr(operation, "termination_result", None)
        return (
            getattr(operation, "termination_incomplete", False)
            or getattr(operation, "resolver_termination_incomplete", False)
            or (
                termination is not None
                and not (termination.group_drained and termination.parent_reaped)
            )
        )

    def _release_add_cleanup_ownership(
        self,
        operation: PackwizAddOperation | ResolvedAddOperation,
    ) -> None:
        with self._lock:
            if (
                self._operation is operation
                and operation.done.is_set()
                and operation.cleanup_error is None
                and not self._add_termination_incomplete(operation)
            ):
                self._operation = None

    def add(self, provider: str, query: str) -> subprocess.CompletedProcess[str]:
        """Compatibility path for synchronous add callers."""
        with self._lock:
            return self._add(provider, query)

    def _add(self, provider: str, query: str) -> subprocess.CompletedProcess[str]:
        self.ensure_active()
        self._ensure_exact_selection_not_prepared()
        if self._operation is not None:
            raise HuroshikiError("Wait for the active transaction operation to finish")
        provider, query = normalize_add_selector(provider, query)
        if provider == "url":
            operation = self.begin_add(
                provider,
                query,
                client=True,
                server=True,
            )
            result = operation.run()
            return subprocess.CompletedProcess(
                ["huroshiki", "url", "add", query],
                result.returncode,
                "",
                "" if result.success else result.message,
            )
        try:
            self._ensure_empty_pack_root_manifest()
            minecraft, loader, loader_version = packctl.project_versions(self.source)
            closure = resolve_mod_closure(
                provider=provider,
                selector=query,
                minecraft=minecraft,
                loader=loader,
                loader_version=loader_version,
                resolver_root=self.root / f"resolver-{uuid4().hex}",
                process_result_callback=self._record_equivalence_process_result,
                diagnostic_project_id=self.project_key.partition(":")[2],
            )
            overrides = self._validated_version_overrides()
            changed = merge_metadata_closure(
                self.source,
                closure,
                requested_side="both",
                equivalence_workspace=self.root / "equivalence",
                process_result_callback=self._record_equivalence_process_result,
            )
            self._assert_version_overrides_preserved(overrides)
        except Exception as error:
            return subprocess.CompletedProcess(
                build_add_command(provider, query), 1, "", str(error)
            )
        self.batches.append(
            TransactionBatch(
                provider=provider,
                query=query,
                changed_files=changed,
                root_identity=closure.root_identity,
                closure_identities=tuple(
                    sorted(item.identity for item in closure.metadata)
                ),
            )
        )
        self._record_source_mutation()
        return subprocess.CompletedProcess(build_add_command(provider, query), 0, "", "")

    def add_mod_transactionally(
        self,
        provider: str,
        selector: str,
        side: str,
        *,
        artifact_id: str | None = None,
    ) -> int:
        """Run one synchronous add entirely in staging, then atomically apply it."""
        with self._lock:
            return self._add_mod_transactionally(
                provider,
                selector,
                side,
                artifact_id=artifact_id,
            )

    def _add_mod_transactionally(
        self,
        provider: str,
        selector: str,
        side: str,
        *,
        artifact_id: str | None,
    ) -> int:
        self.ensure_active()
        self._ensure_exact_selection_not_prepared()
        if self._operation is not None:
            raise HuroshikiError("Wait for the active transaction operation to finish")
        kind, _ = split_project_key(self.project_key)
        if kind != "pack":
            raise HuroshikiError("Synchronous add can only modify MODPACK projects")
        try:
            normalized_side = packctl.normalize_side(side)
        except packctl.ConfigError as error:
            raise HuroshikiError(str(error)) from error
        provider, selector = normalize_add_selector(provider, selector)
        if artifact_id is not None and provider == "url":
            raise HuroshikiError(
                "Exact artifact selection is unavailable for self-hosted URL MODs"
            )
        ensure_safe_pack_source(self.source)

        if provider == "url":
            client, server = flags_from_side(normalized_side)
            result = self.begin_add(
                provider,
                selector,
                client=client,
                server=server,
            ).run()
            if not result.success:
                if result.returncode == 0:
                    raise HuroshikiError(result.message)
                return result.returncode or 1
        else:
            self._ensure_empty_pack_root_manifest()
            minecraft, loader, loader_version = packctl.project_versions(self.source)
            closure = resolve_mod_closure(
                provider=provider,
                selector=selector,
                minecraft=minecraft,
                loader=loader,
                loader_version=loader_version,
                resolver_root=self.root / f"resolver-{uuid4().hex}",
                process_result_callback=self._record_equivalence_process_result,
                diagnostic_project_id=self.project_key.partition(":")[2],
            )
            overrides = self._validated_version_overrides()
            try:
                changed = merge_metadata_closure(
                    self.source,
                    closure,
                    requested_side=normalized_side,
                    equivalence_workspace=self.root / "equivalence",
                    process_result_callback=self._record_equivalence_process_result,
                )
            except Exception as error:
                raise HuroshikiError(f"Could not merge resolved MOD closure: {error}") from error
            self._assert_version_overrides_preserved(overrides)
            self.batches.append(
                TransactionBatch(
                    provider=provider,
                    query=selector,
                    changed_files=changed,
                    root_identity=closure.root_identity,
                    closure_identities=tuple(
                        sorted(item.identity for item in closure.metadata)
                    ),
                )
            )
            self._record_source_mutation()

            if artifact_id is not None:
                root_provider, root_project_id = closure.root_identity
                selected_artifact_id = artifact_id
                if root_provider == "modrinth":
                    root_project_id = canonical_modrinth_id(
                        root_project_id, "Modrinth project ID"
                    )
                    selected_artifact_id = canonical_modrinth_id(
                        artifact_id, "Modrinth version ID"
                    )
                self.prepare_exact_mod_version(
                    ExactModArtifactSelection(
                        root_provider,
                        root_project_id,
                        selected_artifact_id,
                    )
                )

        self.apply()
        return 0

    def _finish_add(
        self,
        operation: PackwizAddOperation,
        pty_result: PtyResult,
    ) -> AddOperationResult:
        with self._lock:
            operation._checkpoint()
            if not self.active or self._operation is not operation:
                self._rollback_add(operation)
                if pty_result.termination_incomplete:
                    self._operation = operation
                return AddOperationResult(
                    returncode=pty_result.returncode or 1,
                    changed_files=(),
                    raw_log=pty_result.raw_log,
                    text_log=pty_result.text_log,
                    event_log=pty_result.event_log,
                    message="Transaction was closed before Packwiz completed",
                    cancelled=True,
                )

            ensure_safe_pack_source(self.source)
            operation._checkpoint()
            if pty_result.termination_incomplete:
                self._rollback_add(operation)
                self._operation = operation
                return AddOperationResult(
                    returncode=pty_result.returncode or 1,
                    changed_files=(),
                    raw_log=pty_result.raw_log,
                    text_log=pty_result.text_log,
                    event_log=pty_result.event_log,
                    message="Packwiz PTY process termination was incomplete",
                    cancelled=pty_result.cancelled,
                )
            if pty_result.orphaned_descendants:
                self._rollback_add(operation)
                return AddOperationResult(
                    returncode=pty_result.returncode or 1,
                    changed_files=(),
                    raw_log=pty_result.raw_log,
                    text_log=pty_result.text_log,
                    event_log=pty_result.event_log,
                    message="Packwiz PTY left background processes after completion",
                    cancelled=pty_result.cancelled,
                )
            if pty_result.returncode != 0:
                self._rollback_add(operation)
                return AddOperationResult(
                    returncode=pty_result.returncode,
                    changed_files=(),
                    raw_log=pty_result.raw_log,
                    text_log=pty_result.text_log,
                    event_log=pty_result.event_log,
                    message="Packwiz was cancelled or failed",
                    cancelled=operation.cancelled,
                )

            operation._checkpoint()
            ensure_safe_pack_source(operation.resolver_source)
            probe_metadata = _read_resolver_metadata(operation.resolver_source)
            operation._checkpoint()
            if operation.provider == "curseforge":
                if len(probe_metadata) != 1:
                    raise HuroshikiError(
                        "CurseForge identity probe must produce exactly one root metadata file"
                    )
                probe_root = probe_metadata[0]
                if probe_root.provider != "curseforge":
                    raise HuroshikiError(
                        "CurseForge identity probe produced non-CurseForge metadata"
                    )
                project_id = canonical_curseforge_project_id(probe_root.project_id)
                if operation.query.isdecimal() and project_id != operation.query:
                    raise HuroshikiError(
                        "CurseForge identity probe returned a different project ID"
                    )
                if operation.on_event is not None:
                    operation.on_event(
                        ParserEvent(
                            "identity_verified",
                            f"Verified CurseForge project ID {project_id}",
                        )
                    )
                operation._checkpoint()
                closure = resolve_mod_closure(
                    provider="curseforge",
                    selector=project_id,
                    canonical_project_id=project_id,
                    minecraft=operation.minecraft,
                    loader=operation.loader,
                    loader_version=operation.loader_version,
                    cancel_event=operation.cancel_event,
                    deadline=operation.deadline,
                    resolver_root=operation.closure_resolver_root,
                    process_result_callback=operation._record_resolver_process_result,
                )
                ensure_safe_pack_source(operation.closure_resolver_root / "source")
            else:
                selected = operation.selection or operation.query
                project_id = resolve_project_selector(
                    operation.provider,
                    selected,
                    cancel_event=operation.cancel_event,
                    deadline=operation.deadline,
                ).canonical_project_id
                operation._checkpoint()
                if project_id is None:
                    raise HuroshikiError(
                        "Selected Packwiz result has no canonical project ID; "
                        "retry with an explicit provider project ID"
                    )
                root_identity = resolved_root_identity(
                    operation.provider, project_id, probe_metadata
                )
                closure = ResolvedModClosure(root_identity, probe_metadata)
            operation._checkpoint()
            self._ensure_empty_pack_root_manifest()
            overrides = self._validated_version_overrides()
            changed = merge_metadata_closure(
                self.source,
                closure,
                requested_side=side_from_flags(operation.client, operation.server),
                cancel_event=operation.cancel_event,
                deadline=operation.deadline,
                equivalence_workspace=operation.resolver_root / "equivalence",
                process_result_callback=operation._record_resolver_process_result,
            )
            self._assert_version_overrides_preserved(overrides)
            operation._checkpoint()

            batch = TransactionBatch(
                provider=operation.provider,
                query=operation.query,
                changed_files=changed,
                root_identity=closure.root_identity,
                closure_identities=tuple(
                    sorted(item.identity for item in closure.metadata)
                ),
            )
            result = AddOperationResult(
                returncode=0,
                changed_files=changed,
                raw_log=pty_result.raw_log,
                text_log=pty_result.text_log,
                event_log=pty_result.event_log,
                message=f"Staged {len(changed)} metadata file(s)",
            )
            operation._pending_batch = batch
            self.batches.append(batch)
            self._retain_add_operation_paths(operation)
            operation._checkpoint_complete = False
            operation._pending_batch = None
            self._operation = None
            self._record_source_mutation()
            return result

    def _finish_url_add(
        self,
        operation: PackwizAddOperation,
    ) -> AddOperationResult:
        operation._checkpoint()
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise AddOperationDeadlineExceeded(
                "Install operation deadline exceeded"
            )
        artifact = download_url_artifact(
            operation.query,
            operation.cancel_event,
            operation.log_dir,
            operation.loader,
            project_url_max_jar_size_bytes(self.project_key),
            allow_private_networks=project_url_allow_private_networks(
                self.project_key
            ),
            total_timeout_seconds=min(
                DEFAULT_URL_TOTAL_TIMEOUT_SECONDS,
                remaining,
            ),
        )
        operation._checkpoint()

        with self._lock:
            if not self.active or self._operation is not operation:
                raise HuroshikiError(
                    "Transaction was closed before the URL download completed"
                )
            operation._checkpoint()
            relative_path = Path("mods") / f"{artifact.mod_id}.pw.toml"
            write_url_metadata(
                operation.resolver_source,
                relative_path,
                artifact,
                "both",
            )
            metadata = _read_resolver_metadata(operation.resolver_source)
            identity = ("url", artifact.mod_id)
            operation._checkpoint()
            overrides = self._validated_version_overrides()
            changed = merge_metadata_closure(
                self.source,
                ResolvedModClosure(identity, metadata),
                requested_side=side_from_flags(operation.client, operation.server),
            )
            self._assert_version_overrides_preserved(overrides)
            operation._checkpoint()
            if not changed:
                raise HuroshikiError("The URL metadata is already current")
            batch = TransactionBatch(
                provider="url",
                query=operation.query,
                changed_files=changed,
            )
            raw_log, text_log, event_log = url_log_paths(operation.log_dir)
            version = f" {artifact.version}" if artifact.version else ""
            result = AddOperationResult(
                returncode=0,
                changed_files=changed,
                raw_log=raw_log,
                text_log=text_log,
                event_log=event_log,
                message=f"Staged {artifact.name}{version} from self-hosted URL",
            )
            operation._pending_batch = batch
            self.batches.append(batch)
            self._retain_add_operation_paths(operation)
            operation._checkpoint_complete = False
            operation._pending_batch = None
            self._operation = None
            self._record_source_mutation()
        return result

    def _rollback_add(
        self, operation: PackwizAddOperation | ResolvedAddOperation
    ) -> None:
        cleanup_errors: list[BaseException] = []
        with self._lock:
            if operation._pending_batch is not None:
                self.batches = [
                    batch
                    for batch in self.batches
                    if batch is not operation._pending_batch
                ]
                operation._pending_batch = None
            if operation.checkpoint.exists():
                if operation._checkpoint_complete:
                    try:
                        self.source.rename(operation.retained_failed_source)
                        try:
                            operation.checkpoint.rename(self.source)
                        except BaseException:
                            operation.retained_failed_source.rename(self.source)
                            raise
                        operation._checkpoint_complete = False
                    except BaseException as error:
                        cleanup_errors.append(error)
                else:
                    try:
                        operation.checkpoint.rename(operation.retained_checkpoint)
                    except BaseException as error:
                        cleanup_errors.append(error)
            try:
                if operation.resolver_root.exists():
                    operation.resolver_root.rename(operation.retained_resolver_root)
            except BaseException as error:
                cleanup_errors.append(error)
            if (
                not cleanup_errors
                and self._operation is operation
                and not self._add_termination_incomplete(operation)
            ):
                self._operation = None
        if cleanup_errors:
            raise HuroshikiError(
                f"Could not restore add operation checkpoint: {cleanup_errors[0]}"
            ) from cleanup_errors[0]

    def staged_mods(self) -> list[ModInfo]:
        self.ensure_active()
        current = metadata_digest_snapshot(self.source)
        paths = sorted(changed_paths(self.baseline, current))
        return [
            read_mod(self.source, path)
            for path in paths
            if (self.source / path).exists()
        ]

    def staged_removed_mods(self) -> list[ModInfo]:
        """Return baseline MODs removed from the current staged source."""
        self.ensure_active()
        current = metadata_digest_snapshot(self.source)
        removed: list[ModInfo] = []
        for path in sorted(changed_paths(self.baseline, current)):
            if (self.source / path).exists():
                continue
            contents = self.baseline_contents.get(path)
            if contents is None:
                continue
            removed.append(
                read_mod_data(path, tomllib.loads(contents.decode("utf-8")))
            )
        return removed

    def staged_exact_mod_targets(self) -> list[StagedExactModTarget]:
        """List exact-selectable artifacts with the strongest known reachability."""
        self.ensure_active()
        if self._exact_selection_prepared:
            return []
        root_identities = {
            (root.provider, root.project_id)
            for root in read_pack_root_manifest(self.source)
        }
        accepted = self._accepted_exact_evidence
        if accepted is not None:
            if (
                accepted.mutation_generation != self._mutation_generation
                or _exact_source_digest(self.source) != accepted.source_digest
            ):
                return []
            reachability = dict(accepted.reachability)
            targets = [
                StagedExactModTarget(
                    mod,
                    "root" if identity in root_identities else "dependency",
                    tuple(reachability.get(identity, ())),
                    True,
                )
                for mod in list_mods_from_source(self.source)
                if (provider := canonical_provider(mod.provider))
                in {"modrinth", "curseforge"}
                and (identity := (provider, mod.project_id)) in reachability
            ]
            return sorted(
                targets,
                key=lambda item: (
                    0 if item.role == "root" else 1,
                    item.mod.name.casefold(),
                    canonical_provider(item.mod.provider),
                    item.mod.project_id,
                ),
            )
        active_batches = [
            batch
            for batch in self.batches
            if batch.root_identity is not None
            and batch.root_identity in root_identities
        ]
        closure_identities = {
            identity
            for batch in active_batches
            for identity in batch.closure_identities
        }
        if not closure_identities:
            return []
        requiring_roots: dict[tuple[str, str], set[str]] = {}
        for batch in active_batches:
            assert batch.root_identity is not None
            root_label = f"{batch.root_identity[0]}:{batch.root_identity[1]}"
            for identity in batch.closure_identities:
                if identity != batch.root_identity:
                    requiring_roots.setdefault(identity, set()).add(root_label)
        targets: list[StagedExactModTarget] = []
        for mod in list_mods_from_source(self.source):
            provider = canonical_provider(mod.provider)
            identity = (provider, mod.project_id)
            if provider not in {"modrinth", "curseforge"}:
                continue
            if identity not in closure_identities:
                continue
            role: Literal["root", "dependency"] = (
                "root" if identity in root_identities else "dependency"
            )
            targets.append(
                StagedExactModTarget(
                    mod,
                    role,
                    tuple(sorted(requiring_roots.get(identity, ()))),
                    False,
                )
            )
        return sorted(
            targets,
            key=lambda item: (
                0 if item.role == "root" else 1,
                item.mod.name.casefold(),
                canonical_provider(item.mod.provider),
                item.mod.project_id,
            ),
        )

    def set_side(self, relative_path: Path, client: bool, server: bool) -> None:
        with self._lock:
            self.ensure_active()
            self._ensure_exact_selection_not_prepared()
            if self._operation is not None:
                raise HuroshikiError("Wait for the active add operation to finish")
            side = side_from_flags(client, server)
            path = safe_child(self.source, relative_path)
            if not path.is_file() or not path.name.endswith(".pw.toml"):
                raise HuroshikiError(f"Unknown metadata file: {relative_path}")
            manifest = self.source / ".huroshiki-roots.json"
            original = path.read_bytes()
            root_identity: tuple[str, str] | None = None
            if manifest.is_file() and not manifest.is_symlink():
                mod = read_mod(self.source, relative_path)
                identity = f"{canonical_provider(mod.provider)}:{mod.project_id}"
                if any(
                    root.canonical_identity == identity
                    for root in read_pack_root_manifest(self.source)
                ):
                    root_identity = canonical_provider(mod.provider), mod.project_id
            packctl.set_side_file(path, side)
            if root_identity is not None:
                try:
                    record_pack_root(
                        self.source, root_identity[0], root_identity[1], side
                    )
                except BaseException:
                    path.write_bytes(original)
                    raise
            self._record_source_mutation()

    def unstage(self, relative_path: Path) -> None:
        """Remove one selected metadata change from the transaction.

        Newly added metadata is deleted. Metadata that existed when the
        transaction started is restored byte-for-byte to its original state.
        index.toml and pack.toml are reconciled by the normal refresh performed
        when a MODPACK transaction is applied.
        """
        self.ensure_active()
        with self._lock:
            self._ensure_exact_selection_not_prepared()
            if self._operation is not None:
                raise HuroshikiError(
                    "Wait for the active add operation to finish"
                )

            current = metadata_digest_snapshot(self.source)
            if relative_path not in changed_paths(self.baseline, current):
                raise HuroshikiError(
                    f"Metadata is not staged: {relative_path}"
                )

            path = safe_child(self.source, relative_path)
            if not path.is_file() or not path.name.endswith(".pw.toml"):
                raise HuroshikiError(
                    f"Unknown staged metadata file: {relative_path}"
                )

            current_mod = read_mod(self.source, relative_path)
            current_identity = (
                f"{canonical_provider(current_mod.provider)}:{current_mod.project_id}"
            )
            current_contents = path.read_bytes()
            manifest = self.source / ".huroshiki-roots.json"
            original_roots: tuple[PackRootRecord, ...] | None = None
            updated_roots: tuple[PackRootRecord, ...] | None = None
            if manifest.is_file() and not manifest.is_symlink():
                original_roots = read_pack_root_manifest(self.source)
                roots = {root.canonical_identity: root for root in original_roots}
                roots.pop(current_identity, None)
                original = self.baseline_contents.get(relative_path)
                if original is not None:
                    restored = parse_provider_metadata(relative_path, original)
                    baseline_roots = {
                        root.canonical_identity: root
                        for root in self.root_manifest_baseline
                    }
                    if restored.canonical_identity in baseline_roots:
                        baseline_root = baseline_roots[restored.canonical_identity]
                        roots[restored.canonical_identity] = PackRootRecord(
                            restored.provider,
                            restored.project_id,
                            baseline_root.side,
                        )
                updated_roots = tuple(roots.values())
            if relative_path in self.baseline_contents:
                temporary = path.with_name(
                    f".{path.name}.huroshiki-unstage-{uuid4().hex}"
                )
                temporary.write_bytes(self.baseline_contents[relative_path])
                temporary.replace(path)
            else:
                path.unlink()
                current_parent = path.parent
                while current_parent != self.source:
                    try:
                        current_parent.rmdir()
                    except OSError:
                        break
                    current_parent = current_parent.parent
            if updated_roots is not None:
                try:
                    write_pack_root_manifest(self.source, updated_roots)
                except BaseException:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(current_contents)
                    assert original_roots is not None
                    write_pack_root_manifest(self.source, original_roots)
                    raise

            remaining_batches: list[TransactionBatch] = []
            for batch in self.batches:
                remaining = tuple(
                    item for item in batch.changed_files
                    if item != relative_path
                )
                if remaining:
                    remaining_batches.append(
                        TransactionBatch(
                            provider=batch.provider,
                            query=batch.query,
                            changed_files=remaining,
                            root_identity=batch.root_identity,
                            closure_identities=batch.closure_identities,
                        )
                    )
            self.batches = remaining_batches
            self._record_source_mutation()

    def prepare_exact_mod_version(
        self,
        selection: ExactModArtifactSelection,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
        progress: Callable[[ModVersionSelectionProgress], None] | None = None,
    ) -> ModVersionSelectionPreview:
        operation_cancel = cancel_event or threading.Event()
        operation_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + UPDATE_OPERATION_TIMEOUT_SECONDS
        )
        operation = ExactModVersionOperation(operation_cancel, operation_deadline)
        self._lifecycle_lock.acquire()
        try:
            with self._lock:
                self.ensure_active()
                if self._operation is not None:
                    raise HuroshikiError(
                        "Wait for the active transaction operation to finish"
                    )
                self._operation = operation
            return self._prepare_exact_mod_version(
                selection,
                progress=progress,
                operation_owner=operation,
            )
        finally:
            with self._lock:
                if self._operation is operation and operation.cleanup_error is None:
                    self._operation = None
                operation.done.set()
            self._lifecycle_lock.release()

    def _prepare_exact_mod_version(
        self,
        selection: ExactModArtifactSelection,
        *,
        progress: Callable[[ModVersionSelectionProgress], None] | None,
        operation_owner: ExactModVersionOperation,
    ) -> ModVersionSelectionPreview:
        """Stage one exact provider artifact without publishing the real Pack."""
        if not isinstance(selection, ExactModArtifactSelection):
            raise HuroshikiError("Exact MOD artifact selection has an invalid type")
        operation_cancel = operation_owner.cancel_event
        operation_deadline = operation_owner.deadline

        def checkpoint() -> None:
            with self._lock:
                owned = self.active and self._operation is operation_owner
            if not owned:
                raise ExactModVersionCancelled(
                    "Exact MOD version selection lost transaction ownership"
                )
            if operation_cancel.is_set():
                raise ExactModVersionCancelled(
                    "Exact MOD version selection was cancelled"
                )
            if time.monotonic() >= operation_deadline:
                raise ExactModVersionDeadlineExceeded(
                    "Exact MOD version selection deadline exceeded"
                )

        def emit(
            phase: Literal[
                "validating",
                "checkpointing",
                "resolving",
                "verifying-root",
                "verifying-dependencies",
                "materializing",
                "merging",
                "complete",
            ],
            message: str,
        ) -> None:
            if progress is not None:
                progress(ModVersionSelectionProgress(phase, message))

        def record_process_result(result: BoundedProcessResult) -> None:
            if result.termination_incomplete or result.orphaned_descendants:
                operation_owner.termination_incomplete = True
            self._record_equivalence_process_result(result)

        diagnostic_messages: list[str] = []

        def record_diagnostic(message: str) -> None:
            diagnostic_messages.append(message)
            emit("resolving", message)

        self.ensure_active()
        kind, project_id = split_project_key(self.project_key)
        if kind != "pack":
            raise HuroshikiError(
                "Exact MOD artifact selection is available only for packs"
            )
        with self._lock:
            self.ensure_active()
            if self._operation is not None:
                if self._operation is not operation_owner:
                    raise HuroshikiError("Wait for the active transaction operation to finish")
            if self._exact_selection_prepared:
                raise HuroshikiError(
                    "An exact MOD version is already prepared; apply or discard it"
                )

        emit("validating", f"Validating {selection.identity_label}")
        checkpoint()
        ensure_safe_pack_source(self.source, checkpoint=checkpoint)
        try:
            overrides_before = read_mod_version_overrides(self.source)
        except ModVersionOverrideError as error:
            raise HuroshikiError(str(error)) from error
        baseline_records = _exact_metadata_records(self.source, checkpoint)
        _validate_mod_version_override_records(
            self.source,
            baseline_records,
            overrides=tuple(
                override
                for override in overrides_before
                if override.canonical_identity != selection.identity_label
            ),
        )
        target_records = baseline_records.get(selection.identity, ())
        if len(target_records) == 0:
            raise HuroshikiError(
                f"Exact MOD selection target is not installed: "
                f"{selection.identity_label}"
            )
        if len(target_records) > 1:
            raise HuroshikiError(
                f"Exact MOD selection target has duplicate identity: "
                f"{selection.identity_label}"
            )
        target_relative, target_contents, target_mod = target_records[0]
        if target_mod.side not in packctl.VALID_SIDES:
            raise HuroshikiError(
                f"Exact MOD selection target has invalid side: {target_relative}"
            )
        try:
            old_provider_identity = parse_provider_metadata(
                target_relative, target_contents
            )
        except Exception as error:
            raise HuroshikiError(
                f"Exact MOD selection target metadata is invalid: {error}"
            ) from error
        if old_provider_identity.file_id is None:
            raise HuroshikiError(
                f"Exact MOD selection target has no artifact ID: "
                f"{selection.identity_label}"
            )
        source_scan = scan_pack_migration_source(self.source, checkpoint=checkpoint)
        try:
            explicit_roots = extract_pack_migration_roots(
                self.source,
                expected_identity=source_scan.root_identity,
                expected_snapshot_digest=source_scan.snapshot_digest,
                checkpoint=checkpoint,
            )
        except PackMigrationRootError as error:
            raise HuroshikiError(
                f"Exact MOD selection requires authoritative root provenance: {error}"
            ) from error
        manifest_before = _exact_manifest_bytes(self.source)
        root_by_identity = {
            root.canonical_identity: root for root in explicit_roots
        }
        selected_root = root_by_identity.get(selection.identity_label)
        selected_side = (
            selected_root.source_side if selected_root is not None else target_mod.side
        )
        versions = packctl.project_versions(self.source)
        operation_before = _file_content_snapshot(self.source, checkpoint)
        operation_digest_before = tuple(
            sorted(tree_digest_snapshot(self.source, checkpoint=checkpoint).items())
        )

        operation_id = uuid4().hex
        checkpoint_source = self.root / f"exact-selection-checkpoint-{operation_id}"
        retained_checkpoint = (
            self.root / f"retained-exact-selection-checkpoint-{operation_id}"
        )
        resolver_source = self.root / f"exact-selection-resolver-{operation_id}"
        failed_source = self.root / f"failed-exact-selection-source-{operation_id}"
        equivalence_workspace = self.root / f"exact-selection-equivalence-{operation_id}"
        checkpoint_created = False

        def restore_checkpoint() -> None:
            if not checkpoint_source.exists():
                if checkpoint_created:
                    raise HuroshikiError(
                        "Exact MOD selection checkpoint is missing; transaction retained"
                    )
                return
            if failed_source.exists():
                raise HuroshikiError(
                    "Exact MOD selection failed-source retention path already exists"
                )
            self.source.rename(failed_source)
            try:
                checkpoint_source.rename(self.source)
            except BaseException as error:
                raise HuroshikiError(
                    "Could not restore exact pre-selection transaction source; "
                    f"failed source retained at {failed_source}"
                ) from error
            restored = _file_content_snapshot(self.source)
            if restored != operation_before:
                raise HuroshikiError(
                    "Restored exact pre-selection transaction source does not match "
                    "the checkpoint"
                )

        try:
            emit("checkpointing", "Creating exact-selection transaction checkpoint")
            copy_transaction_source(
                self.source,
                checkpoint_source,
                checkpoint=checkpoint,
                retained_destination=retained_checkpoint,
            )
            checkpoint_created = True
            if tuple(
                sorted(
                    tree_digest_snapshot(
                        checkpoint_source,
                        checkpoint=checkpoint,
                    ).items()
                )
            ) != operation_digest_before:
                raise HuroshikiError(
                    "Exact MOD selection checkpoint does not match the transaction source"
                )
            checkpoint()
            create_resolver_source(
                resolver_source,
                display_name=f"Resolve exact Pack {project_id}",
                minecraft=versions[0],
                loader=versions[1],
                loader_version=versions[2],
            )
            ensure_safe_pack_source(resolver_source, checkpoint=checkpoint)
            ensure_pack_root_manifest_ignored(resolver_source)
            write_pack_root_manifest(resolver_source, ())
            def resolve_root_closure(
                root: PackMigrationRoot,
                root_index: int,
                *,
                preseed_selections: Sequence[ExactModArtifactSelection] = (),
            ) -> ResolvedModClosure:
                checkpoint()
                identity = (root.provider, root.project_id)
                if root.provider == "url":
                    root_metadata = _exact_metadata_from_root(root, baseline_records)
                    return ResolvedModClosure(
                        identity,
                        (root_metadata,),
                    )
                artifact_id = (
                    selection.artifact_id
                    if identity == selection.identity
                    else root.source_file_id
                )
                if artifact_id is None:
                    raise HuroshikiError(
                        f"Exact root {root.canonical_identity} has no artifact ID"
                    )
                root_project_id = (
                    canonical_modrinth_id(root.project_id, "Modrinth project ID")
                    if root.provider == "modrinth"
                    else root.project_id
                )
                root_artifact_id = (
                    canonical_modrinth_id(artifact_id, "Modrinth version ID")
                    if root.provider == "modrinth"
                    else artifact_id
                )
                root_selection = ExactModArtifactSelection(
                    root.provider,
                    root_project_id,
                    root_artifact_id,
                )
                suffix = (
                    str(root_index)
                    if not preseed_selections
                    else f"{root_index}-constrained"
                )
                root_resolver = self.root / f"exact-selection-root-{operation_id}-{suffix}"
                create_resolver_source(
                    root_resolver,
                    display_name=f"Resolve {root.canonical_identity}",
                    minecraft=versions[0],
                    loader=versions[1],
                    loader_version=versions[2],
                )
                closure = resolve_exact_mod_closure(
                    root_selection,
                    source=root_resolver,
                    cancel_event=operation_cancel,
                    deadline=operation_deadline,
                    checkpoint=checkpoint,
                    preseed_selections=preseed_selections,
                    process_result_callback=record_process_result,
                    diagnostic_project_id=project_id,
                    diagnostic_callback=record_diagnostic,
                )
                _verify_exact_root_metadata(root_selection, closure.metadata)
                return ResolvedModClosure(
                    identity,
                    closure.metadata,
                )

            selected_dependency_roots: set[tuple[str, str]] | None = None
            root_closures: list[tuple[PackMigrationRoot, ResolvedModClosure]] = []
            emit("resolving", f"Resolving all explicit roots for Pack {project_id}")
            for root_index, root in enumerate(explicit_roots):
                root_closures.append((root, resolve_root_closure(root, root_index)))
                emit(
                    "verifying-root",
                    f"Verified root {root.canonical_identity} ({root_index + 1}/"
                    f"{len(explicit_roots)})",
                )

            initial_reachability = _exact_closure_reachability(
                root_closures, checkpoint
            )
            explicit_root_identities = {
                (root.provider, root.project_id) for root in explicit_roots
            }
            dependency_constraints: dict[
                tuple[str, str], ExactModArtifactSelection
            ] = {}
            for override in overrides_before:
                checkpoint()
                identity = (override.provider, override.project_id)
                if identity in explicit_root_identities:
                    continue
                owners = initial_reachability.get(identity)
                if not owners:
                    raise HuroshikiError(
                        "Exact MOD selection found a stale/orphan version override: "
                        f"{override.canonical_identity}"
                    )
                project_identity: str | CanonicalModrinthId = override.project_id
                artifact_identity: str | CanonicalModrinthId = override.artifact_id
                if override.provider == "modrinth":
                    project_identity = canonical_modrinth_id(
                        override.project_id, "Modrinth project ID"
                    )
                    artifact_identity = canonical_modrinth_id(
                        override.artifact_id, "Modrinth version ID"
                    )
                dependency_constraints[identity] = ExactModArtifactSelection(
                    override.provider,
                    project_identity,
                    artifact_identity,
                )
            if selected_root is None:
                initial_owners = initial_reachability.get(selection.identity)
                if not initial_owners:
                    raise HuroshikiError(
                        f"Exact dependency {selection.identity_label} is not required "
                        "by any explicit root"
                    )
                selected_dependency_roots = {
                    (root.provider, root.project_id)
                    for root in explicit_roots
                    if root.canonical_identity in initial_owners
                }
                dependency_constraints[selection.identity] = selection

            constrained_closures: list[
                tuple[PackMigrationRoot, ResolvedModClosure]
            ] = []
            for root_index, (root, closure) in enumerate(root_closures):
                checkpoint()
                preseeds = tuple(
                    dependency_constraints[identity]
                    for identity in sorted(dependency_constraints)
                    if root.canonical_identity
                    in initial_reachability[identity]
                )
                if preseeds:
                    constrained = resolve_root_closure(
                        root,
                        root_index,
                        preseed_selections=preseeds,
                    )
                    for constrained_selection in preseeds:
                        try:
                            _verify_exact_root_metadata(
                                constrained_selection, constrained.metadata
                            )
                        except HuroshikiError as error:
                            constraint_label = (
                                "Exact dependency selection"
                                if selected_root is None
                                and constrained_selection.identity == selection.identity
                                else "Existing version override constraint"
                            )
                            raise HuroshikiError(
                                f"{constraint_label} conflict for "
                                f"{constrained_selection.identity_label}: {error}"
                            ) from error
                    closure = constrained
                constrained_closures.append((root, closure))
            root_closures = constrained_closures
            resulting_reachability = _exact_closure_reachability(
                root_closures, checkpoint
            )
            for constrained_identity in dependency_constraints:
                initial_owners = initial_reachability[constrained_identity]
                resulting_owners = resulting_reachability.get(
                    constrained_identity, {}
                )
                if set(resulting_owners) != set(initial_owners):
                    label = (
                        f"{constrained_identity[0]}:{constrained_identity[1]}"
                    )
                    raise HuroshikiError(
                        f"Exact dependency selection changed ownership for {label}"
                    )

            final_reachability = _exact_closure_reachability(
                root_closures, checkpoint
            )

            emit("merging", "Building the complete explicit-root dependency graph")
            for root, closure in root_closures:
                checkpoint()
                merge_metadata_closure(
                    resolver_source,
                    closure,
                    requested_side=root.source_side,
                    preserve_resolved_dependency_sides=True,
                    explicit_root_sides={
                        (item.provider, item.project_id): item.source_side
                        for item in explicit_roots
                    },
                    exact_selected_identity=selection.identity,
                    exact_selected_side=selected_side,
                    cancel_event=operation_cancel,
                    deadline=operation_deadline,
                    equivalence_workspace=equivalence_workspace,
                    process_result_callback=record_process_result,
                )
            _merge_exact_existing_dependency_sides(
                resolver_source,
                baseline_records,
                selection.identity,
                {
                    (root.provider, root.project_id) for root in explicit_roots
                },
                checkpoint,
            )
            _preserve_exact_selected_side(
                resolver_source,
                selection,
                selected_side,
                checkpoint,
            )
            expected_roots = tuple(
                PackRootRecord(root.provider, root.project_id, root.source_side)
                for root in explicit_roots
            )
            actual_roots = read_pack_root_manifest(resolver_source)
            if actual_roots != expected_roots:
                raise HuroshikiError(
                    "Exact closure reconstruction changed explicit root provenance"
                )
            desired_records = _exact_metadata_records(resolver_source, checkpoint)
            _exact_assert_root_manifest_identities(resolver_source, desired_records)
            for override in overrides_before:
                if override.canonical_identity == selection.identity_label:
                    continue
                records = desired_records.get(
                    (override.provider, override.project_id), ()
                )
                if len(records) != 1:
                    raise HuroshikiError(
                        "Exact MOD selection result would orphan/stale an existing "
                        f"version override: {override.canonical_identity}"
                    )
                metadata = parse_provider_metadata(records[0][0], records[0][1])
                if metadata.file_id != override.artifact_id:
                    raise HuroshikiError(
                        "Exact MOD selection result would drift an existing version "
                        f"override: {override.canonical_identity}"
                    )

            if selected_root is None:
                aggregate_target = desired_records.get(selection.identity, ())
                if len(aggregate_target) != 1:
                    raise HuroshikiError(
                        f"Exact dependency {selection.identity_label} is not reachable "
                        "from the explicit roots"
                    )
                target_entry = aggregate_target[0]
                target_identity = parse_provider_metadata(target_entry[0], target_entry[1])
                if target_identity.file_id != selection.artifact_id:
                    raise HuroshikiError(
                        f"Exact dependency {selection.identity_label} is required at "
                        f"artifact {target_identity.file_id or '<missing>'}, not "
                        f"{selection.artifact_id}"
                    )

            emit(
                "materializing",
                f"Materializing and verifying {len(desired_records)} exact artifacts",
            )
            exact_verifications = _verify_exact_closure_artifacts(
                baseline_records,
                desired_records,
                selection,
                versions,
                workspace=self.root / f"exact-selection-artifacts-{operation_id}",
                context_source=self.source,
                cancel_event=operation_cancel,
                deadline=operation_deadline,
                process_result_callback=record_process_result,
                diagnostic_project_id=project_id,
                diagnostic_callback=record_diagnostic,
                selected_dependency_roots=selected_dependency_roots,
                explicit_root_identities={
                    (root.provider, root.project_id) for root in explicit_roots
                },
                opaque_url_roots={
                    (root.provider, root.project_id)
                    for root in explicit_roots
                    if root.provider == "url"
                },
                checkpoint=checkpoint,
            )

            merged_changes = _exact_metadata_changes(
                baseline_records,
                desired_records,
            )
            checkpoint()
            if _file_content_snapshot(self.source, checkpoint) != operation_before:
                raise HuroshikiError(
                    "Transaction source changed while exact MOD selection was prepared"
                )
            for change in merged_changes:
                checkpoint()
                _apply_update_change(self.source, change, use_after=True)
            existing_override = next(
                (
                    item
                    for item in overrides_before
                    if item.canonical_identity == selection.identity_label
                ),
                None,
            )
            selected_override = ModVersionOverride(
                selection.provider,
                str(selection.project_id),
                str(selection.artifact_id),
                existing_override.locked if existing_override is not None else False,
                existing_override.reason if existing_override is not None else None,
            )
            try:
                ensure_mod_version_overrides_ignored(
                    self.source, checkpoint=checkpoint
                )
                set_mod_version_override(
                    self.source, selected_override, checkpoint=checkpoint
                )
            except (ModVersionOverrideError, OSError) as error:
                raise HuroshikiError(str(error)) from error
            ensure_safe_pack_source(self.source, checkpoint=checkpoint)
            _exact_run_refresh(
                self.source,
                cancel_event=operation_cancel,
                deadline=operation_deadline,
                checkpoint=checkpoint,
                process_result_callback=record_process_result,
                diagnostic_project_id=project_id,
                diagnostic_callback=record_diagnostic,
            )
            ensure_safe_pack_source(self.source, checkpoint=checkpoint)
            if packctl.project_versions(self.source) != versions:
                raise HuroshikiError(
                    "Exact MOD staging changed the Minecraft or loader version"
                )
            if _exact_manifest_bytes(self.source) != manifest_before:
                raise HuroshikiError(
                    "Exact MOD staging changed .huroshiki-roots.json"
                )
            _exact_assert_complete_metadata_graph(
                self.source,
                desired_records,
                checkpoint,
            )
            final_records = _exact_metadata_records(self.source, checkpoint)
            _exact_assert_root_manifest_identities(self.source, final_records)
            final_root_records = final_records.get(selection.identity, ())
            if len(final_root_records) != 1:
                raise HuroshikiError(
                    f"Exact MOD staging produced an invalid target identity: "
                    f"{selection.identity_label}"
                )
            final_relative, final_contents, final_mod = final_root_records[0]
            _verify_exact_root_metadata(
                selection,
                tuple(
                    ResolvedMetadata(
                        selection.identity,
                        final_relative,
                        final_mod.filename,
                        final_contents,
                        selection.provider,
                        selection.project_id,
                    )
                    for final_relative, final_contents, final_mod in final_root_records
                ),
            )
            if selected_root is not None and final_mod.side != selected_side:
                raise HuroshikiError(
                    "Exact MOD staging did not preserve the selected root side"
                )
            final_after = _file_content_snapshot(self.source, checkpoint)
            added = tuple(
                sorted(
                    f"{identity[0]}:{identity[1]}"
                    for identity in desired_records.keys() - baseline_records.keys()
                    if identity != selection.identity
                )
            )
            removed = tuple(
                sorted(
                    f"{identity[0]}:{identity[1]}"
                    for identity in baseline_records.keys() - desired_records.keys()
                    if identity != selection.identity
                )
            )
            preview = ModVersionSelectionPreview(
                identity=selection.identity_label,
                relative_path=final_relative,
                name=final_mod.name,
                provider=selection.provider,
                old_version=metadata_version(
                    tomllib.loads(target_contents.decode("utf-8")),
                    selection.provider,
                ),
                old_artifact_id=old_provider_identity.file_id,
                new_version=metadata_version(
                    tomllib.loads(final_contents.decode("utf-8")),
                    selection.provider,
                ),
                new_artifact_id=selection.artifact_id,
                changes=tuple(
                    change
                    for change in _content_changes(operation_before, final_after)
                    if change.relative_path
                    not in {
                        Path(".packwizignore"),
                        Path(".huroshiki-version-overrides.json"),
                    }
                ),
                added_dependencies=len(added),
                removed_dependencies=len(removed),
                added_dependency_identities=added,
                removed_dependency_identities=removed,
                override_identity=selected_override.canonical_identity,
                override_artifact_id=selected_override.artifact_id,
                override_locked=selected_override.locked,
                diagnostic_messages=tuple(diagnostic_messages),
            )
            checkpoint()
            final_overrides = _validate_mod_version_override_records(
                self.source, final_records
            )
            pending_evidence = ExactStageEvidence(
                selection=selection,
                source_digest=tuple(
                    sorted(
                        tree_digest_snapshot(
                            self.source,
                            checkpoint=checkpoint,
                        ).items()
                    )
                ),
                verification_digest=_exact_verification_binding_digest(
                    exact_verifications, final_records
                ),
                verifications=exact_verifications,
                manifest=manifest_before,
                versions=versions,
                metadata_identities=_exact_metadata_identity_snapshot(final_records),
                reachability=_exact_reachability_snapshot(final_reachability),
                accepted_identities=_exact_override_identity_snapshot(final_overrides),
                mutation_generation=self._mutation_generation,
                checkpoint_digest=operation_digest_before,
            )
            self._exact_selection_prepared = True
            self._pending_exact_evidence = pending_evidence
            self._rollback_source_digest = None
            self._exact_selection_checkpoint = checkpoint_source
            self._exact_selection_failed_source = failed_source
            emit("complete", "Exact MOD version selection staged")
            return preview
        except BaseException as error:
            self._exact_selection_prepared = False
            self._pending_exact_evidence = None
            self._exact_selection_checkpoint = None
            self._exact_selection_failed_source = None
            try:
                restore_checkpoint()
            except BaseException as restore_error:
                operation_owner.cleanup_error = restore_error
                raise HuroshikiError(
                    f"{error}; exact-selection rollback failed: {restore_error}"
                ) from error
            if not operation_owner.termination_incomplete:
                if operation_cancel.is_set():
                    raise ExactModVersionCancelled(
                        "Exact MOD version selection was cancelled"
                    ) from error
                if time.monotonic() >= operation_deadline:
                    raise ExactModVersionDeadlineExceeded(
                        "Exact MOD version selection deadline exceeded"
                    ) from error
            raise

    def prepare_updates(
        self,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
        on_progress: Callable[[UpdateProgress], None] | None = None,
    ) -> list[UpdateCandidate]:
        with self._lock:
            return self._prepare_updates(
                cancel_event=cancel_event,
                deadline=deadline,
                on_progress=on_progress,
            )

    def _prepare_updates(
        self,
        *,
        cancel_event: threading.Event | None,
        deadline: float | None,
        on_progress: Callable[[UpdateProgress], None] | None,
    ) -> list[UpdateCandidate]:
        self.ensure_active()
        self._ensure_exact_selection_not_prepared()
        if self._operation is not None:
            raise HuroshikiError("Wait for the active transaction operation to finish")
        kind, _ = split_project_key(self.project_key)
        if kind != "pack":
            raise HuroshikiError(
                "Template entries resolve compatible versions during MODPACK creation"
            )
        def checkpoint() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise UpdatePreparationCancelled(
                    "Update preparation was cancelled"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise UpdatePreparationDeadlineExceeded(
                    "Update preparation operation deadline exceeded"
                )

        try:
            current_source = tree_digest_snapshot(
                self.source,
                checkpoint=checkpoint,
            )
        except UpdatePreparationDeadlineExceeded:
            current_source = None
        if (
            current_source is not None
            and current_source != self.real_source_baseline
        ):
            raise HuroshikiError(
                "Updates must be prepared before other transaction changes are staged"
            )
        candidates = _prepare_update_candidates(
            self.source,
            self.root,
            self.baseline_contents,
            cancel_event=cancel_event,
            deadline=deadline,
            on_progress=on_progress,
            process_result_callback=self._record_equivalence_process_result,
            diagnostic_project_id=self.project_key.partition(":")[2],
        )
        self.update_candidates = tuple(candidates)
        return candidates

    def select_updates(
        self,
        selected_paths: Iterable[Path],
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        with self._lock:
            self._select_updates(
                selected_paths,
                cancel_event=cancel_event,
                deadline=deadline,
            )

    def _select_updates(
        self,
        selected_paths: Iterable[Path],
        *,
        cancel_event: threading.Event | None,
        deadline: float | None,
    ) -> None:
        self.ensure_active()
        self._ensure_exact_selection_not_prepared()
        if self._operation is not None:
            raise HuroshikiError("Wait for the active transaction operation to finish")
        selected = set(selected_paths)
        available = {
            candidate.root: candidate
            for candidate in self.update_candidates
            if candidate.available
        }
        unknown = selected - available.keys()
        if unknown:
            raise HuroshikiError(
                f"Unknown update selection: {', '.join(map(str, sorted(unknown)))}"
            )
        previous_changes = self.selected_update_changes
        for change in reversed(previous_changes):
            _apply_update_change(self.source, change, use_after=False)
        try:
            merged = _merge_update_closures(
                (available[path] for path in sorted(selected)),
                source=self.source,
                workspace=self.root / "update-equivalence",
                cancel_event=cancel_event,
                deadline=(
                    deadline
                    if deadline is not None
                    else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
                ),
                process_result_callback=self._record_equivalence_process_result,
            )
        except BaseException as error:
            try:
                for change in previous_changes:
                    _apply_update_change(self.source, change, use_after=True)
            except BaseException as restore_error:
                raise HuroshikiError(
                    f"{error}; failed to restore previously selected updates: "
                    f"{restore_error}"
                ) from error
            raise
        for change in merged:
            _apply_update_change(self.source, change, use_after=True)
        self.selected_update_changes = merged
        self._record_source_mutation()

    def remove_mods(
        self,
        slugs: Iterable[str],
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> int:
        with self._lock:
            return self._remove_mods(
                slugs,
                cancel_event=cancel_event,
                deadline=deadline,
            )

    def _remove_mods(
        self,
        slugs: Iterable[str],
        *,
        cancel_event: threading.Event | None,
        deadline: float | None,
    ) -> int:
        self.ensure_active()
        self._ensure_exact_selection_not_prepared()
        if self._operation is not None:
            raise HuroshikiError("Wait for the active transaction operation to finish")
        selected = set(slugs)
        kind, project_id = split_project_key(self.project_key)
        if kind == "template":
            config = packctl.load_yaml(project_root(self.project_key) / "template.yaml")
            raw_mods = config.get("mods", [])
            if not isinstance(raw_mods, list):
                raise HuroshikiError(
                    f"templates/{project_id}/template.yaml mods must be a list"
                )
            selected_indexes = {
                index
                for value in selected
                if (index := template_mod_raw_index(Path(value))) is not None
            }
            legacy_slugs = {
                value
                for value in selected
                if template_mod_raw_index(Path(value)) is None
            }
            for index, entry in packctl.template_mods_indexed(
                project_id,
                allow_invalid_sides=True,
                deduplicate=False,
            ):
                slug = f"{canonical_provider(entry['provider'])}-{entry['project_id']}"
                if slug in legacy_slugs:
                    selected_indexes.add(index)
            self.template_manifest = [
                entry
                for index, entry in enumerate(raw_mods)
                if index not in selected_indexes
            ]
            return 0

        operation_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
        )
        for slug in sorted(selected):
            self.ensure_active()
            ensure_safe_pack_source(self.source)
            removed_identity: str | None = None
            manifest = self.source / ".huroshiki-roots.json"
            if manifest.is_file() and not manifest.is_symlink():
                removed_identity = identify_pack_metadata_by_slug(self.source, slug)
            if removed_identity is not None:
                try:
                    override = get_mod_version_override(
                        self.source, removed_identity
                    )
                except ModVersionOverrideError as error:
                    raise HuroshikiError(str(error)) from error
                if override is not None:
                    raise HuroshikiError(
                        "Removing this MOD would orphan an existing version override: "
                        f"{removed_identity}"
                    )
            _run_noninteractive_packwiz(
                ["packwiz", "remove", slug],
                cwd=self.source,
                cancel_event=cancel_event,
                deadline=operation_deadline,
                label=f"Packwiz remove {slug}",
                process_result_callback=self._record_equivalence_process_result,
                project_id=project_id,
                operation="remove",
            )
            if removed_identity is not None:
                remove_pack_root(self.source, removed_identity)
            ensure_safe_pack_source(self.source)
            self._record_source_mutation()
        return 0

    def apply(
        self,
        *,
        refresh: bool = True,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        with self._lifecycle_lock:
            with self._lock:
                exact_prepared = self._exact_selection_prepared
                try:
                    self._apply(
                        refresh=refresh,
                        cancel_event=cancel_event,
                        deadline=deadline,
                    )
                except BaseException as error:
                    if exact_prepared:
                        try:
                            self._restore_exact_selection_checkpoint()
                        except BaseException as restore_error:
                            raise HuroshikiError(
                                f"{error}; exact MOD selection rollback failed: "
                                f"{restore_error}"
                            ) from error
                    raise

    def _apply(
        self,
        *,
        refresh: bool,
        cancel_event: threading.Event | None,
        deadline: float | None,
    ) -> None:
        self.ensure_active()
        exact_evidence = (
            self._pending_exact_evidence
            if self._exact_selection_prepared
            else self._accepted_exact_evidence
        )
        if exact_evidence is not None:
            self._validate_exact_selection_stage(exact_evidence)
            refresh = False
        elif self._exact_verification_required:
            raise HuroshikiError(
                "Staged changes invalidated exact MOD verification; select an exact "
                "version again to verify the complete closure before applying"
            )
        if self._intent_only_mutation:
            refresh = False
        if self._equivalence_process_results:
            cleanup_deadline = time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
            if deadline is not None:
                cleanup_deadline = min(cleanup_deadline, deadline)
            self._retry_equivalence_process_cleanup(cleanup_deadline)
        with self._lock:
            if self._operation is not None:
                raise HuroshikiError("Wait for the active add operation to finish")

        kind, project_id = split_project_key(self.project_key)
        if kind == "template":
            real_root = project_root(self.project_key)
            if template_config_snapshot(real_root) != self.template_config_baseline:
                raise HuroshikiError(
                    "The template configuration changed while this transaction was open. "
                    "Discard the staged transaction and retry."
                )
            if self.template_manifest is not None:
                packctl.save_template_mods_raw(project_id, self.template_manifest)
                self._finish_state()
                shutil.rmtree(self.root, ignore_errors=True)
                self._mark_lifecycle_completed()
                return
            existing = packctl.template_mods(project_id)
            merged: dict[tuple[str, str], dict[str, str]] = {
                (item["provider"], item["project_id"]): dict(item)
                for item in existing
            }
            for mod in self.staged_mods():
                provider = canonical_provider(mod.provider)
                if not mod.project_id or provider not in {
                    "modrinth",
                    "curseforge",
                    "url",
                }:
                    continue
                entry = {
                    "name": mod.name,
                    "provider": provider,
                    "project_id": mod.project_id,
                    "side": mod.side,
                }
                if provider == "url":
                    if not mod.source_url:
                        continue
                    entry["url"] = mod.source_url
                merged[(provider, mod.project_id)] = entry
            packctl.save_template_mods(project_id, list(merged.values()))
            self._finish_state()
            shutil.rmtree(self.root, ignore_errors=True)
            self._mark_lifecycle_completed()
            return

        ensure_safe_pack_source(self.source)
        if refresh:
            operation_deadline = (
                deadline
                if deadline is not None
                else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
            )
            _run_noninteractive_packwiz(
                ["packwiz", "refresh"],
                cwd=self.source,
                cancel_event=cancel_event,
                deadline=operation_deadline,
                label="Packwiz refresh",
                process_result_callback=self._record_equivalence_process_result,
                project_id=project_id,
                operation="refresh",
            )
            ensure_safe_pack_source(self.source)
        if self._version_override_mutated:
            _validate_mod_version_override_records(
                self.source, _exact_metadata_records(self.source)
            )

        real_root = project_root(self.project_key)
        real_source = real_root / "source"
        backup = self.root / "replaced-source"
        try:
            os.stat(backup, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HuroshikiError(f"Backup path already exists: {backup}")

        staged_fd, staged_metadata = _open_pinned_source(self.source)
        original_fd = -1
        try:
            staged_issues = packctl.pack_source_fd_entry_issues(staged_fd)
            if staged_issues:
                details = "; ".join(
                    f"{relative}: {message}" for relative, message in staged_issues
                )
                raise HuroshikiError(f"Unsafe staged Packwiz source: {details}")
            if self._rollback_source_digest is not None and tuple(
                sorted(_source_fd_snapshot(staged_fd).items())
            ) != self._rollback_source_digest:
                raise HuroshikiError(
                    "The staged source changed after exact-selection rollback; "
                    "publication aborted"
                )
            original_fd, original_metadata = _open_pinned_source(real_source)
            if (
                _source_fd_snapshot(original_fd) != self.real_source_baseline
                or pack_config_snapshot(real_root) != self.pack_config_baseline
            ):
                raise HuroshikiError(
                    "The real Packwiz source changed or pack configuration changed while "
                    "this transaction was open. Discard the staged transaction and retry "
                    "to avoid overwriting external changes."
                )
            real_source.rename(backup)
            backup_metadata = os.stat(backup, follow_symlinks=False)
            if (
                not _same_entry(original_metadata, backup_metadata)
                or tree_digest_snapshot(backup) != self.real_source_baseline
                or pack_config_snapshot(real_root) != self.pack_config_baseline
            ):
                _restore_source_backup(real_source, backup, original_metadata)
                raise HuroshikiError(
                    "The real Packwiz source changed or pack configuration changed while "
                    "this transaction was open. Discard the staged transaction and retry "
                    "to avoid overwriting external changes."
                )
            current_staged = os.stat(self.source, follow_symlinks=False)
            if not _same_entry(staged_metadata, current_staged):
                _restore_source_backup(real_source, backup, original_metadata)
                raise HuroshikiError(
                    "The staged Packwiz source was replaced before publication; the "
                    "original source was restored."
                )
            self.source.rename(real_source)
            try:
                installed = os.stat(real_source, follow_symlinks=False)
            except OSError as error:
                _rollback_source_publication(
                    real_source,
                    backup,
                    staged_metadata,
                    original_metadata,
                    self.root,
                )
                raise HuroshikiError(
                    f"The staged Packwiz source was not installed safely: {error}"
                ) from error
            if not _same_entry(staged_metadata, installed):
                _rollback_source_publication(
                    real_source,
                    backup,
                    staged_metadata,
                    original_metadata,
                    self.root,
                )
                raise HuroshikiError(
                    "The installed Packwiz source was replaced during publication"
                )
        except BaseException as swap_error:
            try:
                backup_present = os.stat(backup, follow_symlinks=False)
            except FileNotFoundError:
                backup_present = None
            if backup_present is not None:
                try:
                    _rollback_source_publication(
                        real_source,
                        backup,
                        staged_metadata,
                        original_metadata,
                        self.root,
                    )
                except HuroshikiError as rollback_error:
                    raise rollback_error from swap_error
            raise
        finally:
            if original_fd >= 0:
                os.close(original_fd)
            os.close(staged_fd)

        self._finish_state()
        self._mark_lifecycle_completed()

    def begin_discard(
        self,
        *,
        deadline: float | None = None,
    ) -> TransactionDiscardOperation:
        operation_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
        )
        with self._lock:
            if self._discard_state == "discarded":
                if self._discard_operation is None:
                    completed = TransactionDiscardOperation(self, operation_deadline)
                    completed._started = True
                    completed.done.set()
                    self._discard_operation = completed
                return self._discard_operation
            if self._discard_state == "discarding":
                if self._discard_operation is None:
                    raise TransactionDiscardIntegrityError(
                        "Transaction discard state has no owner operation"
                    )
                self._discard_operation.deadline = min(
                    self._discard_operation.deadline,
                    operation_deadline,
                )
                return self._discard_operation
            if (
                self._discard_state == "failed"
                and self._discard_operation is not None
                and not self._discard_operation.done.is_set()
            ):
                return self._discard_operation
            self.active = False
            self._discard_state = "discarding"
            self._discard_error = None
            operation = TransactionDiscardOperation(self, operation_deadline)
            self._discard_operation = operation
            return operation

    def retry_discard(
        self,
        *,
        deadline: float | None = None,
    ) -> TransactionDiscardOperation:
        operation = self.begin_discard(deadline=deadline)
        operation.start()
        return operation

    def discard(self, *, deadline: float | None = None) -> None:
        operation = self.begin_discard(deadline=deadline)
        operation.run()
        if not operation.done.is_set():
            raise TransactionDiscardTimeout(
                f"Transaction discard timed out for {self.project_key}"
            )
        operation.raise_for_error()

    def wait_for_discard(self, timeout: float | None = None) -> bool:
        with self._lock:
            operation = self._discard_operation
            state = self._discard_state
        if operation is None:
            return state == "discarded"
        return operation.wait(timeout)

    @property
    def discard_error(self) -> BaseException | None:
        with self._lock:
            return self._discard_error

    def _run_discard_operation(self, discard: TransactionDiscardOperation) -> None:
        with self._lock:
            operation = self._operation
        if operation is not None and not operation.done.is_set():
            operation.cancel(deadline=discard.deadline)
        with self._lifecycle_lock:
            self._run_discard_operation_locked(discard)

    def _run_discard_operation_locked(
        self,
        discard: TransactionDiscardOperation,
    ) -> None:
        with self._lock:
            if self._discard_finalized:
                self._discard_error = None
                self._discard_state = "discarded"
                return
            if self._discard_operation is not discard:
                raise TransactionDiscardIntegrityError(
                    "Transaction discard ownership changed unexpectedly"
                )
            operation = self._operation
        if operation is not None and not operation.done.is_set():
            try:
                operation.cancel(deadline=discard.deadline)
            except BaseException as error:
                raise TransactionDiscardIntegrityError(
                    f"Could not cancel the active transaction operation: {error}"
                ) from error
            remaining = max(0.0, discard.deadline - time.monotonic())
            if not operation.wait(remaining):
                raise TransactionDiscardTimeout(
                    f"Active operation did not stop before discard deadline for "
                    f"{self.project_key}"
                )
        if operation is not None and self._add_termination_incomplete(operation):
            operation.cancel(deadline=discard.deadline)
        if operation is not None and getattr(operation, "cleanup_error", None) is not None:
            raise TransactionDiscardIntegrityError(
                f"Active operation cleanup failed: {operation.cleanup_error}"
            ) from operation.cleanup_error
        if operation is not None and self._add_termination_incomplete(operation):
            raise TransactionDiscardIntegrityError(
                "Active Packwiz process-group cleanup was incomplete"
            )
        if time.monotonic() >= discard.deadline:
            raise TransactionDiscardTimeout(
                f"Transaction discard deadline exceeded for {self.project_key}"
            )
        self._retry_equivalence_process_cleanup(discard.deadline)
        self._finish_discard_once(deadline=discard.deadline)
        with self._lock:
            if self._discard_operation is discard:
                self._discard_error = None
                self._discard_state = "discarded"
                _RETAINED_FAILED_TRANSACTIONS.pop(str(self.root), None)

    def _record_discard_failure(
        self,
        operation: TransactionDiscardOperation,
        error: BaseException,
    ) -> None:
        with self._lock:
            if self._discard_operation is operation:
                self._discard_error = error
                self._discard_state = "failed"
                _RETAINED_FAILED_TRANSACTIONS[str(self.root)] = self

    def _finish_discard_once(self, *, deadline: float) -> None:
        with self._lock:
            if self._discard_finalized:
                return
            if time.monotonic() >= deadline:
                raise TransactionDiscardTimeout(
                    f"Transaction discard deadline exceeded for {self.project_key}"
                )
            try:
                if self._project_lock is not None:
                    self._project_lock.release()
                    self._project_lock = None
            except BaseException as error:
                raise TransactionDiscardIntegrityError(
                    f"Could not finalize transaction discard: {error}"
                ) from error
            self._discard_finalized = True


def create_resolver_source(
    source: Path,
    *,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "mods").mkdir()
    def quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'

    pack_text = (
        f'name = {quote(display_name)}\n'
        'author = "huroshiki"\n'
        'version = "0.0.0"\n'
        'pack-format = "packwiz:1.1.0"\n\n'
        '[index]\n'
        'file = "index.toml"\n'
        'hash-format = "sha256"\n'
        'hash = "resolver"\n\n'
        '[versions]\n'
        f'minecraft = {quote(minecraft)}\n'
        f'{loader} = {quote(loader_version)}\n'
    )
    (source / "pack.toml").write_text(pack_text, encoding="utf-8")
    (source / "index.toml").write_text(
        'hash-format = "sha256"\n', encoding="utf-8"
    )


def canonical_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized in {"mr", "modrinth"}:
        return "modrinth"
    if normalized in {"cf", "curseforge"}:
        return "curseforge"
    if normalized in {"u", "url", "selfhost", "self-hosted"}:
        return "url"
    return normalized


def build_add_command(provider: str, query: str) -> list[str]:
    if provider == "url":
        raise HuroshikiError("URL additions are handled by huroshiki")
    if provider == "curseforge" and query.isdecimal():
        return ["packwiz", "curseforge", "add", "--addon-id", query]
    return ["packwiz", provider, "add", query]


def build_exact_artifact_command(
    selection: ExactModArtifactSelection,
) -> list[str]:
    if selection.provider == "curseforge":
        return [
            "packwiz",
            "--yes",
            "curseforge",
            "add",
            "--addon-id",
            selection.project_id,
            "--file-id",
            selection.artifact_id,
        ]
    return [
        "packwiz",
        "--yes",
        "modrinth",
        "add",
        "--project-id",
        selection.project_id,
        "--version-id",
        selection.artifact_id,
    ]


def normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {
        "m": "modrinth",
        "mr": "modrinth",
        "modrinth": "modrinth",
        "c": "curseforge",
        "cf": "curseforge",
        "curseforge": "curseforge",
        "u": "url",
        "url": "url",
        "selfhost": "url",
        "self-hosted": "url",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise HuroshikiError(f"Unsupported provider: {provider}") from error


def normalize_add_selector(provider: str, query: str) -> tuple[str, str]:
    normalized_provider = normalize_provider(provider)
    if any(
        ord(character) < 32
        or ord(character) == 127
        or (ord(character) > 127 and character.isspace())
        for character in query
    ):
        raise HuroshikiError(
            "Project selector contains unsafe whitespace or control characters"
        )
    selector = query.strip()
    if not selector:
        raise HuroshikiError("Search query is empty")

    lowered = selector.lower()
    if lowered.startswith("mr:"):
        normalized_provider = "modrinth"
        selector = selector[3:].strip()
    elif lowered.startswith("cf:"):
        normalized_provider = "curseforge"
        selector = selector[3:].strip()
    elif "modrinth.com/" in lowered:
        normalized_provider = "modrinth"
    elif lowered.startswith("url:"):
        normalized_provider = "url"
        selector = selector[4:].strip()
    elif "curseforge.com/" in lowered:
        normalized_provider = "curseforge"

    if not selector:
        raise HuroshikiError("Project selector is empty")
    if normalized_provider == "modrinth":
        parsed = urlparse(selector)
        if parsed.scheme or parsed.netloc:
            decoded_path = unquote(parsed.path)
            if any(
                ord(character) < 32
                or ord(character) == 127
                or (ord(character) > 127 and character.isspace())
                for character in decoded_path
            ):
                raise HuroshikiError(
                    "Project selector contains unsafe whitespace or control characters"
                )
    if normalized_provider == "url":
        validate_public_url(selector)
    return normalized_provider, selector


PROVIDER_LOOKUP_TIMEOUT_SECONDS = 30


def _provider_protocol_text(
    record: object,
    key: str,
    *,
    required: bool = True,
) -> str:
    if not isinstance(record, dict):
        raise HuroshikiError("Provider lookup returned a non-object record")
    value = record.get(key, "")
    if not isinstance(value, str) or (required and not value):
        raise HuroshikiError(f"Provider lookup returned invalid {key}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HuroshikiError(f"Provider lookup returned control characters in {key}")
    return value


def _provider_protocol_mapping(
    record: object,
    *,
    fields: set[str],
    context: str,
) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != fields:
        raise HuroshikiError(f"Provider lookup returned an invalid {context}")
    return record


def _provider_protocol_project_id(
    provider: str, record: object
) -> str | CanonicalModrinthId:
    project_id = _provider_protocol_text(record, "project_id")
    if provider == "curseforge":
        try:
            return canonical_curseforge_project_id(project_id)
        except HuroshikiError as error:
            raise HuroshikiError(
                "Provider lookup returned an invalid CurseForge project ID"
            ) from error
    return canonical_modrinth_id(project_id, "Modrinth project ID")


def canonical_curseforge_project_id(value: str) -> str:
    if not value.isdecimal() or len(value) > 20 or int(value) <= 0:
        raise HuroshikiError("CurseForge project ID must be a positive decimal value")
    return str(int(value))


def _run_provider_lookup(
    arguments: list[str],
    *,
    cancel_event: threading.Event | None,
    deadline: float | None,
) -> object:
    request_id = uuid4().hex
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PROVIDER_LOOKUP_TIMEOUT_SECONDS
    )
    result = run_resolver_process(
        [
            sys.executable,
            str(SCRIPTS / "provider_lookup.py"),
            "--request-id",
            request_id,
            *arguments,
        ],
        cwd=ROOT,
        cancel_event=cancel_event,
        deadline=effective_deadline,
    )
    if result.cancelled:
        raise HuroshikiError("Provider lookup was cancelled")
    if result.timed_out:
        raise HuroshikiError("Provider lookup deadline exceeded")
    if result.termination_incomplete:
        raise HuroshikiError("Provider lookup process termination was incomplete")
    if result.orphaned_descendants:
        raise HuroshikiError("Provider lookup left background processes after completion")
    if result.output_limit_exceeded:
        raise HuroshikiError("Provider lookup exceeded the supported output limit")
    if result.returncode != 0:
        raise HuroshikiError(
            concise_process_error(result).replace("Packwiz", "Provider lookup")
        )
    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate provider protocol field {key!r}")
            value[key] = item
        return value

    try:
        envelope = json.loads(result.stdout, object_pairs_hook=strict_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise HuroshikiError("Provider lookup returned invalid JSON") from error
    if not isinstance(envelope, dict) or set(envelope) != {"request_id", "result"}:
        raise HuroshikiError("Provider lookup returned an invalid response envelope")
    if envelope["request_id"] != request_id:
        raise HuroshikiError("Provider lookup returned a mismatched request ID")
    return envelope["result"]


def resolve_project_selector(
    provider: str,
    selector: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ResolvedSelector:
    if cancel_event is not None and cancel_event.is_set():
        raise HuroshikiError("Provider lookup was cancelled")
    normalized_provider, normalized_selector = normalize_add_selector(provider, selector)
    if normalized_provider == "curseforge":
        project_id = canonical_curseforge_project_id(normalized_selector)
        return ResolvedSelector(
            normalized_provider,
            selector,
            project_id,
            project_id,
        )
    if normalized_provider == "modrinth":
        record = _run_provider_lookup(
            [normalized_provider, "resolve", normalized_selector],
            cancel_event=cancel_event,
            deadline=deadline,
        )
        record = _provider_protocol_mapping(
            record,
            fields={"provider", "project_id", "slug", "title"},
            context="resolved project",
        )
        if record.get("provider") != normalized_provider:
            raise HuroshikiError("Provider lookup returned an invalid provider")
        project_id = _provider_protocol_project_id(normalized_provider, record)
        title = _provider_protocol_text(record, "title")
        _provider_protocol_text(record, "slug")
        return ResolvedSelector(
            normalized_provider,
            selector,
            project_id,
            title,
        )
    return ResolvedSelector(normalized_provider, selector, None, normalized_selector)


def search_provider_projects(
    provider: str,
    query: str,
    *,
    minecraft: str,
    loader: str,
    limit: int = 20,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> tuple[ProviderProject, ...]:
    normalized_provider = canonical_provider(provider)
    if normalized_provider != "modrinth":
        raise HuroshikiError(f"Provider search is unavailable for {provider}")
    if not 1 <= limit <= 50:
        raise HuroshikiError("Provider search limit must be between 1 and 50")
    record = _run_provider_lookup(
        [
            normalized_provider,
            "search",
            query,
            "--minecraft",
            minecraft,
            "--loader",
            loader,
            "--limit",
            str(limit),
        ],
        cancel_event=cancel_event,
        deadline=deadline,
    )
    record = _provider_protocol_mapping(
        record,
        fields={"provider", "results"},
        context="search response",
    )
    if record.get("provider") != normalized_provider:
        raise HuroshikiError("Provider lookup returned an invalid provider")
    raw_results = record.get("results")
    if not isinstance(raw_results, list) or len(raw_results) > limit:
        raise HuroshikiError("Provider lookup returned an invalid results list")
    projects: list[ProviderProject] = []
    identities: set[str] = set()
    for item in raw_results:
        item = _provider_protocol_mapping(
            item,
            fields={
                "project_id",
                "slug",
                "title",
                "description",
                "author",
            },
            context="search result",
        )
        project = ProviderProject(
            normalized_provider,
            _provider_protocol_project_id(normalized_provider, item),
            _provider_protocol_text(item, "slug"),
            _provider_protocol_text(item, "title"),
            _provider_protocol_text(item, "description", required=False),
            _provider_protocol_text(item, "author", required=False),
        )
        if project.project_id in identities:
            raise HuroshikiError(
                f"Provider lookup returned duplicate project ID {project.project_id}"
            )
        identities.add(project.project_id)
        projects.append(project)
    return tuple(projects)


def url_max_jar_size_bytes(config: dict[str, object]) -> int:
    value = config.get(
        "url_max_jar_size_bytes",
        DEFAULT_URL_MAX_JAR_SIZE_BYTES,
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise packctl.ConfigError("url_max_jar_size_bytes must be a positive integer")
    return value


def project_url_max_jar_size_bytes(project_key_value: str) -> int:
    return url_max_jar_size_bytes(project_config(project_key_value))


def url_allow_private_networks(config: dict[str, object]) -> bool:
    value = config.get("url_allow_private_networks", False)
    if not isinstance(value, bool):
        raise packctl.ConfigError("url_allow_private_networks must be a boolean")
    return value


def project_url_allow_private_networks(project_key_value: str) -> bool:
    return url_allow_private_networks(project_config(project_key_value))


def safe_child(root: Path, relative: Path) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HuroshikiError(f"Path escaped root: {relative}")
    return candidate


TEMPLATE_TARGETS = OVERLAY_TARGETS


def _pack_content_root(project_key_value: str) -> Path:
    kind, _ = split_project_key(project_key_value)
    if kind != "pack":
        raise HuroshikiError(
            "Content management is currently available only for packs"
        )
    root = project_root(project_key_value)
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        root_mode = 0
    if not stat.S_ISDIR(root_mode):
        raise HuroshikiError(f"Missing project directory: {root}")
    return root


def list_content_entries(
    project_key_value: str,
    side: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> tuple[ContentEntry, ...]:
    root = _pack_content_root(project_key_value)
    checkpoint = lambda: content_checkpoint(cancel_event, deadline)
    return list_content_entries_at(
        project_key_value,
        root,
        side,
        checkpoint=checkpoint,
    )


def read_content_file(
    project_key_value: str,
    side: str,
    relative_path: str | Path,
    *,
    max_bytes: int | None = None,
) -> ContentFile:
    root = _pack_content_root(project_key_value)
    return read_content_file_at(
        project_key_value,
        root,
        side,
        relative_path,
        max_bytes=max_bytes,
    )


def content_snapshot(
    project_key_value: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ContentSnapshot:
    root = _pack_content_root(project_key_value)
    checkpoint = lambda: content_checkpoint(cancel_event, deadline)
    return content_snapshot_at(project_key_value, root, checkpoint=checkpoint)


def inspect_content_import_source(
    source_path: str | Path,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ContentImportSourceSnapshot:
    return inspect_content_import_source_at(
        source_path,
        repository_root=ROOT,
        state_root=STATE_ROOT,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def content_checkpoint(
    cancel_event: threading.Event | None,
    deadline: float | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ContentOperationCancelled("Content operation was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise ContentOperationDeadlineExceeded("Content operation deadline exceeded")


def load_content_browser(
    project_key_value: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ContentBrowseResult:
    root = _pack_content_root(project_key_value)
    return load_content_browser_at(
        project_key_value,
        root,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def load_content_text_document(
    project_key_value: str,
    side: str,
    relative_path: str | Path,
    *,
    expected_snapshot: ContentSnapshot,
    max_bytes: int = CONTENT_EDITOR_MAX_BYTES,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ContentTextDocument:
    root = _pack_content_root(project_key_value)
    return load_content_text_document_at(
        project_key_value,
        root,
        side,
        relative_path,
        expected_snapshot=expected_snapshot,
        max_bytes=max_bytes,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def resolve_content_path_info(
    project_key_value: str,
    side: str,
    relative_path: str | Path,
    *,
    expected_snapshot: ContentSnapshot,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ContentPathInfo:
    content_checkpoint(cancel_event, deadline)
    root = _pack_content_root(project_key_value)
    return resolve_content_path_info_at(
        project_key_value,
        root,
        ROOT,
        side,
        relative_path,
        expected_snapshot=expected_snapshot,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def plan_content_changes(
    project_key_value: str,
    operations: Iterable[ContentOperation],
    *,
    expected_snapshot: ContentSnapshot | None = None,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> ContentChangePlan:
    root = _pack_content_root(project_key_value)
    return plan_content_changes_at(
        project_key_value,
        root,
        TRANSACTION_ROOT,
        tuple(operations),
        expected_snapshot=expected_snapshot,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def plan_content_import(
    project_key_value: str,
    request: ContentImportRequest,
    *,
    expected_snapshot: ContentSnapshot,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> ContentChangePlan:
    root = _pack_content_root(project_key_value)
    return plan_content_import_at(
        project_key_value,
        root,
        TRANSACTION_ROOT,
        request,
        expected_snapshot=expected_snapshot,
        repository_root=ROOT,
        state_root=STATE_ROOT,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def apply_content_changes(
    plan: ContentChangePlan,
    *,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    _apply_content_changes(
        plan,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def discard_content_plan(
    plan: ContentChangePlan,
    *,
    deadline: float | None = None,
) -> None:
    _discard_content_plan(plan, deadline=deadline)


def snapshot_pack_migration_source(
    project_key_value: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationSourceSnapshot:
    kind, project_id = split_project_key(project_key_value)
    if kind != "pack":
        raise HuroshikiError("Pack migration is available only for packs")
    return snapshot_pack_migration_source_at(
        project_key_value,
        packctl.get_pack_root(project_id),
        ROOT,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def plan_pack_copy_migration(
    source_key: str,
    target: PackMigrationTarget,
    *,
    expected_snapshot: PackMigrationSourceSnapshot,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationPlan:
    kind, source_id = split_project_key(source_key)
    if kind != "pack":
        raise HuroshikiError("Pack migration is available only for packs")
    return plan_pack_copy_migration_at(
        source_key,
        packctl.get_pack_root(source_id),
        packctl.get_pack_root(target.target_id, must_exist=False),
        TRANSACTION_ROOT,
        target,
        expected_snapshot=expected_snapshot,
        repository_root=ROOT,
        state_root=STATE_ROOT,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def apply_pack_copy_migration(
    publication: PackMigrationPublicationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> PackMigrationSourceSnapshot:
    return _apply_pack_copy_migration_at(
        publication,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def prepare_pack_migration_publication(
    plan: PackMigrationPlan,
    resolution_plan: object,
    *,
    acknowledged_warning_codes: tuple[str, ...] = (),
    acknowledged_warnings: tuple[str, ...] | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationPublicationPlan:
    return _prepare_pack_migration_publication(
        plan,
        resolution_plan,
        acknowledged_warning_codes=acknowledged_warning_codes,
        acknowledged_warnings=acknowledged_warnings,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def apply_pack_migration_publication(
    publication: PackMigrationPublicationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationSourceSnapshot:
    return _apply_pack_migration_publication(
        publication, cancel_event=cancel_event, deadline=deadline, progress=progress
    )


def retry_pack_migration_cleanup(
    publication: PackMigrationPublicationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[object], None] | None = None,
) -> PackMigrationSourceSnapshot:
    return _retry_pack_migration_cleanup(
        publication,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def discard_pack_migration_plan(
    plan: PackMigrationPlan,
    *,
    deadline: float | None = None,
) -> None:
    _discard_pack_migration_plan(plan, deadline=deadline)


def resolve_pack_migration_plan(
    plan: PackMigrationPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[["PackMigrationProgress"], None] | None = None,
) -> "PackMigrationResolutionPlan":
    from pack_migration_resolution import resolve_pack_migration_plan_at

    return resolve_pack_migration_plan_at(
        plan,
        repository_root=ROOT,
        state_root=STATE_ROOT,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def commit_pack_migration_root_selection(
    plan: PackMigrationPlan,
    selections: tuple["PackMigrationRootSelection", ...],
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> tuple["PackRootRecord", ...]:
    from pack_migration_resolution import commit_pack_migration_root_selection_at

    return commit_pack_migration_root_selection_at(
        plan,
        selections,
        repository_root=ROOT,
        cancel_event=cancel_event,
        deadline=deadline,
    )


def resolve_pack_migration_conflicts(
    plan: PackMigrationPlan,
    request: "PackMigrationResolutionRequest",
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[["PackMigrationProgress"], None] | None = None,
) -> "PackMigrationConflictResolutionResult":
    from pack_migration_resolution import resolve_pack_migration_conflicts_at

    return resolve_pack_migration_conflicts_at(
        plan,
        request,
        repository_root=ROOT,
        state_root=STATE_ROOT,
        cancel_event=cancel_event,
        deadline=deadline,
        progress=progress,
    )


def normalize_template_target(target: str) -> str:
    normalized = target.strip().lower()
    if normalized not in TEMPLATE_TARGETS:
        raise HuroshikiError(
            "Template target must be common, client, or server"
        )
    return normalized


def normalize_template_relative_path(value: str | Path) -> Path:
    try:
        return normalize_overlay_relative_path(value)
    except OverlayPolicyError as error:
        raise HuroshikiError(str(error)) from error


def template_base(project_key_value: str, target: str) -> Path:
    normalized_target = normalize_template_target(target)
    return project_root(project_key_value) / "content" / normalized_target


def resolve_template_path(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> Path:
    relative = normalize_template_relative_path(relative_path)
    try:
        return safe_overlay_child(
            project_root(project_key_value) / "content",
            normalize_template_target(target),
            relative,
        )
    except OverlayPolicyError as error:
        raise HuroshikiError(str(error)) from error


def list_templates(project_key_value: str) -> list[TemplateInfo]:
    root = project_root(project_key_value)
    templates: list[TemplateInfo] = []
    scan = scan_content_overlays(root / "content")
    issue_by_path: dict[Path, list[str]] = {}
    for issue in scan.issues:
        issue_by_path.setdefault(issue.relative_path, []).append(issue.message)
    for entry in scan.entries:
        if entry.relative_path.name == ".gitkeep" or entry.kind == "directory":
            continue
        if entry.relative_path == Path("."):
            templates.append(
                TemplateInfo(
                    target="content",
                    relative_path=Path("."),
                    full_path=root / "content",
                    size=entry.size,
                    error="; ".join(issue_by_path[entry.relative_path]),
                )
            )
            continue
        target = entry.relative_path.parts[0]
        relative = Path(*entry.relative_path.parts[1:])
        templates.append(
            TemplateInfo(
                target=target,
                relative_path=relative,
                full_path=root / "content" / entry.relative_path,
                size=entry.size,
                error="; ".join(issue_by_path.get(entry.relative_path, ())) or None,
            )
        )
    return templates


def filter_templates(
    templates: Iterable[TemplateInfo],
    query: str,
) -> list[TemplateInfo]:
    needle = query.strip().casefold()
    if not needle:
        return list(templates)
    return [
        template
        for template in templates
        if needle in f"{template.target} {template.relative_path}".casefold()
    ]


def create_template(
    project_key_value: str,
    target: str,
    relative_path: str,
) -> TemplateInfo:
    try:
        with packctl.ProjectLock(project_key_value, "create template file"):
            normalized_target = normalize_template_target(target)
            relative = normalize_template_relative_path(relative_path)
            try:
                create_overlay_file(
                    project_root(project_key_value) / "content",
                    normalized_target,
                    relative,
                )
            except OverlayPolicyError as error:
                raise HuroshikiError(str(error)) from error
            return TemplateInfo(
                target=normalized_target,
                relative_path=relative,
                full_path=template_base(project_key_value, normalized_target) / relative,
                size=0,
            )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def read_template_text(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> str:
    kind, _ = split_project_key(project_key_value)
    if kind == "pack":
        try:
            file = read_content_file(project_key_value, target, relative_path)
        except ContentOperationError as error:
            raise HuroshikiError(str(error)) from error
        try:
            return file.contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HuroshikiError(
                f"Template file is not UTF-8 text: {target}/{relative_path}"
            ) from error
    try:
        return read_overlay_text(
            project_root(project_key_value) / "content",
            normalize_template_target(target),
            normalize_template_relative_path(relative_path),
        )
    except OverlayPolicyError as error:
        raise HuroshikiError(str(error)) from error
    except UnicodeDecodeError as error:
        raise HuroshikiError(
            f"Template file is not UTF-8 text: {target}/{relative_path}"
        ) from error


def write_template_text(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
    text: str,
) -> None:
    try:
        with packctl.ProjectLock(project_key_value, "write template file"):
            try:
                write_overlay_text(
                    project_root(project_key_value) / "content",
                    normalize_template_target(target),
                    normalize_template_relative_path(relative_path),
                    text,
                )
            except OverlayPolicyError as error:
                raise HuroshikiError(str(error)) from error
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def delete_template(
    project_key_value: str,
    target: str,
    relative_path: str | Path,
) -> None:
    try:
        with packctl.ProjectLock(project_key_value, "delete template file"):
            try:
                delete_overlay_file(
                    project_root(project_key_value) / "content",
                    normalize_template_target(target),
                    normalize_template_relative_path(relative_path),
                )
            except OverlayPolicyError as error:
                raise HuroshikiError(str(error)) from error
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def _run_checkpoint(checkpoint: Callable[[], None] | None) -> None:
    if checkpoint is not None:
        checkpoint()


def file_digest(
    path: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _run_checkpoint(checkpoint)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_file_bytes(
    path: Path,
    checkpoint: Callable[[], None] | None = None,
) -> bytes:
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        while True:
            _run_checkpoint(checkpoint)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def _checkpointed_paths(
    source: Path,
    pattern: str,
    checkpoint: Callable[[], None] | None,
) -> list[Path]:
    paths: list[Path] = []
    _run_checkpoint(checkpoint)
    for path in source.rglob(pattern):
        _run_checkpoint(checkpoint)
        paths.append(path)
    return sorted(paths)


_SOURCE_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
_SOURCE_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _rename_noreplace(source: Path, destination: Path) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "collision-safe rename is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "collision-safe rename is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _open_pinned_source(source: Path) -> tuple[int, os.stat_result]:
    try:
        metadata = os.stat(source, follow_symlinks=False)
        source_fd = os.open(source, _SOURCE_DIRECTORY_FLAGS)
    except OSError as error:
        raise HuroshikiError(f"Unsafe Packwiz source {source}: {error}") from error
    try:
        opened = os.fstat(source_fd)
    except BaseException:
        os.close(source_fd)
        raise
    if not stat.S_ISDIR(metadata.st_mode) or not _same_entry(metadata, opened):
        os.close(source_fd)
        raise HuroshikiError(
            f"Unsafe Packwiz source {source}: source was replaced while opening"
        )
    return source_fd, opened


def _digest_fd(
    file_fd: int,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    while True:
        _run_checkpoint(checkpoint)
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _source_fd_snapshot(
    directory_fd: int,
    relative: Path = Path("."),
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    _run_checkpoint(checkpoint)
    try:
        with os.scandir(directory_fd) as iterator:
            names = []
            for entry in iterator:
                _run_checkpoint(checkpoint)
                names.append(entry.name)
            names.sort()
    except OSError as error:
        raise HuroshikiError(
            f"Unsafe Packwiz source at {relative}: could not list directory: {error}"
        ) from error
    for name in names:
        _run_checkpoint(checkpoint)
        item_relative = relative / name
        if item_relative.parts[0] == ".":
            item_relative = Path(*item_relative.parts[1:])
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: {error}"
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = os.open(name, _SOURCE_DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
                ) from error
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(metadata, opened):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                    )
                snapshot[item_relative] = "directory"
                snapshot.update(
                    _source_fd_snapshot(child_fd, item_relative, checkpoint)
                )
                current = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_entry(opened, current):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while scanning"
                    )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            kind = "symlink" if stat.S_ISLNK(metadata.st_mode) else "special entry"
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: {kind} is not allowed"
            )
        try:
            file_fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=directory_fd)
        except OSError as error:
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
            ) from error
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_entry(metadata, opened):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                )
            snapshot[item_relative] = _digest_fd(file_fd, checkpoint)
            after_read = os.fstat(file_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not _same_entry(opened, current)
                or opened.st_size != after_read.st_size
                or opened.st_mtime_ns != after_read.st_mtime_ns
                or opened.st_ctime_ns != after_read.st_ctime_ns
            ):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while reading"
                )
        finally:
            os.close(file_fd)
    return snapshot


def _copy_source_fd(
    source_fd: int,
    destination_fd: int,
    relative: Path = Path("."),
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    _run_checkpoint(checkpoint)
    with os.scandir(source_fd) as iterator:
        names = []
        for entry in iterator:
            _run_checkpoint(checkpoint)
            names.append(entry.name)
        names.sort()
    for name in names:
        _run_checkpoint(checkpoint)
        item_relative = relative / name
        if item_relative.parts[0] == ".":
            item_relative = Path(*item_relative.parts[1:])
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = os.open(name, _SOURCE_DIRECTORY_FLAGS, dir_fd=source_fd)
            except OSError as error:
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
                ) from error
            output_fd = -1
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(metadata, opened):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                    )
                source_mode = stat.S_IMODE(opened.st_mode)
                os.mkdir(name, source_mode | stat.S_IRWXU, dir_fd=destination_fd)
                output_fd = os.open(name, _SOURCE_DIRECTORY_FLAGS, dir_fd=destination_fd)
                snapshot[item_relative] = "directory"
                snapshot.update(
                    _copy_source_fd(child_fd, output_fd, item_relative, checkpoint)
                )
                current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or not _same_entry(opened, current):
                    raise HuroshikiError(
                        f"Unsafe Packwiz source at {item_relative}: replaced while copying"
                    )
                current_output = os.stat(
                    name, dir_fd=destination_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(current_output.st_mode) or not _same_entry(
                    os.fstat(output_fd), current_output
                ):
                    raise HuroshikiError(
                        f"Transaction destination at {item_relative} was replaced"
                    )
                os.fchmod(output_fd, source_mode)
            finally:
                if output_fd >= 0:
                    os.close(output_fd)
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            kind = "symlink" if stat.S_ISLNK(metadata.st_mode) else "special entry"
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: {kind} is not allowed"
            )
        try:
            file_fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=source_fd)
        except OSError as error:
            raise HuroshikiError(
                f"Unsafe Packwiz source at {item_relative}: changed while opening: {error}"
            ) from error
        output_fd = -1
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_entry(metadata, opened):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: replaced while opening"
                )
            output_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                stat.S_IMODE(opened.st_mode),
                dir_fd=destination_fd,
            )
            digest = hashlib.sha256()
            while True:
                _run_checkpoint(checkpoint)
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    _run_checkpoint(checkpoint)
                    written = os.write(output_fd, view)
                    view = view[written:]
            os.fchmod(output_fd, stat.S_IMODE(opened.st_mode))
            snapshot[item_relative] = digest.hexdigest()
            after_read = os.fstat(file_fd)
            current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            current_output = os.stat(
                name, dir_fd=destination_fd, follow_symlinks=False
            )
            if (
                not _same_entry(opened, current)
                or opened.st_size != after_read.st_size
                or opened.st_mtime_ns != after_read.st_mtime_ns
                or opened.st_ctime_ns != after_read.st_ctime_ns
            ):
                raise HuroshikiError(
                    f"Unsafe Packwiz source at {item_relative}: changed while copying"
                )
            if not stat.S_ISREG(current_output.st_mode) or not _same_entry(
                os.fstat(output_fd), current_output
            ):
                raise HuroshikiError(
                    f"Transaction destination at {item_relative} was replaced"
                )
        finally:
            if output_fd >= 0:
                os.close(output_fd)
            os.close(file_fd)
    return snapshot


def copy_transaction_source(
    source: Path,
    destination: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
    retained_destination: Path | None = None,
) -> None:
    _run_checkpoint(checkpoint)
    source_fd, source_metadata = _open_pinned_source(source)
    parent_fd = destination_fd = -1
    try:
        issues = (
            packctl.pack_source_fd_entry_issues(source_fd, checkpoint)
            if checkpoint is not None
            else packctl.pack_source_fd_entry_issues(source_fd)
        )
        if issues:
            details = "; ".join(f"{relative}: {message}" for relative, message in issues)
            raise HuroshikiError(f"Unsafe Packwiz source {source}: {details}")
        _run_checkpoint(checkpoint)
        parent_fd = os.open(destination.parent, _SOURCE_DIRECTORY_FLAGS)
        os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
        destination_fd = os.open(
            destination.name, _SOURCE_DIRECTORY_FLAGS, dir_fd=parent_fd
        )
        copied = _copy_source_fd(
            source_fd,
            destination_fd,
            checkpoint=checkpoint,
        )
        _run_checkpoint(checkpoint)
        try:
            current = os.stat(source, follow_symlinks=False)
            current_destination = os.stat(
                destination.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError as error:
            raise HuroshikiError(
                f"A transaction source entry was replaced while copying: {error}"
            ) from error
        if not _same_entry(source_metadata, current):
            raise HuroshikiError(
                "The pack source was replaced while the transaction copy was being created"
            )
        if not _same_entry(os.fstat(destination_fd), current_destination):
            raise HuroshikiError(
                "The transaction destination was replaced while the copy was being created"
            )
        if (
            _source_fd_snapshot(source_fd, checkpoint=checkpoint) != copied
            or _source_fd_snapshot(destination_fd, checkpoint=checkpoint) != copied
        ):
            raise HuroshikiError(
                "The pack source changed while the transaction copy was being created"
            )
        os.fchmod(destination_fd, stat.S_IMODE(source_metadata.st_mode))
    except BaseException:
        if destination_fd >= 0:
            os.close(destination_fd)
            destination_fd = -1
        if parent_fd >= 0:
            os.close(parent_fd)
            parent_fd = -1
        if destination.exists():
            if retained_destination is None:
                shutil.rmtree(destination, ignore_errors=True)
            else:
                destination.rename(retained_destination)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(source_fd)


def ensure_safe_pack_source(
    source: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    issues = (
        packctl.pack_source_entry_issues(source, checkpoint)
        if checkpoint is not None
        else packctl.pack_source_entry_issues(source)
    )
    if not issues:
        return
    details = "; ".join(
        f"{source if relative == Path('.') else source / relative}: {message}"
        for relative, message in issues
    )
    raise HuroshikiError(f"Unsafe Packwiz source: {details}")


def _restore_source_backup(
    real_source: Path,
    backup: Path,
    expected_backup: os.stat_result,
) -> None:
    try:
        backup_metadata = os.stat(backup, follow_symlinks=False)
    except OSError as error:
        raise HuroshikiError(
            f"Cannot restore the exact original Packwiz source from {backup}: {error}"
        ) from error
    if not _same_entry(expected_backup, backup_metadata):
        raise HuroshikiError(
            f"Cannot restore the exact original Packwiz source because {backup} was replaced"
        )
    try:
        os.stat(real_source, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise HuroshikiError(
            f"Cannot restore the original Packwiz source because {real_source} was "
            f"recreated externally; external changes were preserved and the exact "
            f"original remains at {backup}."
        )
    try:
        backup.rename(real_source)
    except BaseException as error:
        raise HuroshikiError(
            f"Failed to restore the original Packwiz source from {backup}: {error}"
        ) from error


def _rollback_source_publication(
    real_source: Path,
    backup: Path,
    expected_installed_staged: os.stat_result,
    original_metadata: os.stat_result,
    transaction_root: Path,
) -> None:
    try:
        current = os.stat(real_source, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    except OSError as error:
        raise HuroshikiError(
            f"Cannot inspect the published Packwiz source during rollback: {error}; "
            f"the exact original remains at {backup}."
        ) from error

    if current is not None and _same_entry(current, expected_installed_staged):
        failed_staged = transaction_root / "failed-staged-source"
        try:
            os.stat(failed_staged, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HuroshikiError(
                f"Cannot retain the failed staged source because {failed_staged} exists; "
                f"the exact original remains at {backup}."
            )
        rename_error: BaseException | None = None
        try:
            real_source.rename(failed_staged)
        except BaseException as error:
            rename_error = error
        try:
            moved = os.stat(failed_staged, follow_symlinks=False)
        except OSError:
            moved = None
        if moved is None:
            raise HuroshikiError(
                "Failed to move the staged Packwiz source out of publication; "
                f"the exact original remains at {backup}: {rename_error}"
            ) from rename_error
        if not _same_entry(moved, expected_installed_staged):
            try:
                _rename_noreplace(failed_staged, real_source)
            except OSError as restore_error:
                raise HuroshikiError(
                    "The installed staged Packwiz source was replaced during rollback; "
                    f"external entries at {real_source} or {failed_staged} were "
                    f"preserved, and the exact original remains at {backup}: "
                    f"{restore_error}"
                ) from restore_error
            raise HuroshikiError(
                "The installed staged Packwiz source was replaced during rollback; "
                f"the moved external source was restored to {real_source}, the exact "
                f"original remains at {backup}, and competing entries were retained."
            ) from rename_error
        current = None

    if current is not None:
        raise HuroshikiError(
            f"Cannot restore the original Packwiz source because {real_source} was "
            f"recreated externally; external changes were preserved and the exact "
            f"original remains at {backup}."
        )
    _restore_source_backup(real_source, backup, original_metadata)


def template_config_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ("template.yaml", "template.local.yaml"):
        path = root / name
        snapshot[name] = file_digest(path) if path.is_file() else "missing"
    return snapshot


def pack_config_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ("pack.yaml", "pack.local.yaml"):
        path = root / name
        snapshot[name] = file_digest(path) if path.is_file() else "missing"
    return snapshot


def tree_digest_snapshot(
    source: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, str]:
    _run_checkpoint(checkpoint)
    source_fd, source_metadata = _open_pinned_source(source)
    try:
        snapshot = _source_fd_snapshot(source_fd, checkpoint=checkpoint)
        _run_checkpoint(checkpoint)
        current = os.stat(source, follow_symlinks=False)
        if not _same_entry(source_metadata, current):
            raise HuroshikiError(
                f"Unsafe Packwiz source {source}: source was replaced while scanning"
            )
        return snapshot
    finally:
        os.close(source_fd)


def metadata_digest_snapshot(
    source: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    for path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
        _run_checkpoint(checkpoint)
        if path.is_file():
            snapshot[path.relative_to(source)] = file_digest(
                path,
                checkpoint=checkpoint,
            )
    return snapshot


def metadata_content_snapshot(
    source: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, bytes]:
    snapshot: dict[Path, bytes] = {}
    for path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
        _run_checkpoint(checkpoint)
        if path.is_file():
            snapshot[path.relative_to(source)] = _read_file_bytes(path, checkpoint)
    _run_checkpoint(checkpoint)
    return snapshot


def changed_paths(
    before: dict[Path, str],
    after: dict[Path, str],
) -> set[Path]:
    return {
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }


def side_from_flags(client: bool, server: bool) -> str:
    if client and server:
        return "both"
    if client:
        return "client"
    if server:
        return "server"
    raise HuroshikiError("A mod must be enabled on the client, server, or both")


def flags_from_side(side: object) -> tuple[bool, bool]:
    if side == "both":
        return True, True
    if side == "client":
        return True, False
    if side == "server":
        return False, True
    return False, False


def pack_versions(source: Path) -> tuple[str, str, str]:
    pack_file = source / "pack.toml"
    if not pack_file.exists():
        return "", "", ""
    try:
        data = tomllib.loads(pack_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"{pack_file}: {error}") from error
    versions = data.get("versions", {})
    if not isinstance(versions, dict):
        return "", "", ""
    minecraft = str(versions.get("minecraft", ""))
    for loader in ("neoforge", "forge", "fabric", "quilt"):
        value = versions.get(loader)
        if value is not None:
            return minecraft, loader, str(value)
    return minecraft, "", ""


def project_info(project_key_value: str) -> ProjectInfo:
    kind, project_id = split_project_key(project_key_value)
    parent = PACKS if kind == "pack" else TEMPLATES
    manifest = parent / project_id / (
        "pack.yaml" if kind == "pack" else "template.yaml"
    )
    fallback = ProjectInfo(
        kind=kind,
        project_id=project_id,
        display_name=project_id,
        minecraft="",
        loader="",
        loader_version="",
        enabled=False,
    )
    try:
        config = packctl.load_project_config(kind, project_id)
        if kind == "pack":
            minecraft, loader, loader_version = pack_versions(
                packctl.get_pack_root(project_id) / "source"
            )
        else:
            minecraft, loader, loader_version = packctl.template_versions(project_id)
        info = replace(
            fallback,
            display_name=str(config.get("display_name", project_id)),
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
            enabled=bool(config.get("enabled", True)),
        )
    except Exception as error:
        return replace(fallback, error=f"{manifest}: {error}")

    try:
        return replace(info, mod_count=len(list_mods(project_key_value)))
    except Exception as error:
        return replace(info, error=str(error))


def list_projects() -> list[ProjectInfo]:
    projects = [
        project_info(project_key("pack", pack_id))
        for pack_id in packctl.pack_ids()
    ]
    projects.extend(
        project_info(project_key("template", template_id))
        for template_id in packctl.template_ids()
    )
    return sorted(projects, key=lambda item: (item.kind, item.display_name.casefold()))


def filter_projects(
    projects: Iterable[ProjectInfo],
    query: str,
) -> list[ProjectInfo]:
    needle = query.strip().casefold()
    if not needle:
        return list(projects)
    return [
        project
        for project in projects
        if needle
        in " ".join(
            (
                project.kind,
                project.project_id,
                project.display_name,
                project.minecraft,
                project.loader,
                project.loader_version,
            )
        ).casefold()
    ]


def provider_from_metadata(data: dict[str, object]) -> tuple[str, str]:
    update = data.get("update", {})
    if isinstance(update, dict):
        if "modrinth" in update:
            project = update.get("modrinth")
            return "MR", extract_project_id(project)
        if "curseforge" in update:
            project = update.get("curseforge")
            return "CF", extract_project_id(project)

    download = data.get("download", {})
    if isinstance(download, dict):
        mode = str(download.get("mode", ""))
        if "curseforge" in mode:
            return "CF", ""
    return "URL", ""


def extract_project_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in (
        "mod-id",
        "project-id",
        "project_id",
        "projectID",
        "projectId",
    ):
        if key in value:
            return str(value[key])
    return ""


def read_mod(source: Path, relative_path: Path) -> ModInfo:
    path = safe_child(source, relative_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"{path}: {error}") from error
    return read_mod_data(relative_path, data)


def metadata_version(data: dict[str, object], provider: str) -> str:
    update = data.get("update", {})
    if isinstance(update, dict):
        provider_data = update.get(canonical_provider(provider), {})
        if isinstance(provider_data, dict):
            for key in ("version", "version-id", "file-id", "fileId"):
                if key in provider_data:
                    return str(provider_data[key])
    filename = str(data.get("filename", ""))
    return filename or "unknown"


def metadata_file_id(data: dict[str, object], provider: str) -> str:
    update = data.get("update", {})
    if isinstance(update, dict):
        provider_data = update.get(canonical_provider(provider), {})
        if isinstance(provider_data, dict):
            ordered_keys: tuple[str, ...]
            if canonical_provider(provider) == "modrinth":
                ordered_keys = ("version-id", "file-id", "fileId", "version")
            elif canonical_provider(provider) == "curseforge":
                ordered_keys = ("file-id", "fileId", "version")
            else:
                ordered_keys = ("file-id", "fileId", "version-id", "version")
            for key in ordered_keys:
                if key in provider_data:
                    return str(provider_data[key])
    return "-"


def update_version_label(version: str, file_id: str) -> str:
    return f"{version} ({file_id})" if file_id not in {"", "-", version} else version


def metadata_is_pinned(data: dict[str, object]) -> bool:
    if data.get("pin") is True:
        return True
    update = data.get("update", {})
    return isinstance(update, dict) and update.get("pin") is True


@dataclass(frozen=True)
class _UpdateMetadata:
    relative_path: Path
    provider: str
    project_id: str
    filename: str
    contents: bytes

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.project_id


UPDATE_RESOLVER_TIMEOUT_SECONDS = 120
UPDATE_OPERATION_TIMEOUT_SECONDS = 600
LOADER_MIGRATION_TIMEOUT_SECONDS = 300
LOADER_MIGRATION_PROCESS_TIMEOUT_SECONDS = 120
PACKWIZ_GENERATED_PATHS = {Path("index.toml"), Path("pack.toml")}


def _file_content_snapshot(
    source: Path,
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, bytes]:
    snapshot: dict[Path, bytes] = {}
    for path in _checkpointed_paths(source, "*", checkpoint):
        _run_checkpoint(checkpoint)
        if path.is_file() and not path.is_symlink():
            snapshot[path.relative_to(source)] = _read_file_bytes(path, checkpoint)
    return snapshot


def _content_changes(
    before: Mapping[Path, bytes],
    after: Mapping[Path, bytes],
) -> tuple[UpdateChange, ...]:
    return tuple(
        UpdateChange(path, before.get(path), after.get(path))
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    )


def _exact_metadata_records(
    source: Path,
    checkpoint: Callable[[], None] | None = None,
) -> dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]]:
    records: dict[tuple[str, str], list[tuple[Path, bytes, ModInfo]]] = {}
    for path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
        _run_checkpoint(checkpoint)
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source)
        contents = _read_file_bytes(path, checkpoint)
        try:
            mod = read_mod_data(relative, tomllib.loads(contents.decode("utf-8")))
        except (UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
            raise HuroshikiError(
                f"Exact MOD selection metadata {relative} is invalid: {error}"
            ) from error
        identity = canonical_provider(mod.provider), mod.project_id
        records.setdefault(identity, []).append((relative, contents, mod))
    return {identity: tuple(items) for identity, items in records.items()}


def _exact_source_digest(source: Path) -> tuple[tuple[Path, str], ...]:
    return tuple(sorted(tree_digest_snapshot(source).items()))


def _exact_metadata_identity_snapshot(
    records: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
) -> tuple[tuple[str, str], ...]:
    snapshot: list[tuple[str, str]] = []
    for identity, entries in sorted(records.items()):
        if len(entries) != 1:
            raise HuroshikiError(
                f"Exact MOD metadata identity is not unique: {identity[0]}:{identity[1]}"
            )
        relative, contents, _mod = entries[0]
        parsed = parse_provider_metadata(relative, contents)
        snapshot.append(
            (f"{identity[0]}:{identity[1]}", parsed.file_id or "")
        )
    return tuple(snapshot)


def _exact_reachability_snapshot(
    reachability: Mapping[tuple[str, str], Mapping[str, str | None]],
) -> tuple[tuple[tuple[str, str], tuple[str, ...]], ...]:
    return tuple(
        (identity, tuple(sorted(owners)))
        for identity, owners in sorted(reachability.items())
    )


def _exact_override_identity_snapshot(
    overrides: Sequence[ModVersionOverride],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{override.canonical_identity}={override.artifact_id}"
            for override in overrides
        )
    )


def _exact_closure_reachability(
    root_closures: Sequence[tuple[PackMigrationRoot, ResolvedModClosure]],
    checkpoint: Callable[[], None],
) -> dict[tuple[str, str], dict[str, str | None]]:
    """Map each resulting identity to the explicit roots that require it."""
    owners: dict[tuple[str, str], dict[str, str | None]] = {}
    for root, closure in root_closures:
        checkpoint()
        root_identity = (root.provider, root.project_id)
        if closure.root_identity != root_identity:
            raise HuroshikiError(
                f"Exact closure root mismatch for {root.canonical_identity}"
            )
        seen: set[tuple[str, str]] = set()
        for item in closure.metadata:
            checkpoint()
            if item.identity in seen:
                raise HuroshikiError(
                    f"Exact closure for {root.canonical_identity} contains duplicate "
                    f"identity {item.provider}:{item.project_id}"
                )
            seen.add(item.identity)
            try:
                metadata_identity = parse_provider_metadata(
                    item.relative_path, item.contents
                )
            except Exception as error:
                raise HuroshikiError(
                    f"Exact closure metadata is invalid for "
                    f"{item.provider}:{item.project_id}: {error}"
                ) from error
            if (
                metadata_identity.provider,
                metadata_identity.project_id,
            ) != item.identity:
                raise HuroshikiError(
                    f"Exact closure metadata identity mismatch for "
                    f"{item.provider}:{item.project_id}"
                )
            owners.setdefault(item.identity, {})[root.canonical_identity] = (
                metadata_identity.file_id
            )
        if root_identity not in seen:
            raise HuroshikiError(
                f"Exact closure does not contain explicit root "
                f"{root.canonical_identity}"
            )
    for identity, requirements in owners.items():
        checkpoint()
        if len(set(requirements.values())) > 1:
            details = ", ".join(
                f"{owner}={artifact_id or '<missing>'}"
                for owner, artifact_id in sorted(requirements.items())
            )
            raise HuroshikiError(
                f"Shared dependency disagreement for {identity[0]}:{identity[1]}: "
                f"{details}"
            )
    return owners


def _exact_metadata_changes(
    current: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
    desired: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
) -> tuple[UpdateChange, ...]:
    changes: list[UpdateChange] = []
    for identity in sorted(current.keys() - desired.keys()):
        entries = current[identity]
        if len(entries) != 1:
            raise HuroshikiError(
                f"Exact closure baseline has duplicate identity "
                f"{identity[0]}:{identity[1]}"
            )
        relative, contents, _ = entries[0]
        changes.append(UpdateChange(relative, contents, None))
    for identity in sorted(desired):
        entries = desired[identity]
        if len(entries) != 1:
            raise HuroshikiError(
                f"Exact closure result has duplicate identity "
                f"{identity[0]}:{identity[1]}"
            )
        desired_relative, desired_contents, _ = entries[0]
        existing = current.get(identity, ())
        if not existing:
            changes.append(UpdateChange(desired_relative, None, desired_contents))
            continue
        if len(existing) != 1:
            raise HuroshikiError(
                f"Exact closure baseline has duplicate identity "
                f"{identity[0]}:{identity[1]}"
            )
        current_relative, current_contents, _ = existing[0]
        if current_relative != desired_relative:
            changes.append(UpdateChange(current_relative, current_contents, None))
            changes.append(UpdateChange(desired_relative, None, desired_contents))
        elif current_contents != desired_contents:
            changes.append(
                UpdateChange(desired_relative, current_contents, desired_contents)
            )
    return tuple(sorted(changes, key=lambda item: item.relative_path))


def _exact_assert_complete_metadata_graph(
    source: Path,
    desired: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
    checkpoint: Callable[[], None],
) -> None:
    actual = _exact_metadata_records(source, checkpoint)
    if actual.keys() != desired.keys():
        missing = sorted(
            f"{identity[0]}:{identity[1]}" for identity in desired.keys() - actual.keys()
        )
        extra = sorted(
            f"{identity[0]}:{identity[1]}" for identity in actual.keys() - desired.keys()
        )
        raise HuroshikiError(
            "Exact closure postcondition disagrees with the resulting graph: "
            f"missing={missing or 'none'}, extra={extra or 'none'}"
        )
    for identity in sorted(desired):
        checkpoint()
        expected_entries = desired[identity]
        actual_entries = actual[identity]
        if len(expected_entries) != 1 or len(actual_entries) != 1:
            raise HuroshikiError(
                f"Exact closure postcondition has duplicate identity "
                f"{identity[0]}:{identity[1]}"
            )
        expected_relative, expected_contents, expected_mod = expected_entries[0]
        actual_relative, actual_contents, actual_mod = actual_entries[0]
        if (
            expected_relative != actual_relative
            or expected_mod.filename != actual_mod.filename
            or expected_mod.side != actual_mod.side
            or _closure_metadata_semantics(expected_contents)
            != _closure_metadata_semantics(actual_contents)
        ):
            raise HuroshikiError(
                f"Exact closure postcondition disagrees for "
                f"{identity[0]}:{identity[1]}"
            )


def _exact_metadata_from_root(
    root: PackMigrationRoot,
    baseline: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
) -> ResolvedMetadata:
    identity = (root.provider, root.project_id)
    entries = baseline.get(identity, ())
    if len(entries) != 1:
        raise HuroshikiError(
            f"Exact root metadata is not unique: {root.canonical_identity}"
        )
    relative, contents, mod = entries[0]
    return ResolvedMetadata(
        identity,
        relative,
        mod.filename,
        contents,
        root.provider,
        root.project_id,
    )


def _verify_exact_closure_artifacts(
    baseline: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
    desired: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
    selection: ExactModArtifactSelection,
    versions: tuple[str, str, str],
    *,
    workspace: Path,
    context_source: Path,
    cancel_event: threading.Event,
    deadline: float,
    process_result_callback: Callable[[BoundedProcessResult], None] | None,
    diagnostic_project_id: str | None,
    diagnostic_callback: Callable[[str], None] | None,
    selected_dependency_roots: set[tuple[str, str]] | None,
    explicit_root_identities: set[tuple[str, str]],
    opaque_url_roots: set[tuple[str, str]],
    checkpoint: Callable[[], None],
) -> tuple[ExactArtifactVerification, ...]:
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    minecraft, loader, loader_version = versions
    context = EquivalenceContext(
        minecraft,
        loader,
        loader_version,
        _equivalence_snapshot_digest(context_source, checkpoint),
        EQUIVALENCE_POLICY_VERSION,
    )
    results: dict[tuple[str, str], ExactArtifactVerification] = {}
    for identity in sorted(desired):
        checkpoint()
        entries = desired[identity]
        if len(entries) != 1:
            raise HuroshikiError(
                f"Exact artifact metadata is not unique for {identity[0]}:{identity[1]}"
            )
        relative, contents, mod = entries[0]
        if identity[0] == "url":
            baseline_entries = baseline.get(identity, ())
            if identity not in opaque_url_roots or len(baseline_entries) != 1:
                raise HuroshikiError(
                    f"Exact selection cannot add or replace URL artifact "
                    f"{identity[0]}:{identity[1]}"
                )
            baseline_relative, baseline_contents, baseline_mod = baseline_entries[0]
            if (
                relative != baseline_relative
                or contents != baseline_contents
                or mod.filename != baseline_mod.filename
                or mod.side != baseline_mod.side
            ):
                raise HuroshikiError(
                    f"Exact selection cannot change opaque URL root "
                    f"{identity[0]}:{identity[1]}"
                )
            continue
        if identity[0] not in {"modrinth", "curseforge"}:
            raise HuroshikiError(f"Unsupported exact artifact provider {identity[0]}")
        provider_identity = parse_provider_metadata(relative, contents)
        artifact_id = provider_identity.file_id
        if artifact_id is None:
            raise HuroshikiError(
                f"Exact artifact metadata has no artifact ID for {identity[0]}:{identity[1]}"
            )
        candidate = _dependency_candidate(
            identity=identity,
            relative_path=relative,
            filename=mod.filename,
            contents=contents,
            side=mod.side,
            provenance="explicit" if identity == selection.identity else "dependency",
            existing=identity in baseline,
        )
        try:
            materialized = materialize_provider_artifact(
                candidate,
                context,
                workspace=workspace,
                cancel_event=cancel_event,
                deadline=deadline,
                process_result_callback=process_result_callback,
                diagnostic_project_id=diagnostic_project_id,
                diagnostic_callback=diagnostic_callback,
            )
        except Exception as error:
            raise HuroshikiError(
                f"Exact artifact materialization failed for {identity[0]}:{identity[1]}: {error}"
            ) from error
        if not isinstance(materialized, MaterializedArtifact):
            raise HuroshikiError(
                f"Exact artifact materialization returned invalid evidence for "
                f"{identity[0]}:{identity[1]}"
            )
        semantic = materialized.semantic_identity
        if semantic is None:
            raise HuroshikiError(
                f"Exact artifact has no resolved {loader} semantic identity: "
                f"{identity[0]}:{identity[1]}"
            )
        if semantic.target_loader != loader.strip().lower():
            raise HuroshikiError(
                f"Exact artifact has incompatible loader metadata for "
                f"{identity[0]}:{identity[1]}"
            )
        results[identity] = ExactArtifactVerification(
            identity,
            artifact_id,
            materialized.sha256,
            semantic,
            materialized.dependency_requirements,
        )
    selected = results.get(selection.identity)
    if selected is None:
        raise HuroshikiError(
            f"Exact selected artifact has no semantic verification: "
            f"{selection.identity_label}"
        )
    old_entries = baseline.get(selection.identity, ())
    if len(old_entries) != 1:
        raise HuroshikiError(
            f"Exact selected artifact baseline is not unique: {selection.identity_label}"
        )
    old_relative, old_contents, old_mod = old_entries[0]
    old_candidate = _dependency_candidate(
        identity=selection.identity,
        relative_path=old_relative,
        filename=old_mod.filename,
        contents=old_contents,
        side=old_mod.side,
        provenance="explicit" if selection.identity == selection.identity else "dependency",
        existing=True,
    )
    try:
        old_materialized = materialize_provider_artifact(
            old_candidate,
            context,
            workspace=workspace,
            cancel_event=cancel_event,
            deadline=deadline,
            process_result_callback=process_result_callback,
            diagnostic_project_id=diagnostic_project_id,
            diagnostic_callback=diagnostic_callback,
        )
    except Exception as error:
        raise HuroshikiError(
            f"Could not verify semantic continuity for {selection.identity_label}: {error}"
        ) from error
    if old_materialized.semantic_identity is None:
        raise HuroshikiError(
            f"Installed artifact has no resolved semantic identity: {selection.identity_label}"
        )
    if old_materialized.semantic_identity.target_loader != loader.strip().lower():
        raise HuroshikiError(
            f"Installed artifact has incompatible loader metadata: "
            f"{selection.identity_label}"
        )
    old_ids = {mod_id for mod_id, _version in old_materialized.semantic_identity.members}
    new_ids = {mod_id for mod_id, _version in selected.semantic_identity.members}
    if old_ids != new_ids:
        raise HuroshikiError(
            f"Exact artifact semantic MOD identity changed for {selection.identity_label}"
        )
    for identity, old_entries in baseline.items():
        if identity == selection.identity or identity not in results:
            continue
        if len(old_entries) != 1:
            raise HuroshikiError(
                f"Exact artifact baseline is not unique for {identity[0]}:{identity[1]}"
            )
        old_relative, old_contents, old_mod = old_entries[0]
        old_candidate = _dependency_candidate(
            identity=identity,
            relative_path=old_relative,
            filename=old_mod.filename,
            contents=old_contents,
            side=old_mod.side,
            provenance="dependency",
            existing=True,
        )
        old_materialized = materialize_provider_artifact(
            old_candidate,
            context,
            workspace=workspace,
            cancel_event=cancel_event,
            deadline=deadline,
            process_result_callback=process_result_callback,
            diagnostic_project_id=diagnostic_project_id,
            diagnostic_callback=diagnostic_callback,
        )
        if old_materialized.semantic_identity is None:
            raise HuroshikiError(
                f"Installed dependency has no resolved semantic identity: "
                f"{identity[0]}:{identity[1]}"
            )
        if old_materialized.semantic_identity.target_loader != loader.strip().lower():
            raise HuroshikiError(
                f"Installed dependency has incompatible loader metadata: "
                f"{identity[0]}:{identity[1]}"
            )
        old_ids = {
            mod_id for mod_id, _version in old_materialized.semantic_identity.members
        }
        new_ids = {
            mod_id
            for mod_id, _version in results[identity].semantic_identity.members
        }
        if old_ids != new_ids:
            raise HuroshikiError(
                f"Exact dependency semantic MOD identity changed for "
                f"{identity[0]}:{identity[1]}"
            )
    verifications = tuple(results[identity] for identity in sorted(results))
    graph = _build_exact_dependency_graph(
        verifications, explicit_root_identities, checkpoint
    )
    _validate_exact_dependency_graph(
        graph,
        verifications,
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        checkpoint=checkpoint,
    )
    if selected_dependency_roots is not None:
        _assert_exact_selected_dependency_reachability(
            graph, selection.identity, selected_dependency_roots
        )
    return verifications


_EXACT_RUNTIME_MOD_IDS = frozenset(
    {"minecraft", "java", "fabricloader", "quilt_loader", "forge", "neoforge"}
)


def _build_exact_dependency_graph(
    verifications: Sequence[ExactArtifactVerification],
    explicit_root_identities: set[tuple[str, str]],
    checkpoint: Callable[[], None],
) -> ExactDependencyGraph:
    """Build immutable required-edge evidence from materialized loader metadata."""
    artifacts = {item.identity: item for item in verifications}
    bindings: dict[str, tuple[str, str]] = {}
    for artifact in verifications:
        checkpoint()
        for mod_id, _version in artifact.semantic_identity.members:
            existing = bindings.get(mod_id)
            if existing is not None and existing != artifact.identity:
                raise HuroshikiError(
                    f"Exact dependency semantic MOD binding is ambiguous for {mod_id}: "
                    f"{existing[0]}:{existing[1]} and "
                    f"{artifact.identity[0]}:{artifact.identity[1]}"
                )
            bindings[mod_id] = artifact.identity

    edges: list[ExactDependencyEdge] = []
    edges_by_parent: dict[tuple[str, str], list[ExactDependencyEdge]] = {}
    for artifact in verifications:
        checkpoint()
        requirements = artifact.dependency_requirements
        if requirements is None:
            continue
        for requirement in requirements:
            if requirement.mod_id in _EXACT_RUNTIME_MOD_IDS:
                continue
            child_identity = bindings.get(requirement.mod_id)
            if child_identity is None:
                raise HuroshikiError(
                    f"Exact dependency requirement cannot be bound for "
                    f"{artifact.identity[0]}:{artifact.identity[1]}: "
                    f"{requirement.mod_id}"
                )
            edge = ExactDependencyEdge(
                artifact.identity,
                child_identity,
                requirement.mod_id,
                requirement.version_range,
            )
            edges.append(edge)
            edges_by_parent.setdefault(artifact.identity, []).append(edge)

    reached_by: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for root_identity in sorted(explicit_root_identities):
        checkpoint()
        if root_identity[0] == "url":
            continue
        if root_identity not in artifacts:
            raise HuroshikiError(
                f"Exact dependency root evidence is missing for "
                f"{root_identity[0]}:{root_identity[1]}"
            )
        pending = [root_identity]
        seen: set[tuple[str, str]] = set()
        while pending:
            checkpoint()
            identity = pending.pop()
            if identity in seen:
                continue
            seen.add(identity)
            reached_by.setdefault(identity, set()).add(root_identity)
            artifact = artifacts[identity]
            if artifact.dependency_requirements is None:
                raise HuroshikiError(
                    f"Exact dependency graph evidence is unavailable for "
                    f"{identity[0]}:{identity[1]}"
                )
            pending.extend(
                edge.child_identity for edge in edges_by_parent.get(identity, ())
            )

    return ExactDependencyGraph(
        tuple(sorted(bindings.items())),
        tuple(sorted(edges, key=lambda item: (
            item.parent_identity,
            item.child_identity,
            item.required_mod_id,
            item.version_range,
        ))),
        tuple(
            (identity, frozenset(roots))
            for identity, roots in sorted(reached_by.items())
        ),
    )



def _validate_exact_runtime_compatibility(
    verification: ExactArtifactVerification,
    *,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> None:
    requirements = verification.dependency_requirements
    if requirements is None:
        raise HuroshikiError(
            f"Exact dependency graph evidence is unavailable for "
            f"{verification.identity[0]}:{verification.identity[1]}"
        )
    loader_id = loader.strip().lower()
    runtime_versions = {"minecraft": minecraft}
    if loader_id == "fabric":
        runtime_versions["fabricloader"] = loader_version
    elif loader_id == "quilt":
        runtime_versions["quilt_loader"] = loader_version
    elif loader_id in {"forge", "neoforge"}:
        runtime_versions[loader_id] = loader_version

    for requirement in requirements:
        if requirement.mod_id not in _EXACT_RUNTIME_MOD_IDS:
            continue
        if requirement.mod_id == "java":
            continue
        version = runtime_versions.get(requirement.mod_id)
        if version is None:
            raise HuroshikiError(
                f"Exact runtime requirement {requirement.mod_id} does not match "
                f"the fixed Pack loader {loader_id}"
            )
        accepted = version_satisfies_requirement(version, requirement.version_range)
        label = f"{verification.identity[0]}:{verification.identity[1]}"
        if accepted is None:
            raise HuroshikiError(
                f"Exact runtime compatibility is unverifiable for {label}: "
                f"{requirement.mod_id} {requirement.version_range}"
            )
        if not accepted:
            raise HuroshikiError(
                f"Exact runtime compatibility conflict: {label} requires "
                f"{requirement.mod_id} {requirement.version_range}, fixed {version}"
            )


def _validate_exact_dependency_graph(
    graph: ExactDependencyGraph,
    verifications: Sequence[ExactArtifactVerification],
    *,
    minecraft: str,
    loader: str,
    loader_version: str,
    checkpoint: Callable[[], None],
) -> None:
    artifacts = {item.identity: item for item in verifications}
    reachable = {identity for identity, _roots in graph.root_reachability}
    for identity in sorted(reachable):
        checkpoint()
        verification = artifacts[identity]
        _validate_exact_runtime_compatibility(
            verification,
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
        )
    for edge in graph.edges:
        checkpoint()
        if edge.parent_identity not in reachable:
            continue
        child = artifacts[edge.child_identity]
        version = dict(child.semantic_identity.members)[edge.required_mod_id]
        accepted = version_satisfies_requirement(version, edge.version_range)
        parent_label = f"{edge.parent_identity[0]}:{edge.parent_identity[1]}"
        if accepted is None:
            raise HuroshikiError(
                f"Exact dependency graph compatibility is unverifiable for {parent_label} "
                f"requirement {edge.required_mod_id} {edge.version_range}"
            )
        if not accepted:
            raise HuroshikiError(
                f"Exact dependency graph conflict: {parent_label} requires "
                f"{edge.required_mod_id} {edge.version_range}, found {version}"
            )


def _assert_exact_selected_dependency_reachability(
    graph: ExactDependencyGraph,
    selected_identity: tuple[str, str],
    expected_selected_roots: set[tuple[str, str]],
) -> None:
    selected_roots = graph.reachable_roots(selected_identity)
    if selected_roots != expected_selected_roots:
        raise HuroshikiError(
            "Exact dependency required-edge reachability changed: "
            f"expected={sorted(expected_selected_roots)}, actual={sorted(selected_roots)}"
        )
    reachable = {identity for identity, _roots in graph.root_reachability}
    if not any(
        edge.child_identity == selected_identity and edge.parent_identity in reachable
        for edge in graph.edges
    ):
        raise HuroshikiError(
            f"Exact dependency {selected_identity[0]}:{selected_identity[1]} has no "
            "reachable machine-readable required edge"
        )
def _exact_verification_digest(

    verifications: Sequence[ExactArtifactVerification],
) -> str:
    payload = [
        {
            "identity": verification.identity,
            "artifact_id": verification.artifact_id,
            "sha256": verification.sha256,
            "semantic": {
                "members": verification.semantic_identity.members,
                "loader": verification.semantic_identity.target_loader,
            },
            "requirements": (
                None
                if verification.dependency_requirements is None
                else tuple(
                    (item.mod_id, item.version_range)
                    for item in verification.dependency_requirements
                )
            ),
        }
        for verification in verifications
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _exact_verification_binding_digest(
    verifications: Sequence[ExactArtifactVerification],
    records: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
) -> str:
    payload = [
        {
            "identity": identity,
            "artifact_id": parse_provider_metadata(relative, contents).file_id,
            "sha256": next(
                item.sha256 for item in verifications if item.identity == identity
            ),
            "semantic": next(
                {
                    "members": item.semantic_identity.members,
                    "loader": item.semantic_identity.target_loader,
                }
                for item in verifications
                if item.identity == identity
            ),
            "requirements": next(
                (
                    None
                    if item.dependency_requirements is None
                    else tuple(
                        (requirement.mod_id, requirement.version_range)
                        for requirement in item.dependency_requirements
                    )
                )
                for item in verifications
                if item.identity == identity
            ),
        }
        for identity, entries in sorted(records.items())
        for relative, contents, mod in entries
        if identity[0] in {"modrinth", "curseforge"}
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_mod_version_override_records(
    source: Path,
    records: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
    *,
    overrides: tuple[ModVersionOverride, ...] | None = None,
) -> tuple[ModVersionOverride, ...]:
    if overrides is None:
        try:
            overrides = read_mod_version_overrides(source)
        except ModVersionOverrideError as error:
            raise HuroshikiError(str(error)) from error
    for override in overrides:
        matching = records.get((override.provider, override.project_id), ())
        if len(matching) != 1:
            raise HuroshikiError(
                f"Version override identity is missing or ambiguous: "
                f"{override.canonical_identity}"
            )
        metadata = parse_provider_metadata(matching[0][0], matching[0][1])
        if metadata.file_id != override.artifact_id:
            raise HuroshikiError(
                f"Version override artifact drifted: {override.canonical_identity} "
                f"expected {override.artifact_id}, found {metadata.file_id or '<missing>'}"
            )
    return overrides


def canonical_mod_version_identity(identity: str) -> str:
    if not isinstance(identity, str) or identity.count(":") != 1:
        raise HuroshikiError(
            "MOD identity must use curseforge:<project-id> or modrinth:<project-id>"
        )
    provider, project_id = identity.split(":", 1)
    if provider == "modrinth":
        project_id = str(
            canonical_modrinth_id(project_id, "Modrinth project ID")
        )
    elif provider == "curseforge":
        project_id = canonical_curseforge_project_id(project_id)
    else:
        raise HuroshikiError(
            "MOD identity must use curseforge:<project-id> or modrinth:<project-id>"
        )
    return f"{provider}:{project_id}"


def mod_version_intent_status(
    source: Path,
    identity: str,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> ModVersionIntentStatus:
    _run_checkpoint(checkpoint)
    canonical_identity = canonical_mod_version_identity(identity)
    provider, project_id = canonical_identity.split(":", 1)
    records = _exact_metadata_records(source, checkpoint)
    matching = records.get((provider, project_id), ())
    if len(matching) > 1:
        raise HuroshikiError(
            f"MOD version intent identity is ambiguous: {canonical_identity}"
        )
    installed = (
        None
        if not matching
        else parse_provider_metadata(matching[0][0], matching[0][1]).file_id
    )
    try:
        override = get_mod_version_override(
            source, canonical_identity, checkpoint=checkpoint
        )
    except ModVersionOverrideError as error:
        raise HuroshikiError(str(error)) from error
    if override is None:
        return ModVersionIntentStatus(
            canonical_identity,
            "automatic",
            installed,
            None,
            None,
            None,
            None,
        )
    status: Literal["active", "drifted", "stale"] = (
        "stale"
        if installed is None
        else "active"
        if installed == override.artifact_id
        else "drifted"
    )
    return ModVersionIntentStatus(
        canonical_identity,
        "user",
        installed,
        override.artifact_id,
        override.locked,
        override.reason,
        status,
    )


def installed_mod_version_intent(
    project_key_value: str,
    mod: ModInfo,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ModVersionIntentStatus:
    def checkpoint() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExactModVersionCancelled(
                "Installed MOD version intent loading was cancelled"
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise ExactModVersionDeadlineExceeded(
                "Installed MOD version intent loading deadline exceeded"
            )

    checkpoint()
    kind, _project_id = split_project_key(project_key_value)
    if kind != "pack":
        raise HuroshikiError("MOD version intent is available only for packs")
    provider = canonical_provider(mod.provider)
    identity = canonical_mod_version_identity(f"{provider}:{mod.project_id}")
    return mod_version_intent_status(
        project_root(project_key_value) / "source",
        identity,
        checkpoint=checkpoint,
    )


VERSION_CATALOG_PACK_TOML_MAX_BYTES = 1024 * 1024
VERSION_CATALOG_METADATA_MAX_BYTES = 2 * 1024 * 1024
VERSION_CATALOG_METADATA_TOTAL_MAX_BYTES = 64 * 1024 * 1024
VERSION_CATALOG_METADATA_MAX_FILES = 10_000
VERSION_CATALOG_SOURCE_MAX_ENTRIES = 20_000
VERSION_CATALOG_SOURCE_MAX_TOTAL_BYTES = 128 * 1024 * 1024


def _mod_version_catalog_source_snapshot(
    source: Path,
    checkpoint: Callable[[], None],
) -> tuple[PackTreeScan, bytes, dict[Path, bytes], bytes | None]:
    """Read only catalog inputs through one descriptor-verified tree scan."""
    try:
        scan = scan_pack_migration_source(
            source,
            checkpoint=checkpoint,
            max_entries=VERSION_CATALOG_SOURCE_MAX_ENTRIES,
            max_total_file_bytes=VERSION_CATALOG_SOURCE_MAX_TOTAL_BYTES,
        )
    except (OSError, PackTreePolicyError) as error:
        raise HuroshikiError(f"Could not inspect Packwiz source safely: {error}") from error
    if any(entry.kind == "invalid" or entry.errors for entry in scan.entries):
        raise HuroshikiError("Packwiz source contains unsafe filesystem entries")
    files = {
        entry.relative_path: entry
        for entry in scan.entries
        if entry.kind == "file"
    }
    pack_entry = files.get(Path("pack.toml"))
    if pack_entry is None:
        raise HuroshikiError(f"{source / 'pack.toml'}: missing required file")
    if pack_entry.size > VERSION_CATALOG_PACK_TOML_MAX_BYTES:
        raise HuroshikiError("pack.toml exceeds the candidate catalog size limit")
    metadata_entries = tuple(
        entry
        for relative, entry in sorted(files.items())
        if relative.name.endswith(".pw.toml")
    )
    if len(metadata_entries) > VERSION_CATALOG_METADATA_MAX_FILES:
        raise HuroshikiError("Packwiz metadata exceeds the candidate catalog file limit")
    if any(
        entry.size > VERSION_CATALOG_METADATA_MAX_BYTES
        for entry in metadata_entries
    ):
        raise HuroshikiError("Packwiz metadata exceeds the candidate catalog file size limit")
    if sum(entry.size for entry in metadata_entries) > VERSION_CATALOG_METADATA_TOTAL_MAX_BYTES:
        raise HuroshikiError("Packwiz metadata exceeds the candidate catalog total size limit")
    try:
        pack_contents = read_pack_control_file(
            source,
            scan,
            Path("pack.toml"),
            max_bytes=VERSION_CATALOG_PACK_TOML_MAX_BYTES,
            checkpoint=checkpoint,
        )
        if hashlib.sha256(pack_contents).hexdigest() != pack_entry.digest:
            raise HuroshikiError("pack.toml changed after the catalog snapshot")
        metadata = {
            entry.relative_path: read_pack_control_file(
                source,
                scan,
                entry.relative_path,
                max_bytes=VERSION_CATALOG_METADATA_MAX_BYTES,
                checkpoint=checkpoint,
            )
            for entry in metadata_entries
        }
        if any(
            hashlib.sha256(metadata[entry.relative_path]).hexdigest()
            != entry.digest
            for entry in metadata_entries
        ):
            raise HuroshikiError("Packwiz metadata changed after the catalog snapshot")
        manifest_entry = files.get(VERSION_OVERRIDE_MANIFEST_PATH)
        manifest_contents = (
            None
            if manifest_entry is None
            else read_pack_control_file(
                source,
                scan,
                VERSION_OVERRIDE_MANIFEST_PATH,
                max_bytes=VERSION_OVERRIDE_MANIFEST_MAX_BYTES,
                checkpoint=checkpoint,
            )
        )
        if (
            manifest_entry is not None
            and manifest_contents is not None
            and hashlib.sha256(manifest_contents).hexdigest()
            != manifest_entry.digest
        ):
            raise HuroshikiError(
                "Version override manifest changed after the catalog snapshot"
            )
    except PackMigrationRootError as error:
        raise HuroshikiError(str(error)) from error
    return scan, pack_contents, metadata, manifest_contents


def list_mod_version_candidates(
    project_key_value: str,
    identity: str,
    *,
    include_prerelease: bool = False,
    limit: int = 20,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ModVersionCandidateCatalog:
    """Read-only catalog of provider versions for an installed pack MOD."""
    operation_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PROVIDER_LOOKUP_TIMEOUT_SECONDS
    )

    def checkpoint() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExactModVersionCancelled("MOD version candidate listing was cancelled")
        if time.monotonic() >= operation_deadline:
            raise ExactModVersionDeadlineExceeded(
                "MOD version candidate listing deadline exceeded"
            )

    checkpoint()
    kind, _project_id = split_project_key(project_key_value)
    if kind != "pack":
        raise HuroshikiError("MOD version candidate listing is available only for packs")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise HuroshikiError("MOD version candidate limit must be between 1 and 100")
    if not isinstance(include_prerelease, bool):
        raise HuroshikiError("include_prerelease must be a boolean")
    if isinstance(identity, str) and identity.startswith("url:"):
        raise HuroshikiError("Provider version catalog is unavailable for URL artifacts")
    if isinstance(identity, str) and identity.startswith("curseforge:"):
        # This is intentionally checked before any filesystem or child-process work.
        canonical_mod_version_identity(identity)
        raise HuroshikiError(
            "Provider version catalog is not currently available for CurseForge; "
            "enter an exact file ID instead."
        )
    canonical_identity = canonical_mod_version_identity(identity)
    provider, project_id = canonical_identity.split(":", 1)
    if provider != "modrinth":
        raise HuroshikiError(
            "Provider version catalog is not currently available for CurseForge; "
            "enter an exact file ID instead."
        )

    source = project_root(project_key_value) / "source"
    before, pack_contents, metadata_snapshot, manifest_contents = (
        _mod_version_catalog_source_snapshot(source, checkpoint)
    )
    pack_file = Path("pack.toml")
    try:
        pack_data = tomllib.loads(pack_contents.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"{source / pack_file}: {error}") from error
    versions = pack_data.get("versions", {})
    if not isinstance(versions, dict):
        raise HuroshikiError("pack.toml has invalid versions")
    minecraft = versions.get("minecraft")
    loaders = [name for name in packctl.LOADER_FLAGS if name in versions]
    if (
        not isinstance(minecraft, str)
        or not minecraft.strip()
        or len(loaders) != 1
    ):
        raise HuroshikiError(
            "Installed pack has an ambiguous or missing Minecraft/loader target"
        )
    minecraft = minecraft.strip()
    loader = loaders[0]
    loader_version = versions.get(loader)
    if not isinstance(loader_version, str) or not loader_version.strip():
        raise HuroshikiError(
            "Installed pack has an ambiguous or missing Minecraft/loader target"
        )

    records: dict[tuple[str, str], list[tuple[Path, bytes, ModInfo]]] = {}
    for relative, contents in sorted(metadata_snapshot.items()):
        checkpoint()
        try:
            mod = read_mod_data(
                relative,
                tomllib.loads(contents.decode("utf-8")),
            )
        except (UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
            raise HuroshikiError(
                f"MOD version candidate metadata {relative} is invalid: {error}"
            ) from error
        record_identity = canonical_provider(mod.provider), mod.project_id
        records.setdefault(record_identity, []).append((relative, contents, mod))
    matching = records.get((provider, project_id), ())
    if len(matching) > 1:
        raise HuroshikiError(
            f"MOD version candidate identity is ambiguous: {canonical_identity}"
        )
    installed = None
    if matching:
        installed = parse_provider_metadata(matching[0][0], matching[0][1]).file_id

    try:
        overrides = (
            ()
            if manifest_contents is None
            else parse_mod_version_overrides(manifest_contents).entries
        )
    except ModVersionOverrideError as error:
        raise HuroshikiError(str(error)) from error
    override = next(
        (
            item
            for item in overrides
            if item.canonical_identity == canonical_identity
        ),
        None,
    )
    if installed is None and override is None:
        raise HuroshikiError(
            f"Installed MOD version target is missing: {canonical_identity}"
        )
    if override is None:
        intent = ModVersionIntentStatus(
            canonical_identity,
            "automatic",
            installed,
            None,
            None,
            None,
            None,
        )
    else:
        status: Literal["active", "drifted", "stale"] = (
            "stale"
            if installed is None
            else "active"
            if installed == override.artifact_id
            else "drifted"
        )
        intent = ModVersionIntentStatus(
            canonical_identity,
            "user",
            installed,
            override.artifact_id,
            override.locked,
            override.reason,
            status,
        )
    arguments = [
        "modrinth",
        "versions",
        project_id,
        "--minecraft",
        minecraft,
        "--loader",
        loader,
        "--limit",
        str(limit),
    ]
    if include_prerelease:
        arguments.append("--include-prerelease")
    raw = _run_provider_lookup(
        arguments,
        cancel_event=cancel_event,
        deadline=operation_deadline,
    )
    checkpoint()
    if not isinstance(raw, list) or len(raw) > limit:
        raise HuroshikiError("Provider lookup returned an invalid version catalog")
    fields = {
        "provider",
        "project_id",
        "artifact_id",
        "version",
        "filename",
        "game_versions",
        "loaders",
        "release_type",
        "published_at",
    }
    candidates: list[ModVersionCandidate] = []
    published_dates: dict[str, datetime] = {}
    seen: set[str] = set()
    for record in raw:
        checkpoint()
        item = _provider_protocol_mapping(
            record,
            fields=fields,
            context="version candidate",
        )
        text_values: dict[str, str] = {}
        for key, maximum in (
            ("provider", 32),
            ("project_id", 64),
            ("artifact_id", 64),
            ("version", 256),
            ("filename", 512),
            ("published_at", 256),
        ):
            value = item[key]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                )
            ):
                raise HuroshikiError(f"Provider lookup returned invalid {key}")
            text_values[key] = value
        if (
            text_values["provider"] != "modrinth"
            or text_values["project_id"] != project_id
        ):
            raise HuroshikiError("Provider lookup returned a mismatched project identity")
        artifact_id = canonical_modrinth_id(
            text_values["artifact_id"],
            "Modrinth version ID",
        )
        if text_values["artifact_id"] in seen:
            raise HuroshikiError("Provider lookup returned duplicate version artifacts")
        seen.add(text_values["artifact_id"])
        if item["release_type"] not in {"release", "beta", "alpha"}:
            raise HuroshikiError("Provider lookup returned an invalid release type")
        if not include_prerelease and item["release_type"] != "release":
            raise HuroshikiError(
                "Provider lookup returned an unexpected prerelease candidate"
            )

        def tuple_field(key: str) -> tuple[str, ...]:
            value = item[key]
            if (
                not isinstance(value, list)
                or not value
                or any(
                    not isinstance(item_value, str)
                    or not item_value
                    or len(item_value) > 256
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in item_value
                    )
                    for item_value in value
                )
            ):
                raise HuroshikiError(f"Provider lookup returned invalid {key}")
            return tuple(value)

        game_versions = tuple_field("game_versions")
        candidate_loaders = tuple_field("loaders")
        if minecraft not in game_versions or loader not in candidate_loaders:
            raise HuroshikiError("Provider lookup returned an incompatible version candidate")
        try:
            published = datetime.fromisoformat(
                text_values["published_at"].replace("Z", "+00:00")
            )
            if published.tzinfo is None or published.utcoffset() is None:
                raise ValueError
        except ValueError as error:
            raise HuroshikiError("Provider lookup returned an invalid publication date") from error
        published_dates[str(artifact_id)] = published.astimezone(timezone.utc)
        candidates.append(
            ModVersionCandidate(
                text_values["provider"],
                canonical_modrinth_id(
                    text_values["project_id"],
                    "Modrinth project ID",
                ),
                artifact_id,
                text_values["version"],
                text_values["filename"],
                game_versions,
                candidate_loaders,
                item["release_type"],
                text_values["published_at"],
            )
        )
    candidates.sort(key=lambda candidate: candidate.artifact_id)
    candidates.sort(
        key=lambda candidate: published_dates[candidate.artifact_id],
        reverse=True,
    )
    views = tuple(
        ModVersionCandidateView(
            candidate,
            candidate.artifact_id == installed,
            override is not None and candidate.artifact_id == override.artifact_id,
            override is not None
            and candidate.artifact_id == override.artifact_id
            and override.locked,
            True,
            (),
        )
        for candidate in candidates
    )
    try:
        after = scan_pack_migration_source(
            source,
            checkpoint=checkpoint,
            max_entries=VERSION_CATALOG_SOURCE_MAX_ENTRIES,
            max_total_file_bytes=VERSION_CATALOG_SOURCE_MAX_TOTAL_BYTES,
        )
    except (OSError, PackTreePolicyError) as error:
        raise HuroshikiError(
            f"Could not revalidate Packwiz source safely: {error}"
        ) from error
    if any(entry.kind == "invalid" or entry.errors for entry in after.entries):
        raise HuroshikiError("Packwiz source contains unsafe filesystem entries")
    if (
        after.root_identity != before.root_identity
        or after.snapshot_digest != before.snapshot_digest
    ):
        raise HuroshikiError("Pack source changed while listing MOD version candidates")
    return ModVersionCandidateCatalog(
        canonical_identity,
        minecraft,
        loader,
        views,
        intent,
        override is not None
        and override.artifact_id
        not in {candidate.artifact_id for candidate in candidates},
    )


def inspect_mod_version_overrides(source: Path) -> tuple[ModVersionOverrideStatus, ...]:
    records = _exact_metadata_records(source)
    try:
        overrides = read_mod_version_overrides(source)
    except ModVersionOverrideError as error:
        raise HuroshikiError(str(error)) from error
    statuses: list[ModVersionOverrideStatus] = []
    for override in overrides:
        matching = records.get((override.provider, override.project_id), ())
        if len(matching) > 1:
            raise HuroshikiError(
                f"Version override identity is ambiguous: {override.canonical_identity}"
            )
        installed = (
            None
            if not matching
            else parse_provider_metadata(matching[0][0], matching[0][1]).file_id
        )
        status: Literal["active", "drifted", "stale"] = (
            "stale"
            if installed is None
            else "active"
            if installed == override.artifact_id
            else "drifted"
        )
        statuses.append(ModVersionOverrideStatus(override, status, installed))
    return tuple(statuses)


def _preserve_exact_selected_side(
    source: Path,
    selection: ExactModArtifactSelection,
    selected_side: str,
    checkpoint: Callable[[], None],
) -> None:
    records = _exact_metadata_records(source, checkpoint)
    matching = records.get(selection.identity, ())
    if len(matching) != 1:
        raise HuroshikiError(
            f"Exact selected identity {selection.identity_label} has "
            f"{len(matching)} metadata records"
        )
    relative, contents, _ = matching[0]
    updated = _metadata_contents_with_side(contents, selected_side)
    if updated != contents:
        safe_child(source, relative).write_bytes(updated)


def _merge_exact_existing_dependency_sides(
    source: Path,
    baseline: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
    selected_identity: tuple[str, str],
    explicit_identities: set[tuple[str, str]],
    checkpoint: Callable[[], None],
) -> None:
    """Union installed coverage for unchanged dependency identities."""
    for identity, entries in _exact_metadata_records(source, checkpoint).items():
        checkpoint()
        if identity == selected_identity or identity in explicit_identities:
            continue
        if len(entries) != 1:
            raise HuroshikiError(
                f"Exact closure has duplicate identity {identity[0]}:{identity[1]}"
            )
        existing = baseline.get(identity, ())
        if not existing:
            continue
        if len(existing) != 1:
            raise HuroshikiError(
                f"Exact closure baseline has duplicate identity "
                f"{identity[0]}:{identity[1]}"
            )
        relative, contents, resolved_mod = entries[0]
        existing_mod = existing[0][2]
        if existing_mod.side_error is not None:
            raise HuroshikiError(
                f"Cannot preserve invalid existing side for "
                f"{existing_mod.relative_path}: {existing_mod.side_error}"
            )
        updated = _metadata_contents_with_side(
            contents,
            union_side(existing_mod.side, resolved_mod.side),
        )
        if updated != contents:
            safe_child(source, relative).write_bytes(updated)


def _exact_manifest_bytes(source: Path) -> bytes | None:
    path = safe_child(source, ROOT_MANIFEST_PATH)
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise HuroshikiError("Exact MOD selection requires a regular root manifest")
    return path.read_bytes()


def _exact_assert_root_manifest_identities(
    source: Path,
    records: dict[tuple[str, str], tuple[tuple[Path, bytes, ModInfo], ...]],
) -> None:
    manifest = safe_child(source, ROOT_MANIFEST_PATH)
    if not manifest.is_file() or manifest.is_symlink():
        return
    for root in read_pack_root_manifest(source):
        identity = tuple(root.canonical_identity.split(":", 1))
        if len(records.get(identity, ())) != 1:
            raise HuroshikiError(
                f"Exact MOD selection removed or duplicated root "
                f"{root.canonical_identity}"
            )


def _exact_run_refresh(
    source: Path,
    *,
    cancel_event: threading.Event,
    deadline: float,
    checkpoint: Callable[[], None],
    process_result_callback: Callable[[BoundedProcessResult], None] | None,
    diagnostic_project_id: str | None = None,
    diagnostic_callback: Callable[[str], None] | None = None,
) -> None:
    checkpoint()
    process_kwargs: dict[str, object] = {
        "cwd": source,
        "cancel_event": cancel_event,
        "deadline": min(
            deadline,
            time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
        ),
    }
    if process_result_callback is not None:
        process_kwargs["result_callback"] = process_result_callback
    result = run_resolver_process(["packwiz", "refresh"], **process_kwargs)
    diagnostic = _record_packwiz_process_diagnostic(
        ["packwiz", "refresh"],
        result,
        project_id=diagnostic_project_id,
        operation="exact-refresh",
        callback=diagnostic_callback,
    )
    failure = _exact_process_failure(
        result, label="Exact Packwiz refresh", command=["packwiz", "refresh"]
    )
    if failure is not None:
        raise HuroshikiError(_packwiz_diagnostic_detail(failure, diagnostic))
    checkpoint()


class LoaderMigrationCancelled(HuroshikiError):
    pass


class LoaderMigrationDeadlineExceeded(HuroshikiError):
    pass


class LoaderMigrationOperation:
    def __init__(
        self,
        project_key: str,
        requested_version: str,
        *,
        deadline: float | None = None,
    ) -> None:
        kind, _ = split_project_key(project_key)
        if kind != "pack":
            raise HuroshikiError("Loader migration is available only for packs")
        if not requested_version or requested_version != requested_version.strip():
            raise HuroshikiError(
                "Loader version must be non-empty and have no surrounding whitespace"
            )
        try:
            packctl.validate_project_text("Loader version", requested_version)
        except packctl.ConfigError as error:
            raise HuroshikiError(str(error)) from error
        self.project_key = project_key
        self.requested_version = requested_version
        self.deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + LOADER_MIGRATION_TIMEOUT_SECONDS
        )
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.progress_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.preview: LoaderMigrationPreview | None = None
        self.error: BaseException | None = None
        self.transaction: PackTransaction | None = None
        self.cancelled = False
        self._started = False
        self._finished = False
        self._state_lock = threading.Lock()

    def _checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise LoaderMigrationCancelled("Loader migration was cancelled")
        if time.monotonic() >= self.deadline:
            raise LoaderMigrationDeadlineExceeded("Loader migration deadline exceeded")

    def _run_packwiz(self, command: list[str], step: str) -> None:
        self._checkpoint()
        self.progress_queue.put(step)
        operation = (
            "loader-migrate"
            if command[2:4] == ["migrate", "loader"]
            else "refresh"
        )
        result = run_resolver_process(
            command,
            cwd=self.transaction.source if self.transaction is not None else ROOT,
            cancel_event=self.cancel_event,
            deadline=min(
                self.deadline,
                time.monotonic() + LOADER_MIGRATION_PROCESS_TIMEOUT_SECONDS,
            ),
        )
        diagnostic = _record_packwiz_process_diagnostic(
            command,
            result,
            project_id=self.project_key.partition(":")[2],
            operation=operation,
            callback=self.progress_queue.put,
        )

        if result.termination_incomplete:
            raise HuroshikiError(
                f"{step}: {_packwiz_diagnostic_detail('Packwiz process termination was incomplete', diagnostic)}"
            )
        if result.orphaned_descendants:
            raise HuroshikiError(
                f"{step}: {_packwiz_diagnostic_detail('Packwiz left background processes', diagnostic)}"
            )
        if result.cancelled:
            raise LoaderMigrationCancelled(
                _packwiz_diagnostic_detail("Loader migration was cancelled", diagnostic)
            )
        if result.timed_out:
            raise LoaderMigrationDeadlineExceeded(
                _packwiz_diagnostic_detail(f"{step} timed out", diagnostic)
            )
        if result.output_limit_exceeded:
            raise HuroshikiError(
                f"{step}: "
                + _packwiz_diagnostic_detail(
                    "Packwiz exceeded the supported output limit", diagnostic
                )
            )
        if result.returncode != 0:
            detail = packctl._redacted_packwiz_output(
                command, result.stderr or result.stdout
            ).strip()
            suffix = f": {detail}" if detail else ""
            raise HuroshikiError(
                _packwiz_diagnostic_detail(
                    f"{step} failed with exit {result.returncode}{suffix}",
                    diagnostic,
                )
            )
        self._checkpoint()

    def run(self) -> None:
        with self._state_lock:
            if self._started or self.done.is_set():
                return
            self._started = True
        try:
            self.progress_queue.put("Creating transaction")
            self.transaction = PackTransaction.create(
                self.project_key,
                checkpoint=self._checkpoint,
            )
            before_versions = packctl.project_versions(self.transaction.source)
            before = _file_content_snapshot(self.transaction.source, self._checkpoint)
            self._run_packwiz(
                [
                    "packwiz",
                    "--yes",
                    "migrate",
                    "loader",
                    self.requested_version,
                ],
                "Loader migration",
            )
            self._run_packwiz(["packwiz", "refresh"], "Packwiz refresh")
            ensure_safe_pack_source(self.transaction.source, checkpoint=self._checkpoint)
            after_versions = packctl.project_versions(self.transaction.source)
            if after_versions[0] != before_versions[0]:
                raise HuroshikiError(
                    "Loader migration changed the Minecraft version; transaction rejected"
                )
            if after_versions[1] != before_versions[1]:
                raise HuroshikiError(
                    "Loader migration changed the loader type; transaction rejected"
                )
            if not after_versions[2]:
                raise HuroshikiError(
                    "Loader migration produced an empty loader version; transaction rejected"
                )
            after = _file_content_snapshot(self.transaction.source, self._checkpoint)
            warnings = (
                ("URL MOD compatibility cannot be verified",)
                if any(
                    record.provider == "url"
                    for record in _update_metadata_snapshot(
                        self.transaction.source,
                        self._checkpoint,
                    ).values()
                )
                else ()
            )
            self.preview = LoaderMigrationPreview(
                self.project_key,
                before_versions[0],
                before_versions[1],
                before_versions[2],
                after_versions[2],
                _content_changes(before, after),
                warnings,
            )
            self.progress_queue.put("Preview ready")
        except LoaderMigrationCancelled:
            self.cancelled = True
        except BaseException as error:
            self.error = error
        finally:
            if self.cancel_event.is_set():
                self.cancelled = True
            if self.cancelled or self.error is not None:
                try:
                    self._discard_once()
                except BaseException as error:
                    if self.error is None:
                        self.error = error
            self.done.set()

    def apply(self) -> None:
        with self._state_lock:
            if (
                not self.done.is_set()
                or self.cancelled
                or self.error is not None
                or self.preview is None
                or self.transaction is None
                or self._finished
            ):
                raise HuroshikiError("Loader migration has no applicable preview")
            self._finished = True
        try:
            if self.preview.changes:
                self.transaction.apply(refresh=False)
            else:
                self.transaction.discard()
        except BaseException:
            self.transaction.discard()
            raise

    def _discard_once(self) -> None:
        with self._state_lock:
            if self._finished:
                return
            self._finished = True
        if self.transaction is not None:
            self.transaction.discard()

    def discard(self) -> None:
        self._discard_once()

    def cancel(self) -> None:
        self.cancelled = True
        self.cancel_event.set()
        if self.done.is_set() or not self._started:
            self._discard_once()
            self.done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def drain_progress(self) -> tuple[str, ...]:
        values: list[str] = []
        while True:
            try:
                values.append(self.progress_queue.get_nowait())
            except queue.Empty:
                return tuple(values)


def prepare_loader_migration(
    project_key: str,
    requested_version: str,
    *,
    deadline: float | None = None,
) -> LoaderMigrationOperation:
    operation = LoaderMigrationOperation(
        project_key,
        requested_version,
        deadline=deadline,
    )
    operation.run()
    if operation.error is not None:
        raise operation.error
    if operation.cancelled or operation.preview is None:
        raise LoaderMigrationCancelled("Loader migration was cancelled")
    return operation


def _update_metadata_record(relative_path: Path, contents: bytes) -> _UpdateMetadata:
    try:
        relative = portable_relative_path(relative_path)
        data = tomllib.loads(contents.decode("utf-8"))
        mod = read_mod_data(relative, data)
        provider = canonical_provider(mod.provider)
        if provider not in {"modrinth", "curseforge", "url"} or not mod.project_id:
            raise HuroshikiError(
                f"Update metadata {relative} has no stable provider/project identity"
            )
        filename = portable_basename(mod.filename, context="Metadata filename")
    except (PortablePathError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HuroshikiError(f"Invalid update metadata {relative_path}: {error}") from error
    return _UpdateMetadata(
        relative,
        provider,
        mod.project_id,
        filename,
        contents,
    )


def _update_metadata_snapshot(
    source: Path,
    checkpoint: Callable[[], None] | None = None,
) -> dict[tuple[str, str], _UpdateMetadata]:
    records: dict[tuple[str, str], _UpdateMetadata] = {}
    paths: dict[str, tuple[str, str]] = {}
    filenames: dict[str, tuple[str, str]] = {}
    for path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
        _run_checkpoint(checkpoint)
        if not path.is_file() or path.is_symlink():
            continue
        record = _update_metadata_record(
            path.relative_to(source),
            _read_file_bytes(path, checkpoint),
        )
        path_key = portable_relative_path_key(record.relative_path)
        filename_key = portable_basename_key(record.filename)
        if record.identity in records:
            previous = records[record.identity]
            raise HuroshikiError(
                f"Update resolver produced identity {record.provider}:{record.project_id} "
                f"at both {previous.relative_path} and {record.relative_path}"
            )
        path_owner = paths.get(path_key)
        if path_owner is not None and path_owner != record.identity:
            if {path_owner[0], record.identity[0]} != {"modrinth", "curseforge"}:
                raise HuroshikiError(
                    f"Update resolver produced portable metadata path collision at "
                    f"{record.relative_path}"
                )
        filename_owner = filenames.get(filename_key)
        if filename_owner is not None and filename_owner != record.identity:
            if {filename_owner[0], record.identity[0]} != {
                "modrinth",
                "curseforge",
            }:
                raise HuroshikiError(
                    f"Update resolver produced portable filename collision "
                    f"{record.filename!r}"
                )
        records[record.identity] = record
        paths[path_key] = record.identity
        filenames[filename_key] = record.identity
    return records


def _candidate_error(
    root: Path,
    mod: ModInfo,
    data: dict[str, object],
    message: str,
    returncode: int | None = None,
    error_kind: str = "resolver",
) -> UpdateCandidate:
    provider = canonical_provider(mod.provider)
    return UpdateCandidate(
        key=f"{provider}:{mod.project_id}",
        root=root,
        slug=mod.slug,
        name=mod.name,
        provider=mod.provider,
        current_version=metadata_version(data, mod.provider),
        current_file_id=metadata_file_id(data, mod.provider),
        new_version="-",
        new_file_id="-",
        status="unavailable",
        error=message,
        error_returncode=returncode,
        error_kind=error_kind,
    )


def _prepare_update_candidates(
    source: Path,
    transaction_root: Path,
    baseline_contents: dict[Path, bytes],
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    on_progress: Callable[[UpdateProgress], None] | None = None,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
    diagnostic_project_id: str | None = None,
) -> list[UpdateCandidate]:
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + UPDATE_OPERATION_TIMEOUT_SECONDS
    )
    process_incomplete = False

    def record_process_result(result: BoundedProcessResult) -> None:
        nonlocal process_incomplete
        process_incomplete = process_incomplete or result.termination_incomplete
        if process_result_callback is not None:
            process_result_callback(result)

    def progress(value: UpdateProgress) -> None:
        if on_progress is not None:
            on_progress(value)

    def check_cancel(completed: int, total: int) -> None:
        if cancel_event is not None and cancel_event.is_set():
            progress(UpdateProgress("cancelled", completed, total))
            raise UpdatePreparationCancelled("Update preparation was cancelled")

    def copy_checkpoint(completed: int, total: int) -> None:
        check_cancel(completed, total)
        if time.monotonic() >= effective_deadline:
            raise UpdatePreparationDeadlineExceeded(
                "Update preparation operation deadline exceeded"
            )

    parsed: list[tuple[Path, bytes, dict[str, object], ModInfo]] = []
    slugs: dict[str, list[Path]] = {}
    for relative_path, original in sorted(baseline_contents.items()):
        check_cancel(len(parsed), len(baseline_contents))
        old_data = tomllib.loads(original.decode("utf-8"))
        old_mod = read_mod_data(relative_path, old_data)
        parsed.append((relative_path, original, old_data, old_mod))
        slugs.setdefault(old_mod.slug, []).append(relative_path)
    ambiguous = {slug: paths for slug, paths in slugs.items() if len(paths) > 1}
    total = len(parsed)
    candidates: list[UpdateCandidate] = []
    eligible: list[tuple[Path, bytes, dict[str, object], ModInfo]] = []
    for relative_path, original, old_data, old_mod in parsed:
        check_cancel(len(candidates), total)
        provider = canonical_provider(old_mod.provider)
        key = f"{provider}:{old_mod.project_id}"
        common = dict(
            key=key,
            root=relative_path,
            slug=old_mod.slug,
            name=old_mod.name,
            provider=old_mod.provider,
            current_version=metadata_version(old_data, old_mod.provider),
            current_file_id=metadata_file_id(old_data, old_mod.provider),
        )
        if old_mod.slug in ambiguous:
            paths = ", ".join(str(path) for path in ambiguous[old_mod.slug])
            candidates.append(
                _candidate_error(
                    relative_path,
                    old_mod,
                    old_data,
                    f"ambiguous Packwiz update slug {old_mod.slug!r}; metadata exists at {paths}",
                )
            )
            continue
        if metadata_is_pinned(old_data):
            candidates.append(
                UpdateCandidate(**common, new_version="-", status="pinned")
            )
            continue
        if provider not in {"modrinth", "curseforge"} or not old_mod.project_id:
            candidates.append(
                UpdateCandidate(**common, new_version="-", status="unavailable")
            )
            continue
        eligible.append((relative_path, original, old_data, old_mod))

    if not eligible:
        progress(UpdateProgress("complete", total, total))
        return sorted(candidates, key=lambda item: item.root)

    resolver_root = transaction_root / "update-resolvers"
    resolver_root.mkdir()
    normalized = resolver_root / "normalized-source"
    normalization_returncode: int | None = None
    try:
        progress(UpdateProgress("normalizing", len(candidates), total))
        check_cancel(len(candidates), total)
        if time.monotonic() >= effective_deadline:
            message = "Update preparation operation deadline exceeded"
            for relative_path, _, old_data, old_mod in eligible:
                candidates.append(
                    _candidate_error(
                        relative_path,
                        old_mod,
                        old_data,
                        message,
                        error_kind="operation_deadline",
                    )
                )
            progress(UpdateProgress("failed", total, total, message=message))
            shutil.rmtree(resolver_root, ignore_errors=True)
            return sorted(candidates, key=lambda item: item.root)
        copy_transaction_source(
            source,
            normalized,
            checkpoint=lambda: copy_checkpoint(len(candidates), total),
        )
        check_cancel(len(candidates), total)
        normalization = run_resolver_process(
            ["packwiz", "refresh"],
            cwd=normalized,
            cancel_event=cancel_event,
            deadline=min(
                effective_deadline,
                time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
            ),
            result_callback=record_process_result,
        )
        normalization_diagnostic = _record_packwiz_process_diagnostic(
            ["packwiz", "refresh"],
            normalization,
            project_id=diagnostic_project_id,
            operation="update-normalize",
        )
        if normalization.cancelled:
            progress(UpdateProgress("cancelled", len(candidates), total))
            raise UpdatePreparationCancelled("Update preparation was cancelled")
        if normalization.termination_incomplete:
            normalization_error = (
                "disposable baseline normalization process termination was incomplete"
            )
        elif normalization.orphaned_descendants:
            normalization_error = (
                "disposable baseline normalization left background processes "
                "after completion"
            )
        elif normalization.timed_out and time.monotonic() >= effective_deadline:
            normalization_error = "Update preparation operation deadline exceeded"
        elif normalization.timed_out:
            normalization_error = (
                "disposable baseline normalization deadline exceeded after "
                f"{UPDATE_RESOLVER_TIMEOUT_SECONDS} seconds"
            )
        elif normalization.output_limit_exceeded:
            normalization_error = (
                "disposable baseline normalization exceeded the supported output limit"
            )
        else:
            normalization_returncode = normalization.returncode
            normalization_error = (
                None
                if normalization.returncode == 0
                else "disposable baseline normalization failed: "
                + concise_process_error(
                    normalization, command=["packwiz", "refresh"]
                )
            )
        if normalization_error is not None:
            error_kind = (
                "operation_deadline"
                if normalization_error == "Update preparation operation deadline exceeded"
                else "resolver"
            )
            normalization_error = _packwiz_diagnostic_detail(
                normalization_error,
                normalization_diagnostic,
            )
            for relative_path, _, old_data, old_mod in eligible:
                candidates.append(
                    _candidate_error(
                        relative_path,
                        old_mod,
                        old_data,
                        normalization_error,
                        normalization_returncode,
                        error_kind,
                    )
                )
            progress(
                UpdateProgress("failed", total, total, message=normalization_error)
            )
            if not process_incomplete:
                shutil.rmtree(resolver_root, ignore_errors=True)
            return sorted(candidates, key=lambda item: item.root)
        check_cancel(len(candidates), total)
        ensure_safe_pack_source(
            normalized,
            checkpoint=lambda: copy_checkpoint(len(candidates), total),
        )
        before_files = _file_content_snapshot(
            normalized,
            lambda: copy_checkpoint(len(candidates), total),
        )
        baseline_records = _update_metadata_snapshot(
            normalized,
            lambda: copy_checkpoint(len(candidates), total),
        )
    except UpdatePreparationCancelled:
        if not process_incomplete:
            shutil.rmtree(resolver_root, ignore_errors=True)
        raise
    except UpdatePreparationDeadlineExceeded as error:
        message = str(error)
        for relative_path, _, old_data, old_mod in eligible:
            candidates.append(
                _candidate_error(
                    relative_path,
                    old_mod,
                    old_data,
                    message,
                    error_kind="operation_deadline",
                )
            )
        if not process_incomplete:
            shutil.rmtree(resolver_root, ignore_errors=True)
        progress(UpdateProgress("failed", total, total, message=message))
        return sorted(candidates, key=lambda item: item.root)
    except (OSError, HuroshikiError) as error:
        message = f"disposable baseline normalization failed: {error}"
        for relative_path, _, old_data, old_mod in eligible:
            candidates.append(_candidate_error(relative_path, old_mod, old_data, message))
        if not process_incomplete:
            shutil.rmtree(resolver_root, ignore_errors=True)
        progress(UpdateProgress("failed", total, total, message=message))
        return sorted(candidates, key=lambda item: item.root)

    initially_completed = total - len(eligible)
    for eligible_index, (relative_path, original, old_data, old_mod) in enumerate(
        eligible
    ):
        completed = initially_completed + eligible_index
        try:
            check_cancel(completed, total)
        except UpdatePreparationCancelled:
            shutil.rmtree(resolver_root, ignore_errors=True)
            raise
        if time.monotonic() >= effective_deadline:
            message = "Update preparation operation deadline exceeded"
            for pending_path, _, pending_data, pending_mod in eligible[eligible_index:]:
                candidates.append(
                    _candidate_error(
                        pending_path,
                        pending_mod,
                        pending_data,
                        message,
                        error_kind="operation_deadline",
                    )
                )
            progress(UpdateProgress("failed", completed, total, message=message))
            break
        progress(
            UpdateProgress(
                "resolving",
                completed,
                total,
                old_mod.name,
                old_mod.provider,
            )
        )
        provider = canonical_provider(old_mod.provider)
        key = f"{provider}:{old_mod.project_id}"
        common = dict(
            key=key,
            root=relative_path,
            slug=old_mod.slug,
            name=old_mod.name,
            provider=old_mod.provider,
            current_version=metadata_version(old_data, old_mod.provider),
        )

        try:
            with nullcontext(
                tempfile.mkdtemp(prefix=f"{old_mod.slug}-", dir=resolver_root)
            ) as directory:
                resolver = Path(directory) / "source"
                copy_transaction_source(
                    normalized,
                    resolver,
                    checkpoint=lambda: copy_checkpoint(completed, total),
                )
                check_cancel(completed, total)
                result = run_resolver_process(
                    ["packwiz", "--yes", "update", old_mod.slug],
                    cwd=resolver,
                    cancel_event=cancel_event,
                    deadline=min(
                        effective_deadline,
                        time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
                    ),
                    result_callback=record_process_result,
                )
                resolver_diagnostic = _record_packwiz_process_diagnostic(
                    ["packwiz", "--yes", "update", old_mod.slug],
                    result,
                    project_id=diagnostic_project_id,
                    operation="update-resolve",
                )

                def resolver_error(message: str) -> str:
                    return _packwiz_diagnostic_detail(message, resolver_diagnostic)

                if result.cancelled:
                    raise UpdatePreparationCancelled(
                        _packwiz_diagnostic_detail(
                            "Update preparation was cancelled",
                            resolver_diagnostic,
                        )
                    )
                if result.termination_incomplete:
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            resolver_error(
                                "Packwiz resolver process termination was incomplete"
                            ),
                        )
                    )
                    continue
                if result.orphaned_descendants:
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            resolver_error(
                                "Packwiz resolver left background processes after completion"
                            ),
                        )
                    )
                    continue
                if result.timed_out:
                    operation_deadline = time.monotonic() >= effective_deadline
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            resolver_error(
                                "Update preparation operation deadline exceeded"
                                if operation_deadline
                                else f"resolver deadline exceeded after "
                                f"{UPDATE_RESOLVER_TIMEOUT_SECONDS} seconds"
                            ),
                            error_kind=(
                                "operation_deadline"
                                if operation_deadline
                                else "resolver"
                            ),
                        )
                    )
                    if operation_deadline:
                        for (
                            pending_path,
                            _,
                            pending_data,
                            pending_mod,
                        ) in eligible[eligible_index + 1 :]:
                            candidates.append(
                                _candidate_error(
                                    pending_path,
                                    pending_mod,
                                    pending_data,
                                    "Update preparation operation deadline exceeded",
                                    error_kind="operation_deadline",
                                )
                            )
                        progress(
                            UpdateProgress(
                                "failed",
                                completed,
                                total,
                                message="Update preparation operation deadline exceeded",
                            )
                        )
                        break
                    continue
                if result.returncode != 0:
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            resolver_error(
                                concise_process_error(
                                    result,
                                    command=[
                                        "packwiz",
                                        "--yes",
                                        "update",
                                        old_mod.slug,
                                    ],
                                )
                            ),
                            result.returncode,
                        )
                    )
                    continue
                if result.output_limit_exceeded:
                    candidates.append(
                        _candidate_error(
                            relative_path,
                            old_mod,
                            old_data,
                            resolver_error(
                                "Packwiz resolver exceeded the supported output limit"
                            ),
                        )
                    )
                    continue
                check_cancel(completed, total)
                ensure_safe_pack_source(
                    resolver,
                    checkpoint=lambda: copy_checkpoint(completed, total),
                )
                resolved_records = _update_metadata_snapshot(
                    resolver,
                    lambda: copy_checkpoint(completed, total),
                )
                changes = _content_changes(
                    before_files,
                    _file_content_snapshot(
                        resolver,
                        lambda: copy_checkpoint(completed, total),
                    ),
                )
        except UpdatePreparationCancelled:
            progress(UpdateProgress("cancelled", completed, total))
            if not process_incomplete:
                shutil.rmtree(resolver_root, ignore_errors=True)
            raise
        except UpdatePreparationDeadlineExceeded as error:
            message = str(error)
            for pending_path, _, pending_data, pending_mod in eligible[eligible_index:]:
                candidates.append(
                    _candidate_error(
                        pending_path,
                        pending_mod,
                        pending_data,
                        message,
                        error_kind="operation_deadline",
                    )
                )
            progress(UpdateProgress("failed", completed, total, message=message))
            break
        except (OSError, HuroshikiError) as error:
            candidates.append(
                _candidate_error(relative_path, old_mod, old_data, str(error))
            )
            continue

        metadata_changed = any(
            change.relative_path.name.endswith(".pw.toml") for change in changes
        )
        if not metadata_changed:
            candidates.append(
                UpdateCandidate(**common, new_version="-", status="current")
            )
            continue
        resolved_root = resolved_records.get((provider, old_mod.project_id))
        if resolved_root is not None:
            resolved_data = tomllib.loads(resolved_root.contents.decode("utf-8"))
            new_version = metadata_version(resolved_data, old_mod.provider)
            new_file_id = metadata_file_id(resolved_data, old_mod.provider)
        else:
            new_version = "-"
            new_file_id = "-"
        added_dependencies = sum(
            identity not in baseline_records and identity != (provider, old_mod.project_id)
            for identity in resolved_records
        )
        candidates.append(
            UpdateCandidate(
                **common,
                new_version=new_version,
                new_file_id=new_file_id,
                status="update",
                changes=changes,
                added_dependencies=added_dependencies,
            )
        )
    try:
        check_cancel(total, total)
    except UpdatePreparationCancelled:
        if not process_incomplete:
            shutil.rmtree(resolver_root, ignore_errors=True)
        raise
    if not process_incomplete:
        shutil.rmtree(resolver_root)
    if not any(candidate.error_kind == "operation_deadline" for candidate in candidates):
        progress(UpdateProgress("complete", total, total))
    return sorted(candidates, key=lambda item: item.root)


def _metadata_semantics(record: _UpdateMetadata) -> dict[str, object]:
    document = tomllib.loads(record.contents.decode("utf-8"))
    document.pop("side", None)
    document["filename"] = portable_basename(
        str(document.get("filename", "")), context="Metadata filename"
    )
    return document


def _merge_metadata_records(
    existing: _UpdateMetadata,
    incoming: _UpdateMetadata,
) -> _UpdateMetadata:
    if (
        portable_relative_path_key(existing.relative_path)
        != portable_relative_path_key(incoming.relative_path)
        or portable_basename_key(existing.filename)
        != portable_basename_key(incoming.filename)
        or _metadata_semantics(existing) != _metadata_semantics(incoming)
    ):
        raise HuroshikiError(
            "Update closure metadata disagreement for shared identity "
            f"{incoming.provider}:{incoming.project_id}; dependency versions, paths, "
            "downloads, or update metadata differ"
        )
    existing_mod = read_mod_data(
        existing.relative_path,
        tomllib.loads(existing.contents.decode("utf-8")),
    )
    incoming_mod = read_mod_data(
        incoming.relative_path,
        tomllib.loads(incoming.contents.decode("utf-8")),
    )
    return replace(
        existing,
        contents=_metadata_contents_with_side(
            existing.contents,
            union_side(existing_mod.side, incoming_mod.side),
        ),
    )


def _merge_update_closures(
    candidates: Iterable[UpdateCandidate],
    *,
    source: Path | None = None,
    workspace: Path | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
) -> tuple[UpdateChange, ...]:
    def checkpoint() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise HuroshikiError("Dependency closure merge was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise HuroshikiError("Dependency closure merge deadline exceeded")

    selected = tuple(candidates)
    if not selected:
        return ()
    baseline_by_identity: dict[tuple[str, str], _UpdateMetadata] = {}
    metadata_operations: dict[tuple[str, str], _UpdateMetadata | None] = {}
    other_operations: dict[str, UpdateChange] = {}

    if source is not None:
        checkpoint()
        ensure_safe_pack_source(source, checkpoint=checkpoint)
        for metadata_path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
            checkpoint()
            if not metadata_path.is_file() or metadata_path.is_symlink():
                continue
            relative_path = metadata_path.relative_to(source)
            record = _update_metadata_record(
                relative_path,
                _read_file_bytes(metadata_path, checkpoint),
            )
            previous = baseline_by_identity.get(record.identity)
            if previous is not None:
                raise HuroshikiError(
                    "Update baseline contains duplicate metadata identity "
                    f"{record.provider}:{record.project_id}"
                )
            baseline_by_identity[record.identity] = record

    for candidate in selected:
        checkpoint()
        before_records: dict[tuple[str, str], _UpdateMetadata] = {}
        after_records: dict[tuple[str, str], _UpdateMetadata] = {}
        for change in candidate.changes:
            checkpoint()
            if not change.relative_path.name.endswith(".pw.toml"):
                if change.relative_path in PACKWIZ_GENERATED_PATHS:
                    continue
                path_key = portable_relative_path_key(change.relative_path)
                previous = other_operations.get(path_key)
                if previous is not None and (
                    previous.relative_path != change.relative_path
                    or previous.before != change.before
                    or previous.after != change.after
                ):
                    raise HuroshikiError(
                        f"Update closure file disagreement at {change.relative_path}"
                    )
                other_operations[path_key] = change
                continue
            if change.before is not None:
                record = _update_metadata_record(change.relative_path, change.before)
                before_records[record.identity] = record
                baseline_by_identity.setdefault(record.identity, record)
            if change.after is not None:
                record = _update_metadata_record(change.relative_path, change.after)
                after_records[record.identity] = record

        for identity in before_records.keys() | after_records.keys():
            incoming = after_records.get(identity)
            if identity not in metadata_operations:
                baseline = baseline_by_identity.get(identity)
                if baseline is not None and incoming is not None:
                    baseline_mod = read_mod_data(
                        baseline.relative_path,
                        tomllib.loads(baseline.contents.decode("utf-8")),
                    )
                    incoming_mod = read_mod_data(
                        incoming.relative_path,
                        tomllib.loads(incoming.contents.decode("utf-8")),
                    )
                    incoming = replace(
                        incoming,
                        contents=_metadata_contents_with_side(
                            incoming.contents,
                            union_side(baseline_mod.side, incoming_mod.side),
                        ),
                    )
                metadata_operations[identity] = incoming
                continue
            existing = metadata_operations[identity]
            if (existing is None) != (incoming is None):
                raise HuroshikiError(
                    "Update closure delete-vs-update conflict for "
                    f"{identity[0]}:{identity[1]}"
                )
            if existing is not None and incoming is not None:
                metadata_operations[identity] = _merge_metadata_records(
                    existing, incoming
                )

    final_records = dict(baseline_by_identity)
    final_records.update(
        (identity, record)
        for identity, record in metadata_operations.items()
        if record is not None
    )
    for identity, record in metadata_operations.items():
        if record is None:
            final_records.pop(identity, None)

    if source is not None:
        checkpoint()
        root_manifest = source / ".huroshiki-roots.json"
        manifest_exists = root_manifest.is_file() and not root_manifest.is_symlink()
        explicit_roots = (
            {
                (canonical_provider(record.provider), record.project_id)
                for record in read_pack_root_manifest(source)
            }
            if manifest_exists
            else set()
        )
        context: EquivalenceContext | None = None
        effective_workspace = workspace or (
            source.parent / f"update-equivalence-{uuid4().hex}"
        )
        while True:
            owners: dict[tuple[str, str], tuple[str, str]] = {}
            collision: tuple[tuple[str, str], tuple[str, str]] | None = None
            for identity, record in final_records.items():
                keys = (
                    ("path", portable_relative_path_key(record.relative_path)),
                    ("filename", portable_basename_key(record.filename)),
                )
                for key in keys:
                    owner = owners.get(key)
                    if owner is not None and owner != identity:
                        collision = (owner, identity)
                        break
                    owners[key] = identity
                if collision is not None:
                    break
            if collision is None:
                break
            left_identity, right_identity = collision
            if {left_identity[0], right_identity[0]} != {"modrinth", "curseforge"}:
                break
            if context is None:
                minecraft, loader, loader_version = packctl.project_versions(source)
                context = EquivalenceContext(
                    minecraft,
                    loader,
                    loader_version,
                    _equivalence_snapshot_digest(source, checkpoint),
                    EQUIVALENCE_POLICY_VERSION,
                )
            left = final_records[left_identity]
            right = final_records[right_identity]
            left_mod = read_mod_data(
                left.relative_path, tomllib.loads(left.contents.decode("utf-8"))
            )
            right_mod = read_mod_data(
                right.relative_path, tomllib.loads(right.contents.decode("utf-8"))
            )
            left_candidate = _dependency_candidate(
                identity=left_identity,
                relative_path=left.relative_path,
                filename=left.filename,
                contents=left.contents,
                side=left_mod.side,
                provenance=_dependency_provenance(
                    existing=left_identity in baseline_by_identity,
                    explicit=left_identity in explicit_roots,
                    provenance_known=(
                        manifest_exists or left_identity not in baseline_by_identity
                    ),
                ),
                existing=left_identity in baseline_by_identity,
            )
            right_candidate = _dependency_candidate(
                identity=right_identity,
                relative_path=right.relative_path,
                filename=right.filename,
                contents=right.contents,
                side=right_mod.side,
                provenance=_dependency_provenance(
                    existing=right_identity in baseline_by_identity,
                    explicit=right_identity in explicit_roots,
                    provenance_known=(
                        manifest_exists or right_identity not in baseline_by_identity
                    ),
                ),
                existing=right_identity in baseline_by_identity,
            )
            evidence = _verify_dependency_collision(
                left_candidate,
                right_candidate,
                context=context,
                workspace=effective_workspace,
                cancel_event=cancel_event,
                deadline=deadline,
                process_result_callback=process_result_callback,
            )
            winner_identity = tuple(evidence.selected_identity.split(":", 1))
            loser_identity = (
                right_identity if winner_identity == left_identity else left_identity
            )
            winner = final_records[winner_identity]
            unioned = union_side(left_mod.side, right_mod.side)
            winner = replace(
                winner,
                contents=_metadata_contents_with_side(winner.contents, unioned),
            )
            final_records[winner_identity] = winner
            metadata_operations[winner_identity] = winner
            final_records.pop(loser_identity)
            if loser_identity in baseline_by_identity:
                metadata_operations[loser_identity] = None
            else:
                metadata_operations.pop(loser_identity, None)

    path_owners: dict[str, tuple[str, str]] = {}
    filename_owners: dict[str, tuple[str, str]] = {}
    for identity, record in final_records.items():
        path_key = portable_relative_path_key(record.relative_path)
        filename_key = portable_basename_key(record.filename)
        if path_key in path_owners and path_owners[path_key] != identity:
            raise HuroshikiError(
                f"Update closure metadata path collision at {record.relative_path}"
            )
        if filename_key in filename_owners and filename_owners[filename_key] != identity:
            raise HuroshikiError(
                f"Update closure filename collision for {record.filename!r}"
            )
        path_owners[path_key] = identity
        filename_owners[filename_key] = identity

    merged: list[UpdateChange] = list(other_operations.values())
    for identity, desired in metadata_operations.items():
        original = baseline_by_identity.get(identity)
        if original is not None and (
            desired is None or original.relative_path != desired.relative_path
        ):
            merged.append(UpdateChange(original.relative_path, original.contents, None))
        if desired is not None:
            before = (
                original.contents
                if original is not None and original.relative_path == desired.relative_path
                else None
            )
            if before != desired.contents:
                merged.append(UpdateChange(desired.relative_path, before, desired.contents))
    return tuple(sorted(merged, key=lambda item: item.relative_path))


def _apply_update_change(
    source: Path,
    change: UpdateChange,
    *,
    use_after: bool,
) -> None:
    contents = change.after if use_after else change.before
    path = safe_child(source, change.relative_path)
    if contents is None:
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != source:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def read_mod_data(relative_path: Path, data: dict[str, object]) -> ModInfo:
    side = data.get("side")
    side_error = packctl.side_validation_error(side)
    client, server = flags_from_side(side)
    provider, project_id = provider_from_metadata(data)
    slug = relative_path.name.removesuffix(".pw.toml")
    download = data.get("download", {})
    source_url = (
        str(download.get("url", ""))
        if isinstance(download, dict)
        else ""
    )
    if canonical_provider(provider) == "url":
        project_id = slug
    return ModInfo(
        relative_path=relative_path,
        slug=slug,
        name=str(data.get("name", slug)),
        provider=provider,
        project_id=project_id,
        filename=str(data.get("filename", "")),
        client=client,
        server=server,
        source_url=source_url,
        side_error=side_error,
    )


def template_mod_relative(
    provider: str, project_id: str, occurrence: int = 0
) -> Path:
    safe_provider = canonical_provider(provider)
    safe_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in project_id
    )
    suffix = "" if occurrence == 0 else f"-{occurrence + 1}"
    return Path("mods") / f"{safe_provider}-{safe_id}{suffix}.pw.toml"


TEMPLATE_MOD_PATH_ROOT = Path(".huroshiki-template-manifest") / "mods"


def template_mod_index_relative(index: int) -> Path:
    if index < 0:
        raise HuroshikiError(f"Invalid template MOD list index: {index}")
    return TEMPLATE_MOD_PATH_ROOT / str(index)


def template_mod_raw_index(relative_path: Path) -> int | None:
    try:
        relative = relative_path.relative_to(TEMPLATE_MOD_PATH_ROOT)
    except ValueError:
        return None
    if len(relative.parts) != 1 or not relative.name.isdecimal():
        return None
    return int(relative.name)


def template_mod_info(
    entry: dict[str, str], occurrence: int = 0, *, raw_index: int | None = None
) -> ModInfo:
    side = entry.get("side")
    side_error = packctl.side_validation_error(side)
    client, server = flags_from_side(side)
    provider = canonical_provider(entry["provider"])
    project_id = entry["project_id"]
    source_url = entry.get("url", "")
    return ModInfo(
        relative_path=(
            template_mod_relative(provider, project_id, occurrence)
            if raw_index is None
            else template_mod_index_relative(raw_index)
        ),
        slug=f"{provider}-{project_id}",
        name=entry["name"],
        provider={"modrinth": "MR", "curseforge": "CF", "url": "URL"}[provider],
        project_id=project_id,
        filename=(
            unquote(Path(urlparse(source_url).path).name)
            if source_url
            else ""
        ),
        client=client,
        server=server,
        source_url=source_url,
        side_error=side_error,
    )


def list_mods(project_key_value: str) -> list[ModInfo]:
    kind, project_id = split_project_key(project_key_value)
    if kind == "template":
        return [
            template_mod_info(entry, raw_index=index)
            for index, entry in packctl.template_mods_indexed(
                project_id,
                allow_invalid_sides=True,
                deduplicate=False,
            )
        ]
    source = project_source(project_key_value)
    return list_mods_from_source(source)


def list_mods_from_source(source: Path) -> list[ModInfo]:
    return [
        read_mod(source, path.relative_to(source))
        for path in sorted(source.rglob("*.pw.toml"))
        if path.is_file()
    ]


def installed_mod_provenance(
    project_key_value: str,
    mod: ModInfo,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> str:
    """Return the authoritative root-manifest role for one installed Pack MOD."""

    def checkpoint() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExactModVersionCancelled("Installed MOD provenance loading was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise ExactModVersionDeadlineExceeded(
                "Installed MOD provenance loading deadline exceeded"
            )

    checkpoint()
    kind, _project_id = split_project_key(project_key_value)
    if kind != "pack":
        return "Recipe entry"
    provider = canonical_provider(mod.provider)
    if provider not in {"modrinth", "curseforge", "url"} or not mod.project_id:
        return "Dependency"
    identity = f"{provider}:{mod.project_id}"
    roots = read_pack_root_manifest(
        project_source(project_key_value), checkpoint=checkpoint
    )
    return (
        "Explicit root"
        if any(root.canonical_identity == identity for root in roots)
        else "Dependency"
    )


def filter_mods(mods: Iterable[ModInfo], query: str) -> list[ModInfo]:
    needle = query.strip().casefold()
    if not needle:
        return list(mods)
    return [
        mod
        for mod in mods
        if needle
        in " ".join(
            (
                mod.name,
                mod.slug,
                mod.provider,
                mod.project_id,
                mod.filename,
                mod.source_url,
                str(mod.relative_path),
            )
        ).casefold()
    ]


def set_installed_mod_side(
    project_key_value: str,
    relative_path: Path,
    client: bool,
    server: bool,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> None:
    try:
        with packctl.ProjectLock(project_key_value, "side"):
            kind, project_id = split_project_key(project_key_value)
            side = side_from_flags(client, server)
            if kind == "template":
                raw_index = template_mod_raw_index(relative_path)
                if raw_index is None:
                    raise HuroshikiError(f"Unknown template MOD: {relative_path}")
                packctl.set_template_mod_side_at_index(project_id, raw_index, side)
                return

            source = project_source(project_key_value)
            path = safe_child(source, relative_path)
            packctl.set_side_and_refresh(
                source,
                path,
                side,
                cancel_event=cancel_event,
                deadline=deadline,
            )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def _apply_profile_entry(
    transaction: PackTransaction,
    entry: Mapping[str, object],
    *,
    cancel_event: threading.Event,
    deadline: float,
) -> Path:
    provider = entry.get("source")
    project = entry.get("project")
    requested_side = entry.get("side")
    if provider not in {"modrinth", "curseforge"}:
        raise HuroshikiError(f"Unsupported profile source: {provider!r}")
    if project is None:
        raise HuroshikiError("Profile entry is missing project")
    if requested_side not in packctl.VALID_SIDES:
        raise HuroshikiError(
            f"Invalid/missing side for {project!r}: {requested_side!r}"
        )

    if provider == "modrinth":
        project_id: str | int = (
            resolve_project_selector("modrinth", str(project)).canonical_project_id or ""
        )
    else:
        try:
            project_id = int(project)
        except (TypeError, ValueError) as error:
            raise HuroshikiError(
                "CurseForge profiles require numeric project IDs"
            ) from error
    minecraft, loader, loader_version = packctl.project_versions(transaction.source)
    closure = resolve_mod_closure(
        provider=str(provider),
        selector=str(project_id),
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        canonical_project_id=str(project_id),
        cancel_event=cancel_event,
        deadline=deadline,
        resolver_root=transaction.root / f"profile-resolver-{uuid4().hex}",
        process_result_callback=transaction._record_equivalence_process_result,
    )
    changed = merge_metadata_closure(
        transaction.source,
        closure,
        requested_side=str(requested_side),
        cancel_event=cancel_event,
        deadline=deadline,
        equivalence_workspace=transaction.root / "profile-equivalence",
        process_result_callback=transaction._record_equivalence_process_result,
    )
    metadata_path = packctl.find_metadata(transaction.source, str(provider), project_id)
    if metadata_path is None:
        raise HuroshikiError(
            f"Metadata not found after merging {provider}:{project_id} closure"
        )
    return metadata_path.relative_to(transaction.source)


def apply_profiles(
    project_key_value: str,
    profiles: Mapping[str, object],
    names: Iterable[str],
    *,
    on_profile: Callable[[str], None] | None = None,
    on_entry: Callable[[str, Path, str], None] | None = None,
) -> None:
    """Apply selected profiles in order as one all-or-nothing pack transaction."""
    kind, _ = split_project_key(project_key_value)
    if kind != "pack":
        raise HuroshikiError("Profiles can only be applied to MODPACK projects")
    selected_names = tuple(names)
    transaction = PackTransaction.create(project_key_value)
    cancel_event = threading.Event()
    deadline = time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
    try:
        for name in selected_names:
            if name not in profiles:
                available = ", ".join(sorted(profiles))
                raise HuroshikiError(
                    f"Unknown profile {name!r}; available: {available}"
                )
            entries = profiles[name] or []
            if not isinstance(entries, list):
                raise HuroshikiError(f"Profile {name!r} must be a list")
            if on_profile is not None:
                on_profile(name)
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    raise HuroshikiError(
                        f"Profile {name!r} entry {index} {entry!r}: expected a mapping"
                    )
                try:
                    relative_path = _apply_profile_entry(
                        transaction,
                        entry,
                        cancel_event=cancel_event,
                        deadline=deadline,
                    )
                    side = str(packctl.read_toml(transaction.source / relative_path)["side"])
                except Exception as error:
                    raise HuroshikiError(
                        f"Profile {name!r} entry {index} {entry!r}: {error}"
                    ) from error
                if on_entry is not None:
                    on_entry(name, relative_path, side)
        try:
            transaction.apply()
        except Exception as error:
            profile_list = ", ".join(repr(name) for name in selected_names)
            raise HuroshikiError(
                f"Profiles {profile_list} could not be applied: {error}"
            ) from error
    finally:
        transaction.discard()


def add_mod_transactionally(
    project_key_value: str,
    provider: str,
    selector: str,
    side: str,
    *,
    artifact_id: str | None = None,
) -> int:
    """Add a mod on a disposable source copy and atomically publish on success."""
    transaction = PackTransaction.create(project_key_value)
    try:
        return transaction.add_mod_transactionally(
            provider,
            selector,
            side,
            artifact_id=artifact_id,
        )
    finally:
        transaction.discard()


def remove_installed_mods(
    project_key_value: str,
    slugs: Iterable[str],
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    selected = set(slugs)
    if not selected:
        return 0
    operation_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
    )
    transaction = PackTransaction.create(project_key_value)
    try:
        result = transaction.remove_mods(
            selected,
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
        if result != 0:
            return result
        transaction.apply(
            cancel_event=cancel_event,
            deadline=operation_deadline,
        )
        return 0
    finally:
        transaction.discard()


def create_project(
    kind: str,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    lock_held: bool = False,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    try:
        packctl.validate_project_creation_fields(
            display_name=display_name,
            minecraft=minecraft,
            loader_version=loader_version,
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error
    if kind == "pack":
        command_name = "new"
    elif kind == "template":
        command_name = "new-template"
    else:
        raise HuroshikiError(f"Unsupported project kind: {kind}")
    args = argparse.Namespace(
        **{
            "pack" if kind == "pack" else "template": project_id,
            "display_name": display_name,
            "minecraft": minecraft,
            "loader": loader,
            "loader_version": loader_version,
        }
    )
    create = packctl._new_pack if command_name == "new" else packctl._new_template
    create_arguments: dict[str, object] = {}
    if kind == "pack" and (cancel_event is not None or deadline is not None):
        create_arguments = {
            "cancel_event": cancel_event,
            "deadline": deadline,
        }
    try:
        if lock_held:
            return create(args, **create_arguments)
        with packctl.ProjectLock(project_key(kind, project_id), "create project"):
            return create(args, **create_arguments)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def delete_project(project_key_value: str) -> packctl.TrashEntry:
    kind, project_id = split_project_key(project_key_value)
    try:
        return packctl.trash_project(kind, project_id)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def list_trash() -> list[packctl.TrashEntry]:
    return packctl.list_trash()


def restore_trash(name: str) -> Path:
    try:
        return packctl.restore_trash(name)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def purge_trash(name: str) -> tuple[int, int]:
    try:
        return packctl.purge_trash(name=name)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def state_items() -> list[StateItem]:
    return packctl.classify_state()


def clean_state(
    *,
    apply: bool = False,
    expected: tuple[StateItem, ...] | None = None,
) -> packctl.StateCleanupReport:
    try:
        return packctl.clean_state(apply=apply, expected=expected)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def project_actions(project_key_value: str) -> tuple[str, ...]:
    kind, _ = split_project_key(project_key_value)
    if kind == "pack":
        return ("build", "publish", "deploy", "restart")
    return ("create MODPACK", "validate")


def project_action_confirmation(
    project_key_value: str,
    action: str,
) -> tuple[str, ...] | None:
    kind, project_id = split_project_key(project_key_value)
    if kind != "pack" or action not in {"deploy", "publish", "restart"}:
        return None

    lines = [f"Pack: {project_id}", f"Action: {action}"]
    if action in {"deploy", "publish"}:
        lines.append(f"Rsync target: {packctl.distribution_target(project_id)}")
    if action in {"publish", "restart"}:
        host, stack, service = packctl.minecraft_server_target(project_id)
        lines.extend(
            (
                f"SSH target: {host}",
                f"Stack directory: {stack}",
                f"Compose service: {service}",
            )
        )
    return tuple(lines)


def _restart_confirmation(
    project_id: str,
    action: str,
    target: tuple[str, str, str],
) -> tuple[str, ...]:
    host, stack, service = target
    return (
        f"Pack: {project_id}",
        f"Action: {action}",
        f"SSH target: {host}",
        f"Stack directory: {stack}",
        f"Compose service: {service}",
    )


def prepare_deploy_preview(
    project_key_value: str,
    action: str,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> ProjectDeployPreview:
    kind, project_id = split_project_key(project_key_value)
    if kind != "pack" or action not in {"deploy", "publish"}:
        raise HuroshikiError(f"Deploy preview is not available for {action}")
    try:
        with packctl.ProjectLock(project_key_value, f"{action} preview"):
            if (
                packctl._build_pack(
                    project_id,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
                != 0
            ):
                raise HuroshikiError("Build failed; deploy preview was not created")
            preview = packctl._deploy_preview(
                project_id,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            restart_target = (
                packctl.minecraft_server_target(project_id)
                if action == "publish"
                else None
            )
            return ProjectDeployPreview(
                project_key_value,
                action,
                preview.target,
                preview.dist_digest,
                preview.changes,
                preview.raw_lines,
                restart_target,
                preview.snapshot,
            )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def discard_deploy_preview(preview: ProjectDeployPreview) -> None:
    if preview.snapshot is None:
        return
    try:
        packctl.discard_deploy_snapshot(preview.snapshot)
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error


def run_project_action(
    project_key_value: str,
    action: str,
    confirmation: tuple[str, ...] | ProjectDeployPreview | None = None,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> int:
    kind, project_id = split_project_key(project_key_value)
    ctl = [sys.executable, str(SCRIPTS / "packctl.py")]
    if kind == "template":
        if action == "create MODPACK":
            raise HuroshikiError("MODPACK creation must be started from the TUI form")
        if action != "validate":
            raise HuroshikiError(
                f"Action {action} is not available for template projects"
            )
        return subprocess.run(
            ctl + ["validate-template", project_id],
            cwd=ROOT,
            text=True,
            check=False,
        ).returncode

    if action not in {"build", "deploy", "restart", "publish"}:
        raise HuroshikiError(f"Unknown project action: {action}")
    deploy_confirmation = (
        confirmation if isinstance(confirmation, ProjectDeployPreview) else None
    )
    try:
        with packctl.ProjectLock(project_key_value, action):
            if action in {"deploy", "publish"}:
                snapshot = (
                    None
                    if deploy_confirmation is None
                    or deploy_confirmation.snapshot is None
                    else packctl.ensure_safe_state_path(
                        deploy_confirmation.snapshot,
                        state_root=packctl.PACKS.parent / ".huroshiki",
                        repository_root=packctl.PACKS.parent,
                    )
                )
                if (
                    deploy_confirmation is None
                    or deploy_confirmation.project_key != project_key_value
                    or deploy_confirmation.action != action
                    or packctl.distribution_target(project_id)
                    != deploy_confirmation.target
                    or snapshot is None
                    or packctl.distribution_digest(snapshot)
                    != deploy_confirmation.dist_digest
                    or (
                        action == "publish"
                        and packctl.minecraft_server_target(project_id)
                        != deploy_confirmation.restart_target
                    )
                ):
                    raise HuroshikiError(
                        "Deploy target or distribution changed after preview; action aborted"
                    )
                result = packctl._deploy_pack(
                    project_id,
                    expected_target=deploy_confirmation.target,
                    expected_dist_digest=deploy_confirmation.dist_digest,
                    snapshot=snapshot,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
                if result != 0 or action == "deploy":
                    return result
                restart_target = packctl.minecraft_server_target(project_id)
                if restart_target != deploy_confirmation.restart_target:
                    raise HuroshikiError(
                        "Restart target changed during deployment; restart aborted"
                    )
            elif action == "build":
                return packctl._build_pack(project_id)
            else:
                restart_target = packctl.minecraft_server_target(project_id)
                if _restart_confirmation(project_id, action, restart_target) != confirmation:
                    raise HuroshikiError(
                        "Remote configuration changed after confirmation; action aborted"
                    )

            if deploy_confirmation is not None:
                if deploy_confirmation.restart_target is None:
                    raise HuroshikiError("Confirmed publish has no restart target")
                host, stack, service = deploy_confirmation.restart_target
            else:
                host, stack, service = restart_target
            remote = (
                f"cd {shlex.quote(stack)} && docker compose restart "
                f"{shlex.quote(service)}"
            )
            packctl.run(["ssh", "--", host, remote])
            return 0
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error
    finally:
        if deploy_confirmation is not None and deploy_confirmation.snapshot is not None:
            try:
                packctl.discard_deploy_snapshot(deploy_confirmation.snapshot)
            except packctl.ConfigError:
                pass


def update_all(
    project_key_value: str,
    *,
    allow_partial: bool = False,
) -> UpdateRunReport:
    kind, _ = split_project_key(project_key_value)
    if kind == "template":
        raise HuroshikiError(
            "Template entries always resolve the newest compatible file when a MODPACK is created"
        )
    cancel_event = threading.Event()
    deadline = time.monotonic() + UPDATE_OPERATION_TIMEOUT_SECONDS

    def checkpoint() -> None:
        if time.monotonic() >= deadline:
            raise UpdatePreparationDeadlineExceeded(
                "Update preparation operation deadline exceeded"
            )

    transaction = PackTransaction.create(
        project_key_value,
        checkpoint=checkpoint,
    )
    try:
        candidates = tuple(
            transaction.prepare_updates(
                cancel_event=cancel_event,
                deadline=deadline,
            )
        )
        available = tuple(candidate for candidate in candidates if candidate.available)
        failures = tuple(candidate for candidate in candidates if candidate.error)
        for candidate in failures:
            print(
                f"Unable to resolve {candidate.name} [{candidate.provider}]: "
                f"{candidate.error}",
                file=sys.stderr,
            )
        if failures and not allow_partial:
            return UpdateRunReport(candidates, (), failures, False, False)
        if not available:
            if failures:
                return UpdateRunReport(candidates, (), failures, False, False)
            print("No MOD updates are available.")
            return UpdateRunReport(candidates, (), (), False, False)
        print("MOD updates:")
        for candidate in available:
            current_label = update_version_label(
                candidate.current_version,
                candidate.current_file_id,
            )
            new_label = update_version_label(
                candidate.new_version,
                candidate.new_file_id,
            )
            print(
                f"  {candidate.name} [{candidate.provider}] "
                f"{current_label} -> {new_label} "
                f"({candidate.file_count} files, "
                f"{candidate.added_dependencies} added dependencies)"
            )
        transaction.select_updates(
            candidate.relative_path for candidate in available
        )
        transaction.apply(
            cancel_event=cancel_event,
            deadline=deadline,
        )
        return UpdateRunReport(
            candidates,
            available,
            failures,
            True,
            bool(failures),
        )
    finally:
        transaction.discard()


def compatible_templates(minecraft: str, loader: str) -> list[ProjectInfo]:
    ids = packctl.compatible_template_ids(minecraft, loader)
    return [
        info
        for template_id in ids
        if not (info := project_info(project_key("template", template_id))).error
    ]


def concise_process_error(
    result: subprocess.CompletedProcess[str] | ResolverProcessResult,
    *,
    command: Sequence[str] = (),
) -> str:
    text = (result.stderr or result.stdout or "Packwiz returned a non-zero exit code").strip()
    text = packctl._redacted_packwiz_output(command, text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:240] if lines else f"exit code {result.returncode}"


def _record_packwiz_process_diagnostic(
    command: Sequence[str],
    result: BoundedProcessResult,
    *,
    project_id: str | None = None,
    operation: str = "packwiz",
    callback: Callable[[str], None] | None = None,
) -> packctl.PackwizDiagnostic:
    diagnostic = packctl.record_packwiz_diagnostic(
        command,
        result,
        project_id=project_id,
        operation=operation,
    )
    if not diagnostic.has_output:
        return diagnostic
    if diagnostic.log_path is None:
        notice = (
            "Packwiz completed with diagnostics, but the diagnostic log "
            f"could not be written: {diagnostic.error}"
        )
    else:
        notice = (
            "Packwiz completed with diagnostics. Details: "
            f"{packctl.relative_state_path(diagnostic.log_path)}"
        )
    if callback is None:
        print(notice, file=sys.stderr)
    else:
        callback(notice)
    return diagnostic


def _packwiz_diagnostic_detail(
    message: str,
    diagnostic: packctl.PackwizDiagnostic,
) -> str:
    if diagnostic.log_path is not None:
        return (
            f"{message}; Details: "
            f"{packctl.relative_state_path(diagnostic.log_path)}"
        )
    if diagnostic.error is not None:
        return (
            f"{message}; diagnostic log could not be written: "
            f"{diagnostic.error}"
        )
    return message


def _run_noninteractive_packwiz(
    command: list[str],
    *,
    cwd: Path,
    cancel_event: threading.Event | None,
    deadline: float,
    label: str,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
    project_id: str | None = None,
    operation: str = "packwiz",
) -> ResolverProcessResult:
    process_deadline = min(
        deadline,
        time.monotonic() + PACKWIZ_PROCESS_TIMEOUT_SECONDS,
    )
    result = run_resolver_process(
        command,
        cwd=cwd,
        cancel_event=cancel_event,
        deadline=process_deadline,
        result_callback=process_result_callback,
    )
    diagnostic = _record_packwiz_process_diagnostic(
        command,
        result,
        project_id=project_id,
        operation=operation,
    )
    failure = process_failure_message(result, label=label)
    if failure is not None:
        raise HuroshikiError(_packwiz_diagnostic_detail(failure, diagnostic))
    return result


TEMPLATE_RESOLVER_TIMEOUT_SECONDS = PACKWIZ_PROCESS_TIMEOUT_SECONDS
RESOLVER_POLL_SECONDS = PROCESS_POLL_SECONDS
RESOLVER_TERMINATE_GRACE_SECONDS = PROCESS_TERMINATE_GRACE_SECONDS
RESOLVER_KILL_GRACE_SECONDS = PROCESS_KILL_GRACE_SECONDS
RESOLVER_REAP_GRACE_SECONDS = PROCESS_REAP_GRACE_SECONDS


@dataclass(frozen=True)
class _ResolvedTemplateRoot:
    entry: MergedTemplateMod
    metadata: tuple[ResolvedMetadata, ...]
    root_identity: tuple[str, str]


def _metadata_contents_with_side(contents: bytes, side: str) -> bytes:
    document = tomlkit.parse(contents.decode("utf-8"))
    document["side"] = packctl.normalize_side(side)
    return tomlkit.dumps(document).encode("utf-8")


def _read_resolver_metadata(
    source: Path,
    side: str | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[ResolvedMetadata, ...]:
    records: list[ResolvedMetadata] = []
    identities: dict[tuple[str, str], Path] = {}
    paths: dict[str, tuple[str, str]] = {}
    filenames: dict[str, tuple[str, str]] = {}
    for path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
        _run_checkpoint(checkpoint)
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source)
        try:
            relative = portable_relative_path(relative)
            path_key = portable_relative_path_key(relative)
        except PortablePathError as error:
            raise HuroshikiError(f"Resolver metadata path {relative}: {error}") from error
        contents = _read_file_bytes(path, checkpoint)
        mod = read_mod_data(relative, tomllib.loads(contents.decode("utf-8")))
        provider = canonical_provider(mod.provider)
        identity = (provider, mod.project_id)
        if provider not in {"modrinth", "curseforge", "url"} or not mod.project_id:
            raise HuroshikiError(
                f"Resolver metadata {relative} has no stable provider/project identity"
            )
        try:
            filename = portable_basename(mod.filename, context="Metadata filename")
            filename_key = portable_basename_key(filename)
        except PortablePathError as error:
            raise HuroshikiError(f"Resolver metadata {relative}: {error}") from error
        previous_path = identities.get(identity)
        if previous_path is not None:
            raise HuroshikiError(
                f"Resolver produced identity {provider}:{mod.project_id} at both "
                f"{previous_path} and {relative}"
            )
        path_owner = paths.get(path_key)
        if path_owner is not None and path_owner != identity:
            raise HuroshikiError(
                f"Resolver produced portable metadata path collision at {relative} for "
                f"{path_owner[0]}:{path_owner[1]} and {provider}:{mod.project_id}"
            )
        filename_owner = filenames.get(filename_key)
        if filename_owner is not None and filename_owner != identity:
            raise HuroshikiError(
                f"Resolver produced portable filename collision {filename!r} for "
                f"{filename_owner[0]}:{filename_owner[1]} and {provider}:{mod.project_id}"
            )
        identities[identity] = relative
        paths[path_key] = identity
        filenames[filename_key] = identity
        records.append(
            ResolvedMetadata(
                identity,
                relative,
                filename,
                contents,
                provider,
                mod.project_id,
            )
        )
    if not records:
        raise HuroshikiError("No metadata changes were produced")
    resolved = tuple(records)
    return _resolved_metadata_with_side(resolved, side) if side is not None else resolved


def _resolved_metadata_with_side(
    metadata: Iterable[ResolvedMetadata], side: str
) -> tuple[ResolvedMetadata, ...]:
    return tuple(
        replace(item, contents=_metadata_contents_with_side(item.contents, side))
        for item in metadata
    )


def resolved_root_identity(
    provider: str,
    canonical_project_id: str,
    metadata: tuple[ResolvedMetadata, ...],
) -> tuple[str, str]:
    expected = (canonical_provider(provider), canonical_project_id)
    matches = [item for item in metadata if item.identity == expected]
    if len(matches) != 1:
        found = ", ".join(f"{item.provider}:{item.project_id}" for item in metadata)
        raise HuroshikiError(
            f"Canonical root identity {expected[0]}:{expected[1]} was resolved "
            f"{len(matches)} times (resolver produced: {found or 'none'})"
        )
    return expected


class UrlCandidateVerificationError(HuroshikiError):
    pass


def resolve_mod_closure(
    *,
    provider: str,
    selector: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    canonical_project_id: str | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    resolver_root: Path | None = None,
    url_max_jar_size_bytes: int | None = None,
    url_allow_private_networks: bool = False,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
    diagnostic_project_id: str | None = None,
    diagnostic_callback: Callable[[str], None] | None = None,
) -> ResolvedModClosure:
    if cancel_event is not None and cancel_event.is_set():
        raise HuroshikiError("MOD resolution was cancelled")
    if canonical_project_id is None:
        try:
            resolved_selector = resolve_project_selector(
                provider,
                selector,
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except HuroshikiError as error:
            if canonical_provider(provider) == "url":
                raise UrlCandidateVerificationError(str(error)) from error
            raise
    else:
        normalized_provider, normalized_selector = normalize_add_selector(
            provider, selector
        )
        resolved_selector = ResolvedSelector(
            normalized_provider,
            selector,
            canonical_project_id,
            normalized_selector,
        )
    normalized_provider = resolved_selector.provider
    normalized_selector = resolved_selector.display_label or selector
    project_id = canonical_project_id or resolved_selector.canonical_project_id
    state_root = ROOT / ".huroshiki"
    transaction_root = state_root / "transactions"
    packctl.make_state_directory(
        transaction_root,
        state_root=state_root,
        repository_root=ROOT,
    )
    if resolver_root is None:
        resolver_context = tempfile.TemporaryDirectory(
            prefix="mod-resolver-", dir=transaction_root
        )
    else:
        resolver_root.mkdir(mode=0o700)
        resolver_context = nullcontext(str(resolver_root))
    with resolver_context as directory:
        source = Path(directory) / "source"
        create_resolver_source(
            source,
            display_name=f"Resolve {normalized_selector}",
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
        )
        if normalized_provider == "url":
            remaining = (
                deadline - time.monotonic() if deadline is not None else None
            )
            if remaining is not None and remaining <= 0:
                raise HuroshikiError("URL resolver deadline exceeded")
            try:
                artifact = download_url_artifact(
                    normalized_selector,
                    cancel_event or threading.Event(),
                    Path(directory) / "logs",
                    loader,
                    (
                        url_max_jar_size_bytes
                        if url_max_jar_size_bytes is not None
                        else DEFAULT_URL_MAX_JAR_SIZE_BYTES
                    ),
                    allow_private_networks=url_allow_private_networks,
                    total_timeout_seconds=(
                        min(DEFAULT_URL_TOTAL_TIMEOUT_SECONDS, remaining)
                        if remaining is not None
                        else DEFAULT_URL_TOTAL_TIMEOUT_SECONDS
                    ),
                )
            except HuroshikiError as error:
                raise UrlCandidateVerificationError(str(error)) from error
            identity = ("url", artifact.mod_id)
            write_url_metadata(
                source,
                Path("mods") / f"{artifact.mod_id}.pw.toml",
                artifact,
                "both",
            )
            return ResolvedModClosure(identity, _read_resolver_metadata(source))
        if project_id is None:
            raise HuroshikiError(
                f"Canonical project ID is unavailable for "
                f"{normalized_provider}:{normalized_selector}; use an explicit project ID"
            )
        if normalized_provider == "modrinth":
            command = [
                "packwiz", "--yes", "modrinth", "add", "--project-id", project_id
            ]
        else:
            command = [
                "packwiz", "--yes", "curseforge", "add", "--addon-id", project_id
            ]
        resolver_deadline = time.monotonic() + TEMPLATE_RESOLVER_TIMEOUT_SECONDS
        if deadline is not None:
            resolver_deadline = min(
                deadline,
                resolver_deadline,
            )
        if process_result_callback is None:
            process = run_resolver_process(
                command,
                cwd=source,
                cancel_event=cancel_event,
                deadline=resolver_deadline,
            )
        else:
            process = run_resolver_process(
                command,
                cwd=source,
                cancel_event=cancel_event,
                deadline=resolver_deadline,
                result_callback=process_result_callback,
            )
        diagnostic = _record_packwiz_process_diagnostic(
            command,
            process,
            project_id=diagnostic_project_id,
            operation=f"resolve-{normalized_provider}",
            callback=diagnostic_callback,
        )

        if process.termination_incomplete:
            raise HuroshikiError(
                _packwiz_diagnostic_detail(
                    "Packwiz resolver process termination was incomplete", diagnostic
                )
            )
        if process.orphaned_descendants:
            raise HuroshikiError(
                _packwiz_diagnostic_detail(
                    "Packwiz resolver left background processes after completion",
                    diagnostic,
                )
            )
        if process.cancelled:
            raise HuroshikiError(
                _packwiz_diagnostic_detail("MOD resolution was cancelled", diagnostic)
            )
        if process.timed_out:
            raise HuroshikiError(
                _packwiz_diagnostic_detail(
                    "Packwiz resolver deadline exceeded", diagnostic
                )
            )
        if process.output_limit_exceeded:
            raise HuroshikiError(
                _packwiz_diagnostic_detail(
                    "Packwiz resolver exceeded the supported output limit", diagnostic
                )
            )
        if process.returncode != 0:
            raise HuroshikiError(
                _packwiz_diagnostic_detail(
                    concise_process_error(process, command=command), diagnostic
                )
            )
        metadata = _read_resolver_metadata(source)
        root_identity = resolved_root_identity(
            normalized_provider, project_id, metadata
        )
        return ResolvedModClosure(root_identity, metadata)


def _exact_process_failure(
    result: ResolverProcessResult,
    *,
    label: str,
    command: Sequence[str] = (),
) -> str | None:
    if result.termination_incomplete:
        return f"{label} process termination was incomplete"
    if result.orphaned_descendants:
        return f"{label} left background processes after completion"
    if result.cancelled:
        return f"{label} was cancelled"
    if result.timed_out:
        return f"{label} deadline exceeded"
    if result.output_limit_exceeded:
        return f"{label} exceeded the supported output limit"
    if result.returncode != 0:
        return f"{label} failed: {concise_process_error(result, command=command)}"
    return None


def _verify_exact_root_metadata(
    selection: ExactModArtifactSelection,
    metadata: Sequence[ResolvedMetadata],
) -> ResolvedMetadata:
    matches = [item for item in metadata if item.identity == selection.identity]
    if len(matches) != 1:
        raise HuroshikiError(
            f"Exact resolver produced {len(matches)} roots for "
            f"{selection.identity_label}"
        )
    root = matches[0]
    try:
        identity = parse_provider_metadata(root.relative_path, root.contents)
    except Exception as error:
        raise HuroshikiError(
            f"Exact resolver produced invalid root metadata for "
            f"{selection.identity_label}: {error}"
        ) from error
    if (
        identity.provider != selection.provider
        or identity.project_id != selection.project_id
        or identity.file_id != selection.artifact_id
    ):
        actual = (
            f"{identity.provider}:{identity.project_id}"
            f" artifact {identity.file_id or '<missing>'}"
        )
        raise HuroshikiError(
            f"Exact resolver selected {actual}; expected "
            f"{selection.identity_label} artifact {selection.artifact_id}"
        )
    return root


def resolve_exact_mod_closure(
    selection: ExactModArtifactSelection,
    *,
    source: Path,
    cancel_event: threading.Event,
    deadline: float,
    checkpoint: Callable[[], None],
    preseed_selections: Sequence[ExactModArtifactSelection] = (),
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
    diagnostic_project_id: str | None = None,
    diagnostic_callback: Callable[[str], None] | None = None,
) -> ResolvedModClosure:
    """Resolve one exact provider artifact in an operation-owned Packwiz source."""
    checkpoint()
    ensure_safe_pack_source(source, checkpoint=checkpoint)
    checkpoint()
    for exact_selection in tuple(preseed_selections) + (selection,):
        command = build_exact_artifact_command(exact_selection)
        resolver_deadline = min(
            deadline,
            time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
        )
        process_kwargs: dict[str, object] = {
            "cwd": source,
            "cancel_event": cancel_event,
            "deadline": resolver_deadline,
        }
        if process_result_callback is not None:
            process_kwargs["result_callback"] = process_result_callback
        result = run_resolver_process(command, **process_kwargs)
        diagnostic = _record_packwiz_process_diagnostic(
            command,
            result,
            project_id=diagnostic_project_id,
            operation=f"exact-{exact_selection.provider}-add",
            callback=diagnostic_callback,
        )
        failure = _exact_process_failure(
            result, label="Exact Packwiz resolution", command=command
        )
        if failure is not None:
            raise HuroshikiError(_packwiz_diagnostic_detail(failure, diagnostic))
        checkpoint()
    refresh_deadline = min(
        deadline,
        time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
    )
    refresh_kwargs: dict[str, object] = {
        "cwd": source,
        "cancel_event": cancel_event,
        "deadline": refresh_deadline,
    }
    if process_result_callback is not None:
        refresh_kwargs["result_callback"] = process_result_callback
    refresh = run_resolver_process(
        ["packwiz", "refresh"],
        **refresh_kwargs,
    )
    diagnostic = _record_packwiz_process_diagnostic(
        ["packwiz", "refresh"],
        refresh,
        project_id=diagnostic_project_id,
        operation="exact-resolver-refresh",
        callback=diagnostic_callback,
    )
    failure = _exact_process_failure(
        refresh, label="Exact Packwiz refresh", command=["packwiz", "refresh"]
    )
    if failure is not None:
        raise HuroshikiError(_packwiz_diagnostic_detail(failure, diagnostic))
    checkpoint()
    ensure_safe_pack_source(source, checkpoint=checkpoint)
    metadata = _read_resolver_metadata(source)
    _verify_exact_root_metadata(selection, metadata)
    return ResolvedModClosure(selection.identity, metadata)


def _closure_metadata_semantics(contents: bytes) -> tuple[object, object, object]:
    document = tomllib.loads(contents.decode("utf-8"))
    return document.get("filename"), document.get("download"), document.get("update")


def _equivalence_snapshot_digest(
    source: Path,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    for path in _checkpointed_paths(source, "*.pw.toml", checkpoint):
        _run_checkpoint(checkpoint)
        if not path.is_file() or path.is_symlink():
            continue
        relative = portable_relative_path(path.relative_to(source))
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_read_file_bytes(path, checkpoint))
        digest.update(b"\0")
    return digest.hexdigest()


def _dependency_provenance(
    *, existing: bool, explicit: bool, provenance_known: bool = True
) -> Provenance:
    if existing and not provenance_known:
        return "unknown"
    return "explicit" if explicit else "dependency"


def _dependency_candidate(
    *,
    identity: tuple[str, str],
    relative_path: Path,
    filename: str,
    contents: bytes,
    side: str,
    provenance: Provenance,
    existing: bool,
) -> DependencyCandidate:
    return DependencyCandidate(
        f"{identity[0]}:{identity[1]}",
        str(relative_path),
        filename,
        contents,
        side,
        provenance,
        existing,
    )


def _resolved_metadata_side(item: ResolvedMetadata) -> str:
    mod = read_mod_data(
        item.relative_path,
        tomllib.loads(item.contents.decode("utf-8")),
    )
    if mod.side_error is not None:
        raise HuroshikiError(
            f"Resolved metadata has invalid side for {item.relative_path}: "
            f"{mod.side_error}"
        )
    return mod.side


def _verify_dependency_collision(
    existing: DependencyCandidate,
    incoming: DependencyCandidate,
    *,
    context: EquivalenceContext,
    workspace: Path,
    cancel_event: threading.Event | None,
    deadline: float | None,
    process_result_callback: Callable[[BoundedProcessResult], None] | None,
):
    def materialize(candidate: DependencyCandidate, _: EquivalenceContext):
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return materialize_provider_artifact(
            candidate,
            context,
            workspace=workspace,
            cancel_event=cancel_event,
            deadline=deadline,
            process_result_callback=process_result_callback,
        )

    try:
        evidence = verify_equivalence(existing, incoming, context, materialize)
    except EquivalenceError as error:
        raise HuroshikiError(str(error)) from error
    if evidence is None:
        raise HuroshikiError(
            "Cross-provider dependency collision could not be verified as equivalent"
        )
    return evidence


def merge_metadata_closure(
    staged_source: Path,
    closure: ResolvedModClosure,
    *,
    requested_side: str,
    preserve_resolved_dependency_sides: bool = False,
    explicit_root_sides: Mapping[tuple[str, str], str] | None = None,
    exact_selected_identity: tuple[str, str] | None = None,
    exact_selected_side: str | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    equivalence_workspace: Path | None = None,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
) -> tuple[Path, ...]:
    def checkpoint() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise HuroshikiError("MOD closure merge was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise HuroshikiError("MOD closure merge deadline exceeded")

    side = packctl.normalize_side(requested_side)
    checkpoint()
    ensure_safe_pack_source(staged_source, checkpoint=checkpoint)
    if not any(item.identity == closure.root_identity for item in closure.metadata):
        raise HuroshikiError(
            f"Resolved closure does not contain requested root "
            f"{closure.root_identity[0]}:{closure.root_identity[1]}"
        )
    existing_mods = [
        read_mod(staged_source, path.relative_to(staged_source))
        for path in _checkpointed_paths(staged_source, "*.pw.toml", checkpoint)
        if path.is_file() and not path.is_symlink()
    ]
    root_manifest = staged_source / ".huroshiki-roots.json"
    manifest_exists = root_manifest.is_file() and not root_manifest.is_symlink()
    explicit_roots = (
        {
            (canonical_provider(record.provider), record.project_id)
            for record in read_pack_root_manifest(staged_source)
        }
        if manifest_exists
        else set()
    )
    checkpoint()
    minecraft, loader, loader_version = packctl.project_versions(staged_source)
    context = EquivalenceContext(
        minecraft,
        loader,
        loader_version,
        _equivalence_snapshot_digest(staged_source, checkpoint),
        EQUIVALENCE_POLICY_VERSION,
    )
    workspace = equivalence_workspace or (
        staged_source.parent / f"equivalence-{uuid4().hex}"
    )
    incoming_records: dict[tuple[str, str], ResolvedMetadata] = {}
    for item in closure.metadata:
        checkpoint()
        canonical_identity = (canonical_provider(item.provider), item.project_id)
        if item.identity != canonical_identity:
            raise HuroshikiError(
                f"Resolved metadata identity mismatch for {item.relative_path}: "
                f"{item.identity!r} vs {canonical_identity!r}"
            )
        if item.identity in incoming_records:
            raise HuroshikiError(
                f"Resolved closure contains duplicate identity "
                f"{item.provider}:{item.project_id}"
            )
        incoming_records[item.identity] = item
    while True:
        incoming_path_owners: dict[str, tuple[str, str]] = {}
        incoming_filename_owners: dict[str, tuple[str, str]] = {}
        collision_pair: tuple[tuple[str, str], tuple[str, str]] | None = None
        for identity, item in sorted(incoming_records.items()):
            checkpoint()
            owners = {
                owner
                for owner in (
                    incoming_path_owners.get(
                        portable_relative_path_key(item.relative_path)
                    ),
                    incoming_filename_owners.get(
                        portable_basename_key(item.filename)
                    ),
                )
                if owner is not None and owner != identity
            }
            if len(owners) > 1:
                raise HuroshikiError(
                    "Resolved closure metadata path and filename have different owners"
                )
            if owners:
                collision_pair = (next(iter(owners)), identity)
                break
            incoming_path_owners[
                portable_relative_path_key(item.relative_path)
            ] = identity
            incoming_filename_owners[portable_basename_key(item.filename)] = identity
        if collision_pair is None:
            break
        left_identity, right_identity = collision_pair
        if {left_identity[0], right_identity[0]} != {"modrinth", "curseforge"}:
            raise HuroshikiError(
                "Resolved closure contains a metadata path or filename collision"
            )
        left_item = incoming_records[left_identity]
        right_item = incoming_records[right_identity]
        left_candidate = _dependency_candidate(
            identity=left_identity,
            relative_path=left_item.relative_path,
            filename=left_item.filename,
            contents=left_item.contents,
            side=(
                side
                if left_identity == closure.root_identity
                else _resolved_metadata_side(left_item)
            ),
            provenance=_dependency_provenance(
                existing=False,
                explicit=left_identity == closure.root_identity,
            ),
            existing=False,
        )
        right_candidate = _dependency_candidate(
            identity=right_identity,
            relative_path=right_item.relative_path,
            filename=right_item.filename,
            contents=right_item.contents,
            side=(
                side
                if right_identity == closure.root_identity
                else _resolved_metadata_side(right_item)
            ),
            provenance=_dependency_provenance(
                existing=False,
                explicit=right_identity == closure.root_identity,
            ),
            existing=False,
        )
        evidence = _verify_dependency_collision(
            left_candidate,
            right_candidate,
            context=context,
            workspace=workspace,
            cancel_event=cancel_event,
            deadline=deadline,
            process_result_callback=process_result_callback,
        )
        selected_identity = tuple(evidence.selected_identity.split(":", 1))
        losing_identity = (
            right_identity if selected_identity == left_identity else left_identity
        )
        incoming_records.pop(losing_identity)
    closure_metadata = tuple(incoming_records.values())
    existing_by_identity: dict[tuple[str, str], ModInfo] = {}
    path_owners: dict[str, tuple[str, str]] = {}
    filename_owners: dict[str, tuple[str, str]] = {}
    for mod in existing_mods:
        checkpoint()
        identity = (canonical_provider(mod.provider), mod.project_id)
        if identity in existing_by_identity:
            raise HuroshikiError(
                f"Existing metadata identity {identity[0]}:{identity[1]} is duplicated"
            )
        existing_by_identity[identity] = mod
        path_owners[portable_relative_path_key(mod.relative_path)] = identity
        filename_owners[portable_basename_key(mod.filename)] = identity

    pending: list[
        tuple[ResolvedMetadata | None, ModInfo, Path, bytes, str]
        | tuple[ResolvedMetadata, None, Path, bytes, str]
    ] = []
    removals: list[Path] = []
    incoming_identities: set[tuple[str, str]] = set()
    for item in closure_metadata:
        checkpoint()
        canonical_identity = (canonical_provider(item.provider), item.project_id)
        if item.identity != canonical_identity:
            raise HuroshikiError(
                f"Resolved metadata identity mismatch for {item.relative_path}: "
                f"{item.identity!r} vs {canonical_identity!r}"
            )
        incoming_identities.add(item.identity)
        item_side = side
        if explicit_root_sides and item.identity in explicit_root_sides:
            item_side = packctl.normalize_side(explicit_root_sides[item.identity])
        elif preserve_resolved_dependency_sides and item.identity != closure.root_identity:
            item_side = _resolved_metadata_side(item)
        if exact_selected_identity is not None and item.identity == exact_selected_identity:
            if exact_selected_side is None:
                raise HuroshikiError("Exact selected side is unavailable")
            item_side = packctl.normalize_side(exact_selected_side)
        path_key = portable_relative_path_key(item.relative_path)
        filename_key = portable_basename_key(item.filename)
        existing = existing_by_identity.get(item.identity)
        path_owner = path_owners.get(path_key)
        filename_owner = filename_owners.get(filename_key)
        collision_owners = {
            owner
            for owner in (path_owner, filename_owner)
            if owner is not None and owner != item.identity
        }
        if collision_owners:
            if len(collision_owners) != 1:
                raise HuroshikiError(
                    "Metadata path and filename are owned by different identities"
                )
            collision_identity = next(iter(collision_owners))
            collision = existing_by_identity.get(collision_identity)
            if collision is None:
                raise HuroshikiError("Cross-provider collision owner is unavailable")
            if {
                canonical_provider(collision.provider),
                canonical_provider(item.provider),
            } != {"modrinth", "curseforge"}:
                if path_owner is not None and path_owner != item.identity:
                    raise HuroshikiError(
                        f"Metadata path collision at {item.relative_path}: "
                        f"{collision_identity[0]}:{collision_identity[1]} vs "
                        f"{item.provider}:{item.project_id}"
                    )
                raise HuroshikiError(
                    f"Filename collision for {item.filename!r}: "
                    f"{collision_identity[0]}:{collision_identity[1]} vs "
                    f"{item.provider}:{item.project_id}"
                )
            collision_contents = safe_child(
                staged_source, collision.relative_path
            ).read_bytes()
            existing_candidate = _dependency_candidate(
                identity=collision_identity,
                relative_path=collision.relative_path,
                filename=collision.filename,
                contents=collision_contents,
                side=collision.side,
                provenance=_dependency_provenance(
                    existing=True,
                    explicit=collision_identity in explicit_roots,
                    provenance_known=manifest_exists,
                ),
                existing=True,
            )
            incoming_candidate = _dependency_candidate(
                identity=item.identity,
                relative_path=item.relative_path,
                filename=item.filename,
                contents=item.contents,
                side=item_side,
                provenance=_dependency_provenance(
                    existing=False,
                    explicit=item.identity == closure.root_identity,
                ),
                existing=False,
            )
            evidence = _verify_dependency_collision(
                existing_candidate,
                incoming_candidate,
                context=context,
                workspace=workspace,
                cancel_event=cancel_event,
                deadline=deadline,
                process_result_callback=process_result_callback,
            )
            assigned_side = union_side(collision.side, item_side)
            if evidence.selected_identity == existing_candidate.provider_identity:
                updated = _metadata_contents_with_side(
                    collision_contents, assigned_side
                )
                pending.append(
                    (None, collision, collision.relative_path, updated, assigned_side)
                )
                continue
            updated = _metadata_contents_with_side(item.contents, assigned_side)
            pending.append((item, None, item.relative_path, updated, assigned_side))
            removals.append(collision.relative_path)
            path_owners.pop(portable_relative_path_key(collision.relative_path), None)
            filename_owners.pop(portable_basename_key(collision.filename), None)
            existing_by_identity.pop(collision_identity, None)
            path_owners[path_key] = item.identity
            filename_owners[filename_key] = item.identity
            continue
        if existing is not None:
            existing_contents = safe_child(staged_source, existing.relative_path).read_bytes()
            if item.provider != "url" and (
                portable_basename_key(existing.filename) != filename_key
                or _closure_metadata_semantics(existing_contents)
                != _closure_metadata_semantics(item.contents)
            ):
                raise HuroshikiError(
                    "Resolved metadata disagreement for existing identity "
                    f"{item.provider}:{item.project_id}"
                )
            if existing.side_error is not None:
                raise HuroshikiError(
                    f"Cannot preserve invalid existing side for {existing.relative_path}: "
                    f"{existing.side_error}"
                )
            assigned_side = union_side(existing.side, item_side)
        else:
            path_owners[path_key] = item.identity
            filename_owners[filename_key] = item.identity
            assigned_side = item_side
        relative_path = existing.relative_path if existing is not None else item.relative_path
        path = safe_child(staged_source, relative_path)
        contents = (
            path.read_bytes()
            if existing is not None and item.provider != "url"
            else item.contents
        )
        pending.append(
            (
                item,
                existing,
                relative_path,
                _metadata_contents_with_side(contents, assigned_side),
                assigned_side,
            )
        )

    changed: list[Path] = []
    for relative_path in removals:
        checkpoint()
        path = safe_child(staged_source, relative_path)
        if path.exists():
            path.unlink()
            changed.append(relative_path)
    for _item, _existing, relative_path, updated, _assigned_side in pending:
        checkpoint()
        path = safe_child(staged_source, relative_path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != updated:
            path.write_bytes(updated)
            changed.append(relative_path)
    if manifest_exists:
        record_pack_root(
            staged_source,
            closure.root_identity[0],
            closure.root_identity[1],
            side,
        )
    return tuple(changed)


def _resolve_template_root(
    entry: MergedTemplateMod,
    *,
    minecraft: str,
    loader: str,
    loader_version: str,
    cancel_event: threading.Event,
    deadline: float,
    resolver_root: Path | None = None,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
) -> _ResolvedTemplateRoot:
    expected_identity = (
        canonical_provider(entry.provider),
        entry.project_id,
    )
    if expected_identity[0] != "url":
        closure = resolve_mod_closure(
            provider=entry.provider,
            selector=entry.project_id,
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
            canonical_project_id=entry.project_id,
            cancel_event=cancel_event,
            deadline=deadline,
            resolver_root=resolver_root,
            process_result_callback=process_result_callback,
        )
        metadata = _resolved_metadata_with_side(closure.metadata, entry.side)
        if closure.root_identity != expected_identity:
            found = ", ".join(
                f"{item.provider}:{item.project_id}" for item in metadata
            )
            raise HuroshikiError(
                f"Requested root identity {expected_identity[0]}:{expected_identity[1]} "
                f"was not independently resolved (resolver produced: {found or 'none'})"
            )
        return _ResolvedTemplateRoot(entry, metadata, expected_identity)

    state_root = ROOT / ".huroshiki"
    transaction_root = state_root / "transactions"
    packctl.make_state_directory(
        transaction_root,
        state_root=state_root,
        repository_root=ROOT,
    )
    with tempfile.TemporaryDirectory(
        prefix="template-resolver-", dir=transaction_root
    ) as directory:
        source = Path(directory) / "source"
        create_resolver_source(
            source,
            display_name=f"Resolve {entry.name}",
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
        )
        log_dir = (
            state_root
            / "logs"
            / "template-create"
            / f"resolver-{uuid4().hex[:8]}"
        )
        packctl.ensure_safe_state_path(
            log_dir,
            state_root=state_root,
            repository_root=ROOT,
        )
        artifact = download_url_artifact(
            entry.url or "",
            cancel_event,
            log_dir,
            loader,
            entry.max_url_jar_size_bytes or DEFAULT_URL_MAX_JAR_SIZE_BYTES,
            total_timeout_seconds=min(
                DEFAULT_URL_TOTAL_TIMEOUT_SECONDS,
                max(0.001, deadline - time.monotonic()),
            ),
            allow_private_networks=entry.url_allow_private_networks,
        )
        if artifact.mod_id != entry.project_id:
            raise HuroshikiError(
                f"URL now contains MOD ID {artifact.mod_id}, expected {entry.project_id}"
            )
        write_url_metadata(
            source,
            Path("mods") / f"{entry.project_id}.pw.toml",
            artifact,
            entry.side,
        )

        metadata = _resolved_metadata_with_side(
            _read_resolver_metadata(source), entry.side
        )
        matching_roots = [item for item in metadata if item.identity == expected_identity]
        if len(matching_roots) != 1:
            found = ", ".join(
                f"{item.provider}:{item.project_id}" for item in metadata
            )
            raise HuroshikiError(
                f"Requested root identity {expected_identity[0]}:{expected_identity[1]} "
                f"was not independently resolved (resolver produced: {found or 'none'})"
            )
        return _ResolvedTemplateRoot(entry, metadata, expected_identity)


def _merge_resolved_template_roots(
    roots: Iterable[_ResolvedTemplateRoot],
    *,
    minecraft: str,
    loader: str,
    loader_version: str,
    workspace: Path,
    cancel_event: threading.Event,
    deadline: float,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
) -> tuple[
    tuple[ResolvedMetadata, ...],
    tuple[RetainedTemplateCandidate, ...],
    tuple[TemplateInstallFailure, ...],
]:
    roots = tuple(roots)
    closure_digest = hashlib.sha256()
    for root in roots:
        for item in sorted(root.metadata, key=lambda value: value.identity):
            closure_digest.update(item.contents)
    context = EquivalenceContext(
        minecraft,
        loader,
        loader_version,
        closure_digest.hexdigest(),
        EQUIVALENCE_POLICY_VERSION,
    )
    merged: dict[tuple[str, str], ResolvedMetadata] = {}
    path_owners: dict[str, tuple[str, str]] = {}
    filename_owners: dict[str, tuple[str, str]] = {}
    url_root_owners: dict[tuple[str, str], str] = {}
    retained: list[RetainedTemplateCandidate] = []
    failures: list[TemplateInstallFailure] = []
    explicit_identities: set[tuple[str, str]] = set()

    for root in roots:
        entry = root.entry
        reason: str | None = None
        resolved_items = {item.identity: item for item in root.metadata}
        skipped_items: set[tuple[str, str]] = set()
        if root.root_identity[0] == "url":
            previous = url_root_owners.get(root.root_identity)
            if previous is not None and previous != entry.candidate_key:
                reason = (
                    f"URL MOD ID/path collision for {root.root_identity[1]!r}; "
                    "the selected URLs cannot both be represented"
                )

        pending_paths = dict(path_owners)
        pending_filenames = dict(filename_owners)
        pending_merged = dict(merged)
        if reason is None:
            for item in root.metadata:
                path_key = portable_relative_path_key(item.relative_path)
                filename_key = portable_basename_key(item.filename)
                existing = pending_merged.get(item.identity)
                if existing is not None:
                    existing_document = tomllib.loads(
                        existing.contents.decode("utf-8")
                    )
                    incoming_document = tomllib.loads(item.contents.decode("utf-8"))
                    existing_document.pop("side", None)
                    incoming_document.pop("side", None)
                    existing_document["filename"] = portable_basename(
                        str(existing_document.get("filename", "")),
                        context="Metadata filename",
                    )
                    incoming_document["filename"] = portable_basename(
                        str(incoming_document.get("filename", "")),
                        context="Metadata filename",
                    )
                    if (
                        portable_relative_path_key(existing.relative_path) != path_key
                        or portable_basename_key(existing.filename) != filename_key
                        or existing_document != incoming_document
                    ):
                        reason = (
                            "resolved metadata disagreement for shared identity "
                            f"{item.provider}:{item.project_id}; dependency versions, "
                            "paths, downloads, or update metadata differ"
                        )
                        break
                    continue
                path_owner = pending_paths.get(path_key)
                filename_owner = pending_filenames.get(filename_key)
                collision_owners = {
                    owner
                    for owner in (path_owner, filename_owner)
                    if owner is not None and owner != item.identity
                }
                if collision_owners:
                    if len(collision_owners) != 1:
                        reason = "metadata path and filename have different owners"
                        break
                    owner = next(iter(collision_owners))
                    previous = pending_merged[owner]
                    if {owner[0], item.identity[0]} != {"modrinth", "curseforge"}:
                        reason = (
                            f"metadata path or filename collision: "
                            f"{owner[0]}:{owner[1]} vs "
                            f"{item.provider}:{item.project_id}"
                        )
                        break
                    previous_mod = read_mod_data(
                        previous.relative_path,
                        tomllib.loads(previous.contents.decode("utf-8")),
                    )
                    existing_candidate = _dependency_candidate(
                        identity=owner,
                        relative_path=previous.relative_path,
                        filename=previous.filename,
                        contents=previous.contents,
                        side=previous_mod.side,
                        provenance=_dependency_provenance(
                            existing=True,
                            explicit=owner in explicit_identities,
                        ),
                        existing=True,
                    )
                    incoming_candidate = _dependency_candidate(
                        identity=item.identity,
                        relative_path=item.relative_path,
                        filename=item.filename,
                        contents=item.contents,
                        side=entry.side,
                        provenance=_dependency_provenance(
                            existing=False,
                            explicit=item.identity == root.root_identity,
                        ),
                        existing=False,
                    )
                    if (
                        existing_candidate.provenance == "explicit"
                        and incoming_candidate.provenance == "explicit"
                    ):
                        reason = (
                            "metadata path collision between explicit roots; "
                            "automatic cross-provider collapse is prohibited"
                        )
                        break
                    try:
                        evidence = _verify_dependency_collision(
                            existing_candidate,
                            incoming_candidate,
                            context=context,
                            workspace=workspace,
                            cancel_event=cancel_event,
                            deadline=deadline,
                            process_result_callback=process_result_callback,
                        )
                    except HuroshikiError as error:
                        reason = str(error)
                        break
                    merged_side = union_side(previous_mod.side, entry.side)
                    if evidence.selected_identity == existing_candidate.provider_identity:
                        pending_merged[owner] = replace(
                            previous,
                            contents=_metadata_contents_with_side(
                                previous.contents, merged_side
                            ),
                        )
                        skipped_items.add(item.identity)
                        continue
                    pending_merged.pop(owner)
                    pending_paths.pop(
                        portable_relative_path_key(previous.relative_path), None
                    )
                    pending_filenames.pop(
                        portable_basename_key(previous.filename), None
                    )
                    item = replace(
                        item,
                        contents=_metadata_contents_with_side(
                            item.contents, merged_side
                        ),
                    )
                    resolved_items[item.identity] = item
                pending_paths[path_key] = item.identity
                pending_filenames[filename_key] = item.identity

        if reason is not None:
            failures.append(
                TemplateInstallFailure(
                    entry.name, entry.provider, entry.project_id, reason
                )
            )
            continue

        path_owners = pending_paths
        filename_owners = pending_filenames
        merged = pending_merged
        if root.root_identity[0] == "url":
            url_root_owners[root.root_identity] = entry.candidate_key
        for item in resolved_items.values():
            if item.identity in skipped_items:
                continue
            existing = merged.get(item.identity)
            if existing is None:
                merged[item.identity] = item
                continue
            existing_mod = read_mod_data(
                existing.relative_path,
                tomllib.loads(existing.contents.decode("utf-8")),
            )
            side = union_side(existing_mod.side, entry.side)
            merged[item.identity] = replace(
                existing,
                contents=_metadata_contents_with_side(existing.contents, side),
            )

        actual = merged[root.root_identity]
        explicit_identities.add(root.root_identity)
        retained.append(
            RetainedTemplateCandidate(
                entry.candidate_key,
                entry.name,
                canonical_provider(entry.provider),
                entry.project_id,
                actual.provider,
                actual.project_id,
                actual.relative_path,
                actual.filename,
            )
        )

    return tuple(merged.values()), tuple(retained), tuple(failures)


@dataclass(frozen=True)
class ImportedRootPreview:
    selection_key: str
    candidate_key: str
    requested_name: str
    requested_identity: tuple[str, str]
    actual_identity: tuple[str, str]
    relative_path: Path
    filename: str


@dataclass(frozen=True)
class TemplateImportPreview:
    added_roots: tuple[ImportedRootPreview, ...]
    added_dependencies: tuple[ModInfo, ...]
    side_changes: tuple[tuple[tuple[str, str], str, str], ...]
    removed: tuple[ModCandidate, ...]
    unchanged: tuple[ModCandidate, ...]
    changes: tuple[UpdateChange, ...]
    warnings: tuple[str, ...]


def _resolved_unchanged_pack_candidates(
    plan: TemplateImportPlan,
    resolved: ResolvedTemplateImportPlan,
) -> tuple[ModCandidate, ...]:
    relevant_identities = {
        candidate.actual_identity
        for candidate in plan.existing_identities
        if candidate.actual_identity is not None
    }
    side_changed_identities = {
        identity for identity, _old, _new in resolved.side_changes
    }
    return tuple(
        candidate
        for candidate in resolved.retained_pack_candidates
        if candidate.actual_identity in relevant_identities
        and candidate.actual_identity not in side_changed_identities
    )


def pack_import_candidates(source: Path, pack_id: str) -> tuple[ModCandidate, ...]:
    return tuple(
        ModCandidate(
            origin_kind="pack",
            origin_id=pack_id,
            name=mod.name,
            provider=canonical_provider(mod.provider),
            project_id=mod.project_id,
            side=mod.side,
            metadata_path=mod.relative_path,
            filename=mod.filename,
            url=mod.source_url or None,
            actual_provider=canonical_provider(mod.provider),
            actual_project_id=mod.project_id,
        )
        for mod in list_mods_from_source(source)
    )


def _template_import_inputs(
    template_ids: Sequence[str],
) -> tuple[
    dict[str, TemplateCompatibility],
    tuple[ModCandidate, ...],
    dict[str, dict[str, str]],
]:
    compatibilities: dict[str, TemplateCompatibility] = {}
    candidates: list[ModCandidate] = []
    baselines: dict[str, dict[str, str]] = {}
    for template_id in template_ids:
        root = packctl.get_template_root(template_id)
        baselines[template_id] = template_config_snapshot(root)
        config = packctl.load_template_config(template_id)
        template_minecraft, template_loader, _ = packctl.template_versions(template_id)
        compatibilities[template_id] = TemplateCompatibility(
            template_id,
            template_minecraft,
            template_loader,
        )
        max_size = url_max_jar_size_bytes(config)
        allow_private = url_allow_private_networks(config)
        raw_mods = config.get("mods", [])
        if raw_mods is None:
            raw_mods = []
        if not isinstance(raw_mods, list):
            raise HuroshikiError(
                f"templates/{template_id}/template.yaml mods must be a list"
            )
        for index, raw_entry in enumerate(raw_mods):
            entry = packctl.normalize_template_mod(
                raw_entry,
                f"templates/{template_id}/mods[{index}]",
            )
            candidates.append(
                template_candidate(
                    template_id,
                    name=entry["name"],
                    provider=canonical_provider(entry["provider"]),
                    project_id=entry["project_id"],
                    side=entry["side"],
                    url=entry.get("url"),
                    url_max_jar_size_bytes=max_size,
                    url_allow_private_networks=allow_private,
                )
            )
    return compatibilities, tuple(candidates), baselines


def resolved_closure_fingerprint(closure: ResolvedModClosure) -> str:
    payload = {
        "root_identity": closure.root_identity,
        "metadata": [
            {
                "identity": item.identity,
                "relative_path": item.relative_path.as_posix(),
                "filename": item.filename,
                "contents_sha256": hashlib.sha256(item.contents).hexdigest(),
            }
            for item in closure.metadata
        ],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def verify_import_candidates(
    candidates: Sequence[ModCandidate],
    *,
    minecraft: str,
    loader: str,
    loader_version: str,
    cancel_event: threading.Event,
    deadline: float,
) -> tuple[ImportCandidateVerification, ...]:
    verified: list[ImportCandidateVerification] = []
    for candidate in candidates:
        if cancel_event.is_set():
            raise LoaderMigrationCancelled("Template import was cancelled")
        if time.monotonic() >= deadline:
            raise LoaderMigrationDeadlineExceeded("Template import deadline exceeded")
        if candidate.provider == "url":
            if candidate.url is None:
                verified.append(
                    ImportCandidateVerification(
                        candidate.selector_identity,
                        None,
                        None,
                        None,
                        None,
                        "URL selector is missing",
                    )
                )
                continue
            candidate_deadline = min(
                deadline,
                time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
            )
            try:
                closure = resolve_mod_closure(
                    provider="url",
                    selector=candidate.url,
                    minecraft=minecraft,
                    loader=loader,
                    loader_version=loader_version,
                    cancel_event=cancel_event,
                    deadline=candidate_deadline,
                    url_max_jar_size_bytes=candidate.url_max_jar_size_bytes,
                    url_allow_private_networks=candidate.url_allow_private_networks,
                )
            except (LoaderMigrationCancelled, LoaderMigrationDeadlineExceeded):
                raise
            except UrlCandidateVerificationError as error:
                if cancel_event.is_set():
                    raise LoaderMigrationCancelled(
                        "Template import was cancelled"
                    ) from error
                if time.monotonic() >= candidate_deadline:
                    raise LoaderMigrationDeadlineExceeded(
                        "Template import URL verification deadline exceeded"
                    ) from error
                verified.append(
                    ImportCandidateVerification(
                        candidate.selector_identity,
                        None,
                        None,
                        None,
                        None,
                        str(error),
                    )
                )
                continue
            except HuroshikiError as error:
                if cancel_event.is_set():
                    raise LoaderMigrationCancelled(
                        "Template import was cancelled"
                    ) from error
                if time.monotonic() >= candidate_deadline:
                    raise LoaderMigrationDeadlineExceeded(
                        "Template import URL verification deadline exceeded"
                    ) from error
                raise
            actual_identity = closure.root_identity
            root_metadata = [
                item for item in closure.metadata if item.identity == actual_identity
            ]
            if len(root_metadata) != 1:
                verified.append(
                    ImportCandidateVerification(
                        candidate.selector_identity,
                        None,
                        None,
                        None,
                        None,
                        "Verified URL closure must contain exactly one root",
                    )
                )
                continue
            verified.append(
                ImportCandidateVerification(
                    candidate.selector_identity,
                    actual_identity,
                    root_metadata[0].relative_path,
                    root_metadata[0].filename,
                    resolved_closure_fingerprint(closure),
                    None,
                    closure,
                )
            )
        else:
            verified.append(
                ImportCandidateVerification(
                    candidate.selector_identity,
                    candidate.logical_identity,
                    candidate.metadata_path,
                    candidate.filename,
                    None,
                    None,
                )
            )
    return tuple(verified)


def build_verified_template_import_plan(
    *,
    pack_key: str,
    pack_versions: tuple[str, str, str],
    template_ids: Sequence[str],
    compatibilities: Mapping[str, TemplateCompatibility],
    pack_candidates: Sequence[ModCandidate],
    template_candidates: Sequence[ModCandidate],
    verifications: Sequence[ImportCandidateVerification],
) -> TemplateImportPlan:
    verification_by_selector = {
        item.selector_identity: item for item in verifications
    }
    final_candidates = tuple(
        replace(
            candidate,
            metadata_path=verification_by_selector[candidate.selector_identity].metadata_path,
            filename=verification_by_selector[candidate.selector_identity].filename,
            actual_provider=(
                verification_by_selector[candidate.selector_identity].actual_identity[0]
                if verification_by_selector[candidate.selector_identity].actual_identity
                is not None
                else None
            ),
            actual_project_id=(
                verification_by_selector[candidate.selector_identity].actual_identity[1]
                if verification_by_selector[candidate.selector_identity].actual_identity
                is not None
                else None
            ),
        )
        for candidate in template_candidates
    )
    try:
        return build_template_import_plan(
            pack_key=pack_key,
            pack_minecraft=pack_versions[0],
            pack_loader=pack_versions[1],
            template_ids=template_ids,
            compatibilities=compatibilities,
            pack_candidates=pack_candidates,
            template_candidates=final_candidates,
            verifications=verifications,
        )
    except TemplateMergeError as error:
        raise HuroshikiError(str(error)) from error


class TemplateImportSession:
    def __init__(
        self,
        transaction: PackTransaction,
        template_ids: tuple[str, ...],
        template_baselines: Mapping[str, dict[str, str]],
        verifications: tuple[ImportCandidateVerification, ...],
        plan: TemplateImportPlan,
        cancel_event: threading.Event,
        deadline: float,
    ) -> None:
        self.transaction = transaction
        self.template_ids = template_ids
        self.template_baselines = dict(template_baselines)
        self.verifications = verifications
        self.plan = plan
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.finished = False

    @classmethod
    def create(
        cls,
        pack_key: str,
        template_ids: Sequence[str],
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> "TemplateImportSession":
        kind, pack_id = split_project_key(pack_key)
        if kind != "pack":
            raise HuroshikiError("Templates can be imported only into packs")
        operation_cancel = cancel_event or threading.Event()
        operation_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + UPDATE_OPERATION_TIMEOUT_SECONDS
        )

        def checkpoint() -> None:
            if operation_cancel.is_set():
                raise LoaderMigrationCancelled("Template import was cancelled")
            if time.monotonic() >= operation_deadline:
                raise LoaderMigrationDeadlineExceeded("Template import deadline exceeded")

        transaction = PackTransaction.create(pack_key, checkpoint=checkpoint)
        try:
            checkpoint()
            pack_versions = packctl.project_versions(transaction.source)
            pack_candidates = pack_import_candidates(transaction.source, pack_id)
            compatibilities, raw_candidates, baselines = _template_import_inputs(
                template_ids
            )
            ordered_candidates = tuple(
                candidate
                for template_id in template_ids
                for candidate in raw_candidates
                if candidate.origin_id == template_id
            )
            merged_candidates = merge_template_import_candidates(ordered_candidates)
            verifications = verify_import_candidates(
                merged_candidates,
                minecraft=pack_versions[0],
                loader=pack_versions[1],
                loader_version=pack_versions[2],
                cancel_event=operation_cancel,
                deadline=operation_deadline,
            )
            if not all(
                template_config_snapshot(packctl.get_template_root(template_id))
                == baseline
                for template_id, baseline in baselines.items()
            ):
                raise HuroshikiError("Template manifest changed during import planning")
            plan = build_verified_template_import_plan(
                pack_key=pack_key,
                pack_versions=pack_versions,
                template_ids=template_ids,
                compatibilities=compatibilities,
                pack_candidates=pack_candidates,
                template_candidates=raw_candidates,
                verifications=verifications,
            )
            return cls(
                transaction,
                tuple(template_ids),
                baselines,
                verifications,
                plan,
                operation_cancel,
                operation_deadline,
            )
        except BaseException:
            transaction.discard()
            raise

    def templates_unchanged(self) -> bool:
        return all(
            template_config_snapshot(packctl.get_template_root(template_id)) == baseline
            for template_id, baseline in self.template_baselines.items()
        )

    def discard(self) -> None:
        if self.finished:
            return
        self.transaction.discard()
        self.finished = True


def prepare_template_import_plan(
    pack_key: str,
    template_ids: Sequence[str],
) -> TemplateImportPlan:
    session = TemplateImportSession.create(pack_key, template_ids)
    try:
        return session.plan
    finally:
        session.discard()


def _remove_import_candidates(
    source: Path,
    candidates: Sequence[ModCandidate],
) -> None:
    for candidate in candidates:
        if candidate.metadata_path is None or candidate.actual_identity is None:
            raise HuroshikiError("Pack removal candidate has incomplete identity data")
        current = read_mod(source, candidate.metadata_path)
        current_identity = (canonical_provider(current.provider), current.project_id)
        if current_identity != candidate.actual_identity:
            raise HuroshikiError("Pack candidate changed before removal")
        safe_child(source, candidate.metadata_path).unlink()


def _apply_import_side_changes(
    source: Path,
    side_changes: Sequence[tuple[tuple[str, str], str, str]],
) -> None:
    for identity, _old, new_side in side_changes:
        matching = [
            mod
            for mod in list_mods_from_source(source)
            if (canonical_provider(mod.provider), mod.project_id) == identity
        ]
        if len(matching) != 1:
            raise HuroshikiError(
                f"Side change identity must resolve exactly once: {identity[0]}:{identity[1]}"
            )
        path = safe_child(source, matching[0].relative_path)
        path.write_bytes(_metadata_contents_with_side(path.read_bytes(), new_side))


def _run_template_import_refresh(
    source: Path,
    *,
    cancel_event: threading.Event,
    deadline: float,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
    diagnostic_project_id: str | None = None,
) -> None:
    refresh = run_resolver_process(
        ["packwiz", "refresh"],
        cwd=source,
        cancel_event=cancel_event,
        deadline=min(
            deadline,
            time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
        ),
        result_callback=process_result_callback,
    )
    diagnostic = _record_packwiz_process_diagnostic(
        ["packwiz", "refresh"],
        refresh,
        project_id=diagnostic_project_id,
        operation="template-import-refresh",
    )
    if (
        refresh.returncode != 0
        or refresh.cancelled
        or refresh.timed_out
        or refresh.orphaned_descendants
        or refresh.termination_incomplete
        or refresh.output_limit_exceeded
    ):
        raise HuroshikiError(
            _packwiz_diagnostic_detail(
                "Template import Packwiz refresh failed", diagnostic
            )
        )


def _removed_identity_requirements(
    resolved_roots: Sequence[tuple[ModCandidate, ResolvedModClosure]],
    removed: Sequence[ModCandidate],
) -> dict[tuple[str, str], tuple[ModCandidate, ...]]:
    removed_identities = {
        candidate.actual_identity
        for candidate in removed
        if candidate.actual_identity is not None
    }
    required_by: dict[tuple[str, str], list[ModCandidate]] = {}
    for root, closure in resolved_roots:
        closure_identities = {item.identity for item in closure.metadata}
        for identity in removed_identities & closure_identities:
            required_by.setdefault(identity, []).append(root)
    return {identity: tuple(roots) for identity, roots in required_by.items()}


def _assert_removed_identities_absent(
    source: Path,
    removed: Sequence[ModCandidate],
) -> None:
    ensure_safe_pack_source(source)
    removed_identities = {
        candidate.actual_identity
        for candidate in removed
        if candidate.actual_identity is not None
    }
    present = {
        (canonical_provider(mod.provider), mod.project_id)
        for mod in list_mods_from_source(source)
    }
    reintroduced = removed_identities & present
    if reintroduced:
        details = ", ".join(
            f"{provider}:{project_id}"
            for provider, project_id in sorted(reintroduced)
        )
        raise HuroshikiError(
            "Template import reintroduced Pack MODs removed by the resolution: "
            + details
        )


def _apply_resolved_import_to_source(
    source: Path,
    *,
    resolved_roots: Sequence[tuple[ModCandidate, ResolvedModClosure]],
    removed: Sequence[ModCandidate],
    side_changes: Sequence[tuple[tuple[str, str], str, str]],
    checkpoint: Callable[[], None],
    cancel_event: threading.Event,
    deadline: float,
    process_result_callback: Callable[[BoundedProcessResult], None] | None = None,
    diagnostic_project_id: str | None = None,
) -> None:
    checkpoint()
    _remove_import_candidates(source, removed)
    for candidate, closure in resolved_roots:
        checkpoint()
        merge_metadata_closure(
            source,
            closure,
            requested_side=candidate.side,
            cancel_event=cancel_event,
            deadline=deadline,
            equivalence_workspace=source.parent / "template-import-equivalence",
            process_result_callback=process_result_callback,
        )
    _assert_removed_identities_absent(source, removed)
    _apply_import_side_changes(source, side_changes)
    _run_template_import_refresh(
        source,
        cancel_event=cancel_event,
        deadline=deadline,
        process_result_callback=process_result_callback,
        diagnostic_project_id=diagnostic_project_id,
    )
    _assert_removed_identities_absent(source, removed)
    ensure_safe_pack_source(source, checkpoint=checkpoint)


def _preflight_import_closures(
    transaction: PackTransaction,
    resolved_roots: Sequence[tuple[ModCandidate, ResolvedModClosure]],
    removed: Sequence[ModCandidate],
    side_changes: Sequence[tuple[tuple[str, str], str, str]],
    checkpoint: Callable[[], None],
    cancel_event: threading.Event,
    deadline: float,
) -> None:
    preflight_source = transaction.root / "import-preflight"
    try:
        copy_transaction_source(
            transaction.source,
            preflight_source,
            checkpoint=checkpoint,
        )
        _apply_resolved_import_to_source(
            preflight_source,
            resolved_roots=resolved_roots,
            removed=removed,
            side_changes=side_changes,
            checkpoint=checkpoint,
            cancel_event=cancel_event,
            deadline=deadline,
            process_result_callback=transaction._record_equivalence_process_result,
            diagnostic_project_id=transaction.project_key.partition(":")[2],
        )
    finally:
        if not transaction._equivalence_process_results:
            shutil.rmtree(preflight_source, ignore_errors=True)


class TemplateImportOperation:
    def __init__(
        self,
        session: TemplateImportSession,
        resolved: ResolvedTemplateImportPlan,
        *,
        deadline: float | None = None,
    ) -> None:
        if resolved.plan_digest != session.plan.plan_digest:
            raise HuroshikiError("Template import resolution has a stale plan digest")
        self.session = session
        self.plan = session.plan
        self.resolved = resolved
        self.deadline = min(
            session.deadline,
            deadline if deadline is not None else session.deadline,
        )
        self.cancel_event = session.cancel_event
        self.done = threading.Event()
        self.progress_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.transaction = session.transaction
        self.preview: TemplateImportPreview | None = None
        self.error: BaseException | None = None
        self.cancelled = False
        self._finished = False
        self._started = False
        self._lock = threading.Lock()

    def _checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise LoaderMigrationCancelled("Template import was cancelled")
        if time.monotonic() >= self.deadline:
            raise LoaderMigrationDeadlineExceeded("Template import deadline exceeded")

    def _verification(
        self, candidate: ModCandidate
    ) -> ImportCandidateVerification:
        matches = [
            item
            for item in self.session.verifications
            if item.selector_identity == candidate.selector_identity
        ]
        if (
            len(matches) != 1
            or not matches[0].succeeded
            or matches[0].actual_identity != candidate.actual_identity
        ):
            raise HuroshikiError("Template import URL verification cache is inconsistent")
        return matches[0]

    def run(self) -> None:
        with self._lock:
            if self._started or self.done.is_set():
                return
            self._started = True
        try:
            self.transaction.ensure_active()
            self._checkpoint()
            if self.resolved.plan_digest != self.session.plan.plan_digest:
                raise HuroshikiError("Template import resolution has a stale plan digest")
            if not self.session.templates_unchanged():
                raise HuroshikiError("Template manifest changed before import execution")
            before_files = _file_content_snapshot(self.transaction.source, self._checkpoint)
            before_identities = {
                (canonical_provider(mod.provider), mod.project_id)
                for mod in list_mods_from_source(self.transaction.source)
            }
            minecraft, loader, loader_version = packctl.project_versions(
                self.transaction.source
            )
            selected_actual_identities = [
                candidate.actual_identity
                for candidate in self.resolved.selected_new_roots
            ]
            if any(identity is None for identity in selected_actual_identities):
                raise HuroshikiError(
                    "Resolved template import contains an unverified root identity"
                )
            if len(set(selected_actual_identities)) != len(selected_actual_identities):
                raise HuroshikiError(
                    "Resolved template import contains duplicate actual root identities"
                )
            resolved_roots: list[tuple[ModCandidate, ResolvedModClosure]] = []
            for index, candidate in enumerate(self.resolved.selected_new_roots, 1):
                self._checkpoint()
                self.progress_queue.put(
                    f"Resolving {index}/{len(self.resolved.selected_new_roots)}: {candidate.name}"
                )
                verification = self._verification(candidate)
                if candidate.provider == "url":
                    closure = verification.cached_closure
                    if not isinstance(closure, ResolvedModClosure):
                        raise HuroshikiError("Verified URL closure is unavailable")
                else:
                    closure = resolve_mod_closure(
                        provider=candidate.provider,
                        selector=candidate.project_id,
                        minecraft=minecraft,
                        loader=loader,
                        loader_version=loader_version,
                        canonical_project_id=candidate.project_id,
                        cancel_event=self.cancel_event,
                        deadline=min(
                            self.deadline,
                            time.monotonic() + UPDATE_RESOLVER_TIMEOUT_SECONDS,
                        ),
                        resolver_root=(
                            self.transaction.root
                            / f"template-import-resolver-{uuid4().hex}"
                        ),
                        process_result_callback=(
                            self.transaction._record_equivalence_process_result
                        ),
                    )
                if closure.root_identity != verification.actual_identity:
                    raise HuroshikiError(
                        f"Resolved root identity changed for {candidate.candidate_key}"
                    )
                resolved_roots.append((candidate, closure))
            removed_requirements = _removed_identity_requirements(
                resolved_roots,
                self.resolved.removed_pack_candidates,
            )
            if removed_requirements:
                details = "; ".join(
                    f"{identity[0]}:{identity[1]} required by "
                    + ", ".join(root.candidate_key for root in roots)
                    for identity, roots in sorted(removed_requirements.items())
                )
                raise HuroshikiError(
                    "Selected Template roots require Pack MODs that the resolution "
                    f"removes: {details}"
                )
            _preflight_import_closures(
                self.transaction,
                resolved_roots,
                self.resolved.removed_pack_candidates,
                self.resolved.side_changes,
                self._checkpoint,
                self.cancel_event,
                self.deadline,
            )
            _apply_resolved_import_to_source(
                self.transaction.source,
                resolved_roots=resolved_roots,
                removed=self.resolved.removed_pack_candidates,
                side_changes=self.resolved.side_changes,
                checkpoint=self._checkpoint,
                cancel_event=self.cancel_event,
                deadline=self.deadline,
                process_result_callback=(
                    self.transaction._record_equivalence_process_result
                ),
                diagnostic_project_id=self.transaction.project_key.partition(":")[2],
            )
            if not self.session.templates_unchanged():
                raise HuroshikiError("Template manifest changed during import")
            after_mods = list_mods_from_source(self.transaction.source)
            selected_by_actual = {
                candidate.actual_identity: candidate
                for candidate in self.resolved.selected_new_roots
                if candidate.actual_identity is not None
            }
            imported_roots: list[ImportedRootPreview] = []
            for actual_identity, candidate in selected_by_actual.items():
                matching = [
                    mod
                    for mod in after_mods
                    if (canonical_provider(mod.provider), mod.project_id)
                    == actual_identity
                ]
                if len(matching) != 1:
                    raise HuroshikiError(
                        f"Imported root must resolve exactly once: {actual_identity[0]}:{actual_identity[1]}"
                    )
                mod = matching[0]
                imported_roots.append(
                    ImportedRootPreview(
                        candidate.selection_key,
                        candidate.candidate_key,
                        candidate.name,
                        candidate.logical_identity,
                        actual_identity,
                        mod.relative_path,
                        mod.filename,
                    )
                )
            added = tuple(
                mod
                for mod in after_mods
                if (canonical_provider(mod.provider), mod.project_id)
                not in before_identities
                and (canonical_provider(mod.provider), mod.project_id)
                not in selected_by_actual
            )
            unchanged = _resolved_unchanged_pack_candidates(
                self.plan,
                self.resolved,
            )
            removed_keys = {
                candidate.selection_key
                for candidate in self.resolved.removed_pack_candidates
            }
            unchanged_keys = {candidate.selection_key for candidate in unchanged}
            if removed_keys & unchanged_keys:
                raise HuroshikiError(
                    "Template import preview classifies a Pack candidate as both "
                    "removed and unchanged"
                )
            side_changed_identities = {
                identity for identity, _old, _new in self.resolved.side_changes
            }
            if any(
                candidate.actual_identity in side_changed_identities
                for candidate in unchanged
            ):
                raise HuroshikiError(
                    "Template import preview classifies a side-modified candidate "
                    "as unchanged"
                )
            self.preview = TemplateImportPreview(
                tuple(imported_roots),
                added,
                self.resolved.side_changes,
                self.resolved.removed_pack_candidates,
                unchanged,
                _content_changes(
                    before_files,
                    _file_content_snapshot(self.transaction.source, self._checkpoint),
                ),
                self.resolved.warnings,
            )
            self.progress_queue.put("Preview ready")
        except LoaderMigrationCancelled:
            self.cancelled = True
        except BaseException as error:
            self.error = error
        finally:
            if self.cancelled or self.error is not None or self.cancel_event.is_set():
                try:
                    self.discard()
                except BaseException as error:
                    if self.error is None:
                        self.error = error
            self.done.set()

    def apply(self) -> None:
        with self._lock:
            if (
                not self.done.is_set()
                or self.cancelled
                or self.error is not None
                or self._finished
                or self.preview is None
            ):
                raise HuroshikiError("Template import has no applicable preview")
            self._finished = True
        if (
            not self.session.templates_unchanged()
            or self.resolved.plan_digest != self.session.plan.plan_digest
        ):
            self.session.discard()
            raise HuroshikiError(
                "Template manifest changed or import plan became stale after preview"
            )
        try:
            self.transaction.ensure_active()
            self.transaction.apply(refresh=False)
            self.session.finished = True
        except BaseException:
            self.session.discard()
            raise

    def discard(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.session.discard()

    def cancel(self) -> None:
        self.cancelled = True
        self.cancel_event.set()
        if not self._started or self.done.is_set():
            self.discard()
            self.done.set()

    def drain_progress(self) -> tuple[str, ...]:
        values: list[str] = []
        while True:
            try:
                values.append(self.progress_queue.get_nowait())
            except queue.Empty:
                return tuple(values)


def prepare_template_composition(
    *,
    template_ids: list[str],
    minecraft: str,
    loader: str,
) -> TemplateComposition:
    try:
        if not template_ids:
            raise TemplateMergeError("At least one template must be selected")
        if len(set(template_ids)) != len(template_ids):
            raise TemplateMergeError("Template selection contains duplicate IDs")
        entries: list[TemplateModEntry] = []
        for template_id in template_ids:
            config = packctl.load_template_config(template_id)
            max_url_size = url_max_jar_size_bytes(config)
            allow_private_networks = url_allow_private_networks(config)
            template_minecraft, template_loader, _ = packctl.template_versions(template_id)
            if template_minecraft != minecraft or template_loader != loader.strip().lower():
                raise HuroshikiError(
                    f"Template {template_id} must use Minecraft {minecraft} and loader {loader}"
                )
            raw_mods = config.get("mods", [])
            if raw_mods is None:
                raw_mods = []
            if not isinstance(raw_mods, list):
                raise packctl.ConfigError(
                    f"templates/{template_id}/template.yaml mods must be a list"
                )
            entries.extend(
                TemplateModEntry(
                    template_id=template_id,
                    name=entry["name"],
                    provider=entry["provider"],
                    project_id=entry["project_id"],
                    url=entry.get("url"),
                    side=entry["side"],
                    max_url_jar_size_bytes=max_url_size,
                    url_allow_private_networks=allow_private_networks,
                )
                for index, raw_entry in enumerate(raw_mods)
                for entry in (
                    packctl.normalize_template_mod(
                        raw_entry,
                        f"templates/{template_id}/mods[{index}]",
                    ),
                )
            )
        return compose_templates(template_ids, entries)
    except TemplateMergeError as error:
        raise HuroshikiError(str(error)) from error


def conflict_multi_selection_error(
    conflict: TemplateConflict,
    candidate_keys: Iterable[str],
) -> str | None:
    requested = set(candidate_keys)
    selected = [
        candidate
        for candidate in conflict.candidates
        if candidate.candidate_key in requested
    ]
    identities: dict[tuple[str, str], MergedTemplateMod] = {}
    for candidate in selected:
        identity = (canonical_provider(candidate.provider), candidate.project_id)
        previous = identities.get(identity)
        if previous is not None:
            return (
                f"Cannot retain both {previous.provider}:{previous.project_id} (A) and "
                f"{candidate.provider}:{candidate.project_id} (B): they resolve to the "
                "same metadata identity/path. Re-select A or B."
            )
        identities[identity] = candidate
    return None


def _create_pack_from_templates(
    *,
    composition: TemplateComposition,
    resolved: ResolvedTemplateComposition,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    cancel_event: threading.Event,
    deadline: float,
) -> TemplateCreationReport:
    resolved_roots: list[_ResolvedTemplateRoot] = []
    resolution_failures: list[TemplateInstallFailure] = []
    template_state_root = ROOT / ".huroshiki"
    template_transaction_root = template_state_root / "transactions"
    packctl.make_state_directory(
        template_transaction_root,
        state_root=template_state_root,
        repository_root=ROOT,
    )
    equivalence_workspace = (
        template_transaction_root / f"template-equivalence-{uuid4().hex}"
    )
    equivalence_workspace.mkdir(mode=0o700)
    process_results: list[BoundedProcessResult] = []

    def record_process_result(result: BoundedProcessResult) -> None:
        if result.termination_incomplete:
            process_results.append(result)

    for entry in resolved.mods:
        if cancel_event.is_set():
            raise HuroshikiError("Template creation was cancelled")
        if time.monotonic() >= deadline:
            raise HuroshikiError("Template creation deadline exceeded")
        print(
            f"== Resolving {entry.name} ({entry.provider}:{entry.project_id}) ==",
            flush=True,
        )
        try:
            resolved_roots.append(
                _resolve_template_root(
                    entry,
                    minecraft=minecraft,
                    loader=loader,
                    loader_version=loader_version,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    resolver_root=(
                        equivalence_workspace / f"root-{len(resolved_roots) + 1}"
                        if canonical_provider(entry.provider) != "url"
                        else None
                    ),
                    process_result_callback=record_process_result,
                )
            )
        except (HuroshikiError, UnicodeError, tomllib.TOMLDecodeError) as error:
            if process_results:
                try:
                    _cleanup_template_creation_processes(
                        equivalence_workspace,
                        process_results,
                        deadline=(
                            time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS
                        ),
                    )
                except TemplateCreationCleanupRequired as cleanup_error:
                    raise cleanup_error from error
                raise HuroshikiError(
                    "Template resolver failed after bounded process cleanup"
                ) from error
            if cancel_event.is_set():
                raise HuroshikiError("Template creation was cancelled") from error
            if time.monotonic() >= deadline:
                raise HuroshikiError("Template creation deadline exceeded") from error
            reason = str(error)
            print(f"warning: {entry.name}: {reason}", file=sys.stderr, flush=True)
            resolution_failures.append(
                TemplateInstallFailure(
                    entry.name, entry.provider, entry.project_id, reason
                )
            )

    try:
        metadata, retained, merge_failures = _merge_resolved_template_roots(
            resolved_roots,
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
            workspace=equivalence_workspace,
            cancel_event=cancel_event,
            deadline=deadline,
            process_result_callback=record_process_result,
        )
    except BaseException as error:
        if process_results:
            try:
                _cleanup_template_creation_processes(
                    equivalence_workspace,
                    process_results,
                    deadline=time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS,
                )
            except TemplateCreationCleanupRequired as cleanup_error:
                raise cleanup_error from error
        elif equivalence_workspace.exists():
            shutil.rmtree(equivalence_workspace)
        raise
    else:
        if process_results:
            _cleanup_template_creation_processes(
                equivalence_workspace,
                process_results,
                deadline=time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS,
            )
            raise HuroshikiError(
                "Template dependency verification left incomplete process ownership"
            )
        if equivalence_workspace.exists():
            shutil.rmtree(equivalence_workspace)
    failures = [*resolution_failures, *merge_failures]
    multi_selected = {
        candidate_key
        for selection in resolved.conflict_selections
        if len(selection.candidate_keys) > 1
        for candidate_key in selection.candidate_keys
    }
    blocking = [
        failure
        for failure in failures
        if next(
            (
                entry.candidate_key
                for entry in resolved.mods
                if entry.provider == failure.provider
                and entry.project_id == failure.project_id
            ),
            None,
        )
        in multi_selected
    ]
    if blocking:
        detail = "; ".join(f"{item.name}: {item.reason}" for item in blocking)
        raise HuroshikiError(
            "Selected conflict candidates cannot all be retained: "
            f"{detail}. Re-select candidate A or B and retry."
        )

    current_composition = prepare_template_composition(
        template_ids=list(composition.template_ids),
        minecraft=minecraft,
        loader=loader,
    )
    if current_composition != composition:
        raise HuroshikiError(
            "Template composition changed during resolver preflight; "
            "review candidates again"
        )

    destination = packctl.get_pack_root(project_id, must_exist=False)
    destination_existed = destination.exists()
    result = create_project(
        "pack",
        project_id,
        display_name,
        minecraft,
        loader,
        loader_version,
        True,
        cancel_event=cancel_event,
        deadline=deadline,
    )
    if result != 0:
        raise HuroshikiError("Failed to create the destination MODPACK")
    owns_destination = not destination_existed
    try:
        pack_key = project_key("pack", project_id)
        source = project_source(pack_key)
        for item in metadata:
            path = safe_child(source, item.relative_path)
            if path.exists():
                raise HuroshikiError(
                    f"Destination metadata path unexpectedly exists: {item.relative_path}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.contents)

        retained_entries = {entry.candidate_key: entry for entry in resolved.mods}
        write_pack_root_manifest(
            source,
            tuple(
                PackRootRecord(
                    canonical_provider(item.actual_provider),
                    item.actual_project_id,
                    retained_entries[item.candidate_key].side,
                )
                for item in retained
            ),
        )

        _run_noninteractive_packwiz(
            ["packwiz", "refresh"],
            cwd=source,
            cancel_event=cancel_event,
            deadline=deadline,
            label="Template creation Packwiz refresh",
            process_result_callback=record_process_result,
            project_id=project_id,
            operation="template-refresh",
        )

        return TemplateCreationReport(
            pack_key=pack_key,
            template_ids=resolved.template_ids,
            conflict_selections=resolved.conflict_selections,
            conflict_warnings=resolved.warnings,
            installed=tuple(item.name for item in retained),
            failed=tuple(failures),
            retained=retained,
        )
    except BaseException as error:
        if process_results:
            try:
                _cleanup_template_creation_processes(
                    destination,
                    process_results,
                    deadline=time.monotonic() + TRANSACTION_DISCARD_TIMEOUT_SECONDS,
                )
            except TemplateCreationCleanupRequired as cleanup_error:
                raise cleanup_error from error
        if owns_destination and destination.exists():
            try:
                shutil.rmtree(destination)
            except BaseException as rollback_error:
                raise HuroshikiError(
                    f"{error}; failed to roll back destination {destination}: "
                    f"{rollback_error}"
                ) from error
        raise


def create_pack_from_templates(
    *,
    template_ids: list[str],
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    conflict_resolutions: Mapping[str, ConflictResolution] | None = None,
    expected_composition: TemplateComposition | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> TemplateCreationReport:
    try:
        packctl.validate_project_creation_fields(
            display_name=display_name,
            minecraft=minecraft,
            loader_version=loader_version,
        )
    except packctl.ConfigError as error:
        raise HuroshikiError(str(error)) from error
    operation_cancel = cancel_event or threading.Event()
    operation_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + PACKWIZ_OPERATION_TIMEOUT_SECONDS
    )
    pack_key = project_key("pack", project_id)
    retry_retained_template_creation_cleanup(pack_key)
    project_lock = packctl.ProjectLock(pack_key, "create project")
    project_lock.acquire()
    release_lock = True
    try:
        composition = prepare_template_composition(
            template_ids=template_ids,
            minecraft=minecraft,
            loader=loader,
        )
        if expected_composition is not None and composition != expected_composition:
            raise HuroshikiError(
                "Template composition changed after preview; review candidates again"
            )
        try:
            resolved = resolve_composition(composition, conflict_resolutions)
        except TemplateMergeError as error:
            raise HuroshikiError(str(error)) from error
        try:
            return _create_pack_from_templates(
                composition=composition,
                resolved=resolved,
                project_id=project_id,
                display_name=display_name,
                minecraft=minecraft,
                loader=loader,
                loader_version=loader_version,
                cancel_event=operation_cancel,
                deadline=operation_deadline,
            )
        except TemplateCreationCleanupRequired as error:
            _RETAINED_TEMPLATE_CREATIONS[pack_key] = (
                project_lock,
                error.workspace,
                error.results,
            )
            release_lock = False
            raise
    finally:
        if release_lock:
            project_lock.release()


def create_pack_from_template(
    *,
    template_id: str,
    project_id: str,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> TemplateCreationReport:
    return create_pack_from_templates(
        template_ids=[template_id],
        project_id=project_id,
        display_name=display_name,
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        cancel_event=cancel_event,
        deadline=deadline,
    )
