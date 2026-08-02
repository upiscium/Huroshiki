"""Pure, filesystem-neutral equivalence rules for provider dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Callable, Literal
import zipfile


class EquivalenceError(ValueError):
    """The candidates or their evidence cannot establish equivalence."""


Provider = Literal["modrinth", "curseforge"]
EvidenceKind = Literal["declared-sha256", "exact-sha256", "jar-mod-identity"]
EQUIVALENCE_POLICY_VERSION = "1"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_UNRESOLVED = {
    "", "unknown", "unresolved", "latest", "recommended", "none", "null", "*", "?"
}


@dataclass(frozen=True)
class DependencyCandidate:
    provider_identity: str
    relative_metadata_path: str
    filename: str
    contents: bytes
    side: str
    explicit_root: bool = False
    existing: bool = False


@dataclass(frozen=True)
class SemanticJarIdentity:
    members: tuple[tuple[str, str], ...]
    target_loader: str

    def __post_init__(self) -> None:
        members = tuple(sorted(self.members))
        if not members or members != self.members:
            raise EquivalenceError("semantic JAR members must be non-empty and sorted")
        if len(set(members)) != len(members):
            raise EquivalenceError("semantic JAR members must not be duplicated")
        for mod_id, version in members:
            if not _resolved(mod_id) or not _resolved(version):
                raise EquivalenceError("semantic JAR identity contains an unresolved member")
        if not _resolved(self.target_loader):
            raise EquivalenceError("semantic JAR identity has an unresolved loader")


@dataclass(frozen=True)
class MaterializedArtifact:
    sha256: str
    semantic_identity: SemanticJarIdentity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", _hash(self.sha256))


@dataclass(frozen=True)
class EquivalenceContext:
    minecraft: str
    loader: str
    loader_version: str
    source_snapshot_digest: str
    policy_version: str

    @property
    def target_loader(self) -> str:
        return self.loader.strip().lower()


@dataclass(frozen=True)
class EquivalenceEvidence:
    left_identity: str
    right_identity: str
    kind: EvidenceKind
    selected_identity: str
    artifact_sha256: str | None
    semantic_identity: SemanticJarIdentity | None
    left_metadata_sha256: str
    right_metadata_sha256: str
    context_digest: str
    binding_digest: str


def _resolved(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and lowered not in _UNRESOLVED and not any(
        marker in lowered
        for marker in ("${", "<unresolved", "<unknown", "@version@", "{version}")
    )


def _hash(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise EquivalenceError("expected a valid SHA-256 hex digest")
    return value.lower()


def _identity(candidate: DependencyCandidate) -> tuple[Provider, str]:
    try:
        provider, project = candidate.provider_identity.strip().lower().split(":", 1)
    except ValueError as error:
        raise EquivalenceError("provider identity must be provider:project-id") from error
    if provider not in ("modrinth", "curseforge") or not project.strip():
        raise EquivalenceError("only Modrinth/CurseForge identities qualify")
    if provider == "curseforge" and not project.isdecimal():
        raise EquivalenceError("CurseForge identity must contain a numeric project ID")
    return provider, project


def _canonical_identity(candidate: DependencyCandidate) -> str:
    provider, project = _identity(candidate)
    return f"{provider}:{project}"


def metadata_sha256(candidate: DependencyCandidate) -> str:
    return hashlib.sha256(candidate.contents).hexdigest()


def declared_download_hash(candidate: DependencyCandidate) -> str:
    """Return Packwiz's declared hash, rejecting every non-strict variant."""
    try:
        document = tomllib.loads(candidate.contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise EquivalenceError("metadata is not valid TOML") from error
    download = document.get("download")
    if not isinstance(download, dict):
        raise EquivalenceError("metadata has no download mapping")
    fmt = download.get("hash-format")
    value = download.get("hash")
    if fmt != "sha256" or not isinstance(value, str):
        raise EquivalenceError("metadata lacks a strict sha256 download hash")
    return _hash(value.strip())


def _optional_download_hash(candidate: DependencyCandidate) -> str | None:
    """Parse declarations while allowing a wholly absent declaration for JAR proof."""
    try:
        document = tomllib.loads(candidate.contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise EquivalenceError("metadata is not valid TOML") from error
    download = document.get("download")
    if not isinstance(download, dict):
        return None
    if "hash-format" not in download and "hash" not in download:
        return None
    if download.get("hash-format") != "sha256":
        return None
    return declared_download_hash(candidate)


def parse_semantic_jar(path: Path, target_loader: str) -> SemanticJarIdentity:
    """Parse the complete top-level MOD identity for exactly one target loader."""
    loader = target_loader.strip().lower().split(":", 1)[0]
    descriptors = {
        "neoforge": "META-INF/neoforge.mods.toml",
        "forge": "META-INF/mods.toml",
        "fabric": "fabric.mod.json",
        "quilt": "quilt.mod.json",
    }
    try:
        descriptor = descriptors[loader]
    except KeyError as error:
        raise EquivalenceError(f"unsupported semantic JAR loader {target_loader!r}") from error
    try:
        with zipfile.ZipFile(path) as jar:
            infos = jar.infolist()
            if len(infos) > 10_000:
                raise EquivalenceError("JAR contains too many entries")
            if sum(item.filename == descriptor for item in infos) != 1:
                raise EquivalenceError(
                    "JAR must contain exactly one target-loader descriptor"
                )
            entries = {item.filename: item for item in infos}
            info = entries.get(descriptor)
            if info is None:
                raise EquivalenceError("JAR has no metadata for the target loader")
            if info.file_size > 1024 * 1024:
                raise EquivalenceError("JAR metadata exceeds the size limit")
            with jar.open(info) as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise EquivalenceError("JAR metadata exceeds the size limit")
    except zipfile.BadZipFile as error:
        raise EquivalenceError("artifact is not a valid JAR") from error

    members: list[tuple[str, str]] = []

    def member(mod_id: object, version: object) -> tuple[str, str]:
        if not isinstance(mod_id, str) or not isinstance(version, str):
            raise EquivalenceError("MOD ID and version must be strings")
        return mod_id.strip().lower(), version.strip()

    try:
        if loader in {"forge", "neoforge"}:
            document = tomllib.loads(raw.decode("utf-8"))
            mods = document.get("mods")
            if not isinstance(mods, list) or not mods:
                raise EquivalenceError("loader metadata has no MOD declarations")
            for record in mods:
                if not isinstance(record, dict):
                    raise EquivalenceError("loader metadata has an invalid MOD declaration")
                members.append(member(record.get("modId"), record.get("version")))
        else:
            document = json.loads(raw.decode("utf-8"))
            if not isinstance(document, dict):
                raise EquivalenceError("loader metadata is not an object")
            if loader == "fabric":
                members.append(member(document.get("id"), document.get("version")))
            else:
                quilt = document.get("quilt_loader")
                if not isinstance(quilt, dict):
                    raise EquivalenceError("Quilt metadata has no quilt_loader object")
                members.append(member(quilt.get("id"), quilt.get("version")))
    except (UnicodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        raise EquivalenceError("loader metadata is malformed") from error
    if len({mod_id for mod_id, _ in members}) != len(members):
        raise EquivalenceError("loader metadata contains duplicate MOD IDs")
    return SemanticJarIdentity(tuple(sorted(members)), loader)


def context_digest(context: EquivalenceContext) -> str:
    payload = {"minecraft": context.minecraft, "loader": context.loader,
               "loader_version": context.loader_version,
               "source_snapshot_digest": context.source_snapshot_digest,
               "policy_version": context.policy_version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _winner(left: DependencyCandidate, right: DependencyCandidate) -> DependencyCandidate:
    if left.explicit_root and right.explicit_root:
        raise EquivalenceError("two explicit roots cannot be merged")
    def rank(candidate: DependencyCandidate) -> tuple[int, int, str]:
        root_rank = 0 if candidate.explicit_root and candidate.existing else 1 if candidate.explicit_root else 2 if candidate.existing else 3
        provider = _identity(candidate)[0]
        return root_rank, 0 if provider == "modrinth" else 1, _canonical_identity(candidate)
    return min((left, right), key=rank)


def verify_equivalence(
    left: DependencyCandidate,
    right: DependencyCandidate,
    context: EquivalenceContext,
    materialize: Callable[[DependencyCandidate, EquivalenceContext], MaterializedArtifact] | None = None,
) -> EquivalenceEvidence | None:
    """Verify a pair, returning digest-bound evidence or ``None`` on rejection."""
    try:
        left_provider, _ = _identity(left)
        right_provider, _ = _identity(right)
    except EquivalenceError:
        return None
    if left_provider == right_provider:
        return None
    winner = _winner(left, right)
    ordered = sorted((left, right), key=_canonical_identity)
    canonical_left, canonical_right = ordered
    left_meta = metadata_sha256(canonical_left)
    right_meta = metadata_sha256(canonical_right)
    try:
        left_declared = _optional_download_hash(canonical_left)
        right_declared = _optional_download_hash(canonical_right)
    except EquivalenceError:
        return None
    kind: EvidenceKind | None = None
    artifact_sha: str | None = None
    semantic: SemanticJarIdentity | None = None
    if left_declared and right_declared and left_declared == right_declared:
        kind, artifact_sha = "declared-sha256", left_declared
    elif materialize is not None:
        try:
            lm = materialize(canonical_left, context)
            rm = materialize(canonical_right, context)
            if not isinstance(lm, MaterializedArtifact) or not isinstance(rm, MaterializedArtifact):
                return None
            if lm.sha256 == rm.sha256:
                kind, artifact_sha = "exact-sha256", lm.sha256
            elif (lm.semantic_identity and rm.semantic_identity
                  and lm.semantic_identity == rm.semantic_identity
                  and lm.semantic_identity.target_loader == context.target_loader
                  and lm.sha256 != rm.sha256):
                kind, semantic = "jar-mod-identity", lm.semantic_identity
        except (EquivalenceError, OSError, ValueError):
            return None
    if kind is None:
        return None
    selected = _canonical_identity(winner)
    ctx_digest = context_digest(context)
    evidence_payload = json.dumps(
        {
            "artifact_sha256": artifact_sha,
            "semantic_identity": (
                {
                    "loader": semantic.target_loader,
                    "members": semantic.members,
                }
                if semantic is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    binding = hashlib.sha256(
        (left_meta + right_meta + ctx_digest + kind + selected + evidence_payload).encode()
    ).hexdigest()
    return EquivalenceEvidence(_canonical_identity(canonical_left), _canonical_identity(canonical_right), kind, selected,
                               artifact_sha, semantic, left_meta, right_meta, ctx_digest, binding)


def select_winner(left: DependencyCandidate, right: DependencyCandidate) -> DependencyCandidate:
    """Apply the documented winner policy, independently of equivalence proof."""
    _identity(left); _identity(right)
    return _winner(left, right)
