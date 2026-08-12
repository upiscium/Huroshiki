from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from dependency_equivalence import MaterializedArtifact, SemanticJarIdentity


class ExactModVersionSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.16.0"\n',
            encoding="utf-8",
        )
        (self.source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.root / "templates"),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.root / "templates"),
            patch.object(packctl, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                packctl,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(packctl, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(
                packctl,
                "TRASH_ROOT",
                self.root / ".huroshiki" / "trash",
            ),
            patch.object(
                core,
                "run_resolver_process",
                side_effect=self.run_fake_resolver,
            ),
            patch.object(
                core,
                "materialize_provider_artifact",
                side_effect=self.materialize,
            ),
        ]
        for item in self.patches:
            item.start()
        self.commands: list[tuple[str, ...]] = []
        self.key = core.project_key("pack", "demo")

    @staticmethod
    def selection(
        provider: str,
        project_id: str,
        artifact_id: str,
    ) -> core.ExactModArtifactSelection:
        if provider == "modrinth":
            project_id = core.canonical_modrinth_id(project_id)
            artifact_id = core.canonical_modrinth_id(artifact_id)
        return core.ExactModArtifactSelection(provider, project_id, artifact_id)

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def metadata(
        provider: str,
        project_id: str,
        artifact_id: str,
        *,
        side: str = "both",
        filename: str | None = None,
    ) -> str:
        filename = filename or f"{project_id}.jar"
        digest = "a" * 64
        if provider == "modrinth":
            update = (
                "[update.modrinth]\n"
                f'mod-id = "{project_id}"\n'
                f'version = "{artifact_id}"\n'
            )
        else:
            update = (
                "[update.curseforge]\n"
                f"project-id = {project_id}\n"
                f"file-id = {artifact_id}\n"
            )
        return (
            f'name = "{project_id}"\n'
            f'filename = "{filename}"\n'
            f'side = "{side}"\n'
            '[download]\n'
            'hash-format = "sha256"\n'
            f'hash = "{digest}"\n'
            f'url = "https://example.invalid/{project_id}.jar"\n'
            f"{update}"
        )

    def write_installed_mods(self, provider: str, project_id: str) -> None:
        dependency_provider = (
            "modrinth" if provider == "curseforge" else "curseforge"
        )
        dependency_project = "987654" if dependency_provider == "curseforge" else "dependency"
        (self.source / "mods" / "root.pw.toml").write_text(
            self.metadata(
                provider,
                project_id,
                "1" if provider == "curseforge" else "old-artifact",
                side="server",
            ),
            encoding="utf-8",
        )
        (self.source / "mods" / "dependency.pw.toml").write_text(
            self.metadata(
                dependency_provider,
                dependency_project,
                "dependency-artifact" if dependency_provider == "modrinth" else "987655",
                side="client",
                filename="dependency.jar",
            ),
            encoding="utf-8",
        )

    def run_fake_resolver(
        self,
        command: list[str],
        *,
        cwd: Path,
        cancel_event: threading.Event,
        deadline: float,
        result_callback=None,
    ) -> core.ResolverProcessResult:
        del cancel_event, deadline
        self.commands.append(tuple(command))
        if command == ["packwiz", "refresh"]:
            (cwd / "index.toml").write_text(
                'hash-format = "sha256"\nrefreshed = true\n',
                encoding="utf-8",
            )
        elif "--version-id" in command:
            project_id = command[command.index("--project-id") + 1]
            artifact_id = command[command.index("--version-id") + 1]
            (cwd / "mods" / "root.pw.toml").write_text(
                self.metadata("modrinth", project_id, artifact_id, side="both"),
                encoding="utf-8",
            )
            (cwd / "mods" / "dependency.pw.toml").write_text(
                self.metadata(
                    "curseforge",
                    "987654",
                    "987656",
                    side="client",
                    filename="dependency.jar",
                ),
                encoding="utf-8",
            )
        elif "--file-id" in command:
            project_id = command[command.index("--addon-id") + 1]
            artifact_id = command[command.index("--file-id") + 1]
            (cwd / "mods" / "root.pw.toml").write_text(
                self.metadata("curseforge", project_id, artifact_id, side="both"),
                encoding="utf-8",
            )
            (cwd / "mods" / "dependency.pw.toml").write_text(
                self.metadata(
                    "modrinth",
                    "dependency",
                    "dependency-new",
                    side="client",
                    filename="dependency.jar",
                ),
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"Unexpected Packwiz command: {command}")
        result = core.ResolverProcessResult(0, "", "", False, False)
        if result_callback is not None:
            result_callback(result)
        return result

    def materialize(self, candidate, *_args, **_kwargs):
        _provider, project_id = candidate.provider_identity.split(":", 1)
        return MaterializedArtifact(
            "b" * 64,
            SemanticJarIdentity(
                (("root" if project_id == "root" else project_id, "2.0"),),
                "fabric",
            ),
        )

    def make_transaction(self, provider: str, project_id: str) -> core.PackTransaction:
        self.write_installed_mods(provider, project_id)
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(
            self.source,
            (
                core.PackRootRecord(
                    provider,
                    project_id,
                    "server",
                ),
            ),
        )
        return core.PackTransaction.create(self.key)

    def resolved_metadata(
        self,
        project_id: str,
        artifact_id: str,
        *,
        side: str = "both",
        filename: str | None = None,
    ) -> core.ResolvedMetadata:
        relative = Path("mods") / f"{project_id}.pw.toml"
        filename = filename or f"{project_id}.jar"
        contents = self.metadata(
            "modrinth",
            project_id,
            artifact_id,
            side=side,
            filename=filename,
        ).encode()
        return core.ResolvedMetadata(
            ("modrinth", project_id),
            relative,
            filename,
            contents,
            "modrinth",
            project_id,
        )

    def resolved_dependency_metadata(
        self,
        project_id: str,
        artifact_id: str,
        *,
        side: str,
    ) -> core.ResolvedMetadata:
        return self.resolved_metadata(project_id, artifact_id, side=side)

    def write_graph_pack(
        self,
        roots: tuple[tuple[str, str, str], ...],
        dependencies: tuple[tuple[str, str], ...],
    ) -> core.PackTransaction:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        for project_id, artifact_id, side in roots:
            (self.source / "mods" / f"{project_id}.pw.toml").write_text(
                self.metadata("modrinth", project_id, artifact_id, side=side),
                encoding="utf-8",
            )
        for project_id, artifact_id in dependencies:
            (self.source / "mods" / f"{project_id}.pw.toml").write_text(
                self.metadata("modrinth", project_id, artifact_id),
                encoding="utf-8",
            )
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(
            self.source,
            tuple(
                core.PackRootRecord("modrinth", project_id, side)
                for project_id, _artifact_id, side in roots
            ),
        )
        return core.PackTransaction.create(self.key)

    def graph_closure(
        self,
        root_project: str,
        root_artifact: str,
        *dependencies: tuple[str, str],
    ) -> core.ResolvedModClosure:
        records = [self.resolved_metadata(root_project, root_artifact)]
        records.extend(
            self.resolved_metadata(project_id, artifact_id)
            for project_id, artifact_id in dependencies
        )
        return core.ResolvedModClosure(
            ("modrinth", root_project),
            tuple(records),
        )

    def graph_closure_with_dependency_side(
        self,
        root_project: str,
        root_artifact: str,
        dependency_side: str,
        dependency_artifact: str,
    ) -> core.ResolvedModClosure:
        return core.ResolvedModClosure(
            ("modrinth", root_project),
            (
                self.resolved_metadata(root_project, root_artifact),
                self.resolved_dependency_metadata(
                    "dependency", dependency_artifact, side=dependency_side
                ),
            ),
        )

    def test_selection_validation_and_commands(self) -> None:
        self.assertEqual(
            core.build_exact_artifact_command(
                self.selection("modrinth", "sodium", "v1")
            ),
            [
                "packwiz",
                "--yes",
                "modrinth",
                "add",
                "--project-id",
                "sodium",
                "--version-id",
                "v1",
            ],
        )
        self.assertEqual(
            core.build_exact_artifact_command(
                self.selection("curseforge", "123", "456")
            ),
            [
                "packwiz",
                "--yes",
                "curseforge",
                "add",
                "--addon-id",
                "123",
                "--file-id",
                "456",
            ],
        )
        for provider, project_id, artifact_id in (
            ("curseforge", "0", "1"),
            ("curseforge", "01", "1"),
            ("curseforge", "1", "01"),
            ("modrinth", " sodium", "v1"),
            ("modrinth", "sodium", "v 1"),
        ):
            with self.subTest(provider=provider, project_id=project_id):
                with self.assertRaises(core.HuroshikiError):
                    self.selection(provider, project_id, artifact_id)

        for project_id, artifact_id in (
            ("sodium-extra", "release-1"),
            ("https://modrinth.com/mod/sodium-extra", "release-1"),
            ("Sodium Extra", "release-1"),
            ("canonical-project", "https://modrinth.com/version/release-1"),
        ):
            with self.subTest(project_id=project_id, artifact_id=artifact_id):
                with self.assertRaises(core.HuroshikiError):
                    core.ExactModArtifactSelection("modrinth", project_id, artifact_id)

        with patch.object(core, "resolve_project_selector") as lookup:
            with self.assertRaises(core.HuroshikiError):
                core.ExactModArtifactSelection("modrinth", "sodium-extra", "release-1")
        lookup.assert_not_called()

        canonical_project = core.canonical_modrinth_id("sodium-extra")
        canonical_version = core.canonical_modrinth_id("release-1")
        selection = core.ExactModArtifactSelection(
            "modrinth", canonical_project, canonical_version
        )
        self.assertIs(type(selection.project_id), core.CanonicalModrinthId)
        self.assertEqual(
            core.build_exact_artifact_command(selection)[-4:],
            ["--project-id", "sodium-extra", "--version-id", "release-1"],
        )

    def test_invalid_exact_ids_are_rejected_before_any_resolver_process(self) -> None:
        invalid = (
            ("curseforge", "0", "1"),
            ("curseforge", "-1", "1"),
            ("curseforge", "01", "1"),
            ("curseforge", "1.0", "1"),
            ("curseforge", "", "1"),
            ("curseforge", " ", "1"),
            ("curseforge", "1\x00", "1"),
            ("curseforge", "123", "0"),
            ("curseforge", "123", "-1"),
            ("curseforge", "123", "01"),
            ("curseforge", "123", "1.0"),
            ("curseforge", "123", ""),
            ("curseforge", "123", " \t"),
            ("curseforge", "123", "1\x7f"),
        )
        with patch.object(core, "run_resolver_process") as resolver:
            for provider, project_id, artifact_id in invalid:
                with self.subTest(provider=provider, project_id=project_id):
                    with self.assertRaises(core.HuroshikiError):
                        core.ExactModArtifactSelection(
                            provider, project_id, artifact_id
                        )
            resolver.assert_not_called()

        invalid_modrinth = (
            "",
            " ",
            "\tcanonical",
            "canonical\n",
            "canonical\x00",
            "x" * 129,
            "https://modrinth.com/mod/canonical",
            "Display Name",
        )
        with patch.object(core, "run_resolver_process") as resolver:
            for value in invalid_modrinth:
                with self.subTest(value=value):
                    with self.assertRaises(core.HuroshikiError):
                        core.canonical_modrinth_id(value)
                    with self.assertRaises(core.HuroshikiError):
                        core.ExactModArtifactSelection("modrinth", value, "v1")
                    with self.assertRaises(core.HuroshikiError):
                        core.ExactModArtifactSelection("modrinth", "root", value)
            resolver.assert_not_called()

    def test_exact_process_failure_prioritizes_integrity_then_cancel_and_deadline(self) -> None:
        cases = (
            (
                core.ResolverProcessResult(
                    7, "", "resolver failed", True, True, True, True
                ),
                "termination was incomplete",
            ),
            (
                core.ResolverProcessResult(
                    7, "", "resolver failed", True, True, True, False
                ),
                "background processes",
            ),
            (
                core.ResolverProcessResult(
                    7, "", "resolver failed", True, True, False, False
                ),
                "was cancelled",
            ),
            (
                core.ResolverProcessResult(
                    7, "", "resolver failed", False, True, False, False
                ),
                "deadline exceeded",
            ),
            (
                core.ResolverProcessResult(
                    7, "", "resolver failed", False, False, False, False
                ),
                "failed",
            ),
        )
        for result, message in cases:
            with self.subTest(message=message):
                failure = core._exact_process_failure(result, label="Exact resolver")
                self.assertIsNotNone(failure)
                self.assertIn(message, failure or "")

    def test_unsupported_exact_provider_is_rejected_without_process_start(self) -> None:
        with patch.object(core, "run_resolver_process") as resolver:
            for provider in ("url", "unknown", "mr", "cf"):
                with self.subTest(provider=provider):
                    with self.assertRaises(core.HuroshikiError):
                        core.ExactModArtifactSelection(provider, "123", "456")
            resolver.assert_not_called()

    def test_missing_and_duplicate_target_identity_fail_before_exact_resolver(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(core, "resolve_exact_mod_closure") as resolver:
                with self.assertRaisesRegex(core.HuroshikiError, "not installed"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "missing", "v2")
                    )
                resolver.assert_not_called()
        finally:
            transaction.discard()

        duplicate_source = self.source / "mods/duplicate.pw.toml"
        duplicate_source.write_text(
            self.metadata("modrinth", "root", "old-artifact"), encoding="utf-8"
        )
        transaction = core.PackTransaction.create(self.key)
        try:
            with patch.object(core, "resolve_exact_mod_closure") as resolver:
                with self.assertRaisesRegex(core.HuroshikiError, "duplicate identity"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
                resolver.assert_not_called()
        finally:
            transaction.discard()

    def test_exact_root_removes_dependency_orphaned_by_resulting_closure(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closures = {
            ("modrinth", "root", "r2"): self.graph_closure("root", "r2"),
        }

        def resolve(selection, **_):
            return closures[(selection.provider, selection.project_id, selection.artifact_id)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "r2")
                )
            self.assertEqual(preview.removed_dependencies, 1)
            self.assertEqual(preview.removed_dependency_identities, ("modrinth:dependency",))
            self.assertFalse(
                transaction.source.joinpath("mods/dependency.pw.toml").exists()
            )
            self.assertTrue(self.source.joinpath("mods/dependency.pw.toml").exists())
        finally:
            transaction.discard()

    def test_exact_root_stages_new_dependency_and_reports_preview_identity(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (),
        )
        closure = self.graph_closure("root", "r2", ("dependency", "d1"))
        try:
            with patch.object(core, "resolve_exact_mod_closure", return_value=closure):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "r2")
                )
            self.assertEqual(preview.added_dependencies, 1)
            self.assertEqual(preview.added_dependency_identities, ("modrinth:dependency",))
            self.assertIn(
                'version = "d1"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
            self.assertTrue(any(change.relative_path == Path("mods/dependency.pw.toml") for change in preview.changes))
        finally:
            transaction.discard()

    def test_exact_root_changes_dependency_artifact_without_counting_add_or_remove(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root", "r2", ("dependency", "d2"))
        try:
            with patch.object(core, "resolve_exact_mod_closure", return_value=closure):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "r2")
                )
            self.assertEqual(preview.added_dependencies, 0)
            self.assertEqual(preview.removed_dependencies, 0)
            self.assertEqual(preview.added_dependency_identities, ())
            self.assertEqual(preview.removed_dependency_identities, ())
            self.assertIn(
                'version = "d2"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_selection_can_target_a_mod_staged_by_an_earlier_add(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (),
        )
        added_closure = self.graph_closure("introduced", "x1")
        try:
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="introduced",
                canonical_project_id="introduced",
                side="both",
            )
            with patch.object(core, "resolve_mod_closure", return_value=added_closure):
                result = add.run()
            self.assertTrue(result.success, result.message)
            before_exact = core.tree_digest_snapshot(transaction.source)
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                side_effect=core.HuroshikiError("exact resolver failed"),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "exact resolver failed"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "introduced", "x2")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before_exact)
            self.assertIn(
                'version = "x1"',
                transaction.source.joinpath("mods/introduced.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_closure_path_and_filename_collisions_fail_closed(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (),
        )
        root = self.resolved_metadata("root", "r2", filename="root.jar")
        cases = (
            (
                core.ResolvedMetadata(
                    ("modrinth", "dependency"),
                    root.relative_path,
                    "dependency.jar",
                    self.metadata(
                        "modrinth", "dependency", "d1", filename="dependency.jar"
                    ).encode(),
                    "modrinth",
                    "dependency",
                ),
                "collision",
            ),
            (
                self.resolved_metadata("dependency", "d1", filename="root.jar"),
                "collision",
            ),
        )
        before = core.tree_digest_snapshot(transaction.source)
        try:
            for dependency, message in cases:
                with self.subTest(message=message):
                    closure = core.ResolvedModClosure(
                        ("modrinth", "root"), (root, dependency)
                    )
                    with patch.object(
                        core, "resolve_exact_mod_closure", return_value=closure
                    ):
                        with self.assertRaisesRegex(core.HuroshikiError, message):
                            transaction.prepare_exact_mod_version(
                                self.selection("modrinth", "root", "r2")
                            )
                    self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
        finally:
            transaction.discard()

    def test_shared_dependency_remains_reachable_from_another_explicit_root(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"), ("root-b", "b1", "both")),
            (("dependency", "d1"),),
        )
        closures = {
            ("modrinth", "root-a", "a2"): self.graph_closure("root-a", "a2"),
            ("modrinth", "root-b", "b1"): self.graph_closure(
                "root-b", "b1", ("dependency", "d1")
            ),
        }

        def resolve(selection, **_):
            return closures[(selection.provider, selection.project_id, selection.artifact_id)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root-a", "a2")
                )
            self.assertEqual(preview.removed_dependencies, 0)
            self.assertEqual(preview.removed_dependency_identities, ())
            self.assertTrue(
                transaction.source.joinpath("mods/dependency.pw.toml").exists()
            )
        finally:
            transaction.discard()

    def test_exact_rebuild_preserves_mixed_side_explicit_roots(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "server"), ("root-b", "b1", "client")),
            (),
        )
        try:
            def resolve(selection, **_):
                if selection.identity == ("modrinth", "root-a"):
                    return self.graph_closure_with_dependency_side(
                        "root-a", "a2", "client", "root-b"
                    )
                return self.graph_closure("root-b", "b1")

            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root-a", "a2")
                )
            self.assertIn(
                'side = "server"',
                transaction.source.joinpath("mods/root-a.pw.toml").read_text(),
            )
            self.assertIn(
                'side = "client"',
                transaction.source.joinpath("mods/root-b.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_shared_dependency_disagreement_fails_closed_and_restores_source(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"), ("root-b", "b1", "both")),
            (("dependency", "d1"),),
        )
        before = core.tree_digest_snapshot(transaction.source)
        real_before = core.tree_digest_snapshot(self.source)
        closures = {
            ("modrinth", "root-a", "a2"): self.graph_closure(
                "root-a", "a2", ("dependency", "d2")
            ),
            ("modrinth", "root-b", "b1"): self.graph_closure(
                "root-b", "b1", ("dependency", "d1")
            ),
        }

        def resolve(selection, **_):
            return closures[(selection.provider, selection.project_id, selection.artifact_id)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                with self.assertRaisesRegex(core.HuroshikiError, "disagreement"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root-a", "a2")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
            self.assertEqual(core.tree_digest_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_dependency_exact_selection_rejects_artifact_outside_parent_result(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closures = {
            ("modrinth", "root", "r1"): self.graph_closure(
                "root", "r1", ("dependency", "d1")
            ),
            ("modrinth", "dependency", "d2"): self.graph_closure(
                "dependency", "d2"
            ),
        }

        def resolve(selection, **_):
            return closures[(selection.provider, selection.project_id, selection.artifact_id)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                with self.assertRaisesRegex(core.HuroshikiError, "expected"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "dependency", "d2")
                    )
            self.assertIn(
                'version = "d1"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_dependency_exact_selection_succeeds_when_all_owners_resolve_selected_artifact(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closures = {
            ("modrinth", "root", "r1"): self.graph_closure(
                "root", "r1", ("dependency", "d2")
            ),
            ("modrinth", "dependency", "d2"): self.graph_closure(
                "dependency", "d2"
            ),
        }

        def resolve(selection, **_):
            return closures[(selection.provider, selection.project_id, selection.artifact_id)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "dependency", "d2")
                )
            self.assertEqual(preview.old_artifact_id, "d1")
            self.assertEqual(preview.new_artifact_id, "d2")
            self.assertEqual(preview.removed_dependencies, 0)
        finally:
            transaction.discard()

    def test_exact_rebuild_preserves_existing_dependency_side_coverage(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "server"),),
            (("dependency", "d1"),),
        )
        transaction.source.joinpath("mods/dependency.pw.toml").write_text(
            self.metadata("modrinth", "dependency", "d1", side="server"),
            encoding="utf-8",
        )
        try:
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure_with_dependency_side(
                    "root", "r2", "client", "d2"
                ),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "r2")
                )
            self.assertIn(
                'side = "both"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_rebuild_preserves_existing_dependency_both_side(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "server"),),
            (("dependency", "d1"),),
        )
        transaction.source.joinpath("mods/dependency.pw.toml").write_text(
            self.metadata("modrinth", "dependency", "d1", side="both"),
            encoding="utf-8",
        )
        try:
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure(
                    "root", "r2", ("dependency", "d2")
                ),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "r2")
                )
            self.assertIn(
                'side = "both"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_rebuild_keeps_new_dependency_provider_side(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "server"),),
            (),
        )
        try:
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure_with_dependency_side(
                    "root", "r2", "client", "d2"
                ),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "r2")
                )
            self.assertIn(
                'side = "client"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_selection_rejects_missing_semantic_identity_and_rolls_back(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        before = core.tree_digest_snapshot(transaction.source)

        try:
            with patch.object(
                core,
                "materialize_provider_artifact",
                return_value=MaterializedArtifact("b" * 64),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "no resolved"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
            self.assertIn(
                'version = "old-artifact"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_selection_rejects_wrong_semantic_identity(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core,
                "materialize_provider_artifact",
                side_effect=lambda candidate, *_args, **_kwargs: MaterializedArtifact(
                    "b" * 64,
                    SemanticJarIdentity(
                        ((
                            "different-mod"
                            if 'version = "v2"' in candidate.contents.decode("utf-8")
                            else "root",
                            "2.0",
                        ),),
                        "fabric",
                    ),
                ),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "identity changed"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
        finally:
            transaction.discard()

    def test_exact_selection_accepts_same_semantic_mod_with_new_version(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core,
                "materialize_provider_artifact",
                return_value=MaterializedArtifact(
                    "b" * 64,
                    SemanticJarIdentity((("root", "2.0"),), "fabric"),
                ),
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "v2")
                )
            self.assertEqual(preview.new_artifact_id, "v2")
        finally:
            transaction.discard()

    def test_exact_selection_rejects_wrong_target_loader_semantics(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core,
                "materialize_provider_artifact",
                return_value=MaterializedArtifact(
                    "b" * 64,
                    SemanticJarIdentity(( ("root", "2.0"),), "neoforge"),
                ),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "incompatible loader"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
        finally:
            transaction.discard()

    def test_exact_selection_rejects_unverifiable_url_closure_member(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        url_metadata = (
            'name = "URL"\n'
            'filename = "url.jar"\n'
            'side = "both"\n'
            '[download]\n'
            'hash-format = "sha256"\n'
            'hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
            'url = "https://example.invalid/url.jar"\n'
            '[huroshiki]\n'
            'project-id = "url-mod"\n'
        )
        transaction.source.joinpath("mods/url.pw.toml").write_text(
            url_metadata,
            encoding="utf-8",
        )
        try:
            with self.assertRaisesRegex(core.HuroshikiError, "URL artifact"):
                with patch.object(
                    core,
                    "resolve_exact_mod_closure",
                    return_value=core.ResolvedModClosure(
                        ("modrinth", "root"),
                        (
                            self.resolved_metadata("root", "r2"),
                            core.ResolvedMetadata(
                                ("url", "url-mod"),
                                Path("mods/url.pw.toml"),
                                "url.jar",
                                url_metadata.encode(),
                                "url",
                                "url-mod",
                            ),
                        ),
                    ),
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "r2")
                    )
        finally:
            transaction.discard()

    def test_exact_dependency_selection_preserves_dependency_side(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "server"),),
            (("dependency", "d1"),),
        )
        transaction.source.joinpath("mods/dependency.pw.toml").write_text(
            self.metadata("modrinth", "dependency", "d1", side="client"),
            encoding="utf-8",
        )

        def resolve(selection, **_):
            if selection.identity == ("modrinth", "root"):
                return self.graph_closure_with_dependency_side(
                    "root", "r1", "both", "d2"
                )
            return self.graph_closure_with_dependency_side(
                "dependency", selection.artifact_id, "both", selection.artifact_id
            )

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "dependency", "d2")
                )
            self.assertIn(
                'side = "client"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
            self.assertIn(
                'side = "server"',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_missing_root_manifest_fails_closed_without_promoting_dependency(self) -> None:
        self.write_installed_mods("modrinth", "root")
        (self.source / ".packwizignore").write_text("\n", encoding="utf-8")
        transaction = core.PackTransaction.create(self.key)
        try:
            with self.assertRaisesRegex(core.HuroshikiError, "authoritative root provenance"):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "v2")
                )
        finally:
            transaction.discard()

    def test_failed_exact_selection_preserves_earlier_staged_change(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        staged = transaction.source / "mods/staged.pw.toml"
        staged.write_text(
            self.metadata("modrinth", "staged", "s1"), encoding="utf-8"
        )
        before = core.tree_digest_snapshot(transaction.source)

        def fail_resolve(*_args, **_kwargs):
            raise core.HuroshikiError("incompatible explicit-root dependency")

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=fail_resolve):
                with self.assertRaisesRegex(core.HuroshikiError, "incompatible"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "r2")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
            self.assertTrue(staged.exists())
            self.assertFalse(self.source.joinpath("mods/staged.pw.toml").exists())
        finally:
            transaction.discard()

    def test_exact_modrinth_selection_previews_applies_and_unions_dependency_side(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        transaction.source.joinpath("mods/dependency.pw.toml").write_text(
            self.metadata(
                "curseforge",
                "987654",
                "987655",
                side="server",
                filename="dependency.jar",
            ),
            encoding="utf-8",
        )
        manifest_before = (self.source / ".huroshiki-roots.json").read_bytes()
        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=self.materialize
            ) as materialize:
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "v2")
                )
            self.assertEqual(preview.identity, "modrinth:root")
            self.assertEqual(preview.old_artifact_id, "old-artifact")
            self.assertEqual(preview.new_artifact_id, "v2")
            self.assertEqual(preview.added_dependencies, 0)
            self.assertEqual(preview.removed_dependencies, 0)
            self.assertGreater(materialize.call_count, 0)
            self.assertEqual(
                self.commands,
                [
                    (
                        "packwiz",
                        "--yes",
                        "modrinth",
                        "add",
                        "--project-id",
                        "root",
                        "--version-id",
                        "v2",
                    ),
                    ("packwiz", "refresh"),
                    ("packwiz", "refresh"),
                ],
            )
            self.assertIn(
                'side = "both"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
            self.assertIn(
                'side = "server"',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertIn(
                'version = "old-artifact"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            transaction.apply()
            self.assertIn(
                'version = "v2"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertIn(
                'side = "both"',
                self.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
            self.assertIn(
                'side = "server"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertEqual(
                core.read_pack_root_manifest(self.source),
                (core.PackRootRecord("modrinth", "root", "server"),),
            )
            self.assertEqual(
                (self.source / ".huroshiki-roots.json").read_bytes(),
                manifest_before,
            )
        finally:
            transaction.discard()

    def test_exact_curseforge_artifact_mismatch_restores_transaction_source(self) -> None:
        transaction = self.make_transaction("curseforge", "123")
        original = core.tree_digest_snapshot(transaction.source)
        self.commands.clear()

        def wrong_resolver(*args, **kwargs):
            result = self.run_fake_resolver(*args, **kwargs)
            if "--file-id" in args[0]:
                path = kwargs["cwd"] / "mods/root.pw.toml"
                path.write_text(
                    self.metadata("curseforge", "123", "999"),
                    encoding="utf-8",
                )
            return result

        try:
            with patch.object(core, "run_resolver_process", side_effect=wrong_resolver):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "produced 0 roots|expected"
                ):
                    transaction.prepare_exact_mod_version(
                        core.ExactModArtifactSelection("curseforge", "123", "456")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), original)
            self.assertIn(
                'file-id = 1',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_modrinth_project_mismatch_fails_closed(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        before = core.tree_digest_snapshot(transaction.source)

        def wrong_resolver(command, *, cwd, **kwargs):
            result = self.run_fake_resolver(command, cwd=cwd, **kwargs)
            if "--version-id" in command:
                (cwd / "mods/root.pw.toml").write_text(
                    self.metadata("modrinth", "different-project", "v2"),
                    encoding="utf-8",
                )
            return result

        try:
            with patch.object(core, "run_resolver_process", side_effect=wrong_resolver):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "produced 0 roots|expected"
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
        finally:
            transaction.discard()

    def test_exact_modrinth_version_mismatch_fails_closed(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        before = core.tree_digest_snapshot(transaction.source)

        def wrong_resolver(command, *, cwd, **kwargs):
            result = self.run_fake_resolver(command, cwd=cwd, **kwargs)
            if "--version-id" in command:
                (cwd / "mods/root.pw.toml").write_text(
                    self.metadata("modrinth", "root", "different-version"),
                    encoding="utf-8",
                )
            return result

        try:
            with patch.object(core, "run_resolver_process", side_effect=wrong_resolver):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "produced 0 roots|expected"
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
        finally:
            transaction.discard()

    def test_exact_curseforge_selection_previews_and_applies(self) -> None:
        transaction = self.make_transaction("curseforge", "123")
        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=self.materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    core.ExactModArtifactSelection("curseforge", "123", "456")
                )
            self.assertEqual(preview.identity, "curseforge:123")
            self.assertEqual(preview.old_artifact_id, "1")
            self.assertEqual(preview.new_artifact_id, "456")
            transaction.apply()
            self.assertIn(
                "file-id = 456",
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertEqual(
                self.commands[0],
                (
                    "packwiz",
                    "--yes",
                    "curseforge",
                    "add",
                    "--addon-id",
                    "123",
                    "--file-id",
                    "456",
                ),
            )
        finally:
            transaction.discard()

    def test_staged_exact_selection_cannot_be_changed_and_stale_apply_rolls_back(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=self.materialize
            ):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "v2")
                )
            with self.assertRaisesRegex(core.HuroshikiError, "apply or discard"):
                transaction.set_side(Path("mods/root.pw.toml"), True, True)
            transaction.source.joinpath("mods/root.pw.toml").write_text(
                self.metadata("modrinth", "root", "tampered"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                transaction.apply()
            self.assertIn(
                'version = "old-artifact"',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertIn(
                'version = "old-artifact"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_cancellation_before_checkpoint_does_not_modify_transaction(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        original = core.tree_digest_snapshot(transaction.source)
        cancel_event = threading.Event()
        cancel_event.set()
        try:
            with self.assertRaises(core.ExactModVersionCancelled):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", "root", "v2"),
                    cancel_event=cancel_event,
                )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), original)
        finally:
            transaction.discard()

    def test_discard_owns_and_cancels_running_exact_selection(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        started = threading.Event()
        worker_error: list[BaseException] = []

        def blocking_resolver(
            command: list[str],
            *,
            cwd: Path,
            cancel_event: threading.Event,
            deadline: float,
            result_callback=None,
        ) -> core.ResolverProcessResult:
            del cwd, deadline
            self.commands.append(tuple(command))
            if "--version-id" in command:
                started.set()
                while not cancel_event.is_set():
                    time.sleep(0.005)
                return core.ResolverProcessResult(-15, "", "", True, False)
            raise AssertionError(command)

        def prepare() -> None:
            try:
                with patch.object(
                    core,
                    "run_resolver_process",
                    side_effect=blocking_resolver,
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", "root", "v2")
                    )
            except BaseException as error:
                worker_error.append(error)

        worker = threading.Thread(target=prepare, name="exact-selection-test-worker")
        worker.start()
        self.assertTrue(started.wait(1))
        transaction.discard()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker_error), 1)
        self.assertIsInstance(worker_error[0], core.HuroshikiError)
        self.assertIn(
            'version = "old-artifact"',
            transaction.source.joinpath("mods/root.pw.toml").read_text(),
        )
        self.assertFalse(packctl.project_lock_is_active(self.key))


if __name__ == "__main__":
    unittest.main()
