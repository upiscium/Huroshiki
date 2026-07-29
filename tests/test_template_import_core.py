from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from template_import import resolve_template_import_plan


PACK_TOML = """name = "Demo"
pack-format = "packwiz:1.1.0"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.1"
"""


def metadata(name: str, project_id: str, filename: str) -> bytes:
    return f'''name = "{name}"
filename = "{filename}"
side = "both"
[download]
url = "https://cdn.example/{filename}"
hash-format = "sha256"
hash = "00"
[update.modrinth]
mod-id = "{project_id}"
version = "1"
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

    def operation(self) -> core.TemplateImportOperation:
        plan = core.prepare_template_import_plan("pack:demo", ["base"])
        resolved = resolve_template_import_plan(plan)
        return core.TemplateImportOperation(plan, resolved)

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
        self.assertEqual([item.project_id for item in operation.preview.added_roots], ["root"])
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
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_resolution_digest_and_cli_parser_fail_closed(self) -> None:
        plan = core.prepare_template_import_plan("pack:demo", ["base"])
        resolution = self.root / "resolution.yaml"
        resolution.write_text(
            "version: 1\nplan_digest: stale\nname_conflicts: {}\nside_conflicts: {}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "stale plan digest"):
            packctl._template_import_resolution(resolution, plan)
        args = packctl.parser().parse_args(
            ["apply-template", "demo", "base", "--apply", "--json"]
        )
        self.assertIs(args.func, packctl.cmd_apply_template)
        self.assertEqual(args.templates, ["base"])
        self.assertTrue(args.apply)


if __name__ == "__main__":
    unittest.main()
