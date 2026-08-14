from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from mod_version_overrides import ModVersionOverride, read_mod_version_overrides, set_mod_version_override
from dependency_equivalence import (
    LoaderDependencyRequirement,
    MaterializedArtifact,
    SemanticJarIdentity,
)


MR_PROJECT_IDS = {
    "root": "ProjA001",
    "root-a": "ProjA002",
    "root-b": "ProjB002",
    "root-c": "ProjC002",
    "dependency": "Depen001",
    "intermediate-b": "InterB01",
    "intermediate-e": "InterE01",
    "child": "Child001",
    "replacement": "Repla001",
    "introduced": "Intro001",
    "missing": "Miss0001",
    "staged": "Stage001",
    "different-project": "DiffP001",
    "sodium": "Sodium01",
}
MR_VERSION_IDS = {
    "r1": "VersR001",
    "r2": "VersR002",
    "a1": "VersA001",
    "a2": "VersA002",
    "b1": "VersB001",
    "c1": "VersC001",
    "d1": "VersD001",
    "d2": "VersD002",
    "e1": "VersE001",
    "e2": "VersE002",
    "x1": "VersX001",
    "x2": "VersX002",
    "v1": "VersV001",
    "v2": "VersV002",
    "s1": "StageV01",
    "old-artifact": "OldVer01",
    "different-version": "DiffV001",
    "tampered": "Tamper01",
    "dependency-artifact": "DepArt01",
    "dependency-new": "DepNew01",
}


def mr_project(value: str) -> str:
    return MR_PROJECT_IDS.get(value, value)


def mr_version(value: str) -> str:
    return MR_VERSION_IDS.get(value, value)


def mr_project_key(value: str) -> str:
    return next((key for key, item in MR_PROJECT_IDS.items() if item == value), value)


def mr_version_key(value: str) -> str:
    return next((key for key, item in MR_VERSION_IDS.items() if item == value), value)


def branded_project(key: str) -> core.CanonicalModrinthId:
    return core.canonical_modrinth_id(MR_PROJECT_IDS[key])


def selection_fixture_key(selection) -> tuple[str, str, str]:
    return (
        selection.provider,
        mr_project_key(selection.project_id),
        mr_version_key(selection.artifact_id),
    )


