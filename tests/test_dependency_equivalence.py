from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from dependency_equivalence import (
    DependencyCandidate,
    EquivalenceContext,
    EquivalenceError,
    LoaderDependencyRequirement,
    MaterializedArtifact,
    SemanticJarIdentity,
    declared_download_hash,
    parse_loader_dependency_requirements,
    parse_semantic_jar,
    select_winner,
    version_satisfies_requirement,
    verify_equivalence,
)


CTX = EquivalenceContext("1.21.1", "neoforge", "21.1.1", "source-digest", "110-v1")


def candidate(
    identity: str,
    metadata: str = "name = 'x'",
    provenance: str = "dependency",
    existing: bool = False,
) -> DependencyCandidate:
    return DependencyCandidate(
        identity,
        "mods/x.pw.toml",
        "x.jar",
        metadata.encode(),
        "both",
        provenance=provenance,
        existing=existing,
    )


class DependencyEquivalenceTest(unittest.TestCase):
    def test_declared_hash_is_strict_and_produces_bound_evidence(self) -> None:
        digest = "a" * 64
        metadata = f'[download]\nhash-format = "sha256"\nhash = "{digest}"\nurl = "https://example.invalid/x.jar"\n'
        evidence = verify_equivalence(candidate("modrinth:abc", metadata), candidate("curseforge:123", metadata), CTX)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual("declared-sha256", evidence.kind)
        self.assertEqual(digest, evidence.artifact_sha256)
        self.assertEqual(64, len(evidence.binding_digest))

    def test_provider_and_hash_rejections(self) -> None:
        self.assertIsNone(verify_equivalence(candidate("url:x"), candidate("curseforge:1"), CTX))
        bad = '[download]\nhash-format = "md5"\nhash = "' + "a" * 64 + '"\nurl = "https://example.invalid/x.jar"'
        self.assertIsNone(verify_equivalence(candidate("modrinth:x", bad), candidate("curseforge:1", bad), CTX))
        with self.assertRaises(EquivalenceError):
            declared_download_hash(candidate("modrinth:x"))

    def test_materialized_exact_and_semantic_paths(self) -> None:
        left, right = candidate("modrinth:x"), candidate("curseforge:1")
        sha_left = hashlib.sha256(b"left").hexdigest()
        sha_right = hashlib.sha256(b"right").hexdigest()
        same = lambda c, ctx: MaterializedArtifact(sha_left)
        self.assertEqual("exact-sha256", verify_equivalence(left, right, CTX, same).kind)  # type: ignore[union-attr]
        identity = SemanticJarIdentity((("mod", "1.0"),), CTX.target_loader)
        results = {"modrinth:x": MaterializedArtifact(sha_left, identity), "curseforge:1": MaterializedArtifact(sha_right, identity)}
        evidence = verify_equivalence(left, right, CTX, lambda c, ctx: results[c.provider_identity])
        self.assertEqual("jar-mod-identity", evidence.kind)  # type: ignore[union-attr]
        wrong = SemanticJarIdentity((("mod", "1.0"),), "fabric")
        self.assertIsNone(verify_equivalence(left, right, CTX, lambda c, ctx: MaterializedArtifact(sha_left if c is left else sha_right, wrong)))

    def test_winner_policy_uses_provenance_and_canonical_identity(self) -> None:
        explicit = candidate("curseforge:2", provenance="explicit", existing=True)
        dependency = candidate("modrinth:z", provenance="dependency")
        self.assertIs(select_winner(explicit, dependency), explicit)
        self.assertIs(select_winner(dependency, explicit), explicit)

        modrinth = candidate("modrinth:z", provenance="dependency", existing=True)
        curseforge = candidate("curseforge:1", provenance="dependency")
        self.assertIs(select_winner(modrinth, curseforge), modrinth)
        self.assertIs(select_winner(curseforge, modrinth), modrinth)
        existing_curseforge = candidate(
            "curseforge:2", provenance="dependency", existing=True
        )
        incoming_modrinth = candidate("modrinth:a", provenance="dependency")
        self.assertIs(
            select_winner(existing_curseforge, incoming_modrinth),
            existing_curseforge,
        )
        first = candidate("modrinth:a", provenance="dependency", existing=True)
        second = candidate("modrinth:z", provenance="dependency")
        self.assertIs(select_winner(second, first), first)

        unknown = candidate("curseforge:2", provenance="unknown", existing=True)
        self.assertIs(select_winner(unknown, dependency), unknown)

    def test_rejected_provenance_pairs_fail_before_materialization(self) -> None:
        rejected = (("explicit", "explicit"), ("unknown", "explicit"),
                    ("explicit", "unknown"), ("unknown", "unknown"))
        for left_role, right_role in rejected:
            with self.subTest(left_role=left_role, right_role=right_role):
                left = candidate("modrinth:x", provenance=left_role)
                right = candidate("curseforge:1", provenance=right_role)
                with self.assertRaises(EquivalenceError):
                    select_winner(left, right)
                materialize = Mock()
                with self.assertRaises(EquivalenceError):
                    verify_equivalence(left, right, CTX, materialize)
                materialize.assert_not_called()

    def test_binding_canonically_includes_candidate_policy_inputs(self) -> None:
        digest = "b" * 64
        metadata = (
            '[download]\nhash-format = "sha256"\n'
            f'hash = "{digest}"\nurl = "https://example.invalid/x.jar"\n'
        )
        unknown = candidate(
            "modrinth:x", metadata, provenance="unknown", existing=True
        )
        explicit = candidate(
            "modrinth:x", metadata, provenance="explicit", existing=True
        )
        dependency = candidate("curseforge:1", metadata, provenance="dependency")

        unknown_evidence = verify_equivalence(unknown, dependency, CTX)
        explicit_evidence = verify_equivalence(explicit, dependency, CTX)
        swapped_evidence = verify_equivalence(dependency, unknown, CTX)
        changed_existing_evidence = verify_equivalence(
            candidate(
                "modrinth:x", metadata, provenance="dependency", existing=True
            ),
            dependency,
            CTX,
        )
        nonexisting_evidence = verify_equivalence(
            candidate("modrinth:x", metadata, provenance="dependency"),
            dependency,
            CTX,
        )
        canonical_left_unknown = verify_equivalence(
            candidate(
                "curseforge:1", metadata, provenance="unknown", existing=True
            ),
            candidate("modrinth:x", metadata, provenance="dependency"),
            CTX,
        )
        canonical_left_explicit = verify_equivalence(
            candidate(
                "curseforge:1", metadata, provenance="explicit", existing=True
            ),
            candidate("modrinth:x", metadata, provenance="dependency"),
            CTX,
        )
        canonical_left_existing = verify_equivalence(
            candidate(
                "curseforge:1", metadata, provenance="dependency", existing=True
            ),
            candidate(
                "modrinth:x", metadata, provenance="dependency", existing=True
            ),
            CTX,
        )
        canonical_left_nonexisting = verify_equivalence(
            candidate("curseforge:1", metadata, provenance="dependency"),
            candidate(
                "modrinth:x", metadata, provenance="dependency", existing=True
            ),
            CTX,
        )

        assert unknown_evidence is not None
        assert explicit_evidence is not None
        assert swapped_evidence is not None
        assert changed_existing_evidence is not None
        assert nonexisting_evidence is not None
        assert canonical_left_unknown is not None
        assert canonical_left_explicit is not None
        assert canonical_left_existing is not None
        assert canonical_left_nonexisting is not None
        self.assertNotEqual(
            unknown_evidence.binding_digest, explicit_evidence.binding_digest
        )
        self.assertEqual(
            unknown_evidence.binding_digest, swapped_evidence.binding_digest
        )
        self.assertNotEqual(
            changed_existing_evidence.binding_digest,
            nonexisting_evidence.binding_digest,
        )
        self.assertNotEqual(
            canonical_left_unknown.binding_digest,
            canonical_left_explicit.binding_digest,
        )
        self.assertNotEqual(
            canonical_left_existing.binding_digest,
            canonical_left_nonexisting.binding_digest,
        )

    def test_invalid_provenance_is_rejected_at_construction(self) -> None:
        with self.assertRaises(EquivalenceError):
            candidate("modrinth:x", provenance="not-a-role")

    def test_semantic_identity_is_sorted_and_complete(self) -> None:
        with self.assertRaises(EquivalenceError):
            SemanticJarIdentity((("z", "1"), ("a", "1")), CTX.target_loader)
        with self.assertRaises(EquivalenceError):
            SemanticJarIdentity((("a", "latest"),), CTX.target_loader)

    def test_loader_dependency_requirements_are_parsed_from_fabric_and_neoforge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fabric = root / "fabric.jar"
            with zipfile.ZipFile(fabric, "w") as jar:
                jar.writestr(
                    "fabric.mod.json",
                    json.dumps(
                        {
                            "id": "owner",
                            "version": "1.0",
                            "depends": {"dependency": ">=2.0 <3.0"},
                        }
                    ),
                )
            neoforge = root / "neoforge.jar"
            with zipfile.ZipFile(neoforge, "w") as jar:
                jar.writestr(
                    "META-INF/neoforge.mods.toml",
                    'modLoader="javafml"\nloaderVersion="[4,)"\n'
                    '[[mods]]\nmodId="owner"\nversion="1.0"\n'
                    '[[dependencies.owner]]\nmodId="dependency"\n'
                    'mandatory=true\nversionRange="[2.0,3.0)"\n',
                )

            self.assertEqual(
                parse_loader_dependency_requirements(fabric, "fabric"),
                (LoaderDependencyRequirement("dependency", ">=2.0 <3.0"),),
            )
            self.assertEqual(
                parse_loader_dependency_requirements(neoforge, "neoforge"),
                (LoaderDependencyRequirement("dependency", "[2.0,3.0)"),),
            )

    def test_quilt_semantic_and_required_dependency_requirements_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "quilt.jar"
            with zipfile.ZipFile(artifact, "w") as jar:
                jar.writestr(
                    "quilt.mod.json",
                    json.dumps(
                        {
                            "quilt_loader": {
                                "id": "owner",
                                "version": "1.0",
                                "depends": [
                                    {"id": "dependency", "versions": ">=2"},
                                    {"id": "minecraft", "versions": ">=1.21"},
                                    {"id": "quilt_loader", "versions": ">=0.26"},
                                    {
                                        "id": "optional-mod",
                                        "versions": ">=1",
                                        "optional": True,
                                    },
                                ],
                            }
                        }
                    ),
                )

            self.assertEqual(
                parse_semantic_jar(artifact, "quilt"),
                SemanticJarIdentity((("owner", "1.0"),), "quilt"),
            )
            self.assertEqual(
                parse_loader_dependency_requirements(artifact, "quilt"),
                (
                    LoaderDependencyRequirement("dependency", ">=2"),
                    LoaderDependencyRequirement("minecraft", ">=1.21"),
                    LoaderDependencyRequirement("quilt_loader", ">=0.26"),
                ),
            )

    def test_quilt_ambiguous_dependency_constraint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "quilt.jar"
            with zipfile.ZipFile(artifact, "w") as jar:
                jar.writestr("quilt.mod.json", json.dumps({"quilt_loader": {"id": "owner", "version": "1", "depends": [{"id": "dependency", "versions": [">=2"]}]}}))
            self.assertIsNone(parse_loader_dependency_requirements(artifact, "quilt"))

    def test_dependency_version_ranges_fail_closed_when_ambiguous(self) -> None:
        for requirement in (">=2.0 <3.0", "[2.0,3.0)"):
            with self.subTest(requirement=requirement):
                self.assertTrue(version_satisfies_requirement("2.5", requirement))
                self.assertFalse(version_satisfies_requirement("3.0", requirement))
        self.assertIsNone(version_satisfies_requirement("2.0", "^2.0"))
        self.assertIsNone(version_satisfies_requirement("2.0-beta", ">=2.0"))


if __name__ == "__main__":
    unittest.main()
