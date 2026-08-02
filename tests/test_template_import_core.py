from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from pack_migration_roots import PackRootRecord, write_pack_root_manifest
from template_import import ImportConflictResolution, resolve_template_import_plan


PACK_TOML = """name = "Demo"
pack-format = "packwiz:1.1.0"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.1"
"""
SHA256 = "0" * 64
BAD_SHA256 = "1" * 64


def metadata(
    name: str,
    project_id: str,
    filename: str,
    *,
    provider: str = "modrinth",
    side: str = "both",
    hash_value: str = SHA256,
) -> bytes:
    update = (
        f'[update.modrinth]\nmod-id = "{project_id}"\nversion = "1"'
        if provider == "modrinth"
        else f"[update.curseforge]\nproject-id = {project_id}"
    )
    return f'''name = "{name}"
filename = "{filename}"
side = "{side}"
[download]
url = "https://cdn.example/{filename}"
hash-format = "sha256"
hash = "{hash_value}"
{update}
    '''.encode()


def url_metadata(name: str, filename: str, url: str) -> bytes:
    return f'''name = "{name}"
filename = "{filename}"
side = "both"
[download]
url = "{url}"
hash-format = "sha256"
hash = "{SHA256}"
'''.encode()


class TemplateImportCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack = self.packs / "demo"
        self.source = self.pack / "source"
        self.template = self.templates / "base"
        self.source.mkdir(parents=True)
        self.template.mkdir(parents=True)
        (self.pack / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        (self.source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (self.source / "index.toml").write_text("hash-format = \"sha256\"\n", encoding="utf-8")
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Root\n    provider: modrinth\n"
            "    project_id: root\n    side: client\n",
            encoding="utf-8",
        )
        (self.pack / "content" / "common").mkdir(parents=True)
        (self.pack / "content" / "common" / "keep.txt").write_text("keep")
        self.patches = (
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def closure(self) -> core.ResolvedModClosure:
        records = (
            core.ResolvedMetadata(
                ("modrinth", "root"),
                Path("mods/root.pw.toml"),
                "root.jar",
                metadata("Root", "root", "root.jar"),
                "modrinth",
                "root",
            ),
            core.ResolvedMetadata(
                ("modrinth", "dependency"),
                Path("mods/dependency.pw.toml"),
                "dependency.jar",
                metadata("Dependency", "dependency", "dependency.jar"),
                "modrinth",
                "dependency",
            ),
        )
        return core.ResolvedModClosure(("modrinth", "root"), records)

    def url_closure(self, actual_id: str = "actual") -> core.ResolvedModClosure:
        record = core.ResolvedMetadata(
            ("url", actual_id),
            Path(f"mods/{actual_id}.pw.toml"),
            f"{actual_id}.jar",
            url_metadata(
                "Actual Root",
                f"{actual_id}.jar",
                "https://mods.example/requested.jar",
            ),
            "url",
            actual_id,
        )
        return core.ResolvedModClosure(("url", actual_id), (record,))

    def single_closure(
        self,
        provider: str,
        project_id: str,
        name: str,
    ) -> core.ResolvedModClosure:
        record = core.ResolvedMetadata(
            (provider, project_id),
            Path(f"mods/{project_id}.pw.toml"),
            f"{project_id}.jar",
            metadata(name, project_id, f"{project_id}.jar"),
            provider,
            project_id,
        )
        return core.ResolvedModClosure((provider, project_id), (record,))

    def use_url_template(self) -> None:
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Requested\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/requested.jar\n",
            encoding="utf-8",
        )

    def use_url_conflict_template(self) -> None:
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Good\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/good.jar\n"
            "  - name: Bad\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/bad.jar\n",
            encoding="utf-8",
        )

    def use_failed_replacement_with_root_template(self) -> None:
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Failed Replacement\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/requested.jar\n"
            "  - name: Root\n    provider: modrinth\n"
            "    project_id: root\n    side: client\n",
            encoding="utf-8",
        )

    def install_logical_url(self) -> None:
        mods = self.source / "mods"
        mods.mkdir(exist_ok=True)
        (mods / "logical.pw.toml").write_bytes(
            url_metadata(
                "Installed Logical",
                "logical.jar",
                "https://mods.example/requested.jar",
            )
        )

    def operation(self) -> core.TemplateImportOperation:
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        resolved = resolve_template_import_plan(session.plan)
        return core.TemplateImportOperation(session, resolved)

    @staticmethod
    def refresh_ok(command: list[str], **_: object) -> core.ResolverProcessResult:
        return core.ResolverProcessResult(0, "", "", False, False)

    def test_dry_run_classifies_root_dependency_and_preserves_real_pack(self) -> None:
        before = core.tree_digest_snapshot(self.source)
        overlay = (self.pack / "content" / "common" / "keep.txt").read_bytes()
        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            [item.actual_identity for item in operation.preview.added_roots],
            [("modrinth", "root")],
        )
        self.assertEqual(
            [item.project_id for item in operation.preview.added_dependencies],
            ["dependency"],
        )
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        self.assertEqual((self.pack / "content" / "common" / "keep.txt").read_bytes(), overlay)
        operation.discard()
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_apply_publishes_complete_closure_atomically(self) -> None:
        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
            operation.apply()
        self.assertTrue((self.source / "mods/root.pw.toml").is_file())
        self.assertTrue((self.source / "mods/dependency.pw.toml").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_removed_pack_identity_required_by_root_fails_before_preview(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "dependency.pw.toml").write_bytes(
            metadata("Root", "dependency", "dependency.jar")
        )
        before = core.tree_digest_snapshot(self.source)
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.name_conflicts[0]
        root_option = next(
            option
            for option in conflict.options
            if any(
                candidate.origin_kind == "template"
                and candidate.project_id == "root"
                for candidate in option.candidates
            )
        )
        resolved = resolve_template_import_plan(
            session.plan,
            name_resolutions={
                conflict.key: ImportConflictResolution((root_option.option_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(core, "resolve_mod_closure", return_value=self.closure()):
            operation.run()
        self.assertIsInstance(operation.error, core.HuroshikiError)
        self.assertIn(
            "modrinth:dependency required by modrinth:root",
            str(operation.error),
        )
        self.assertIsNone(operation.preview)
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        self.assertTrue((self.source / "mods/dependency.pw.toml").is_file())
        self.assertFalse((self.source / "mods/root.pw.toml").exists())
        self.assertTrue(operation.transaction.root.is_dir())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_removed_requirement_reports_every_requiring_root(self) -> None:
        removed = core.ModCandidate(
            "pack",
            "demo",
            "Dependency",
            "modrinth",
            "dependency",
            "both",
            metadata_path=Path("mods/dependency.pw.toml"),
            actual_provider="modrinth",
            actual_project_id="dependency",
        )
        roots = tuple(
            core.ModCandidate(
                "template",
                "base",
                name,
                "modrinth",
                project_id,
                "both",
                actual_provider="modrinth",
                actual_project_id=project_id,
            )
            for name, project_id in (("One", "one"), ("Two", "two"))
        )
        dependency = core.ResolvedMetadata(
            ("modrinth", "dependency"),
            Path("mods/dependency.pw.toml"),
            "dependency.jar",
            metadata("Dependency", "dependency", "dependency.jar"),
            "modrinth",
            "dependency",
        )
        resolved_roots = tuple(
            (
                root,
                core.ResolvedModClosure(
                    root.actual_identity,
                    (
                        core.ResolvedMetadata(
                            root.actual_identity,
                            Path(f"mods/{root.project_id}.pw.toml"),
                            f"{root.project_id}.jar",
                            metadata(root.name, root.project_id, f"{root.project_id}.jar"),
                            "modrinth",
                            root.project_id,
                        ),
                        dependency,
                    ),
                ),
            )
            for root in roots
        )
        requirements = core._removed_identity_requirements(
            resolved_roots,
            (removed,),
        )
        self.assertEqual(
            [root.candidate_key for root in requirements[("modrinth", "dependency")]],
            ["modrinth:one", "modrinth:two"],
        )

    def test_refresh_reintroduction_is_rejected_by_staged_postcondition(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "shared.pw.toml").write_bytes(
            metadata("Same", "shared", "shared.jar")
        )
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: alternate\n    side: both\n",
            encoding="utf-8",
        )
        before = core.tree_digest_snapshot(self.source)
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.name_conflicts[0]
        alternate = next(
            option
            for option in conflict.options
            if any(candidate.origin_kind == "template" for candidate in option.candidates)
        )
        resolved = resolve_template_import_plan(
            session.plan,
            name_resolutions={
                conflict.key: ImportConflictResolution((alternate.option_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)

        def refresh_reintroduces(_command: list[str], **kwargs: object):
            source = Path(kwargs["cwd"])
            (source / "mods/shared.pw.toml").write_bytes(
                metadata("Same", "shared", "shared.jar")
            )
            return self.refresh_ok([])

        with (
            patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.single_closure("modrinth", "alternate", "Same"),
            ),
            patch.object(
                core,
                "run_resolver_process",
                side_effect=refresh_reintroduces,
            ),
        ):
            operation.run()
        self.assertIn("reintroduced Pack MODs", str(operation.error))
        self.assertIsNone(operation.preview)
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        self.assertTrue(operation.transaction.root.is_dir())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_cli_dependency_reintroduction_returns_one_without_preview(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "dependency.pw.toml").write_bytes(
            metadata("Root", "dependency", "dependency.jar")
        )
        before = core.tree_digest_snapshot(self.source)
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.name_conflicts[0]
        root_option = next(
            option
            for option in conflict.options
            if any(candidate.origin_kind == "template" for candidate in option.candidates)
        )
        resolution = self.root / "reintroduction-resolution.yaml"
        resolution.write_text(
            "version: 4\n"
            f'plan_digest: "{session.plan.plan_digest}"\n'
            "name_conflicts:\n"
            f'  "{conflict.key}":\n'
            "    options:\n"
            f'      - "{root_option.option_key}"\n'
            "    acknowledge_duplicate_risk: false\n"
            "url_selector_conflicts: {}\n"
            "logical_identity_conflicts: {}\n"
            "actual_identity_conflicts: {}\n"
            "side_conflicts: {}\n",
            encoding="utf-8",
        )
        session.discard()
        args = packctl.parser().parse_args(
            [
                "apply-template",
                "demo",
                "base",
                "--resolution",
                str(resolution),
                "--json",
            ]
        )
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(packctl.cmd_apply_template(args), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("required by modrinth:root", stderr.getvalue())
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_preview_removed_and_unchanged_are_resolution_disjoint(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "shared.pw.toml").write_bytes(
            metadata("Same", "shared", "shared.jar")
        )
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: shared\n    side: both\n"
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: alternate\n    side: both\n",
            encoding="utf-8",
        )
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.name_conflicts[0]
        alternate = next(
            option
            for option in conflict.options
            if any(candidate.project_id == "alternate" for candidate in option.candidates)
        )
        resolved = resolve_template_import_plan(
            session.plan,
            name_resolutions={
                conflict.key: ImportConflictResolution((alternate.option_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        with (
            patch.object(
                core,
                "resolve_mod_closure",
                return_value=self.single_closure("modrinth", "alternate", "Same"),
            ),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            [candidate.project_id for candidate in operation.preview.removed],
            ["shared"],
        )
        self.assertEqual(operation.preview.unchanged, ())
        self.assertFalse(
            {candidate.selection_key for candidate in operation.preview.removed}
            & {candidate.selection_key for candidate in operation.preview.unchanged}
        )
        operation.discard()

    def test_retained_equivalent_source_is_reported_unchanged(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "shared.pw.toml").write_bytes(
            metadata("Shared", "shared", "shared.jar")
        )
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Shared\n    provider: modrinth\n"
            "    project_id: shared\n    side: both\n",
            encoding="utf-8",
        )
        operation = self.operation()
        with patch.object(
            core, "run_resolver_process", side_effect=self.refresh_ok
        ):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            [candidate.project_id for candidate in operation.preview.unchanged],
            ["shared"],
        )
        self.assertEqual(operation.preview.removed, ())
        operation.discard()

    def test_cross_provider_dependency_collision_merges_once_with_side_union(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "existing-root.pw.toml").write_bytes(
            metadata("Existing Root", "existing-root", "existing-root.jar", provider="modrinth")
        )
        (mods / "shared.pw.toml").write_bytes(
            metadata("Shared", "202", "shared.jar", provider="curseforge", side="client")
        )
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "existing-root", "client"),),
        )
        self.template.joinpath("template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\nreference_loader_version: 21.1.0\nmods:\n"
            "  - name: Root\n    provider: modrinth\n    project_id: root\n    side: server\n",
            encoding="utf-8",
        )
        incoming = core.ResolvedModClosure(
            ("modrinth", "root"),
            (
                core.ResolvedMetadata(
                    ("modrinth", "root"), Path("mods/root.pw.toml"), "root.jar",
                    metadata("Root", "root", "root.jar"), "modrinth", "root",
                ),
                core.ResolvedMetadata(
                    ("modrinth", "equivalent-dependency"), Path("mods/shared.pw.toml"),
                    "shared.jar", metadata("Shared", "equivalent-dependency", "shared.jar", side="server"),
                    "modrinth", "equivalent-dependency",
                ),
            ),
        )
        operation = self.operation()
        with patch.object(core, "resolve_mod_closure", return_value=incoming), patch.object(
            core, "run_resolver_process", side_effect=self.refresh_ok
        ):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(len(list(operation.transaction.source.glob("mods/*.pw.toml"))), 3)
        self.assertIn('side = "both"', (operation.transaction.source / "mods/shared.pw.toml").read_text())
        roots = {(item.provider, item.project_id) for item in core.read_pack_root_manifest(operation.transaction.source)}
        self.assertEqual(roots, {("modrinth", "existing-root"), ("modrinth", "root")})
        operation.discard()

    def test_cross_provider_version_mismatch_fails_closed_without_preview(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "existing-root.pw.toml").write_bytes(
            metadata("Existing Root", "existing-root", "existing-root.jar")
        )
        (mods / "shared.pw.toml").write_bytes(
            metadata("Shared", "202", "shared.jar", provider="curseforge")
        )
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "existing-root", "client"),),
        )
        incoming = core.ResolvedModClosure(
            ("modrinth", "root"),
            (
                core.ResolvedMetadata(
                    ("modrinth", "root"), Path("mods/root.pw.toml"), "root.jar",
                    metadata("Root", "root", "root.jar"), "modrinth", "root",
                ),
                core.ResolvedMetadata(
                    ("modrinth", "different-version"), Path("mods/shared.pw.toml"),
                    "shared.jar", metadata("Shared", "different-version", "shared.jar", hash_value=BAD_SHA256),
                    "modrinth", "different-version",
                ),
            ),
        )
        before = core.tree_digest_snapshot(self.source)
        operation = self.operation()
        with patch.object(core, "resolve_mod_closure", return_value=incoming):
            operation.run()
        self.assertIsNotNone(operation.error)
        self.assertIsNone(operation.preview)
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        operation.discard()

    def test_side_changed_candidate_is_not_reported_unchanged(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        installed = metadata("Shared", "shared", "shared.jar").replace(
            b'side = "both"', b'side = "client"'
        )
        (mods / "shared.pw.toml").write_bytes(installed)
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Shared\n    provider: modrinth\n"
            "    project_id: shared\n    side: server\n",
            encoding="utf-8",
        )
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        resolved = resolve_template_import_plan(
            session.plan,
            side_decisions={("modrinth", "shared"): "use_template"},
        )
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(
            core, "run_resolver_process", side_effect=self.refresh_ok
        ):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(operation.preview.unchanged, ())
        self.assertEqual(
            operation.preview.side_changes,
            ((('modrinth', 'shared'), 'client', 'server'),),
        )
        operation.discard()

    def test_resolver_refresh_and_template_change_fail_closed(self) -> None:
        for failure in ("resolver", "refresh"):
            with self.subTest(failure=failure):
                operation = self.operation()
                resolver = (
                    patch.object(core, "resolve_mod_closure", side_effect=core.HuroshikiError("resolver failed"))
                    if failure == "resolver"
                    else patch.object(core, "resolve_mod_closure", return_value=self.closure())
                )
                refresh = core.ResolverProcessResult(1, "", "failed", False, False)
                with resolver, patch.object(core, "run_resolver_process", return_value=refresh):
                    operation.run()
                self.assertIsNotNone(operation.error)
                self.assertFalse((self.source / "mods").exists())
                self.assertFalse(packctl.project_lock_is_active("pack:demo"))

        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        path = self.template / "template.yaml"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(core.HuroshikiError, "Template manifest changed"):
            operation.apply()
        self.assertFalse((self.source / "mods").exists())

    def test_cli_conflict_without_resolution_fails_without_transaction(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "installed.pw.toml").write_bytes(
            metadata("Root", "installed", "installed.jar")
        )
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "templates": ["base"],
                "resolution": None,
                "apply": False,
                "json": False,
            },
        )()
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(packctl.cmd_apply_template(args), 2)
        self.assertIn("resolution file", stderr.getvalue())
        self.assertIn("version: 4", stderr.getvalue())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

        args.json = True
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(packctl.cmd_apply_template(args), 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["plan_digest"], core.prepare_template_import_plan(
            "pack:demo", ["base"]
        ).plan_digest)
        self.assertEqual(payload["conflicts"]["name"][0]["key"], "root")
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_resolution_digest_and_cli_parser_fail_closed(self) -> None:
        plan = core.prepare_template_import_plan("pack:demo", ["base"])
        resolution = self.root / "resolution.yaml"
        resolution.write_text(
            "version: 4\nplan_digest: stale\nname_conflicts: {}\nside_conflicts: {}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "stale plan digest"):
            packctl._template_import_resolution(resolution, plan)
        resolution.write_text(
            "version: 1\nplan_digest: stale\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "no longer supported"):
            packctl._template_import_resolution(resolution, plan)
        resolution.write_text(
            "version: 2\nplan_digest: stale\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "version 2.*no longer"):
            packctl._template_import_resolution(resolution, plan)
        resolution.write_text(
            "version: 3\nplan_digest: stale\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "version 3.*no longer"):
            packctl._template_import_resolution(resolution, plan)
        args = packctl.parser().parse_args(
            ["apply-template", "demo", "base", "--apply", "--json"]
        )
        self.assertIs(args.func, packctl.cmd_apply_template)
        self.assertEqual(args.templates, ["base"])
        self.assertTrue(args.apply)

    def test_session_uses_transaction_source_as_only_pack_plan_source(self) -> None:
        original = core.pack_import_candidates
        observed: list[Path] = []

        def inspect_source(source: Path, pack_id: str):
            observed.append(source)
            self.assertNotEqual(source, self.source)
            self.assertTrue(packctl.project_lock_is_active("pack:demo"))
            return original(source, pack_id)

        with patch.object(core, "pack_import_candidates", side_effect=inspect_source):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertEqual(observed, [session.transaction.source])
        session.discard()

    def test_external_pack_change_after_session_creation_blocks_apply(self) -> None:
        original = core.pack_import_candidates

        def mutate_real_pack(source: Path, pack_id: str):
            mods = self.source / "mods"
            mods.mkdir(exist_ok=True)
            (mods / "late.pw.toml").write_bytes(metadata("Late", "late", "late.jar"))
            return original(source, pack_id)

        with patch.object(
            core,
            "pack_import_candidates",
            side_effect=mutate_real_pack,
        ):
            operation = self.operation()
        self.assertEqual(operation.plan.pack_candidates, ())
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
            operation.apply()
        self.assertTrue((self.source / "mods/late.pw.toml").is_file())

    def test_external_config_change_after_preview_blocks_apply(self) -> None:
        operation = self.operation()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
        (self.pack / "pack.local.yaml").write_text(
            "url_max_jar_size_bytes: 1024\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(core.HuroshikiError, "configuration changed"):
            operation.apply()
        self.assertFalse((self.source / "mods").exists())

    def test_failed_session_creation_releases_project_lock(self) -> None:
        with patch.object(
            core,
            "pack_import_candidates",
            side_effect=core.HuroshikiError("failed"),
        ), self.assertRaisesRegex(core.HuroshikiError, "failed"):
            core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_url_policy_identity_and_cached_closure_reach_preview(self) -> None:
        self.use_url_template()
        (self.template / "template.local.yaml").write_text(
            "url_max_jar_size_bytes: 1234\n"
            "url_allow_private_networks: true\n",
            encoding="utf-8",
        )
        closure = self.url_closure()
        with patch.object(core, "resolve_mod_closure", return_value=closure) as resolver:
            session = core.TemplateImportSession.create("pack:demo", ["base"])
            resolved = resolve_template_import_plan(session.plan)
            operation = core.TemplateImportOperation(session, resolved)
            with patch.object(
                core, "run_resolver_process", side_effect=self.refresh_ok
            ):
                operation.run()
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(resolver.call_args.kwargs["url_max_jar_size_bytes"], 1234)
        self.assertTrue(resolver.call_args.kwargs["url_allow_private_networks"])
        self.assertEqual(
            operation.preview.added_roots[0].requested_identity,
            ("url", "logical"),
        )
        self.assertEqual(
            operation.preview.added_roots[0].selection_key,
            session.plan.template_candidates[0].selection_key,
        )
        self.assertEqual(
            operation.preview.added_roots[0].actual_identity,
            ("url", "actual"),
        )
        self.assertEqual(operation.preview.added_dependencies, ())
        operation.discard()

    def test_actual_identity_change_invalidates_resolution(self) -> None:
        self.use_url_template()
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("first")
        ):
            first = core.TemplateImportSession.create("pack:demo", ["base"])
        first_digest = first.plan.plan_digest
        first.discard()
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("second")
        ):
            second = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertNotEqual(first_digest, second.plan.plan_digest)
        second.discard()

    def test_removal_rechecks_actual_identity_before_unlink(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        path = mods / "installed.pw.toml"
        path.write_bytes(metadata("Installed", "installed", "installed.jar"))
        transaction = core.PackTransaction.create("pack:demo")
        candidate = core.pack_import_candidates(transaction.source, "demo")[0]
        staged = transaction.source / candidate.metadata_path
        staged.write_bytes(metadata("Changed", "changed", "changed.jar"))
        with self.assertRaisesRegex(core.HuroshikiError, "changed before removal"):
            core._remove_import_candidates(transaction.source, (candidate,))
        transaction.discard()

    def test_failed_unselected_url_candidate_does_not_block_session(self) -> None:
        self.use_url_conflict_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=(
                self.url_closure("good_actual"),
                core.UrlCandidateVerificationError("HTTP 404"),
            ),
        ) as resolver:
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(
            [item.succeeded for item in session.plan.verifications],
            [True, False],
        )
        payload = packctl._template_import_conflict_payload(session.plan)
        self.assertEqual(
            [
                option["candidates"][0]["status"]
                for option in payload["url_selector"][0]["options"]
            ],
            ["verified", "failed"],
        )
        self.assertEqual(
            payload["url_selector"][0]["options"][1]["candidates"][0]["error"],
            "HTTP 404",
        )
        self.assertTrue(
            payload["url_selector"][0]["options"][0]["candidates"][0][
                "selection_key"
            ].startswith("template:")
        )
        self.assertEqual(
            payload["url_selector"][0]["options"][0]["candidates"][0][
                "origin_kind"
            ],
            "template",
        )
        good, bad = session.plan.template_candidates
        resolved = resolve_template_import_plan(
            session.plan,
            url_selector_resolutions={
                "url:logical": ImportConflictResolution((good.selection_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(core, "run_resolver_process", side_effect=self.refresh_ok):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            operation.preview.added_roots[0].actual_identity,
            ("url", "good_actual"),
        )
        self.assertNotIn(bad.candidate_key, [
            item.candidate_key for item in resolved.selected_template_candidates
        ])
        operation.discard()

    def test_selecting_failed_url_candidate_is_rejected(self) -> None:
        self.use_url_conflict_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=(
                self.url_closure("good_actual"),
                core.UrlCandidateVerificationError("HTTP 404"),
            ),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        _good, bad = session.plan.template_candidates
        with self.assertRaisesRegex(core.TemplateMergeError, "HTTP 404"):
            resolve_template_import_plan(
                session.plan,
                url_selector_resolutions={
                    "url:logical": ImportConflictResolution((bad.selection_key,))
                },
            )
        session.discard()
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_failed_non_conflicting_candidate_blocks_operation(self) -> None:
        self.use_url_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.UrlCandidateVerificationError("invalid JAR"),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(session.plan.requires_resolution)
        with self.assertRaisesRegex(core.TemplateMergeError, "invalid JAR"):
            resolve_template_import_plan(session.plan)
        session.discard()

    def test_failed_logical_replacement_can_keep_pack_and_import_other_root(self) -> None:
        self.install_logical_url()
        self.use_failed_replacement_with_root_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.UrlCandidateVerificationError("HTTP 404"),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.logical_identity_conflicts[0]
        installed = conflict.pack_candidate
        failed = conflict.template_candidates[0]
        self.assertEqual(installed.candidate_key, failed.candidate_key)
        self.assertNotEqual(installed.selection_key, failed.selection_key)
        resolved = resolve_template_import_plan(
            session.plan,
            logical_identity_resolutions={
                conflict.key: ImportConflictResolution((installed.selection_key,))
            },
        )
        self.assertNotIn(failed, resolved.selected_template_candidates)
        self.assertEqual(resolved.removed_pack_candidates, ())
        operation = core.TemplateImportOperation(session, resolved)
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
        ):
            operation.run()
            operation.apply()
        self.assertTrue((self.source / "mods/logical.pw.toml").is_file())
        self.assertTrue((self.source / "mods/root.pw.toml").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_failed_logical_replacement_selection_is_rejected(self) -> None:
        self.install_logical_url()
        self.use_failed_replacement_with_root_template()
        before = core.tree_digest_snapshot(self.source)
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.UrlCandidateVerificationError("HTTP 404"),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.logical_identity_conflicts[0]
        failed = conflict.template_candidates[0]
        with self.assertRaisesRegex(core.TemplateMergeError, "HTTP 404"):
            resolve_template_import_plan(
                session.plan,
                logical_identity_resolutions={
                    conflict.key: ImportConflictResolution((failed.selection_key,))
                },
            )
        session.discard()
        self.assertEqual(core.tree_digest_snapshot(self.source), before)
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_same_selector_resolution_v4_round_trip_and_validation(self) -> None:
        self.install_logical_url()
        self.use_url_template()
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("new-id")
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        conflict = session.plan.logical_identity_conflicts[0]
        installed = conflict.pack_candidate
        incoming = conflict.template_candidates[0]
        self.assertEqual(installed.candidate_key, incoming.candidate_key)
        self.assertNotEqual(installed.selection_key, incoming.selection_key)
        resolution = self.root / "resolution-v4.yaml"

        def write_selection(lines: list[str]) -> None:
            resolution.write_text(
                "version: 4\n"
                f'plan_digest: "{session.plan.plan_digest}"\n'
                "name_conflicts: {}\n"
                "url_selector_conflicts: {}\n"
                "logical_identity_conflicts:\n"
                "  \"url:logical\":\n"
                "    options:\n"
                + "".join(f'      - "{item}"\n' for item in lines)
                + "    acknowledge_duplicate_risk: false\n"
                "actual_identity_conflicts: {}\n"
                "side_conflicts: {}\n",
                encoding="utf-8",
            )

        write_selection([installed.selection_key])
        keep = packctl._template_import_resolution(resolution, session.plan)
        self.assertEqual(keep.removed_pack_candidates, ())
        write_selection([incoming.selection_key])
        replace_plan = packctl._template_import_resolution(resolution, session.plan)
        self.assertEqual(replace_plan.removed_pack_candidates, (installed,))
        self.assertEqual(replace_plan.selected_new_roots, (incoming,))
        write_selection(["template:url:unknown@https://mods.example/unknown.jar"])
        with self.assertRaisesRegex(packctl.ConfigError, "Invalid resolution"):
            packctl._template_import_resolution(resolution, session.plan)
        write_selection([installed.selection_key, installed.selection_key])
        with self.assertRaisesRegex(packctl.ConfigError, "Invalid resolution"):
            packctl._template_import_resolution(resolution, session.plan)
        resolution.write_text(
            "version: 4\n"
            f'plan_digest: "{session.plan.plan_digest}"\n'
            "name_conflicts: {}\n"
            "url_selector_conflicts: {}\n"
            "logical_identity_conflicts:\n"
            "  \"url:logical\":\n"
            "    candidates: []\n"
            "actual_identity_conflicts: {}\n"
            "side_conflicts: {}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "options must be strings"):
            packctl._template_import_resolution(resolution, session.plan)
        session.discard()
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_url_verification_cancellation_and_deadline_remain_global(self) -> None:
        self.use_url_template()
        cancelled = threading.Event()

        def cancel_then_fail(**_: object):
            cancelled.set()
            raise core.HuroshikiError("download cancelled")

        with patch.object(
            core, "resolve_mod_closure", side_effect=cancel_then_fail
        ), self.assertRaises(core.LoaderMigrationCancelled):
            core.TemplateImportSession.create(
                "pack:demo", ["base"], cancel_event=cancelled
            )
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.LoaderMigrationDeadlineExceeded("deadline"),
        ), self.assertRaises(core.LoaderMigrationDeadlineExceeded):
            core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_url_verification_state_failure_remains_global(self) -> None:
        self.use_url_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.HuroshikiError("state directory is corrupt"),
        ), self.assertRaisesRegex(core.HuroshikiError, "state directory"):
            core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_closure_metadata_change_changes_fingerprint_and_digest(self) -> None:
        self.use_url_template()
        first_closure = self.url_closure("actual")
        changed_record = first_closure.metadata[0].__class__(
            **{
                **first_closure.metadata[0].__dict__,
                "contents": first_closure.metadata[0].contents + b"\n# changed\n",
            }
        )
        second_closure = core.ResolvedModClosure(
            first_closure.root_identity,
            (changed_record,),
        )
        with patch.object(core, "resolve_mod_closure", return_value=first_closure):
            first = core.TemplateImportSession.create("pack:demo", ["base"])
        first_fingerprint = first.plan.verifications[0].closure_fingerprint
        first_digest = first.plan.plan_digest
        first.discard()
        with patch.object(core, "resolve_mod_closure", return_value=second_closure):
            second = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertNotEqual(
            first_fingerprint,
            second.plan.verifications[0].closure_fingerprint,
        )
        self.assertNotEqual(first_digest, second.plan.plan_digest)
        second.discard()

    def test_verification_success_changes_digest_from_failure(self) -> None:
        self.use_url_template()
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("actual")
        ):
            succeeded = core.TemplateImportSession.create("pack:demo", ["base"])
        success_digest = succeeded.plan.plan_digest
        succeeded.discard()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.UrlCandidateVerificationError("HTTP 404"),
        ):
            failed = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertNotEqual(success_digest, failed.plan.plan_digest)
        self.assertEqual(failed.plan.verifications[0].error, "HTTP 404")
        failed.discard()

    def test_all_failed_url_candidates_are_reported(self) -> None:
        self.use_url_conflict_template()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=(
                core.UrlCandidateVerificationError("HTTP 404"),
                core.UrlCandidateVerificationError("invalid JAR"),
            ),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertEqual(
            [item.error for item in session.plan.verifications],
            ["HTTP 404", "invalid JAR"],
        )
        session.discard()

    def test_group_option_payload_lists_every_source_origin(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "shared.pw.toml").write_bytes(
            metadata("Same", "shared", "shared.jar")
        )
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Same\n    provider: modrinth\n"
            "    project_id: shared\n    side: client\n"
            "  - name: Same\n    provider: curseforge\n"
            "    project_id: '2'\n    side: client\n",
            encoding="utf-8",
        )
        session = core.TemplateImportSession.create("pack:demo", ["base"])
        payload = packctl._template_import_conflict_payload(session.plan)
        options = payload["name"][0]["options"]
        grouped = next(option for option in options if option["option_key"].startswith("group:"))
        self.assertEqual(
            [candidate["origin_kind"] for candidate in grouped["candidates"]],
            ["pack", "template"],
        )
        self.assertEqual(
            len({candidate["selection_key"] for candidate in grouped["candidates"]}),
            2,
        )
        resolution = self.root / "group-resolution.yaml"
        resolution.write_text(
            "version: 4\n"
            f'plan_digest: "{session.plan.plan_digest}"\n'
            "name_conflicts:\n"
            "  \"same\":\n"
            "    options:\n"
            f'      - "{grouped["option_key"]}"\n'
            "    acknowledge_duplicate_risk: false\n"
            "url_selector_conflicts: {}\n"
            "logical_identity_conflicts: {}\n"
            "actual_identity_conflicts: {}\n"
            "side_conflicts: {}\n",
            encoding="utf-8",
        )
        resolved = packctl._template_import_resolution(resolution, session.plan)
        self.assertEqual(resolved.removed_pack_candidates, ())
        self.assertEqual(len(resolved.selected_option_keys), 1)
        self.assertEqual(
            resolved.selected_template_candidates[0].project_id,
            "shared",
        )
        self.assertEqual(resolved.selected_new_roots, ())
        member_key = grouped["candidates"][0]["selection_key"]
        resolution.write_text(
            resolution.read_text(encoding="utf-8").replace(
                grouped["option_key"], member_key
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "Invalid resolution"):
            packctl._template_import_resolution(resolution, session.plan)
        self.assertTrue((self.source / "mods/shared.pw.toml").is_file())
        session.discard()

    def test_multiple_failed_url_replacements_allow_pack_keep_path(self) -> None:
        self.install_logical_url()
        (self.template / "template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods:\n"
            "  - name: Failed A\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/a.jar\n"
            "  - name: Failed B\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/b.jar\n",
            encoding="utf-8",
        )
        before = core.tree_digest_snapshot(self.source)
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.UrlCandidateVerificationError("HTTP 404"),
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertEqual(len(session.plan.logical_identity_conflicts), 1)
        self.assertEqual(session.plan.url_selector_conflicts, ())
        conflict = session.plan.logical_identity_conflicts[0]
        pack_option = next(
            option
            for option in conflict.options
            if any(candidate.origin_kind == "pack" for candidate in option.candidates)
        )
        resolved = resolve_template_import_plan(
            session.plan,
            logical_identity_resolutions={
                conflict.key: ImportConflictResolution((pack_option.option_key,))
            },
        )
        self.assertEqual(resolved.selected_template_candidates, ())
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(
            core, "run_resolver_process", side_effect=self.refresh_ok
        ):
            operation.run()
        operation.discard()
        self.assertEqual(core.tree_digest_snapshot(self.source), before)

    def test_cli_json_reports_selected_options_and_root_member(self) -> None:
        args = packctl.parser().parse_args(
            ["apply-template", "demo", "base", "--json"]
        )
        output = StringIO()
        with (
            patch.object(core, "resolve_mod_closure", return_value=self.closure()),
            patch.object(core, "run_resolver_process", side_effect=self.refresh_ok),
            redirect_stdout(output),
        ):
            self.assertEqual(packctl.cmd_apply_template(args), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["selected_options"], ["template:modrinth:root"])
        self.assertEqual(
            payload["resolved_roots"][0]["selection_key"],
            "template:modrinth:root",
        )
        self.assertFalse((self.source / "mods/root.pw.toml").exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_cli_failed_non_conflicting_candidate_returns_one_and_unlocks(self) -> None:
        self.use_url_template()
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "templates": ["base"],
                "resolution": None,
                "apply": False,
                "json": False,
            },
        )()
        stderr = StringIO()
        with patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.UrlCandidateVerificationError("HTTP 404"),
        ), redirect_stderr(stderr):
            self.assertEqual(packctl.cmd_apply_template(args), 1)
        self.assertIn("HTTP 404", stderr.getvalue())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_cli_explicit_cancellation_returns_130(self) -> None:
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "templates": ["base"],
                "resolution": None,
                "apply": False,
                "json": False,
            },
        )()
        with patch.object(
            core.TemplateImportSession,
            "create",
            side_effect=core.LoaderMigrationCancelled("cancelled"),
        ), redirect_stderr(StringIO()):
            self.assertEqual(packctl.cmd_apply_template(args), 130)

    def test_logical_divergence_template_selection_replaces_atomically(self) -> None:
        self.use_url_template()
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "logical.pw.toml").write_bytes(
            url_metadata(
                "Installed",
                "old.jar",
                "https://mods.example/old.jar",
            )
        )
        with patch.object(
            core, "resolve_mod_closure", return_value=self.url_closure("actual-new")
        ):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        incoming = session.plan.template_candidates[0]
        conflict = session.plan.logical_identity_conflicts[0]
        resolved = resolve_template_import_plan(
            session.plan,
            logical_identity_resolutions={
                conflict.key: ImportConflictResolution((incoming.selection_key,))
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        with patch.object(core, "run_resolver_process", side_effect=self.refresh_ok):
            operation.run()
            operation.apply()
        self.assertFalse((self.source / "mods/logical.pw.toml").exists())
        self.assertTrue((self.source / "mods/actual-new.pw.toml").is_file())


if __name__ == "__main__":
    unittest.main()