def branded_version(key: str) -> core.CanonicalModrinthId:
    return core.canonical_modrinth_id(MR_VERSION_IDS[key])


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
        project_id: str | core.CanonicalModrinthId,
        artifact_id: str | core.CanonicalModrinthId,
    ) -> core.ExactModArtifactSelection:
        if provider == "modrinth":
            if (
                type(project_id) is not core.CanonicalModrinthId
                or type(artifact_id) is not core.CanonicalModrinthId
            ):
                raise AssertionError("Modrinth test selections require branded opaque IDs")
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
            project_id = mr_project(project_id)
            artifact_id = mr_version(artifact_id)
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
        document = candidate.contents.decode("utf-8")
        project_id = mr_project_key(project_id)
        artifact_id = core.parse_provider_metadata(
            Path(candidate.relative_metadata_path), candidate.contents
        ).file_id
        artifact_id = mr_version_key(artifact_id or "")
        version = (
            "1.0"
            if artifact_id in {"1", "d1", "old-artifact", "987655"}
            else "2.0"
        )
        requirements = ()
        if project_id in {"root", "root-a", "root-b"} and (
            "dependency" in document or project_id == "root"
        ):
            requirements = (
                LoaderDependencyRequirement("dependency", ">=2.0"),
            )
        return MaterializedArtifact(
            "b" * 64,
            SemanticJarIdentity(
                ((
                    "dependency"
                    if candidate.filename == "dependency.jar"
                    else "root" if project_id == "root" else project_id,
                    version,
                ),),
                "fabric",
            ),
            requirements,
        )

    def make_transaction(self, provider: str, project_id: str) -> core.PackTransaction:
        self.write_installed_mods(provider, project_id)
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        core.write_pack_root_manifest(
            self.source,
            (
                core.PackRootRecord(
                    provider,
                    mr_project(project_id) if provider == "modrinth" else project_id,
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
            ("modrinth", mr_project(project_id)),
            relative,
            filename,
            contents,
            "modrinth",
            mr_project(project_id),
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
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        core.write_pack_root_manifest(
            self.source,
            tuple(
                core.PackRootRecord("modrinth", mr_project(project_id), side)
                for project_id, _artifact_id, side in roots
            ),
        )
        return core.PackTransaction.create(self.key)

    def write_pack_with_url_root(self) -> tuple[core.PackTransaction, Path, bytes]:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        self.source.joinpath("mods/root.pw.toml").write_text(
            self.metadata("modrinth", "root", "r1"), encoding="utf-8"
        )
        url_relative = Path("mods/url-mod.pw.toml")
        url_contents = (
            'name = "URL Root"\n'
            'filename = "url-root.jar"\n'
            'side = "client"\n'
            '[download]\n'
            'hash-format = "sha256"\n'
            f'hash = "{"a" * 64}"\n'
            'url = "https://example.invalid/url-root.jar"\n'
            '[huroshiki]\n'
            'project-id = "url-mod"\n'
        ).encode()
        self.source.joinpath(url_relative).write_bytes(url_contents)
        self.source.joinpath(".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(
            self.source,
            (
                core.PackRootRecord("modrinth", mr_project("root"), "both"),
                core.PackRootRecord("url", "url-mod", "client"),
            ),
        )
        return core.PackTransaction.create(self.key), url_relative, url_contents

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
            ("modrinth", mr_project(root_project)),
            tuple(records),
        )

    def dependency_graph_materializer(
        self,
        requirements: dict[str, tuple[tuple[str, str], ...] | None],
        versions: dict[str, str],
        members: dict[str, tuple[tuple[str, str], ...]] | None = None,
    ):
        members = members or {}

        def materialize(candidate, *_args, **_kwargs):
            _provider, project_id = candidate.provider_identity.split(":", 1)
            project_id = mr_project_key(project_id)
            artifact_id = core.parse_provider_metadata(
                Path(candidate.relative_metadata_path), candidate.contents
            ).file_id
            assert artifact_id is not None
            artifact_id = mr_version_key(artifact_id)
            semantic_members = members.get(
                artifact_id,
                ((project_id, versions.get(artifact_id, "1.0")),),
            )
            raw_requirements = requirements.get(project_id, ())
            loader_requirements = (
                None
                if raw_requirements is None
                else tuple(
                    LoaderDependencyRequirement(mod_id, version_range)
                    for mod_id, version_range in raw_requirements
                )
            )
            return MaterializedArtifact(
                "b" * 64,
                SemanticJarIdentity(tuple(sorted(semantic_members)), "fabric"),
                loader_requirements,
            )

        return materialize

    def graph_closure_with_dependency_side(
        self,
        root_project: str,
        root_artifact: str,
        dependency_side: str,
        dependency_artifact: str,
    ) -> core.ResolvedModClosure:
        return core.ResolvedModClosure(
            ("modrinth", mr_project(root_project)),
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
                self.selection("modrinth", branded_project("sodium"), branded_version("v1"))
            ),
            [
                "packwiz",
                "--yes",
                "modrinth",
                "add",
                "--project-id",
                MR_PROJECT_IDS["sodium"],
                "--version-id",
                MR_VERSION_IDS["v1"],
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

        canonical_project = core.canonical_modrinth_id("A1b2C3d4")
        canonical_version = core.canonical_modrinth_id("E5f6G7h8")
        selection = core.ExactModArtifactSelection(
            "modrinth", canonical_project, canonical_version
        )
        self.assertIs(type(selection.project_id), core.CanonicalModrinthId)
        self.assertEqual(
            core.build_exact_artifact_command(selection)[-4:],
            ["--project-id", "A1b2C3d4", "--version-id", "E5f6G7h8"],
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
            "sodium",
            "sodium-extra",
            "release-1",
            "1.20.1",
            "Abc1234",
            "Abcd12345",
            "Abcd-123",
            "Abcd_123",
            "Abcd.123",
            "Abcd 123",
            "Abcd123\n",
            "Abcd12\x00",
            "https://modrinth.com/mod/sodium",
        )
        with patch.object(core, "run_resolver_process") as resolver:
            for value in invalid_modrinth:
                with self.subTest(value=value):
                    with self.assertRaises(core.HuroshikiError):
                        core.canonical_modrinth_id(value)
                    with self.assertRaises(core.HuroshikiError):
                        core.ExactModArtifactSelection(
                            "modrinth", value, branded_version("v1")
                        )
                    with self.assertRaises(core.HuroshikiError):
                        core.ExactModArtifactSelection(
                            "modrinth", branded_project("root"), value
                        )
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
                        self.selection("modrinth", branded_project("missing"), branded_version("v2"))
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
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
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
            return closures[selection_fixture_key(selection)]

        try:
            materialize = self.dependency_graph_materializer({"root": ()}, {})
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
                )
            self.assertEqual(preview.removed_dependencies, 1)
            self.assertEqual(preview.removed_dependency_identities, (f"modrinth:{mr_project("dependency")}",))
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
            materialize = self.dependency_graph_materializer({"root": (("dependency", ">=1"),)}, {})
            with patch.object(core, "resolve_exact_mod_closure", return_value=closure), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
                )
            self.assertEqual(preview.added_dependencies, 1)
            self.assertEqual(preview.added_dependency_identities, (f"modrinth:{mr_project("dependency")}",))
            self.assertIn(
                f'version = "{mr_version("d1")}"',
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
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
                )
            self.assertEqual(preview.added_dependencies, 0)
            self.assertEqual(preview.removed_dependencies, 0)
            self.assertEqual(preview.added_dependency_identities, ())
            self.assertEqual(preview.removed_dependency_identities, ())
            self.assertIn(
                f'version = "{mr_version("d2")}"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_resolved_add_then_exact_selection_applies_selected_artifact(self) -> None:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        self.source.joinpath(".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(self.source, ())
        real_before = core._file_content_snapshot(self.source)
        transaction = core.PackTransaction.create(self.key)
        try:
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="introduced",
                canonical_project_id=mr_project("introduced"),
                side="client",
            )
            with patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.graph_closure("introduced", "x1"),
            ):
                result = add.run()
            self.assertTrue(result.success, result.message)
            self.assertIn(
                f'version = "{mr_version("x1")}"',
                transaction.source.joinpath("mods/introduced.pw.toml").read_text(),
            )
            self.assertEqual(
                core.read_pack_root_manifest(transaction.source),
                (
                    core.PackRootRecord(
                        "modrinth", mr_project("introduced"), "client"
                    ),
                ),
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)

            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure("introduced", "x2"),
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("introduced"),
                        branded_version("x2"),
                    )
                )
            self.assertEqual(preview.new_artifact_id, mr_version("x2"))
            self.assertIn(
                f'version = "{mr_version("x2")}"',
                transaction.source.joinpath("mods/introduced.pw.toml").read_text(),
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)

            transaction.apply()
            self.assertIn(
                f'version = "{mr_version("x2")}"',
                self.source.joinpath("mods/introduced.pw.toml").read_text(),
            )
            self.assertEqual(
                core.read_pack_root_manifest(self.source),
                (
                    core.PackRootRecord(
                        "modrinth", mr_project("introduced"), "client"
                    ),
                ),
            )
        finally:
            transaction.discard()

    def test_empty_pack_add_initializes_root_provenance_for_staged_selection(self) -> None:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        self.source.joinpath(".huroshiki-roots.json").unlink(missing_ok=True)
        self.source.joinpath(".packwizignore").write_text("", encoding="utf-8")
        transaction = core.PackTransaction.create(self.key)
        try:
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="introduced",
                canonical_project_id=mr_project("introduced"),
                side="client",
            )
            with patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.graph_closure("introduced", "x1"),
            ):
                result = add.run()
            self.assertTrue(result.success, result.message)
            self.assertEqual(
                core.read_pack_root_manifest(transaction.source),
                (
                    core.PackRootRecord(
                        "modrinth", mr_project("introduced"), "client"
                    ),
                ),
            )
            targets = transaction.staged_exact_mod_targets()
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].role, "root")
            self.assertEqual(targets[0].mod.project_id, mr_project("introduced"))
        finally:
            transaction.discard()

    def test_staged_targets_include_new_and_unchanged_shared_dependencies(self) -> None:
        transaction = self.write_graph_pack(
            (("existing", "e1", "both"),),
            (("shared", "d1"),),
        )
        closure = self.graph_closure(
            "introduced",
            "x1",
            ("shared", "d1"),
            ("new-dependency", "n1"),
        )
        try:
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="introduced",
                canonical_project_id=mr_project("introduced"),
                side="both",
            )
            with patch.object(core, "resolve_mod_closure", return_value=closure):
                result = add.run()
            self.assertTrue(result.success, result.message)
            targets = {
                target.mod.project_id: target
                for target in transaction.staged_exact_mod_targets()
            }
            self.assertEqual(targets[mr_project("introduced")].role, "root")
            self.assertEqual(targets[mr_project("shared")].role, "dependency")
            self.assertEqual(
                targets[mr_project("shared")].required_by,
                (f"modrinth:{mr_project('introduced')}",),
            )
            self.assertEqual(
                targets[mr_project("new-dependency")].role, "dependency"
            )
        finally:
            transaction.discard()

    def test_staged_add_rejects_drift_of_existing_locked_dependency_override(self) -> None:
        transaction = self.write_graph_pack(
            (("existing", "e1", "both"),),
            (("dependency", "d1"),),
        )
        set_mod_version_override(
            transaction.source,
            ModVersionOverride(
                "modrinth",
                mr_project("dependency"),
                mr_version("d1"),
                True,
                "keep dependency stable",
            ),
        )
        before_add = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        add = transaction.begin_resolved_add(
            provider="modrinth",
            selector="introduced",
            canonical_project_id=mr_project("introduced"),
            side="both",
        )
        try:
            with patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.graph_closure(
                    "introduced", "x1", ("dependency", "d2")
                ),
            ):
                result = add.run()
            self.assertFalse(result.success)
            self.assertTrue(result.message)
            self.assertEqual(core._file_content_snapshot(transaction.source), before_add)
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_accepted_exact_preview_can_rollback_to_post_add_bytes(self) -> None:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        self.source.joinpath(".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(self.source, ())
        real_before = core._file_content_snapshot(self.source)
        transaction = core.PackTransaction.create(self.key)
        try:
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="introduced",
                canonical_project_id=mr_project("introduced"),
                side="both",
            )
            with patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.graph_closure("introduced", "x1"),
            ):
                self.assertTrue(add.run().success)
            post_add = core._file_content_snapshot(transaction.source)
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure("introduced", "x2"),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("introduced"),
                        branded_version("x2"),
                    )
                )
            self.assertTrue(transaction.exact_selection_prepared)
            transaction.rollback_exact_mod_version()
            self.assertFalse(transaction.exact_selection_prepared)
            self.assertEqual(core._file_content_snapshot(transaction.source), post_add)
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
            transaction.unstage(Path("mods/introduced.pw.toml"))
        finally:
            transaction.discard()

    def test_multiple_accepted_exact_selections_compose_and_later_cancel_restores(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"), ("root-b", "b1", "both")),
            (),
        )

        def closure_for(selection, **_kwargs):
            _provider, project_id, artifact_id = selection_fixture_key(selection)
            return self.graph_closure(project_id, artifact_id)

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", side_effect=closure_for
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("root-a"),
                        branded_version("a2"),
                    )
                )
                transaction.accept_exact_mod_version()
                first_accepted = core._file_content_snapshot(transaction.source)
                self.assertTrue(transaction.exact_selection_accepted)
                self.assertFalse(transaction.exact_selection_prepared)
                self.assertTrue(
                    all(
                        target.required_by_complete
                        for target in transaction.staged_exact_mod_targets()
                    )
                )

                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("root-b"),
                        branded_version("x2"),
                    )
                )
                transaction.accept_exact_mod_version()
                second_accepted = core._file_content_snapshot(transaction.source)
                self.assertNotEqual(second_accepted, first_accepted)

                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("root-a"),
                        branded_version("a1"),
                    )
                )
                transaction.rollback_exact_mod_version()
                self.assertEqual(
                    core._file_content_snapshot(transaction.source), second_accepted
                )
                self.assertTrue(transaction.exact_selection_accepted)

            transaction.apply()
            self.assertIn(
                f'version = "{mr_version("a2")}"',
                self.source.joinpath("mods/root-a.pw.toml").read_text(),
            )
            self.assertIn(
                f'version = "{mr_version("x2")}"',
                self.source.joinpath("mods/root-b.pw.toml").read_text(),
            )
        finally:
            if transaction.active:
                transaction.discard()

    def test_mutation_after_accept_requires_fresh_complete_exact_verification(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),), (("dependency", "d2"),)
        )
        try:
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure("root", "r2", ("dependency", "d2")),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("root"),
                        branded_version("r2"),
                    )
                )
                transaction.accept_exact_mod_version()
            target = transaction.staged_exact_mod_targets()[0]
            transaction.set_side(target.mod.relative_path, True, False)
            self.assertFalse(transaction.exact_selection_accepted)
            with self.assertRaisesRegex(core.HuroshikiError, "invalidated exact MOD"):
                transaction.apply()
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure(
                    "root", "r2", ("dependency", "d2")
                ),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("root"),
                        branded_version("r2"),
                    )
                )
                transaction.accept_exact_mod_version()
            transaction.apply()
            self.assertFalse(transaction.active)
        finally:
            if transaction.active:
                transaction.discard()

    def test_resolved_add_after_accept_invalidates_evidence_and_undo_history(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),), (("dependency", "d2"),)
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
                    self.selection(
                        "modrinth",
                        branded_project("root"),
                        branded_version("r2"),
                    )
                )
                transaction.accept_exact_mod_version()
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="introduced",
                canonical_project_id=mr_project("introduced"),
                side="both",
            )
            with patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.graph_closure("introduced", "x1"),
            ):
                result = add.run()
            self.assertTrue(result.success, result.message)
            self.assertFalse(transaction.exact_selection_accepted)
            with self.assertRaisesRegex(
                core.HuroshikiError, "No exact MOD version selection"
            ):
                transaction.rollback_exact_mod_version()
            with self.assertRaisesRegex(core.HuroshikiError, "invalidated exact MOD"):
                transaction.apply()
        finally:
            if transaction.active:
                transaction.discard()

    def test_final_apply_excludes_concurrent_mutation_after_exact_validation(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),), (("dependency", "d2"),)
        )
        entered_validation = threading.Event()
        release_validation = threading.Event()
        mutation_started = threading.Event()
        apply_errors: list[BaseException] = []
        mutation_errors: list[BaseException] = []
        try:
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure(
                    "root", "r2", ("dependency", "d2")
                ),
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("root"),
                        branded_version("r2"),
                    )
                )
                transaction.accept_exact_mod_version()
            target = transaction.staged_exact_mod_targets()[0]
            validate = transaction._validate_exact_selection_stage

            def blocking_validate(evidence):
                entered_validation.set()
                self.assertTrue(release_validation.wait(2))
                validate(evidence)

            def apply() -> None:
                try:
                    transaction.apply()
                except BaseException as error:
                    apply_errors.append(error)

            def mutate() -> None:
                mutation_started.set()
                try:
                    transaction.set_side(target.mod.relative_path, True, False)
                except BaseException as error:
                    mutation_errors.append(error)

            with patch.object(
                transaction,
                "_validate_exact_selection_stage",
                side_effect=blocking_validate,
            ):
                apply_thread = threading.Thread(target=apply, daemon=False)
                mutation_thread = threading.Thread(target=mutate, daemon=False)
                apply_thread.start()
                self.assertTrue(entered_validation.wait(2))
                mutation_thread.start()
                self.assertTrue(mutation_started.wait(2))
                self.assertTrue(mutation_thread.is_alive())
                release_validation.set()
                apply_thread.join(2)
                mutation_thread.join(2)

            self.assertFalse(apply_errors)
            self.assertEqual(len(mutation_errors), 1)
            self.assertRegex(str(mutation_errors[0]), "no longer active")
            self.assertFalse(transaction.active)
        finally:
            release_validation.set()
            if transaction.active:
                transaction.discard()

    def test_accepted_rollback_digest_blocks_external_staged_replacement(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),), (("dependency", "d2"),)
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
                    self.selection(
                        "modrinth",
                        branded_project("root"),
                        branded_version("r2"),
                    )
                )
                transaction.accept_exact_mod_version()
            transaction.rollback_exact_mod_version()
            transaction.source.joinpath("pack.toml").write_text(
                '[versions]\nminecraft = "1.20.1"\nfabric = "0.16.0"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                core.HuroshikiError, "changed after exact-selection rollback"
            ):
                transaction.apply(refresh=False)
        finally:
            if transaction.active:
                transaction.discard()

    def test_staged_dependency_selection_rebuilds_added_root_closure_shape(self) -> None:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        self.source.joinpath(".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(self.source, ())
        real_before = core._file_content_snapshot(self.source)
        transaction = core.PackTransaction.create(self.key)
        try:
            add = transaction.begin_resolved_add(
                provider="modrinth",
                selector="root",
                canonical_project_id=mr_project("root"),
                side="both",
            )
            initial = self.graph_closure(
                "root",
                "r1",
                ("dependency", "d1"),
                ("intermediate-e", "e1"),
            )
            with patch.object(core, "resolve_mod_closure", return_value=initial):
                self.assertTrue(add.run().success)
            resulting = self.graph_closure(
                "root",
                "r1",
                ("dependency", "d2"),
                ("child", "f1"),
            )
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=resulting
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("dependency"),
                        branded_version("d2"),
                    )
                )
            self.assertEqual(
                preview.removed_dependency_identities,
                (f"modrinth:{mr_project('intermediate-e')}",),
            )
            self.assertEqual(
                preview.added_dependency_identities,
                (f"modrinth:{mr_project('child')}",),
            )
            self.assertTrue(transaction.source.joinpath("mods/root.pw.toml").is_file())
            self.assertFalse(
                transaction.source.joinpath("mods/intermediate-e.pw.toml").exists()
            )
            self.assertTrue(transaction.source.joinpath("mods/child.pw.toml").is_file())
            self.assertEqual(
                [mod.project_id for mod in transaction.staged_removed_mods()],
                [],
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_shared_dependency_selection_preserves_every_staged_add_root(self) -> None:
        for path in self.source.joinpath("mods").iterdir():
            path.unlink()
        self.source.joinpath(".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        core.write_pack_root_manifest(self.source, ())
        transaction = core.PackTransaction.create(self.key)
        try:
            for root_name, artifact in (("root-a", "a1"), ("root-b", "b1")):
                add = transaction.begin_resolved_add(
                    provider="modrinth",
                    selector=root_name,
                    canonical_project_id=mr_project(root_name),
                    side="both",
                )
                with patch.object(
                    core,
                    "resolve_mod_closure",
                    return_value=self.graph_closure(
                        root_name, artifact, ("dependency", "d1")
                    ),
                ):
                    self.assertTrue(add.run().success)

            def resolve(selection, **_kwargs):
                project = selection_fixture_key(selection)[1]
                artifact = "a1" if project == "root-a" else "b1"
                return self.graph_closure(
                    project, artifact, ("dependency", "d2")
                )

            materialize = self.dependency_graph_materializer(
                {
                    "root-a": (("dependency", ">=1"),),
                    "root-b": (("dependency", ">=1"),),
                },
                {},
            )
            with patch.object(
                core, "resolve_exact_mod_closure", side_effect=resolve
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth",
                        branded_project("dependency"),
                        branded_version("d2"),
                    )
                )
            self.assertTrue(transaction.source.joinpath("mods/root-a.pw.toml").is_file())
            self.assertTrue(transaction.source.joinpath("mods/root-b.pw.toml").is_file())
            self.assertEqual(
                {
                    root.canonical_identity
                    for root in core.read_pack_root_manifest(transaction.source)
                },
                {
                    f"modrinth:{mr_project('root-a')}",
                    f"modrinth:{mr_project('root-b')}",
                },
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
                        self.selection("modrinth", branded_project("introduced"), branded_version("x2"))
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before_exact)
            self.assertIn(
                f'version = "{mr_version("x1")}"',
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
                    ("modrinth", mr_project("dependency")),
                    root.relative_path,
                    "dependency.jar",
                    self.metadata(
                        "modrinth", "dependency", "d1", filename="dependency.jar"
                    ).encode(),
                    "modrinth",
                    mr_project("dependency"),
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
                        ("modrinth", mr_project("root")), (root, dependency)
                    )
                    with patch.object(
                        core, "resolve_exact_mod_closure", return_value=closure
                    ):
                        with self.assertRaisesRegex(core.HuroshikiError, message):
                            transaction.prepare_exact_mod_version(
                                self.selection("modrinth", branded_project("root"), branded_version("r2"))
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
            return closures[selection_fixture_key(selection)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
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
                if selection.identity == ("modrinth", mr_project("root-a")):
                    return self.graph_closure_with_dependency_side(
                        "root-a", "a2", "client", "root-b"
                    )
                return self.graph_closure("root-b", "b1")

            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
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
            return closures[selection_fixture_key(selection)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                with self.assertRaisesRegex(core.HuroshikiError, "disagreement"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
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
            return closures[selection_fixture_key(selection)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                with self.assertRaisesRegex(core.HuroshikiError, "expected"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                    )
            self.assertIn(
                f'version = "{mr_version("d1")}"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_dependency_exact_selection_conflicting_owner_restores_all_bytes(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"), ("root-b", "b1", "both")),
            (("dependency", "d1"),),
        )
        before = core._file_content_snapshot(transaction.source)
        calls: dict[tuple[str, str], int] = {}

        def resolve(selection, **_):
            identity = selection.identity
            calls[identity] = calls.get(identity, 0) + 1
            if identity == ("modrinth", mr_project("root-a")):
                artifact = "d2" if calls[identity] == 2 else "d1"
                return self.graph_closure("root-a", "a1", ("dependency", artifact))
            if identity == ("modrinth", mr_project("root-b")):
                return self.graph_closure("root-b", "b1", ("dependency", "d1"))
            raise AssertionError(identity)

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                with self.assertRaisesRegex(
                    core.HuroshikiError,
                    "ownership|selection conflict|Shared dependency disagreement",
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                    )
            self.assertEqual(core._file_content_snapshot(transaction.source), before)
        finally:
            transaction.discard()

    def test_shared_dependency_conflict_restores_all_transaction_bytes(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"), ("root-b", "b1", "both")),
            (("dependency", "d1"),),
        )
        before = core._file_content_snapshot(transaction.source)
        closures = {
            ("modrinth", "root-a", "a2"): self.graph_closure(
                "root-a", "a2", ("dependency", "d2")
            ),
            ("modrinth", "root-b", "b1"): self.graph_closure(
                "root-b", "b1", ("dependency", "d1")
            ),
        }

        def resolve(selection, **_):
            return closures[selection_fixture_key(selection)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                with self.assertRaisesRegex(core.HuroshikiError, "disagreement"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                    )
            self.assertEqual(core._file_content_snapshot(transaction.source), before)
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
            return closures[selection_fixture_key(selection)]

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                )
            self.assertEqual(preview.old_artifact_id, mr_version("d1"))
            self.assertEqual(preview.new_artifact_id, mr_version("d2"))
            self.assertEqual(preview.removed_dependencies, 0)
            self.assertEqual(preview.added_dependencies, 0)
        finally:
            transaction.discard()

    def test_root_selection_validates_resulting_dependency_constraint(self) -> None:
        def run(version: str, succeeds: bool) -> None:
            transaction = self.write_graph_pack(
                (("root-a", "a1", "both"),),
                (("dependency", "d1"),),
            )
            before = core._file_content_snapshot(transaction.source)
            real_before = core._file_content_snapshot(self.source)
            closure = self.graph_closure("root-a", "a2", ("dependency", "d2"))
            materialize = self.dependency_graph_materializer(
                {"root-a": (("dependency", ">=2"),)},
                {"d1": "1.0", "d2": version},
            )
            try:
                with patch.object(
                    core, "resolve_exact_mod_closure", return_value=closure
                ), patch.object(
                    core, "materialize_provider_artifact", side_effect=materialize
                ):
                    if succeeds:
                        preview = transaction.prepare_exact_mod_version(
                            self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                        )
                        self.assertEqual(preview.new_artifact_id, mr_version("a2"))
                    else:
                        with self.assertRaisesRegex(
                            core.HuroshikiError, "graph conflict"
                        ):
                            transaction.prepare_exact_mod_version(
                                self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                            )
                        self.assertEqual(
                            core._file_content_snapshot(transaction.source), before
                        )
                        self.assertEqual(
                            core._file_content_snapshot(self.source), real_before
                        )
            finally:
                transaction.discard()

        run("2.5", True)
        run("1.5", False)

    def test_selected_dependency_outgoing_child_is_fully_validated(self) -> None:
        def run(version: str, succeeds: bool) -> None:
            transaction = self.write_graph_pack(
                (("root-a", "a1", "both"),),
                (("dependency", "d1"), ("child", "e1")),
            )
            closure = self.graph_closure(
                "root-a", "a1", ("dependency", "d2"), ("child", "e2")
            )
            materialize = self.dependency_graph_materializer(
                {
                    "root-a": (("dependency", ">=1"),),
                    "dependency": (("child", ">=2"),),
                },
                {"d1": "1.0", "d2": "2.0", "e1": "1.0", "e2": version},
            )
            try:
                with patch.object(
                    core, "resolve_exact_mod_closure", return_value=closure
                ), patch.object(
                    core, "materialize_provider_artifact", side_effect=materialize
                ):
                    if succeeds:
                        preview = transaction.prepare_exact_mod_version(
                            self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                        )
                        self.assertEqual(preview.new_artifact_id, mr_version("d2"))
                    else:
                        with self.assertRaisesRegex(
                            core.HuroshikiError, "graph conflict"
                        ):
                            transaction.prepare_exact_mod_version(
                                self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                            )
            finally:
                transaction.discard()

        run("2.5", True)
        run("1.5", False)

    def test_exact_selection_validates_minecraft_runtime_constraint(self) -> None:
        def run(requirement: str, succeeds: bool) -> None:
            transaction = self.write_graph_pack((("root-a", "a1", "both"),), ())
            closure = self.graph_closure("root-a", "a2")
            materialize = self.dependency_graph_materializer(
                {"root-a": (("minecraft", requirement),)},
                {},
            )
            try:
                with patch.object(
                    core, "resolve_exact_mod_closure", return_value=closure
                ), patch.object(
                    core, "materialize_provider_artifact", side_effect=materialize
                ):
                    if succeeds:
                        transaction.prepare_exact_mod_version(
                            self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                        )
                    else:
                        with self.assertRaisesRegex(
                            core.HuroshikiError, "runtime compatibility conflict"
                        ):
                            transaction.prepare_exact_mod_version(
                                self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                            )
            finally:
                transaction.discard()

        run(">=1.21 <1.22", True)
        run(">=1.22", False)

    def test_exact_selection_validates_fabric_loader_runtime_constraint(self) -> None:
        transaction = self.write_graph_pack((("root-a", "a1", "both"),), ())
        closure = self.graph_closure("root-a", "a2")
        materialize = self.dependency_graph_materializer(
            {"root-a": (("fabricloader", ">=0.17"),)},
            {},
        )
        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "runtime compatibility conflict"
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                    )
        finally:
            transaction.discard()

    def test_quilt_required_mod_and_runtime_constraints_are_validated(self) -> None:
        self.source.joinpath("pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nquilt = "0.26.0"\n',
            encoding="utf-8",
        )
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root-a", "a2", ("dependency", "d2"))

        def materialize(candidate, *_args, **_kwargs):
            _provider, project_id = candidate.provider_identity.split(":", 1)
            project_id = mr_project_key(project_id)
            artifact_id = core.parse_provider_metadata(
                Path(candidate.relative_metadata_path), candidate.contents
            ).file_id
            artifact_id = mr_version_key(artifact_id or "")
            version = "2.0" if artifact_id == "d2" else "1.0"
            requirements = (
                (
                    LoaderDependencyRequirement("dependency", ">=2"),
                    LoaderDependencyRequirement("quilt_loader", ">=0.27"),
                )
                if project_id == "root-a"
                else ()
            )
            return MaterializedArtifact(
                "b" * 64,
                SemanticJarIdentity(((project_id, version),), "quilt"),
                requirements,
            )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "runtime compatibility conflict"
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                    )
        finally:
            transaction.discard()

    def test_quilt_required_mod_constraint_fails_closed(self) -> None:
        self.source.joinpath("pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nquilt = "0.26.0"\n',
            encoding="utf-8",
        )
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root-a", "a2", ("dependency", "d2"))

        def materialize(candidate, *_args, **_kwargs):
            _provider, project_id = candidate.provider_identity.split(":", 1)
            project_id = mr_project_key(project_id)
            artifact_id = core.parse_provider_metadata(
                Path(candidate.relative_metadata_path), candidate.contents
            ).file_id
            artifact_id = mr_version_key(artifact_id or "")
            version = "2.0" if artifact_id == "d2" else "1.0"
            requirements = (
                (
                    LoaderDependencyRequirement("dependency", ">=3"),
                    LoaderDependencyRequirement("quilt_loader", ">=0.26"),
                )
                if project_id == "root-a"
                else ()
            )
            return MaterializedArtifact(
                "b" * 64,
                SemanticJarIdentity(((project_id, version),), "quilt"),
                requirements,
            )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "graph conflict"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root-a"), branded_version("a2"))
                    )
        finally:
            transaction.discard()



    def test_transitive_dependency_exact_selection_uses_direct_parent_edge(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("intermediate-b", "b1"), ("dependency", "d1")),
        )
        closure = self.graph_closure(
            "root-a", "a1", ("intermediate-b", "b1"), ("dependency", "d2")
        )
        materialize = self.dependency_graph_materializer(
            {
                "root-a": (("intermediate-b", ">=1"),),
                "intermediate-b": (("dependency", ">=2"),),
            },
            {"d1": "1.0", "d2": "2.5"},
        )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                )
            self.assertEqual(preview.new_artifact_id, mr_version("d2"))
        finally:
            transaction.discard()

    def test_transitive_dependency_conflict_restores_transaction_and_pack(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("intermediate-b", "b1"), ("dependency", "d1")),
        )
        before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        closure = self.graph_closure(
            "root-a", "a1", ("intermediate-b", "b1"), ("dependency", "d2")
        )
        materialize = self.dependency_graph_materializer(
            {
                "root-a": (("intermediate-b", ">=1"),),
                "intermediate-b": (("dependency", ">=2"),),
            },
            {"d1": "1.0", "d2": "1.5"},
        )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "graph conflict"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                    )
            self.assertEqual(core._file_content_snapshot(transaction.source), before)
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_shared_transitive_dependency_checks_every_direct_parent(self) -> None:
        def run(version: str, succeeds: bool) -> None:
            transaction = self.write_graph_pack(
                (("root-a", "a1", "both"), ("root-c", "c1", "both")),
                (
                    ("intermediate-b", "b1"),
                    ("intermediate-e", "e1"),
                    ("dependency", "d1"),
                ),
            )
            closures = {
                "root-a": self.graph_closure(
                    "root-a", "a1", ("intermediate-b", "b1"), ("dependency", "d2")
                ),
                "root-c": self.graph_closure(
                    "root-c", "c1", ("intermediate-e", "e1"), ("dependency", "d2")
                ),
            }
            materialize = self.dependency_graph_materializer(
                {
                    "root-a": (("intermediate-b", ">=1"),),
                    "root-c": (("intermediate-e", ">=1"),),
                    "intermediate-b": (("dependency", ">=2"),),
                    "intermediate-e": (("dependency", "<3"),),
                },
                {"d1": "1.0", "d2": version},
            )

            def resolve(selection, **_kwargs):
                return closures[mr_project_key(selection.project_id)]

            try:
                with patch.object(
                    core, "resolve_exact_mod_closure", side_effect=resolve
                ), patch.object(
                    core, "materialize_provider_artifact", side_effect=materialize
                ):
                    if succeeds:
                        preview = transaction.prepare_exact_mod_version(
                            self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                        )
                        self.assertEqual(preview.new_artifact_id, mr_version("d2"))
                    else:
                        with self.assertRaisesRegex(
                            core.HuroshikiError, "graph conflict"
                        ):
                            transaction.prepare_exact_mod_version(
                                self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                            )
            finally:
                transaction.discard()

        run("2.5", True)
        run("3.0", False)

    def test_preseed_without_required_edge_fails_closed(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root-a", "a1", ("dependency", "d2"))
        materialize = self.dependency_graph_materializer(
            {"root-a": ()},
            {"d1": "1.0", "d2": "2.0"},
        )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "required-edge reachability"
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                    )
        finally:
            transaction.discard()

    def test_missing_intermediate_dependency_evidence_fails_closed(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("intermediate-b", "b1"), ("dependency", "d1")),
        )
        closure = self.graph_closure(
            "root-a", "a1", ("intermediate-b", "b1"), ("dependency", "d2")
        )
        materialize = self.dependency_graph_materializer(
            {
                "root-a": (("intermediate-b", ">=1"),),
                "intermediate-b": None,
            },
            {"d1": "1.0", "d2": "2.0"},
        )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "evidence is unavailable"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                    )
        finally:
            transaction.discard()

    def test_multi_mod_selected_dependency_needs_one_declared_provided_id(self) -> None:
        transaction = self.write_graph_pack(
            (("root-a", "a1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root-a", "a1", ("dependency", "d2"))
        materialize = self.dependency_graph_materializer(
            {"root-a": (("d", ">=2"),)},
            {},
            {
                "d1": (("d", "1.0"), ("d-helper", "1.0")),
                "d2": (("d", "2.0"), ("d-helper", "2.0")),
            },
        )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                )
            self.assertEqual(preview.new_artifact_id, mr_version("d2"))
        finally:
            transaction.discard()

    def test_dependency_preseed_presence_does_not_prove_owner_compatibility(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root", "r1", ("dependency", "d2"))

        def materialize(candidate, *_args, **_kwargs):
            _provider, project_id = candidate.provider_identity.split(":", 1)
            project_id = mr_project_key(project_id)
            artifact_id = core.parse_provider_metadata(
                Path(candidate.relative_metadata_path), candidate.contents
            ).file_id
            artifact_id = mr_version_key(artifact_id or "")
            version = "2.0" if artifact_id == "d2" else "1.0"
            requirements = (
                (LoaderDependencyRequirement("dependency", "<2.0"),)
                if project_id == "root"
                else ()
            )
            return MaterializedArtifact(
                "b" * 64,
                SemanticJarIdentity(((project_id, version),), "fabric"),
                requirements,
            )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "graph conflict"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
                    )
        finally:
            transaction.discard()

    def test_dependency_semantic_id_replacement_fails_closed(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root", "r2", ("dependency", "d2"))

        def materialize(candidate, *_args, **_kwargs):
            candidate_project_id = mr_project_key(candidate.provider_identity.split(":", 1)[1])
            artifact_id = core.parse_provider_metadata(
                Path(candidate.relative_metadata_path), candidate.contents
            ).file_id
            artifact_id = mr_version_key(artifact_id or "")
            project_id = candidate_project_id
            mod_id = (
                "other"
                if project_id == "dependency" and artifact_id == "d2"
                else project_id
            )
            version = "2.0" if artifact_id in {"r2", "d2"} else "1.0"
            return MaterializedArtifact(
                "b" * 64,
                SemanticJarIdentity(((mod_id, version),), "fabric"),
            )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "semantic MOD identity"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root"), branded_version("r2"))
                    )
        finally:
            transaction.discard()

    def test_dependency_multi_mod_semantic_id_set_must_match_completely(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        closure = self.graph_closure("root", "r2", ("dependency", "d2"))

        def materialize(candidate, *_args, **_kwargs):
            project_id = mr_project_key(candidate.provider_identity.split(":", 1)[1])
            artifact_id = core.parse_provider_metadata(
                Path(candidate.relative_metadata_path), candidate.contents
            ).file_id
            artifact_id = mr_version_key(artifact_id or "")
            if project_id == "dependency":
                members = (
                    (("d", "2.0"), ("other", "2.0"))
                    if artifact_id == "d2"
                    else (("d", "1.0"), ("library", "1.0"))
                )
            else:
                members = ((project_id, "2.0"),)
            return MaterializedArtifact(
                "b" * 64,
                SemanticJarIdentity(tuple(sorted(members)), "fabric"),
            )

        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "semantic MOD identity"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root"), branded_version("r2"))
                    )
        finally:
            transaction.discard()

    def test_resulting_closure_refresh_failure_restores_all_transaction_bytes(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"),),
        )
        before = core._file_content_snapshot(transaction.source)
        closure = self.graph_closure("root", "r2", ("replacement", "x1"))

        try:
            materialize = self.dependency_graph_materializer({"root": (("replacement", ">=1"),)}, {})
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ), patch.object(
                core,
                "run_resolver_process",
                return_value=core.ResolverProcessResult(
                    7, "", "refresh failed", False, False
                ),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "refresh"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root"), branded_version("r2"))
                    )
            self.assertEqual(core._file_content_snapshot(transaction.source), before)
            self.assertTrue(
                transaction.source.joinpath("mods/dependency.pw.toml").exists()
            )
            self.assertFalse(
                transaction.source.joinpath("mods/replacement.pw.toml").exists()
            )
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
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
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
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
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
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
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
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), before)
            self.assertIn(
                f'version = "{mr_version("old-artifact")}"',
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
                            if f'version = "{mr_version("v2")}"' in candidate.contents.decode("utf-8")
                            else "root",
                            "2.0",
                        ),),
                        "fabric",
                    ),
                ),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "identity changed"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
                    )
        finally:
            transaction.discard()

    def test_exact_selection_accepts_same_semantic_mod_with_new_version(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core,
                "materialize_provider_artifact",
                side_effect=lambda candidate, *_args, **_kwargs: MaterializedArtifact(
                    "b" * 64,
                    SemanticJarIdentity(((
                        "dependency" if candidate.filename == "dependency.jar" else "root",
                        "2.0",
                    ),), "fabric"),
                    (),
                ),
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("root"), branded_version("v2"))
                )
            self.assertEqual(preview.new_artifact_id, mr_version("v2"))
        finally:
            transaction.discard()

    def test_unchanged_url_root_coexists_with_provider_exact_selection(self) -> None:
        transaction, url_relative, url_contents = self.write_pack_with_url_root()
        real_before = core._file_content_snapshot(self.source)
        manifest_before = transaction.source.joinpath(
            ".huroshiki-roots.json"
        ).read_bytes()
        try:
            materialize = self.dependency_graph_materializer({"root": ()}, {})
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure("root", "r2"),
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("root"), branded_version("r2"))
                )
            self.assertEqual(preview.new_artifact_id, mr_version("r2"))
            self.assertEqual(
                transaction.source.joinpath(url_relative).read_bytes(), url_contents
            )
            self.assertEqual(
                transaction.source.joinpath(".huroshiki-roots.json").read_bytes(),
                manifest_before,
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_changed_url_root_during_reconstruction_fails_closed(self) -> None:
        transaction, _url_relative, _url_contents = self.write_pack_with_url_root()
        real_metadata = core._exact_metadata_from_root

        def changed_metadata(root, baseline):
            item = real_metadata(root, baseline)
            if root.provider != "url":
                return item
            return core.ResolvedMetadata(
                item.identity,
                item.relative_path,
                item.filename,
                item.contents.replace(b"URL Root", b"Changed!"),
                item.provider,
                item.project_id,
            )

        try:
            with patch.object(
                core,
                "resolve_exact_mod_closure",
                return_value=self.graph_closure("root", "r2"),
            ), patch.object(
                core, "_exact_metadata_from_root", side_effect=changed_metadata
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "opaque URL root"):
                    transaction.prepare_exact_mod_version(
                        self.selection("modrinth", branded_project("root"), branded_version("r2"))
                    )
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
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
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
                        self.selection("modrinth", branded_project("root"), branded_version("r2"))
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
            if selection.identity == ("modrinth", mr_project("root")):
                return self.graph_closure_with_dependency_side(
                    "root", "r1", "both", "d2"
                )
            return self.graph_closure_with_dependency_side(
                "dependency", selection.artifact_id, "both", selection.artifact_id
            )

        try:
            with patch.object(core, "resolve_exact_mod_closure", side_effect=resolve):
                transaction.prepare_exact_mod_version(
                    self.selection("modrinth", branded_project("dependency"), branded_version("d2"))
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
                    self.selection("modrinth", branded_project("root"), branded_version("v2"))
                )
        finally:
            transaction.discard()

    def test_preview_dependency_identity_order_is_deterministic(self) -> None:
        transaction = self.write_graph_pack(
            (("root", "r1", "both"),),
            (("dependency", "d1"), ("child", "v1")),
        )
        closure = self.graph_closure(
            "root",
            "r2",
            ("replacement", "x1"),
            ("introduced", "x2"),
        )
        materialize = self.dependency_graph_materializer(
            {
                "root": (("introduced", ">=1"), ("replacement", ">=1")),
                "replacement": (),
                "introduced": (),
            },
            {},
        )
        try:
            with patch.object(
                core, "resolve_exact_mod_closure", return_value=closure
            ), patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth", branded_project("root"), branded_version("r2")
                    )
                )
            self.assertEqual(
                preview.added_dependency_identities,
                ("modrinth:Intro001", "modrinth:Repla001"),
            )
            self.assertEqual(
                preview.removed_dependency_identities,
                ("modrinth:Child001", "modrinth:Depen001"),
            )
            self.assertEqual(
                preview.added_dependencies,
                len(preview.added_dependency_identities),
            )
            self.assertEqual(
                preview.removed_dependencies,
                len(preview.removed_dependency_identities),
            )
            self.assertEqual(
                {change.relative_path for change in preview.changes},
                {
                    Path("index.toml"),
                    Path("mods/child.pw.toml"),
                    Path("mods/dependency.pw.toml"),
                    Path("mods/introduced.pw.toml"),
                    Path("mods/replacement.pw.toml"),
                    Path("mods/root.pw.toml"),
                },
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
                        self.selection("modrinth", branded_project("root"), branded_version("r2"))
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
                    self.selection("modrinth", branded_project("root"), branded_version("v2"))
                )
            self.assertEqual(preview.identity, f"modrinth:{mr_project("root")}")
            self.assertEqual(preview.old_artifact_id, mr_version("old-artifact"))
            self.assertEqual(preview.new_artifact_id, mr_version("v2"))
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
                        mr_project("root"),
                        "--version-id",
                        mr_version("v2"),
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
                f'version = "{mr_version("old-artifact")}"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            transaction.apply()
            self.assertIn(
                f'version = "{mr_version("v2")}"',
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
                (core.PackRootRecord("modrinth", mr_project("root"), "server"),),
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
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
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
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
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
                    self.selection("modrinth", branded_project("root"), branded_version("v2"))
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
                f'version = "{mr_version("old-artifact")}"',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertIn(
                f'version = "{mr_version("old-artifact")}"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_successful_exact_prepare_then_discard_never_publishes(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        real_before = core._file_content_snapshot(self.source)
        with patch.object(
            core, "materialize_provider_artifact", side_effect=self.materialize
        ):
            transaction.prepare_exact_mod_version(
                self.selection(
                    "modrinth", branded_project("root"), branded_version("v2")
                )
            )
        self.assertIn(
            f'version = "{mr_version("v2")}"',
            transaction.source.joinpath("mods/root.pw.toml").read_text(),
        )
        self.assertEqual(core._file_content_snapshot(self.source), real_before)

        transaction.discard()

        self.assertEqual(core._file_content_snapshot(self.source), real_before)
        self.assertFalse(transaction.active)
        self.assertFalse(packctl.project_lock_is_active(self.key))
        with self.assertRaisesRegex(core.HuroshikiError, "no longer active"):
            transaction.apply()
        with self.assertRaisesRegex(core.HuroshikiError, "no longer active"):
            transaction.prepare_exact_mod_version(
                self.selection(
                    "modrinth", branded_project("root"), branded_version("v2")
                )
            )

    def test_caller_cancellation_during_exact_resolver_restores_staging(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        staged = transaction.source.joinpath("prior-staged.txt")
        staged.write_bytes(b"prior staged bytes\n")
        transaction_before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        started = threading.Event()
        cancel_event = threading.Event()
        worker_error: list[BaseException] = []

        def resolver(command, *, cancel_event, result_callback=None, **_kwargs):
            self.assertIn("--version-id", command)
            started.set()
            self.assertTrue(cancel_event.wait(2))
            result = core.ResolverProcessResult(-15, "", "", True, False)
            if result_callback is not None:
                result_callback(result)
            return result

        def prepare() -> None:
            try:
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth", branded_project("root"), branded_version("v2")
                    ),
                    cancel_event=cancel_event,
                )
            except BaseException as error:
                worker_error.append(error)

        try:
            with patch.object(core, "run_resolver_process", side_effect=resolver):
                worker = threading.Thread(target=prepare, name="exact-caller-cancel")
                worker.start()
                self.assertTrue(started.wait(1))
                cancel_event.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(worker_error), 1)
            self.assertIsInstance(worker_error[0], core.ExactModVersionCancelled)
            self.assertEqual(
                core._file_content_snapshot(transaction.source), transaction_before
            )
            self.assertEqual(staged.read_bytes(), b"prior staged bytes\n")
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
            self.assertTrue(transaction.active)
            self.assertTrue(packctl.project_lock_is_active(self.key))
            self.assertIsNone(transaction._operation)
        finally:
            transaction.discard()
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_deadline_expires_after_exact_resolver_starts(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        staged = transaction.source.joinpath("prior-staged.txt")
        staged.write_bytes(b"deadline baseline\n")
        transaction_before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        started = threading.Event()

        def resolver(command, *, deadline, result_callback=None, **_kwargs):
            self.assertIn("--version-id", command)
            started.set()
            while time.monotonic() < deadline:
                time.sleep(0.001)
            result = core.ResolverProcessResult(-15, "", "", False, True)
            if result_callback is not None:
                result_callback(result)
            return result

        try:
            with patch.object(core, "run_resolver_process", side_effect=resolver):
                with self.assertRaisesRegex(
                    core.ExactModVersionDeadlineExceeded, "deadline exceeded"
                ):
                    transaction.prepare_exact_mod_version(
                        self.selection(
                            "modrinth", branded_project("root"), branded_version("v2")
                        ),
                        deadline=time.monotonic() + 0.2,
                    )
            self.assertTrue(started.is_set())
            self.assertEqual(
                core._file_content_snapshot(transaction.source), transaction_before
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
            self.assertTrue(transaction.active)
            self.assertTrue(packctl.project_lock_is_active(self.key))
        finally:
            transaction.discard()
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_caller_cancellation_during_materialization_restores_staging(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        staged = transaction.source.joinpath("prior-staged.txt")
        staged.write_bytes(b"materialization baseline\n")
        transaction_before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        started = threading.Event()
        cancel_event = threading.Event()
        worker_error: list[BaseException] = []

        def materialize(*_args, cancel_event, **_kwargs):
            started.set()
            self.assertTrue(cancel_event.wait(2))
            raise core.HuroshikiError("Artifact materialization was cancelled")

        def prepare() -> None:
            try:
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth", branded_project("root"), branded_version("v2")
                    ),
                    cancel_event=cancel_event,
                )
            except BaseException as error:
                worker_error.append(error)

        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=materialize
            ):
                worker = threading.Thread(
                    target=prepare, name="exact-materialization-cancel"
                )
                worker.start()
                self.assertTrue(started.wait(1))
                cancel_event.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(worker_error), 1)
            self.assertIsInstance(worker_error[0], core.ExactModVersionCancelled)
            self.assertEqual(
                core._file_content_snapshot(transaction.source), transaction_before
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_incomplete_exact_resolver_cleanup_is_retained_for_discard_retry(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        transaction_before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        process_result = core.ResolverProcessResult(
            7,
            "",
            "ordinary resolver failure",
            True,
            True,
            False,
            True,
            process_group=12345,
        )

        def resolver(*_args, result_callback=None, **_kwargs):
            if result_callback is not None:
                result_callback(process_result)
            return process_result

        with patch.object(core, "run_resolver_process", side_effect=resolver):
            with self.assertRaisesRegex(
                core.HuroshikiError, "termination was incomplete"
            ):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth", branded_project("root"), branded_version("v2")
                    )
                )
        self.assertEqual(core._file_content_snapshot(transaction.source), transaction_before)
        self.assertEqual(core._file_content_snapshot(self.source), real_before)
        self.assertEqual(transaction._equivalence_process_results, [process_result])
        self.assertTrue(transaction.root.exists())

        incomplete = core.ProcessTerminationResult(False, False, True)
        with patch.object(
            core, "stop_resolver_process_group", return_value=incomplete
        ) as stop:
            with self.assertRaisesRegex(
                core.TransactionDiscardIntegrityError, "cleanup was incomplete"
            ):
                transaction.discard(deadline=time.monotonic() + 1)
        stop.assert_called_once()
        self.assertTrue(transaction.root.exists())
        self.assertTrue(packctl.project_lock_is_active(self.key))
        self.assertEqual(transaction._equivalence_process_results, [process_result])

        complete = core.ProcessTerminationResult(True, True, True)
        with patch.object(
            core, "stop_resolver_process_group", return_value=complete
        ) as stop:
            transaction.discard(deadline=time.monotonic() + 1)
        stop.assert_called_once()
        self.assertEqual(transaction._equivalence_process_results, [])
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_failed_staged_exact_cleanup_blocks_rollback_navigation_until_retry(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        before = core._file_content_snapshot(transaction.source)
        process_result = core.ResolverProcessResult(
            7,
            "",
            "resolver failure",
            False,
            False,
            False,
            True,
            process_group=12345,
        )

        def resolver(*_args, result_callback=None, **_kwargs):
            if result_callback is not None:
                result_callback(process_result)
            return process_result

        try:
            with patch.object(core, "run_resolver_process", side_effect=resolver):
                with self.assertRaisesRegex(core.HuroshikiError, "termination"):
                    transaction.prepare_exact_mod_version(
                        self.selection(
                            "modrinth", branded_project("root"), branded_version("v2")
                        )
                    )
            self.assertTrue(transaction.process_cleanup_pending)
            incomplete = core.ProcessTerminationResult(False, False, True)
            with patch.object(
                core, "stop_resolver_process_group", return_value=incomplete
            ):
                with self.assertRaisesRegex(
                    core.TransactionDiscardIntegrityError, "cleanup was incomplete"
                ):
                    transaction.rollback_exact_mod_version(
                        deadline=time.monotonic() + 1
                    )
            self.assertTrue(transaction.process_cleanup_pending)
            self.assertEqual(core._file_content_snapshot(transaction.source), before)

            complete = core.ProcessTerminationResult(True, True, True)
            with patch.object(
                core, "stop_resolver_process_group", return_value=complete
            ):
                transaction.rollback_exact_mod_version(
                    deadline=time.monotonic() + 1
                )
            self.assertFalse(transaction.process_cleanup_pending)
            self.assertEqual(core._file_content_snapshot(transaction.source), before)
        finally:
            transaction.discard()

    def test_orphaned_exact_resolver_integrity_outranks_cancellation(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        transaction_before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)
        cancel_event = threading.Event()

        def resolver(*_args, result_callback=None, **_kwargs):
            cancel_event.set()
            result = core.ResolverProcessResult(
                7,
                "",
                "ordinary resolver failure",
                True,
                True,
                True,
                False,
            )
            if result_callback is not None:
                result_callback(result)
            return result

        try:
            with patch.object(core, "run_resolver_process", side_effect=resolver):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "left background processes"
                ) as raised:
                    transaction.prepare_exact_mod_version(
                        self.selection(
                            "modrinth", branded_project("root"), branded_version("v2")
                        ),
                        cancel_event=cancel_event,
                    )
            self.assertNotIsInstance(raised.exception, core.ExactModVersionCancelled)
            self.assertEqual(
                core._file_content_snapshot(transaction.source), transaction_before
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
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
                    self.selection("modrinth", branded_project("root"), branded_version("v2")),
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
                        self.selection("modrinth", branded_project("root"), branded_version("v2"))
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
            f'version = "{mr_version("old-artifact")}"',
            transaction.source.joinpath("mods/root.pw.toml").read_text(),
        )
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_exact_selection_stages_default_unlocked_override_in_transaction_source_only(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        real_before = core._file_content_snapshot(self.source)
        overrides_before = read_mod_version_overrides(self.source)
        self.assertEqual(overrides_before, ())
        preview = transaction.prepare_exact_mod_version(
            self.selection(
                "modrinth", branded_project("root"), branded_version("v2")
            )
        )
        self.assertEqual(preview.new_artifact_id, mr_version("v2"))
        self.assertFalse((self.source / ".huroshiki-version-overrides.json").exists())
        staged = read_mod_version_overrides(transaction.source)
        self.assertEqual(staged, (ModVersionOverride("modrinth", mr_project("root"), mr_version("v2"), False),))
        self.assertEqual(core._file_content_snapshot(self.source), real_before)

        transaction.apply()
        applied = read_mod_version_overrides(self.source)
        self.assertEqual(
            applied,
            (ModVersionOverride("modrinth", mr_project("root"), mr_version("v2"), False),),
        )

    def test_exact_selection_pin_false_to_true_stages_only_in_transaction_source_then_applies(self) -> None:
        set_mod_version_override(
            self.source,
            ModVersionOverride(
                "modrinth",
                mr_project("root"),
                mr_version("old-artifact"),
                False,
            ),
        )
        transaction = self.make_transaction("modrinth", "root")
        try:
            real_before = core._file_content_snapshot(self.source)
            staged = read_mod_version_overrides(transaction.source)
            self.assertEqual(
                staged,
                (
                    ModVersionOverride(
                        "modrinth",
                        mr_project("root"),
                        mr_version("old-artifact"),
                        False,
                    ),
                ),
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)

            transaction.set_mod_version_pin(
                f"modrinth:{mr_project('root')}", locked=True
            )
            staged = read_mod_version_overrides(transaction.source)
            self.assertEqual(
                staged,
                (
                    ModVersionOverride(
                        "modrinth",
                        mr_project("root"),
                        mr_version("old-artifact"),
                        True,
                    ),
                ),
            )

            transaction.apply()
            applied = read_mod_version_overrides(self.source)
            self.assertEqual(
                applied,
                (
                    ModVersionOverride(
                        "modrinth",
                        mr_project("root"),
                        mr_version("old-artifact"),
                        True,
                    ),
                ),
            )
        finally:
            if transaction.active:
                transaction.discard()

    def test_exact_selection_pin_false_to_true_discard_keeps_real_override_unlocked(self) -> None:
        set_mod_version_override(
            self.source,
            ModVersionOverride(
                "modrinth",
                mr_project("root"),
                mr_version("old-artifact"),
                False,
            ),
        )
        transaction = self.make_transaction("modrinth", "root")
        try:
            real_before = core._file_content_snapshot(self.source)
            transaction.set_mod_version_pin(
                f"modrinth:{mr_project('root')}", locked=True
            )
            self.assertEqual(
                read_mod_version_overrides(transaction.source),
                (
                    ModVersionOverride(
                        "modrinth",
                        mr_project("root"),
                        mr_version("old-artifact"),
                        True,
                    ),
                ),
            )
            transaction.discard()
            self.assertEqual(
                read_mod_version_overrides(self.source),
                (ModVersionOverride(
                    "modrinth",
                    mr_project("root"),
                    mr_version("old-artifact"),
                    False,
                ),),
            )
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            if transaction.active:
                transaction.discard()

    def test_exact_selection_set_pin_without_override_preserves_staged_and_real_snapshots(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        real_before = core._file_content_snapshot(self.source)
        staged_before = core._file_content_snapshot(transaction.source)
        try:
            with self.assertRaisesRegex(
                core.HuroshikiError,
                "Cannot change pin state without an existing user selection",
            ):
                transaction.set_mod_version_pin(f"modrinth:{mr_project('root')}")
            self.assertEqual(core._file_content_snapshot(transaction.source), staged_before)
            self.assertEqual(core._file_content_snapshot(self.source), real_before)
        finally:
            transaction.discard()

    def test_exact_selection_preview_reports_override_identity_artifact_and_locked(self) -> None:
        set_mod_version_override(
            self.source,
            ModVersionOverride(
                "modrinth",
                mr_project("root"),
                mr_version("old-artifact"),
                False,
            ),
        )
        transaction = self.make_transaction("modrinth", "root")
        try:
            preview = transaction.prepare_exact_mod_version(
                self.selection(
                    "modrinth", branded_project("root"), branded_version("v2")
                )
            )
            self.assertEqual(preview.override_identity, f"modrinth:{mr_project('root')}")
            self.assertEqual(preview.override_artifact_id, mr_version("v2"))
            self.assertFalse(preview.override_locked)
        finally:
            transaction.discard()

    def test_exact_selection_success_preserves_diagnostic_and_stages_override(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        real_before = core._file_content_snapshot(self.source)
        original = self.run_fake_resolver

        def diagnostic_resolver(*args, **kwargs):
            result = original(*args, **kwargs)
            return core.ResolverProcessResult(
                result.returncode,
                result.stdout,
                "first diagnostic\nsecond diagnostic\n",
                result.cancelled,
                result.timed_out,
                result.orphaned_descendants,
                result.termination_incomplete,
            )

        with patch.object(core, "run_resolver_process", side_effect=diagnostic_resolver):
            preview = transaction.prepare_exact_mod_version(
                self.selection(
                    "modrinth", branded_project("root"), branded_version("v2")
                )
            )
        self.assertTrue(preview.diagnostic_messages)
        self.assertIn("Details: .huroshiki/logs/demo/", " ".join(preview.diagnostic_messages))
        logs = list((self.root / ".huroshiki/logs/demo").glob("*.log"))
        self.assertTrue(logs)
        self.assertIn("second diagnostic", logs[0].read_text(encoding="utf-8"))
        self.assertEqual(core._file_content_snapshot(self.source), real_before)
        self.assertEqual(
            read_mod_version_overrides(transaction.source)[0].artifact_id,
            mr_version("v2"),
        )
        transaction.discard()

    def test_exact_selection_failure_logs_and_restores_override_bytes(self) -> None:
        set_mod_version_override(
            self.source,
            ModVersionOverride(
                "modrinth", mr_project("root"), mr_version("old-artifact"), True
            ),
        )
        transaction = self.make_transaction("modrinth", "root")
        staged_before = core._file_content_snapshot(transaction.source)
        real_before = core._file_content_snapshot(self.source)

        def failed_resolver(command, **kwargs):
            result = core.ResolverProcessResult(
                2, "resolver stdout\n", "resolver failure\nfull detail\n", False, False
            )
            callback = kwargs.get("result_callback")
            if callback is not None:
                callback(result)
            return result

        with patch.object(core, "run_resolver_process", side_effect=failed_resolver):
            with self.assertRaisesRegex(core.HuroshikiError, "Details: .huroshiki/logs/demo/"):
                transaction.prepare_exact_mod_version(
                    self.selection(
                        "modrinth", branded_project("root"), branded_version("v2")
                    )
                )
        self.assertEqual(core._file_content_snapshot(transaction.source), staged_before)
        self.assertEqual(core._file_content_snapshot(self.source), real_before)
        logs = list((self.root / ".huroshiki/logs/demo").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("full detail", logs[0].read_text(encoding="utf-8"))
        transaction.discard()

    def test_exact_selection_preserves_existing_locked_override_reason(self) -> None:
        set_mod_version_override(
            self.source,
            ModVersionOverride(
                "modrinth",
                mr_project("root"),
                mr_version("old-artifact"),
                True,
                "Compatibility",
            ),
        )
        transaction = self.make_transaction("modrinth", "root")
        transaction.prepare_exact_mod_version(
            self.selection(
                "modrinth", branded_project("root"), branded_version("v2")
            )
        )
        staged = read_mod_version_overrides(transaction.source)
        self.assertEqual(
            staged,
            (
                ModVersionOverride(
                    "modrinth",
                    mr_project("root"),
                    mr_version("v2"),
                    True,
                    "Compatibility",
                ),
            ),
        )
        transaction.apply()
        applied = read_mod_version_overrides(self.source)
        self.assertEqual(
            applied,
            (
                ModVersionOverride(
                    "modrinth",
                    mr_project("root"),
                    mr_version("v2"),
                    True,
                    "Compatibility",
                ),
            ),
        )

    def test_exact_selection_preserves_existing_unlocked_override_state(self) -> None:
        set_mod_version_override(
            self.source,
            ModVersionOverride(
                "modrinth", mr_project("root"), mr_version("old-artifact"), False
            ),
        )
        transaction = self.make_transaction("modrinth", "root")
        transaction.prepare_exact_mod_version(
            self.selection(
                "modrinth", branded_project("root"), branded_version("v2")
            )
        )
        staged = read_mod_version_overrides(transaction.source)
        self.assertEqual(
            staged,
            (
                ModVersionOverride(
                    "modrinth",
                    mr_project("root"),
                    mr_version("v2"),
                    False,
                ),
            ),
        )
        transaction.apply()
        applied = read_mod_version_overrides(self.source)
        self.assertEqual(
            applied,
            (
                ModVersionOverride(
                    "modrinth",
                    mr_project("root"),
                    mr_version("v2"),
                    False,
                ),
            ),
        )

    def test_exact_selection_fails_when_other_override_is_orphaned_or_drifted(self) -> None:
        for changed_artifact in (None, "d2"):
            with self.subTest(artifact=changed_artifact):
                transaction = self.write_graph_pack(
                    (("root", "r1", "both"),),
                    (("dependency", "d1"),),
                )
                set_mod_version_override(
                    transaction.source,
                    ModVersionOverride(
                        "modrinth",
                        mr_project("dependency"),
                        mr_version("d1"),
                        False,
                    ),
                )
                set_mod_version_override(
                    self.source,
                    ModVersionOverride(
                        "modrinth",
                        mr_project("dependency"),
                        mr_version("d1"),
                        False,
                    ),
                )
                transaction_before = core._file_content_snapshot(transaction.source)
                real_before = core._file_content_snapshot(self.source)
                closure = (
                    self.graph_closure("root", "r2")
                    if changed_artifact is None
                    else self.graph_closure("root", "r2", ("dependency", changed_artifact))
                )

                try:
                    with patch.object(core, "resolve_exact_mod_closure", return_value=closure):
                        with self.assertRaisesRegex(
                            core.HuroshikiError,
                            "version override",
                        ):
                            transaction.prepare_exact_mod_version(
                                self.selection(
                                    "modrinth", branded_project("root"), branded_version("r2")
                                )
                            )
                    self.assertEqual(
                        core._file_content_snapshot(transaction.source),
                        transaction_before,
                    )
                    self.assertEqual(core._file_content_snapshot(self.source), real_before)
                    self.assertEqual(
                        read_mod_version_overrides(transaction.source),
                        read_mod_version_overrides(self.source),
                    )
                finally:
                    transaction.discard()


if __name__ == "__main__":
    unittest.main()
