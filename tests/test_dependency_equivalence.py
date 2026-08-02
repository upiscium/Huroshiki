from __future__ import annotations

import hashlib
import unittest

from dependency_equivalence import (
    DependencyCandidate,
    EquivalenceContext,
    EquivalenceError,
    MaterializedArtifact,
    SemanticJarIdentity,
    declared_download_hash,
    select_winner,
    verify_equivalence,
)


CTX = EquivalenceContext("1.21.1", "neoforge", "21.1.1", "source-digest", "110-v1")


def candidate(identity: str, metadata: str = "name = 'x'", **flags: bool) -> DependencyCandidate:
    return DependencyCandidate(identity, "mods/x.pw.toml", "x.jar", metadata.encode(), "both", **flags)


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

    def test_winner_policy_and_explicit_root_conflict(self) -> None:
        existing = candidate("curseforge:2", existing=True)
        incoming = candidate("modrinth:z")
        self.assertIs(select_winner(existing, incoming), existing)
        existing_root = candidate("curseforge:2", existing=True, explicit_root=True)
        incoming_root = candidate("modrinth:z", explicit_root=True)
        with self.assertRaises(EquivalenceError):
            select_winner(existing_root, incoming_root)

    def test_semantic_identity_is_sorted_and_complete(self) -> None:
        with self.assertRaises(EquivalenceError):
            SemanticJarIdentity((("z", "1"), ("a", "1")), CTX.target_loader)
        with self.assertRaises(EquivalenceError):
            SemanticJarIdentity((("a", "latest"),), CTX.target_loader)


if __name__ == "__main__":
    unittest.main()
