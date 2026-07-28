from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from url_artifacts import UrlArtifact


PACK_TOML = '''name = "Generated"
author = "tester"
version = "0.1.0"
pack-format = "packwiz:1.1.0"
[index]
file = "index.toml"
hash-format = "sha256"
hash = "placeholder"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.999"
'''


def metadata(
    provider: str,
    project_id: str,
    filename: str,
    side: str = "both",
    version: str = "v1",
) -> str:
    provider_table = "modrinth" if provider == "modrinth" else "curseforge"
    project_key = "mod-id" if provider == "modrinth" else "project-id"
    return (
        f'name = "{project_id}"\nfilename = "{filename}"\nside = "{side}"\n'
        f'[download]\nurl = "https://example.invalid/{filename}"\n'
        f'[update.{provider_table}]\n{project_key} = "{project_id}"\n'
        f'version = "{version}"\n'
    )


class TemplateResolverMergeTest(unittest.TestCase):
    @staticmethod
    def run_fake_resolver(command, *, cwd, cancel_event, deadline):
        result = core.subprocess.run(command, cwd=cwd, check=False)
        return core.ResolverProcessResult(
            result.returncode,
            result.stdout or "",
            result.stderr or "",
            False,
            False,
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.template = self.templates / "base"
        self.template.mkdir(parents=True)
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
            patch.object(
                core,
                "run_resolver_process",
                side_effect=self.run_fake_resolver,
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_template(self, mods: str) -> None:
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.234\nmods:\n" + mods,
            encoding="utf-8",
        )

    def fake_create(self, *args) -> int:
        pack_root = self.packs / "generated"
        (pack_root / "source" / "mods").mkdir(parents=True)
        (pack_root / "source" / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (pack_root / "source" / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (pack_root / "pack.yaml").write_text(
            "id: generated\ndisplay_name: Generated\nenabled: true\n",
            encoding="utf-8",
        )
        return 0

    def create(self, **extra):
        arguments = dict(
            template_ids=["base"],
            project_id="generated",
            display_name="Generated",
            minecraft="1.21.1",
            loader="neoforge",
            loader_version="21.1.999",
        )
        arguments.update(extra)
        return core.create_pack_from_templates(**arguments)

    def test_three_isolated_roots_union_unchanged_shared_dependency_sides(self) -> None:
        self.write_template(
            "  - name: Client One\n    provider: modrinth\n"
            "    project_id: client-one\n    side: client\n"
            "  - name: Server\n    provider: modrinth\n"
            "    project_id: server\n    side: server\n"
            "  - name: Client Two\n    provider: modrinth\n"
            "    project_id: client-two\n    side: client\n"
        )
        resolver_directories: list[Path] = []

        def fake_run(command, *, cwd=None, **kwargs):
            source = Path(cwd)
            if command[-1] == "refresh":
                return subprocess.CompletedProcess(command, 0, "", "")
            root_id = command[-1]
            self.assertEqual(list(source.rglob("*.pw.toml")), [])
            resolver_directories.append(source)
            (source / "mods" / f"{root_id}.pw.toml").write_text(
                metadata("modrinth", root_id, f"{root_id}.jar"), encoding="utf-8"
            )
            (source / "mods" / "shared.pw.toml").write_text(
                metadata("modrinth", "shared", "shared.jar"), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project", side_effect=self.fake_create),
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            report = self.create()

        self.assertEqual(len({path.parent for path in resolver_directories}), 3)
        self.assertEqual(report.installed, ("Client One", "Server", "Client Two"))
        mods = self.packs / "generated" / "source" / "mods"
        self.assertEqual(packctl.read_toml(mods / "client-one.pw.toml")["side"], "client")
        self.assertEqual(packctl.read_toml(mods / "server.pw.toml")["side"], "server")
        self.assertEqual(packctl.read_toml(mods / "client-two.pw.toml")["side"], "client")
        self.assertEqual(packctl.read_toml(mods / "shared.pw.toml")["side"], "both")
        client = self.packs / "generated" / "client-test"
        server = self.packs / "generated" / "server-test"
        with patch.object(packctl, "run"):
            packctl.build_target(self.packs / "generated", "client", client)
            packctl.build_target(self.packs / "generated", "server", server)
        self.assertFalse((server / "mods" / "client-one.pw.toml").exists())
        self.assertFalse((client / "mods" / "server.pw.toml").exists())
        self.assertTrue((client / "mods" / "shared.pw.toml").exists())
        self.assertTrue((server / "mods" / "shared.pw.toml").exists())

    def test_requested_root_is_distinguished_from_dependency_identity(self) -> None:
        self.write_template(
            "  - name: Requested\n    provider: modrinth\n"
            "    project_id: requested\n    side: both\n"
        )

        def fake_run(command, *, cwd=None, **kwargs):
            if command[-1] != "refresh":
                source = Path(cwd)
                (source / "mods" / "wrong.pw.toml").write_text(
                    metadata("modrinth", "dependency", "dependency.jar"),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project", side_effect=self.fake_create),
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            report = self.create()
        self.assertEqual(report.installed, ())
        self.assertEqual(len(report.failed), 1)
        self.assertIn("Canonical root identity", report.failed[0].reason)
        self.assertEqual(report.retained, ())

    def test_composed_exact_root_selector_is_resolved_only_once(self) -> None:
        addon = self.templates / "addon"
        addon.mkdir()
        manifest_mod = (
            "  - name: Shared Root\n    provider: modrinth\n"
            "    project_id: shared-root\n"
        )
        self.write_template(manifest_mod + "    side: client\n")
        (addon / "template.yaml").write_text(
            "id: addon\ndisplay_name: Addon\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.234\n"
            + "mods:\n"
            + manifest_mod
            + "    side: server\n",
            encoding="utf-8",
        )
        calls = 0

        def fake_run(command, *, cwd=None, **kwargs):
            nonlocal calls
            if command[-1] != "refresh":
                calls += 1
                (Path(cwd) / "mods" / "shared-root.pw.toml").write_text(
                    metadata("modrinth", "shared-root", "shared-root.jar"),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project", side_effect=self.fake_create),
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            report = self.create(template_ids=["base", "addon"])
        self.assertEqual(calls, 1)
        self.assertEqual(report.installed, ("Shared Root",))
        retained_path = self.packs / "generated" / "source" / report.retained[0].relative_path
        self.assertEqual(packctl.read_toml(retained_path)["side"], "both")

    def test_manifest_change_during_resolution_aborts_before_destination(self) -> None:
        self.write_template(
            "  - name: Root\n    provider: modrinth\n"
            "    project_id: root\n    side: both\n"
        )

        def fake_run(command, *, cwd=None, **kwargs):
            (Path(cwd) / "mods" / "root.pw.toml").write_text(
                metadata("modrinth", "root", "root.jar"), encoding="utf-8"
            )
            with (self.template / "template.yaml").open("a", encoding="utf-8") as manifest:
                manifest.write("  - name: Added\n    provider: modrinth\n")
                manifest.write("    project_id: added\n    side: both\n")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project") as create,
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "changed during resolver"):
                self.create()
        create.assert_not_called()
        self.assertFalse((self.packs / "generated").exists())

    def test_path_collision_in_multi_source_conflict_requires_reselection(self) -> None:
        self.write_template(
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: first\n    side: both\n"
            "  - name: Same\n    provider: curseforge\n"
            "    project_id: '2'\n    side: both\n"
        )
        composition = core.prepare_template_composition(
            template_ids=["base"], minecraft="1.21.1", loader="neoforge"
        )
        conflict = composition.conflicts[0]
        resolution = {
            conflict.key: core.ConflictResolution(
                tuple(item.candidate_key for item in conflict.candidates), True
            )
        }

        def fake_run(command, *, cwd=None, **kwargs):
            source = Path(cwd)
            if command[-1] == "first":
                text = metadata("modrinth", "first", "first.jar")
            else:
                text = metadata("curseforge", "2", "second.jar")
            (source / "mods" / "same.pw.toml").write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project") as create,
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "path collision.*Re-select"):
                self.create(conflict_resolutions=resolution)
        create.assert_not_called()
        self.assertFalse((self.packs / "generated").exists())

    def test_no_change_candidate_in_multi_source_conflict_requires_reselection(self) -> None:
        self.write_template(
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: first\n    side: both\n"
            "  - name: Same\n    provider: curseforge\n"
            "    project_id: '2'\n    side: both\n"
        )
        composition = core.prepare_template_composition(
            template_ids=["base"], minecraft="1.21.1", loader="neoforge"
        )
        conflict = composition.conflicts[0]
        resolution = {
            conflict.key: core.ConflictResolution(
                tuple(item.candidate_key for item in conflict.candidates), True
            )
        }

        def fake_run(command, *, cwd=None, **kwargs):
            if command[-1] == "first":
                (Path(cwd) / "mods" / "first.pw.toml").write_text(
                    metadata("modrinth", "first", "first.jar"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project") as create,
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "No metadata.*Re-select"):
                self.create(conflict_resolutions=resolution)
        create.assert_not_called()

    def test_filename_collision_fails_only_later_ordinary_root(self) -> None:
        self.write_template(
            "  - name: First\n    provider: modrinth\n"
            "    project_id: first\n    side: both\n"
            "  - name: Second\n    provider: modrinth\n"
            "    project_id: second\n    side: both\n"
        )

        def fake_run(command, *, cwd=None, **kwargs):
            if command[-1] != "refresh":
                root_id = command[-1]
                (Path(cwd) / "mods" / f"{root_id}.pw.toml").write_text(
                    metadata("modrinth", root_id, "collision.jar"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project", side_effect=self.fake_create),
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            report = self.create()
        self.assertEqual(report.installed, ("First",))
        self.assertEqual(len(report.failed), 1)
        self.assertIn("filename collision", report.failed[0].reason)
        self.assertEqual(len(report.retained), 1)

    def test_dependency_version_divergence_rejects_later_ordinary_root_in_report(self) -> None:
        self.write_template(
            "  - name: First\n    provider: modrinth\n"
            "    project_id: first\n    side: both\n"
            "  - name: Second\n    provider: modrinth\n"
            "    project_id: second\n    side: both\n"
        )

        def fake_run(command, *, cwd=None, **kwargs):
            if command[-1] != "refresh":
                root_id = command[-1]
                source = Path(cwd)
                (source / "mods" / f"{root_id}.pw.toml").write_text(
                    metadata("modrinth", root_id, f"{root_id}.jar"), encoding="utf-8"
                )
                (source / "mods" / "shared.pw.toml").write_text(
                    metadata(
                        "modrinth", "shared", f"shared-{root_id}.jar",
                        version="v1" if root_id == "first" else "v2",
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(
            core, "create_project", side_effect=self.fake_create
        ), patch.object(core.subprocess, "run", side_effect=fake_run):
            report = self.create()
        self.assertEqual(report.installed, ("First",))
        self.assertEqual(len(report.failed), 1)
        self.assertIn("metadata disagreement", report.failed[0].reason)
        self.assertEqual(tuple(item.name for item in report.retained), ("First",))

    def test_dependency_divergence_for_retained_conflict_requires_reselection(self) -> None:
        self.write_template(
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: first\n    side: both\n"
            "  - name: Same\n    provider: curseforge\n"
            "    project_id: '2'\n    side: both\n"
        )
        composition = core.prepare_template_composition(
            template_ids=["base"], minecraft="1.21.1", loader="neoforge"
        )
        conflict = composition.conflicts[0]
        resolution = {
            conflict.key: core.ConflictResolution(
                tuple(item.candidate_key for item in conflict.candidates), True
            )
        }

        def fake_run(command, *, cwd=None, **kwargs):
            source = Path(cwd)
            root_id = command[-1]
            provider = "modrinth" if root_id == "first" else "curseforge"
            (source / "mods" / f"{root_id}.pw.toml").write_text(
                metadata(provider, root_id, f"{root_id}.jar"), encoding="utf-8"
            )
            (source / "mods" / "shared.pw.toml").write_text(
                metadata("modrinth", "shared", "shared.jar", version=root_id),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(core, "create_project") as create, patch.object(
            core.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaisesRegex(
                core.HuroshikiError, "metadata disagreement.*Re-select"
            ):
                self.create(conflict_resolutions=resolution)
        create.assert_not_called()

    def test_portable_path_and_filename_collisions_and_windows_names_fail(self) -> None:
        cases = (
            (("A.pw.toml", "a.pw.toml"), ("one.jar", "two.jar"), "path collision"),
            (("one.pw.toml", "two.pw.toml"), ("Same.jar", "same.jar"), "filename collision"),
        )
        for paths, filenames, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                (source / "mods").mkdir()
                for index, (name, filename) in enumerate(zip(paths, filenames), start=1):
                    (source / "mods" / name).write_text(
                        metadata("modrinth", str(index), filename), encoding="utf-8"
                    )
                with self.assertRaisesRegex(core.HuroshikiError, message):
                    core._read_resolver_metadata(source, "both")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "mods").mkdir()
            (source / "mods" / "CON.pw.toml").write_text(
                metadata("modrinth", "one", "valid.jar"), encoding="utf-8"
            )
            with self.assertRaisesRegex(core.HuroshikiError, "reserved Windows"):
                core._read_resolver_metadata(source, "both")

    def test_compatible_dual_candidates_have_final_retained_records(self) -> None:
        self.write_template(
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: first\n    side: client\n"
            "  - name: Same\n    provider: curseforge\n"
            "    project_id: '2'\n    side: server\n"
        )
        composition = core.prepare_template_composition(
            template_ids=["base"], minecraft="1.21.1", loader="neoforge"
        )
        conflict = composition.conflicts[0]
        resolution = {
            conflict.key: core.ConflictResolution(
                tuple(item.candidate_key for item in conflict.candidates), True
            )
        }

        def fake_run(command, *, cwd=None, **kwargs):
            source = Path(cwd)
            if command[-1] == "refresh":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[-1] == "first":
                relative = "first.pw.toml"
                text = metadata("modrinth", "first", "first.jar")
            else:
                relative = "second.pw.toml"
                text = metadata("curseforge", "2", "second.jar")
            (source / "mods" / relative).write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(core, "create_project", side_effect=self.fake_create),
            patch.object(core.subprocess, "run", side_effect=fake_run),
        ):
            report = self.create(conflict_resolutions=resolution)
        self.assertEqual(len(report.retained), 2)
        self.assertEqual(
            {(item.actual_provider, item.actual_project_id) for item in report.retained},
            {("modrinth", "first"), ("curseforge", "2")},
        )
        for item in report.retained:
            self.assertTrue(
                (self.packs / "generated" / "source" / item.relative_path).is_file()
            )
        self.assertIn("Retained template candidates:", report.warning_lines)

    def test_url_id_and_metadata_path_collision_requires_reselection(self) -> None:
        self.write_template(
            "  - name: Same URL Root\n    provider: url\n"
            "    project_id: same-id\n"
            "    url: https://example.test/a.jar\n    side: both\n"
            "  - name: Same URL Root\n    provider: url\n"
            "    project_id: same-id\n"
            "    url: https://example.test/b.jar\n    side: both\n"
        )
        composition = core.prepare_template_composition(
            template_ids=["base"], minecraft="1.21.1", loader="neoforge"
        )
        conflict = composition.conflicts[0]
        keys = tuple(item.candidate_key for item in conflict.candidates)
        self.assertIn(
            "Re-select A or B",
            core.conflict_multi_selection_error(conflict, keys) or "",
        )
        resolution = {conflict.key: core.ConflictResolution(keys, True)}
        artifacts = [
            UrlArtifact("A", "same-id", "1", "a.jar", "https://example.test/a.jar", "00", ("neoforge",)),
            UrlArtifact("B", "same-id", "1", "b.jar", "https://example.test/b.jar", "11", ("neoforge",)),
        ]
        with (
            patch.object(core, "create_project") as create,
            patch.object(core, "download_url_artifact", side_effect=artifacts),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "URL MOD ID/path collision.*Re-select"):
                self.create(conflict_resolutions=resolution)
        create.assert_not_called()

    def test_url_root_remains_bounded_and_has_no_dependency_closure(self) -> None:
        self.write_template(
            "  - name: URL Root\n    provider: url\n"
            "    project_id: url-root\n"
            "    url: https://example.test/url-root.jar\n"
            "    side: client\n"
        )
        artifact = UrlArtifact(
            "URL Root",
            "url-root",
            "1.0",
            "url-root.jar",
            "https://example.test/url-root.jar",
            "00",
            ("neoforge",),
        )
        with (
            patch.object(core, "create_project", side_effect=self.fake_create),
            patch.object(core, "download_url_artifact", return_value=artifact) as download,
            patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
        ):
            report = self.create()
        self.assertEqual(report.installed, ("URL Root",))
        self.assertEqual(download.call_args.args[4], 256 * 1024 * 1024)
        self.assertEqual(run.call_count, 1)
        metadata_files = list((self.packs / "generated" / "source").rglob("*.pw.toml"))
        self.assertEqual([path.name for path in metadata_files], ["url-root.pw.toml"])
        self.assertEqual(packctl.read_toml(metadata_files[0])["side"], "client")


if __name__ == "__main__":
    unittest.main()
