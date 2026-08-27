"""Executable orchestration coverage for profile version intent.

The profile path is deliberately exercised through a real PackTransaction.  The
provider and Packwiz boundaries are faked, but metadata, provenance, intent,
materialisation, refresh, and publication are not.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import threading
import tomllib
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from mod_version_overrides import ModVersionOverride, read_mod_version_overrides
from pack_migration_roots import PackRootRecord, read_pack_root_manifest
from dependency_equivalence import (
    LoaderDependencyRequirement,
    MaterializedArtifact,
    SemanticJarIdentity,
)
from tests import test_mod_version_selection as exact_selection


class ProfileVersionIntentOrchestrationTest(unittest.TestCase):
    """Small, deterministic profile fixtures modelled on the exact-selection suite."""

    def setUp(self) -> None:
        self.fixture = exact_selection.ExactModVersionSelectionTest()
        self.fixture.setUp()
        self.source = self.fixture.source
        self.key = self.fixture.key

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def profiles(self, *entries: dict[str, object]) -> dict[str, object]:
        return {"base": list(entries)}

    def snapshot(self) -> dict[Path, bytes | str]:
        result: dict[Path, bytes | str] = {}
        for path in sorted(self.source.rglob("*")):
            relative = path.relative_to(self.source)
            if path.is_symlink():
                result[relative] = f"symlink:{path.readlink()}"
            elif path.is_file():
                result[relative] = path.read_bytes()
        return result

    def run(self, profiles, **kwargs):
        # Keep the compact fixture call-site while retaining TestCase's runner API.
        if hasattr(profiles, "startTest"):
            return super().run(profiles)
        return core.apply_profiles(self.key, profiles, ["base"], **kwargs)

    def seed(self, *roots: tuple[str, str, str]) -> None:
        for path in (self.source / "mods").glob("*.pw.toml"):
            path.unlink()
        records = []
        for index, (provider, project, side) in enumerate(roots):
            if provider == "curseforge":
                artifact = "7"
            else:
                artifact = "VersB001" if project == "ProjB002" else "VersR001"
            filename = "root.pw.toml" if index == 0 else "root-b.pw.toml"
            (self.source / "mods" / filename).write_text(
                self.metadata(provider, project, artifact, side=side), encoding="utf-8"
            )
            records.append(PackRootRecord(provider, project, side))
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        core.write_pack_root_manifest(self.source, tuple(records))

    def seed_dependency(self, provider: str, project: str, artifact: str) -> None:
        (self.source / "mods" / f"dependency-{project}.pw.toml").write_text(
            self.metadata(
                provider,
                project,
                artifact,
                filename=(
                    "dependency-b.jar" if project == "987655" else "dependency.jar"
                ),
            ),
            encoding="utf-8",
        )

    def exact_closure_factory(self, *, shared: bool = False, two_dependencies: bool = False):
        """Return complete, path-distinct closures for profile resolver calls."""
        def resolve(selection, **_kwargs):
            project = str(selection.project_id)
            root_path = "mods/root.pw.toml" if project == "ProjA001" else "mods/root-b.pw.toml"
            records = [core.ResolvedMetadata(
                selection.identity, Path(root_path), "root.jar",
                self.metadata(selection.provider, project, str(selection.artifact_id)).encode(),
                selection.provider, project,
            )]
            if project == "ProjA001" or shared:
                records.append(core.ResolvedMetadata(
                    ("curseforge", "987654"), Path("mods/dependency.pw.toml"),
                    "dependency.jar", self.metadata(
                        "curseforge", "987654", "987656", filename="dependency.jar"
                    ).encode(),
                    "curseforge", "987654",
                ))
                if two_dependencies:
                    records.append(core.ResolvedMetadata(
                        ("curseforge", "987655"), Path("mods/dependency-b.pw.toml"),
                        "dependency-b.jar", self.metadata(
                            "curseforge", "987655", "987657", filename="dependency-b.jar"
                        ).encode(),
                        "curseforge", "987655",
                    ))
            return core.ResolvedModClosure(selection.identity, tuple(records))
        return resolve

    def coherent_materialize(self, candidate, *args, **kwargs):
        """Extend the shared fixture's semantic evidence for the second edge."""
        if candidate.filename == "dependency-b.jar":
            return MaterializedArtifact(
                "c" * 64,
                SemanticJarIdentity((("dependency-b", "2.0"),), "fabric"),
                (),
            )
        return self.fixture.materialize(candidate, *args, **kwargs)

    def two_dependency_materialize(self, candidate, *args, **kwargs):
        result = self.coherent_materialize(candidate, *args, **kwargs)
        if result.dependency_requirements:
            return MaterializedArtifact(
                result.sha256,
                result.semantic_identity,
                (
                    LoaderDependencyRequirement("dependency", ">=2.0"),
                    LoaderDependencyRequirement("dependency-b", ">=2.0"),
                ),
            )
        return result

    def override(self, provider: str, project: str, artifact: str, *, locked=False, reason=None) -> None:
        core.write_mod_version_overrides(
            self.source,
            (ModVersionOverride(provider, project, artifact, locked, reason),),
        )

    def metadata(
        self,
        provider: str,
        project: str,
        artifact: str,
        *,
        side="both",
        filename: str | None = None,
    ) -> str:
        return self.fixture.metadata(
            provider, project, artifact, side=side, filename=filename
        )

    def test_legacy_automatic_profile_without_manifest_uses_normal_path(self):
        closure = core.ResolvedModClosure(("curseforge", "101"), (core.ResolvedMetadata(
            ("curseforge", "101"), Path("mods/101.pw.toml"), "101.jar",
            self.metadata("curseforge", "101", "7").encode(), "curseforge", "101"),))
        with patch.object(core, "resolve_mod_closure", return_value=closure):
            self.run(self.profiles({"source": "curseforge", "project": 101, "side": "client"}))
        self.assertIn('side = "client"', (self.source / "mods/101.pw.toml").read_text())
        self.assertFalse((self.source / ".huroshiki-version-overrides.json").exists())

    def test_exact_curseforge_root_publishes_selected_artifact(self):
        self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "client"}))
        text = (self.source / "mods/root.pw.toml").read_text()
        self.assertIn("file-id = 7", text)
        self.assertEqual(read_pack_root_manifest(self.source)[0].provider, "curseforge")

    def test_exact_modrinth_root_publishes_selected_artifact(self):
        self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR001", "side": "server"}))
        self.assertIn('version = "VersR001"', (self.source / "mods/root.pw.toml").read_text())

    def test_selected_artifact_is_present_in_staged_and_final_metadata(self):
        self.seed(("modrinth", "ProjA001", "both"))
        before = (self.source / "mods/root.pw.toml").read_bytes()
        self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))
        self.assertNotEqual(before, (self.source / "mods/root.pw.toml").read_bytes())
        self.assertIn('version = "VersR002"', (self.source / "mods/root.pw.toml").read_text())

    def test_exact_selection_persists_unlocked_intent(self):
        self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))
        item = read_mod_version_overrides(self.source)[0]
        self.assertEqual((item.artifact_id, item.locked, item.reason), ("7", False, None))

    def test_exact_profile_preserves_unrelated_transaction_source_state(self):
        pack_contents = (
            'name = "Custom Pack"\n'
            'author = "Pack Author"\n'
            'version = "9.4.2"\n'
            '[versions]\n'
            'minecraft = "1.21.1"\n'
            'fabric = "0.16.0"\n'
        )
        self.source.joinpath("pack.toml").write_text(
            pack_contents, encoding="utf-8"
        )
        unrelated_file = self.source / "notes" / "keep.bin"
        unrelated_file.parent.mkdir()
        unrelated_file.write_bytes(b"unrelated\x00source\n")
        unrelated_metadata = self.source / "resourcepacks" / "unrelated.pw.toml"
        unrelated_metadata.parent.mkdir()
        unrelated_metadata.write_text(
            self.metadata(
                "curseforge", "999", "4", filename="unrelated.jar"
            ),
            encoding="utf-8",
        )
        custom_ignore = (
            "/custom-cache/\n"
            "/.huroshiki-roots.json\n"
            "/.huroshiki-version-overrides.json\n"
        )
        self.source.joinpath(".packwizignore").write_text(
            custom_ignore, encoding="utf-8"
        )
        core.write_pack_root_manifest(self.source, ())
        metadata_before = unrelated_metadata.read_bytes()
        observed_sources: list[Path] = []
        original_apply = core.PackTransaction.apply

        def apply(transaction, **kwargs):
            observed_sources.append(transaction.source)
            return original_apply(transaction, **kwargs)

        def refresh_with_custom_ignore(command, *, cwd, **kwargs):
            result = self.fixture.run_fake_resolver(
                command, cwd=cwd, **kwargs
            )
            if command == ["packwiz", "refresh"] and cwd.name == "source":
                path = cwd / ".packwizignore"
                text = path.read_text(encoding="utf-8")
                path.write_text(
                    text + "/refresh-generated/\n", encoding="utf-8"
                )
            return result

        with patch.object(
            core.PackTransaction, "apply", new=apply
        ), patch.object(
            core,
            "run_resolver_process",
            side_effect=refresh_with_custom_ignore,
        ):
            self.run(
                self.profiles(
                    {
                        "source": "curseforge",
                        "project": 101,
                        "artifact_id": "7",
                        "side": "both",
                    }
                )
            )

        self.assertEqual(self.source.joinpath("pack.toml").read_text(), pack_contents)
        self.assertEqual(unrelated_file.read_bytes(), b"unrelated\x00source\n")
        self.assertEqual(unrelated_metadata.read_bytes(), metadata_before)
        ignore_after = self.source.joinpath(".packwizignore").read_text()
        self.assertIn("/custom-cache/", ignore_after.splitlines())
        self.assertIn("/.huroshiki-roots.json", ignore_after.splitlines())
        self.assertIn(
            "/.huroshiki-version-overrides.json", ignore_after.splitlines()
        )
        self.assertIn("/refresh-generated/", ignore_after.splitlines())
        self.assertTrue(observed_sources)
        self.assertEqual(observed_sources[0].name, "source")
        self.assertNotIn("profile-aggregate", str(observed_sources[0]))
        parsed_pack = tomllib.loads(self.source.joinpath("pack.toml").read_text())
        self.assertEqual(parsed_pack["name"], "Custom Pack")
        self.assertEqual(parsed_pack["author"], "Pack Author")
        self.assertEqual(parsed_pack["version"], "9.4.2")
        self.assertEqual(parsed_pack["versions"]["minecraft"], "1.21.1")
        self.assertEqual(parsed_pack["versions"]["fabric"], "0.16.0")
        self.assertTrue(
            tomllib.loads(self.source.joinpath("index.toml").read_text())[
                "refreshed"
            ]
        )

    def assert_baseline_root_authority_fails_before_resolver(self, message: str):
        profile = self.profiles(
            {
                "source": "curseforge",
                "project": 101,
                "artifact_id": "7",
                "side": "both",
            }
        )
        with patch.object(core, "resolve_exact_mod_closure") as exact, patch.object(
            core, "resolve_mod_closure"
        ) as automatic, self.assertRaisesRegex(core.HuroshikiError, message):
            self.run(profile)
        exact.assert_not_called()
        automatic.assert_not_called()

    def test_missing_declared_root_fails_before_resolver(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.source.joinpath("mods/root.pw.toml").unlink()
        self.assert_baseline_root_authority_fails_before_resolver(
            "baseline root Authority.*Root metadata is missing"
        )

    def test_duplicate_declared_root_metadata_fails_before_resolver(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.source.joinpath("mods/duplicate.pw.toml").write_bytes(
            self.source.joinpath("mods/root.pw.toml").read_bytes()
        )
        self.assert_baseline_root_authority_fails_before_resolver(
            "duplicate identity|Duplicate metadata identity"
        )

    def test_declared_root_identity_disagreement_fails_before_resolver(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.source.joinpath("mods/root.pw.toml").write_text(
            self.metadata("modrinth", "ProjB002", "VersB001"),
            encoding="utf-8",
        )
        self.assert_baseline_root_authority_fails_before_resolver(
            "baseline root Authority.*Root metadata is missing"
        )

    def test_exact_root_change_removes_only_affected_dependency_metadata(self):
        self.seed(("modrinth", "ProjA001", "both"))
        dependency = self.source / "mods" / "dependency.pw.toml"
        dependency.write_text(
            self.metadata(
                "curseforge",
                "987654",
                "987656",
                filename="dependency.jar",
            ),
            encoding="utf-8",
        )
        unrelated = self.source / "notes" / "unrelated.txt"
        unrelated.parent.mkdir()
        unrelated.write_text("preserve me\n", encoding="utf-8")

        def closure(selection, **_kwargs):
            root = core.ResolvedMetadata(
                selection.identity,
                Path("mods/root.pw.toml"),
                "root.jar",
                self.metadata(
                    "modrinth",
                    "ProjA001",
                    str(selection.artifact_id),
                ).encode(),
                "modrinth",
                "ProjA001",
            )
            entries = [root]
            if str(selection.artifact_id) == "VersR001":
                entries.append(
                    core.ResolvedMetadata(
                        ("curseforge", "987654"),
                        Path("mods/dependency.pw.toml"),
                        "dependency.jar",
                        dependency.read_bytes(),
                        "curseforge",
                        "987654",
                    )
                )
            return core.ResolvedModClosure(selection.identity, tuple(entries))

        def materialize_without_dependency(candidate, *args, **kwargs):
            result = self.fixture.materialize(candidate, *args, **kwargs)
            if candidate.provider_identity == "modrinth:ProjA001":
                return MaterializedArtifact(
                    result.sha256,
                    result.semantic_identity,
                    (),
                )
            return result

        with patch.object(
            core, "resolve_exact_mod_closure", side_effect=closure
        ), patch.object(
            core,
            "materialize_provider_artifact",
            side_effect=materialize_without_dependency,
        ):
            self.run(
                self.profiles(
                    {
                        "source": "modrinth",
                        "project": "ProjA001",
                        "artifact_id": "VersR002",
                        "side": "both",
                    }
                )
            )

        self.assertFalse(dependency.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve me\n")
        self.assertIn(
            'version = "VersR002"',
            self.source.joinpath("mods/root.pw.toml").read_text(),
        )

    def test_resolver_artifact_mismatch_rolls_back_source_and_intent(self):
        original = self.snapshot()
        with patch.object(core, "resolve_exact_mod_closure", side_effect=core.HuroshikiError("artifact mismatch")):
            with self.assertRaises(core.HuroshikiError):
                self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))
        self.assertEqual(self.snapshot(), original)

    def test_runtime_incompatibility_from_verifier_rolls_back(self):
        original = self.snapshot()
        with patch.object(core, "materialize_provider_artifact", side_effect=core.HuroshikiError("Minecraft runtime incompatible")):
            with self.assertRaisesRegex(core.HuroshikiError, "incompatible"):
                self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))
        self.assertEqual(self.snapshot(), original)

    def test_source_and_override_manifest_roll_back_together(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.override("modrinth", "ProjA001", "VersR001")
        original = self.snapshot()
        with patch.object(core, "resolve_exact_mod_closure", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))
        self.assertEqual(self.snapshot(), original)

    def test_dependency_entry_is_not_promoted_to_root(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.seed_dependency("curseforge", "987654", "987656")
        with patch.object(core, "resolve_exact_mod_closure", side_effect=self.exact_closure_factory()):
            self.run(self.profiles({"source": "curseforge", "project": 987654, "artifact_id": "987656", "scope": "dependency"}))
        roots = read_pack_root_manifest(self.source)
        self.assertEqual([(r.provider, r.project_id) for r in roots], [("modrinth", "ProjA001")])

    def test_owner_scoped_preseed_rebuilds_only_owning_root(self):
        self.seed(("modrinth", "ProjA001", "both"), ("modrinth", "ProjB002", "both"))
        self.seed_dependency("curseforge", "987654", "987656")
        self.override("curseforge", "987654", "987656")
        calls = []
        def record(*args, **kwargs):
            calls.append(tuple(kwargs.get("preseed_selections", ())))
            return self.exact_closure_factory()(*args, **kwargs)
        with patch.object(core, "resolve_exact_mod_closure", side_effect=record), patch.object(core, "materialize_provider_artifact", side_effect=self.coherent_materialize):
            self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))
        constrained = [items for items in calls if items]
        self.assertEqual(len(constrained), 1)
        self.assertEqual(constrained[0][0].identity_label, "curseforge:987654")

    def test_shared_dependency_is_preseeded_for_each_owner(self):
        self.seed(("modrinth", "ProjA001", "both"), ("modrinth", "ProjB002", "both"))
        self.seed_dependency("curseforge", "987654", "987656")
        self.override("curseforge", "987654", "987656")
        with patch.object(core, "resolve_exact_mod_closure", side_effect=self.exact_closure_factory(shared=True)) as resolver, patch.object(core, "materialize_provider_artifact", side_effect=self.coherent_materialize):
            self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))
        constrained = [c.kwargs["preseed_selections"] for c in resolver.call_args_list if c.kwargs.get("preseed_selections")]
        self.assertEqual(len(constrained), 2)
        self.assertTrue(all(items[0].identity_label == "curseforge:987654" for items in constrained))

    def test_new_and_existing_owner_are_retained_in_root_manifest(self):
        self.seed(("modrinth", "ProjA001", "client"))
        with patch.object(core, "resolve_exact_mod_closure", side_effect=self.exact_closure_factory()), patch.object(core, "materialize_provider_artifact", side_effect=self.coherent_materialize):
            self.run(self.profiles({"source": "modrinth", "project": "ProjB002", "artifact_id": "VersB001", "side": "server"}))
        self.assertEqual({r.project_id for r in read_pack_root_manifest(self.source)}, {"ProjA001", "ProjB002"})

    def test_owner_transfer_keeps_dependency_non_root(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.seed_dependency("curseforge", "987654", "987656")
        with patch.object(core, "resolve_exact_mod_closure", side_effect=self.exact_closure_factory()), patch.object(core, "materialize_provider_artifact", side_effect=self.coherent_materialize):
            self.run(self.profiles({"source": "curseforge", "project": 987654, "artifact_id": "987656", "scope": "dependency"}))
        self.assertEqual([(r.provider, r.project_id) for r in read_pack_root_manifest(self.source)], [("modrinth", "ProjA001")])
        self.assertEqual(read_mod_version_overrides(self.source)[0].canonical_identity, "curseforge:987654")

    def test_orphaned_intent_fails_closed(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.override("modrinth", "Unowned1", "VersD001")
        with self.assertRaisesRegex(core.HuroshikiError, "identity is missing or ambiguous"):
            self.run(self.profiles())

    def test_stale_override_is_rejected_before_resolver(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.override("modrinth", "Depen001", "VersD001")
        with patch.object(core, "resolve_exact_mod_closure") as resolver:
            with self.assertRaisesRegex(core.HuroshikiError, "identity is missing or ambiguous"):
                self.run(self.profiles())
        resolver.assert_not_called()

    def test_same_artifact_constraints_compose(self):
        self.run(self.profiles(
            {"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"},
            {"source": "curseforge", "project": 101, "artifact_id": "7", "side": "server"},
        ))
        self.assertEqual(len(read_mod_version_overrides(self.source)), 1)

    def test_different_artifacts_fail_before_resolver(self):
        with patch.object(core, "resolve_exact_mod_closure") as resolver:
            with self.assertRaisesRegex(core.HuroshikiError, "constraint conflict"):
                self.run(self.profiles(
                    {"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"},
                    {"source": "curseforge", "project": 101, "artifact_id": "8", "side": "both"},
                ))
        resolver.assert_not_called()

    def test_late_exact_conflict_precedes_automatic_provider_lookup(self):
        profiles = self.profiles(
            {"source": "modrinth", "project": "create", "side": "both"},
            {
                "source": "curseforge",
                "project": 101,
                "artifact_id": "7",
                "side": "both",
            },
            {
                "source": "curseforge",
                "project": 101,
                "artifact_id": "8",
                "side": "both",
            },
        )
        with patch.object(core, "resolve_project_selector") as lookup:
            with self.assertRaisesRegex(core.HuroshikiError, "constraint conflict"):
                self.run(profiles)
        lookup.assert_not_called()

    def test_selected_order_produces_equivalent_version_result(self):
        entries = {
            "client": [
                {
                    "source": "curseforge",
                    "project": 101,
                    "artifact_id": "7",
                    "side": "client",
                }
            ],
            "server": [
                {
                    "source": "curseforge",
                    "project": 101,
                    "artifact_id": "7",
                    "side": "server",
                }
            ],
        }
        core.apply_profiles(self.key, entries, ["client", "server"])
        first = (
            (self.source / "mods/root.pw.toml").read_bytes(),
            read_pack_root_manifest(self.source),
            read_mod_version_overrides(self.source),
        )

        self.fixture.tearDown()
        self.fixture = exact_selection.ExactModVersionSelectionTest()
        self.fixture.setUp()
        self.source = self.fixture.source
        self.key = self.fixture.key
        core.apply_profiles(self.key, entries, ["server", "client"])
        second = (
            (self.source / "mods/root.pw.toml").read_bytes(),
            read_pack_root_manifest(self.source),
            read_mod_version_overrides(self.source),
        )
        self.assertEqual(first, second)

    def test_root_dependency_role_conflict_is_rejected(self):
        with self.assertRaisesRegex(core.HuroshikiError, "root/dependency"):
            self.run(self.profiles(
                {"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"},
                {"source": "curseforge", "project": 101, "artifact_id": "7", "scope": "dependency"},
            ))

    def test_overlapping_compatible_dependencies_are_passed_as_one_set(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.seed_dependency("curseforge", "987654", "987656")
        self.seed_dependency("curseforge", "987655", "987657")
        self.override("curseforge", "987654", "987656")
        core.write_mod_version_overrides(self.source, (
            ModVersionOverride("curseforge", "987654", "987656"),
            ModVersionOverride("curseforge", "987655", "987657"),
        ))
        with patch.object(core, "resolve_exact_mod_closure", side_effect=self.exact_closure_factory(two_dependencies=True)) as resolver, patch.object(core, "materialize_provider_artifact", side_effect=self.two_dependency_materialize):
            self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))
        constrained = [c.kwargs["preseed_selections"] for c in resolver.call_args_list if c.kwargs.get("preseed_selections")]
        self.assertEqual(len(constrained), 1)
        self.assertEqual({item.identity_label for item in constrained[0]}, {"curseforge:987654", "curseforge:987655"})

    def test_incompatible_dependency_range_fails_closed(self):
        with patch.object(core, "materialize_provider_artifact", side_effect=core.HuroshikiError("incompatible dependency range")):
            with self.assertRaisesRegex(core.HuroshikiError, "incompatible"):
                self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))

    def test_locked_intent_preserves_reason(self):
        self.seed(("curseforge", "101", "both"))
        self.override("curseforge", "101", "7", locked=True, reason="release pin")
        self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))
        item = read_mod_version_overrides(self.source)[0]
        self.assertEqual((item.locked, item.reason), (True, "release pin"))

    def test_conflicting_locked_intent_is_typed_and_has_no_synthetic_reason(self):
        self.seed(("curseforge", "101", "both"))
        self.override("curseforge", "101", "7", locked=True)
        with self.assertRaises(core.ProfileVersionIntentError) as caught:
            self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "8", "side": "both"}))
        self.assertIsNone(caught.exception.user_pin_reason)

    def test_unrelated_override_is_preserved(self):
        self.seed(("modrinth", "ProjA001", "both"))
        self.seed_dependency("curseforge", "987654", "987656")
        self.override("curseforge", "987654", "987656")
        with patch.object(core, "resolve_exact_mod_closure", side_effect=self.exact_closure_factory()), patch.object(core, "materialize_provider_artifact", side_effect=self.coherent_materialize):
            self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR002", "side": "both"}))
        self.assertEqual({x.canonical_identity for x in read_mod_version_overrides(self.source)}, {"curseforge:987654", "modrinth:ProjA001"})

    def test_stale_source_drift_is_detected_before_publication(self):
        def drift(*args, **kwargs):
            self.source.joinpath("pack.toml").write_text("drift", encoding="utf-8")
            return self.fixture.run_fake_resolver(*args, **kwargs)
        with patch.object(core, "run_resolver_process", side_effect=drift):
            with self.assertRaises(core.HuroshikiError):
                self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))

    def test_unlocked_replacement_updates_intent(self):
        self.seed(("curseforge", "101", "both"))
        self.override("curseforge", "101", "7")
        self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "8", "side": "both"}))
        self.assertEqual(read_mod_version_overrides(self.source)[0].artifact_id, "8")

    def test_automatic_entry_preserves_existing_exact_intent(self):
        self.seed(("curseforge", "101", "both"))
        self.override("curseforge", "101", "7")
        with patch.object(core, "resolve_mod_closure", return_value=core.ResolvedModClosure(("curseforge", "101"), ())):
            self.run(self.profiles({"source": "curseforge", "project": 101, "side": "both"}))
        self.assertEqual(read_mod_version_overrides(self.source)[0].artifact_id, "7")

    def test_stored_intent_transaction_copy_honors_shared_cancellation(self):
        self.seed(("curseforge", "101", "both"))
        self.override("curseforge", "101", "7")
        event = threading.Event()
        original_copy = core.copy_transaction_source

        def cancel_copy(source, destination, **kwargs):
            event.set()
            return original_copy(source, destination, **kwargs)

        with patch.object(core, "copy_transaction_source", side_effect=cancel_copy):
            with self.assertRaises(core.ProfileCancelled):
                self.run(self.profiles(), cancel_event=event)

    def test_cancellation_during_automatic_leaves_source_unchanged(self):
        event = threading.Event(); event.set()
        before = self.snapshot()
        with self.assertRaises(core.ProfileCancelled):
            self.run(self.profiles({"source": "curseforge", "project": 101, "side": "both"}), cancel_event=event)
        self.assertEqual(self.snapshot(), before)

    def test_deadline_during_constrained_operation_leaves_source_unchanged(self):
        before = self.snapshot()
        with self.assertRaises(core.ProfileDeadlineExceeded):
            self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}), deadline=0)
        self.assertEqual(self.snapshot(), before)

    def test_materialization_cancellation_is_rolled_back(self):
        event = threading.Event()
        def cancel(*args, **kwargs):
            event.set()
            raise core.UpdatePreparationCancelled("cancelled")
        before = self.snapshot()
        with patch.object(core, "materialize_provider_artifact", side_effect=cancel):
            with self.assertRaises(core.ProfileCancelled):
                self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}), cancel_event=event)
        self.assertEqual(self.snapshot(), before)

    def test_exact_resolver_cancellation_is_profile_authority(self):
        event = threading.Event()

        def cancel(*_args, **_kwargs):
            event.set()
            raise core.HuroshikiError("resolver stopped")

        with patch.object(core, "resolve_exact_mod_closure", side_effect=cancel):
            with self.assertRaises(core.ProfileCancelled):
                self.run(
                    self.profiles(
                        {
                            "source": "curseforge",
                            "project": 101,
                            "artifact_id": "7",
                            "side": "both",
                        }
                    ),
                    cancel_event=event,
                )

    def test_exact_resolver_deadline_is_profile_authority(self):
        with patch.object(
            core,
            "resolve_exact_mod_closure",
            side_effect=core.UpdatePreparationDeadlineExceeded("resolver deadline"),
        ):
            with self.assertRaises(core.ProfileDeadlineExceeded):
                self.run(
                    self.profiles(
                        {
                            "source": "curseforge",
                            "project": 101,
                            "artifact_id": "7",
                            "side": "both",
                        }
                    )
                )

    def test_refresh_failure_rolls_back_metadata_and_intent(self):
        before = self.snapshot()
        failure = core.ResolverProcessResult(9, "", "refresh failed", False, False)
        with patch.object(core, "run_resolver_process", return_value=failure):
            with self.assertRaises(core.HuroshikiError):
                self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))
        self.assertEqual(self.snapshot(), before)

    def test_refresh_cannot_remove_required_ignore_authority(self):
        before = self.snapshot()
        for removed_line in (
            "/.huroshiki-roots.json",
            "/.huroshiki-version-overrides.json",
        ):
            with self.subTest(removed_line=removed_line):
                def mutate_ignore(command, *, cwd, **kwargs):
                    result = self.fixture.run_fake_resolver(
                        command, cwd=cwd, **kwargs
                    )
                    if command == ["packwiz", "refresh"] and cwd.name == "source":
                        path = cwd / ".packwizignore"
                        lines = [
                            line
                            for line in path.read_text(
                                encoding="utf-8"
                            ).splitlines()
                            if line != removed_line
                        ]
                        path.write_text(
                            "\n".join(lines) + "\n", encoding="utf-8"
                        )
                    return result

                with patch.object(
                    core, "run_resolver_process", side_effect=mutate_ignore
                ):
                    with self.assertRaisesRegex(
                        core.HuroshikiError, "canonically excluded"
                    ):
                        self.run(
                            self.profiles(
                                {
                                    "source": "curseforge",
                                    "project": 101,
                                    "artifact_id": "7",
                                    "side": "both",
                                }
                            )
                        )
                self.assertEqual(self.snapshot(), before)

    def test_profile_metadata_delta_never_follows_parent_symlink(self):
        original = self.source / "mods" / "original.pw.toml"
        original.write_bytes(b"before")
        real_mods = self.source / "mods-real"
        self.source.joinpath("mods").rename(real_mods)
        external = self.fixture.root / "external"
        external.mkdir()
        external_target = external / "original.pw.toml"
        external_target.write_bytes(b"outside")
        self.source.joinpath("mods").symlink_to(external, target_is_directory=True)
        source_metadata = self.source.stat(follow_symlinks=False)

        with self.assertRaises(OSError):
            core._apply_profile_metadata_change(
                self.source,
                core.UpdateChange(
                    Path("mods/original.pw.toml"), b"before", b"after"
                ),
                expected_root_identity=(
                    source_metadata.st_dev,
                    source_metadata.st_ino,
                ),
                checkpoint=lambda: None,
            )
        self.assertEqual(external_target.read_bytes(), b"outside")

    def test_external_source_mutation_is_not_overwritten(self):
        def mutate(command, *, cwd, **kwargs):
            if command == ["packwiz", "refresh"]:
                self.source.joinpath("index.toml").write_bytes(b"external")
            return self.fixture.run_fake_resolver(command, cwd=cwd, **kwargs)
        with patch.object(core, "run_resolver_process", side_effect=mutate):
            with self.assertRaises(core.HuroshikiError):
                self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "both"}))

    def test_root_manifest_side_is_preserved_and_callback_is_canonical(self):
        events = []
        self.run(self.profiles({"source": "curseforge", "project": 101, "artifact_id": "7", "side": "server"}), on_entry=lambda *x: events.append(x))
        self.assertEqual(read_pack_root_manifest(self.source)[0].side, "server")
        self.assertEqual(events[0][0], "base")

    def test_on_exact_reports_canonical_identity_artifact_and_role(self):
        events = []
        self.run(self.profiles({"source": "modrinth", "project": "ProjA001", "artifact_id": "VersR001", "side": "both"}), on_exact=lambda *x: events.append(x))
        self.assertEqual(events[0], ("modrinth:ProjA001", "VersR001", "root"))

    def test_cli_reports_exact_identity_artifact_and_role(self):
        output = io.StringIO()

        def apply(_key, _profiles, _names, **callbacks):
            callbacks["on_exact"]("curseforge:987654", "987656", "dependency")

        with patch.object(packctl, "load_profiles", return_value={"base": []}), patch.object(
            core, "apply_profiles", side_effect=apply
        ), redirect_stdout(output):
            self.assertEqual(
                packctl.cmd_profile(argparse.Namespace(pack="demo", names=["base"])),
                0,
            )
        self.assertIn(
            "exact dependency: curseforge:987654 -> artifact 987656",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
