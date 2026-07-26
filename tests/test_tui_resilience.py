from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input, Static

import huroshiki
import huroshiki_core as core
import packctl


PACK_TOML = '''name = "Demo"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.234"
'''


def metadata(name: str, side_line: str) -> str:
    return f'''name = "{name}"
filename = "{name.lower()}.jar"
{side_line}
[update.modrinth]
mod-id = "{name.lower()}"
'''


def project(project_id: str, *, error: str | None = None) -> core.ProjectInfo:
    return core.ProjectInfo(
        kind="pack",
        project_id=project_id,
        display_name=project_id.title(),
        minecraft="1.21.1",
        loader="neoforge",
        loader_version="21.1.234",
        enabled=True,
        error=error,
        mod_count=None if error else 1,
    )


def mod(name: str, side: str = "both") -> core.ModInfo:
    client, server = core.flags_from_side(side)
    return core.ModInfo(
        relative_path=Path("mods") / f"{name.lower()}.pw.toml",
        slug=name.lower(),
        name=name,
        provider="MR",
        project_id=name.lower(),
        filename=f"{name.lower()}.jar",
        client=client,
        server=server,
    )


class ProjectLoadingResilienceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.packs.mkdir()
        self.templates.mkdir()
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_pack(
        self,
        pack_id: str,
        *,
        yaml_text: str | None = None,
        pack_toml: str = PACK_TOML,
        mod_text: str | None = None,
    ) -> Path:
        root = self.packs / pack_id
        mods = root / "source" / "mods"
        mods.mkdir(parents=True)
        (root / "pack.yaml").write_text(
            yaml_text
            if yaml_text is not None
            else f"id: {pack_id}\ndisplay_name: {pack_id.title()}\nenabled: true\n",
            encoding="utf-8",
        )
        (root / "source" / "pack.toml").write_text(pack_toml, encoding="utf-8")
        if mod_text is not None:
            (mods / "example.pw.toml").write_text(mod_text, encoding="utf-8")
        return root

    def test_malformed_projects_are_isolated_and_recover_after_reload(self) -> None:
        self.write_pack("good", mod_text=metadata("Good", 'side = "both"'))
        bad_yaml = self.write_pack("bad-yaml", yaml_text="invalid: [")
        bad_pack = self.write_pack("bad-pack", pack_toml="[versions")
        bad_mod = self.write_pack("bad-mod", mod_text="name = [")
        bad_template = self.templates / "bad-template"
        bad_template.mkdir()
        (bad_template / "template.yaml").write_text("invalid: [", encoding="utf-8")
        bad_entry = self.templates / "bad-entry"
        bad_entry.mkdir()
        (bad_entry / "template.yaml").write_text(
            '''id: bad-entry
display_name: Bad Entry
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Broken
    provider: nowhere
    project_id: broken
    side: both
''',
            encoding="utf-8",
        )

        projects = {item.key: item for item in core.list_projects()}

        self.assertEqual(set(projects), {
            "pack:good",
            "pack:bad-yaml",
            "pack:bad-pack",
            "pack:bad-mod",
            "template:bad-template",
            "template:bad-entry",
        })
        self.assertEqual(projects["pack:good"].mod_count, 1)
        self.assertIsNone(projects["pack:good"].error)
        for key in set(projects) - {"pack:good"}:
            self.assertIsNotNone(projects[key].error, key)
        self.assertIn("example.pw.toml", projects["pack:bad-mod"].error or "")

        (bad_yaml / "pack.yaml").write_text(
            "id: bad-yaml\ndisplay_name: Repaired\nenabled: true\n",
            encoding="utf-8",
        )
        repaired = {item.key: item for item in core.list_projects()}["pack:bad-yaml"]
        self.assertIsNone(repaired.error)
        self.assertEqual(repaired.display_name, "Repaired")

    def test_missing_empty_and_unknown_sides_remain_invalid_and_are_repairable(self) -> None:
        root = self.write_pack("sides")
        mods = root / "source" / "mods"
        values = {
            "missing": "",
            "empty": 'side = ""',
            "unknown": 'side = "unknown"',
            "valid": 'side = "server"',
        }
        for name, side_line in values.items():
            (mods / f"{name}.pw.toml").write_text(
                metadata(name.title(), side_line), encoding="utf-8"
            )

        listed = {item.slug: item for item in core.list_mods("pack:sides")}
        for name in ("missing", "empty", "unknown"):
            self.assertFalse(listed[name].client)
            self.assertFalse(listed[name].server)
            self.assertIsNotNone(listed[name].side_error)
            self.assertNotEqual(listed[name].side, "both")
        self.assertIsNone(listed["valid"].side_error)
        self.assertTrue(listed["valid"].server)
        self.assertEqual(core.project_info("pack:sides").mod_count, 4)

        repairs = {
            "missing": (True, False, "client"),
            "empty": (False, True, "server"),
            "unknown": (True, True, "both"),
        }
        with patch.object(
            packctl.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0, "stderr": ""})(),
        ):
            for name, (client, server, expected) in repairs.items():
                core.set_installed_mod_side(
                    "pack:sides",
                    Path("mods") / f"{name}.pw.toml",
                    client,
                    server,
                )
                self.assertEqual(
                    packctl.read_toml(mods / f"{name}.pw.toml")["side"],
                    expected,
                )

        self.assertTrue(all(item.side_error is None for item in core.list_mods("pack:sides")))

    def test_invalid_template_side_is_visible_and_repairable(self) -> None:
        template = self.templates / "base"
        template.mkdir()
        (template / "template.yaml").write_text(
            '''id: base
display_name: Base
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Broken Side
    provider: modrinth
    project_id: broken
  - name: Valid
    provider: modrinth
    project_id: valid
    side: server
''',
            encoding="utf-8",
        )

        listed = core.list_mods("template:base")
        self.assertEqual([item.name for item in listed], ["Broken Side", "Valid"])
        self.assertIsNotNone(listed[0].side_error)
        self.assertIsNone(core.project_info("template:base").error)

        core.set_installed_mod_side(
            "template:base", listed[0].relative_path, True, False
        )
        repaired = core.list_mods("template:base")
        self.assertIsNone(repaired[0].side_error)
        self.assertEqual(repaired[0].side, "client")

    def test_template_side_repair_preserves_duplicate_raw_entries_and_extra_fields(self) -> None:
        template = self.templates / "duplicates"
        template.mkdir()
        manifest = template / "template.yaml"
        manifest.write_text(
            """id: duplicates
display_name: Duplicates
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: First
    provider: modrinth
    project_id: same
    side: client
    custom: keep-first
  - name: Second
    provider: modrinth
    project_id: same
    side: invalid
    custom: keep-second
""",
            encoding="utf-8",
        )

        listed = core.list_mods("template:duplicates")
        self.assertEqual([item.name for item in listed], ["First", "Second"])
        core.set_installed_mod_side(
            "template:duplicates", listed[1].relative_path, False, True
        )

        raw = packctl.load_yaml(manifest)["mods"]
        self.assertEqual([entry["name"] for entry in raw], ["First", "Second"])
        self.assertEqual([entry["custom"] for entry in raw], ["keep-first", "keep-second"])
        self.assertEqual([entry["side"] for entry in raw], ["client", "server"])

    def test_template_paths_distinguish_duplicate_foo_from_real_foo_2(self) -> None:
        template = self.templates / "collisions"
        template.mkdir()
        manifest = template / "template.yaml"
        manifest.write_text(
            """id: collisions
display_name: Collisions
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Foo First
    provider: modrinth
    project_id: foo
    side: client
  - name: Foo Duplicate
    provider: modrinth
    project_id: foo
    side: invalid
  - name: Foo Dash Two
    provider: modrinth
    project_id: foo-2
    side: server
""",
            encoding="utf-8",
        )

        listed = core.list_mods("template:collisions")
        self.assertEqual(len({item.relative_path for item in listed}), 3)
        self.assertEqual(
            [item.name for item in core.filter_mods(listed, "foo-2")],
            ["Foo Dash Two"],
        )

        core.set_installed_mod_side(
            "template:collisions", listed[1].relative_path, False, True
        )
        raw = packctl.load_yaml(manifest)["mods"]
        self.assertEqual([entry["side"] for entry in raw], ["client", "server", "server"])

    def test_template_deletion_selects_exact_raw_duplicate(self) -> None:
        template = self.templates / "delete-duplicate"
        template.mkdir()
        manifest = template / "template.yaml"
        manifest.write_text(
            """id: delete-duplicate
display_name: Delete Duplicate
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: First
    provider: modrinth
    project_id: foo
    side: client
    custom: keep-first
  - name: Selected
    provider: modrinth
    project_id: foo
    side: server
    custom: remove
  - name: Last
    provider: modrinth
    project_id: foo-2
    side: both
    custom: keep-last
""",
            encoding="utf-8",
        )

        listed = core.list_mods("template:delete-duplicate")
        self.assertEqual(
            core.remove_installed_mods(
                "template:delete-duplicate", [str(listed[1].relative_path)]
            ),
            0,
        )

        raw = packctl.load_yaml(manifest)["mods"]
        self.assertEqual([entry["name"] for entry in raw], ["First", "Last"])
        self.assertEqual([entry["custom"] for entry in raw], ["keep-first", "keep-last"])


class _MainTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self) -> None:
        super().__init__()
        self.opened: str | None = None

    def on_mount(self) -> None:
        self.push_screen(huroshiki.MainMenuScreen())

    def open_project(self, key: str) -> None:
        self.opened = key


class _InstalledTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def on_mount(self) -> None:
        self.push_screen(huroshiki.InstalledModsScreen("pack:demo"))


class _FilesTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def on_mount(self) -> None:
        self.push_screen(huroshiki.TemplateScreen("pack:demo"))


class FilterAndErrorInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_main_q_types_in_input_then_clears_from_list_and_quits(self) -> None:
        projects = [project("alpha"), project("beta")]
        with patch.object(huroshiki.core, "list_projects", return_value=projects):
            app = _MainTestApp()
            with patch.object(app, "exit") as exit_app:
                async with app.run_test() as pilot:
                    screen = app.screen
                    search = screen.query_one("#pack-search", Input)
                    table = screen.query_one("#pack-table", DataTable)
                    search.focus()

                    await pilot.press("q")
                    await pilot.pause()

                    self.assertEqual(search.value, "q")
                    self.assertIs(screen.focused, search)
                    search.value = "missing"
                    screen.reload_projects("missing")
                    self.assertEqual(table.row_count, 0)
                    table.focus()
                    await pilot.press("q")
                    await pilot.pause()
                    self.assertEqual(search.value, "")
                    self.assertIs(screen.focused, table)
                    self.assertLess(table.cursor_row, table.row_count)
                    exit_app.assert_not_called()

                    await pilot.press("q")
                    await pilot.pause()
                    exit_app.assert_called_once()

    async def test_installed_q_clears_zero_results_and_preserves_delete_selection(self) -> None:
        mods = [mod("Alpha"), mod("Beta")]
        with (
            patch.object(
                huroshiki.core,
                "project_config",
                return_value={"display_name": "Demo"},
            ),
            patch.object(huroshiki.core, "list_mods", return_value=mods),
        ):
            app = _InstalledTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                selected = mods[1].relative_path
                screen.selected_paths.add(selected)
                search = screen.query_one("#installed-search", Input)
                search.value = "missing"
                screen.reload_mods("missing")
                search.focus()

                self.assertEqual(
                    screen.query_one("#installed-table", DataTable).row_count,
                    0,
                )
                await pilot.press("q")
                await pilot.pause()
                self.assertEqual(search.value, "")
                self.assertEqual(len(screen.visible_mods), 2)
                self.assertIn(selected, screen.selected_paths)
                self.assertTrue(
                    next(item for item in screen.visible_mods if item.relative_path == selected).selected
                )
                self.assertIs(
                    screen.focused, screen.query_one("#installed-table", DataTable)
                )
                self.assertLess(
                    screen.query_one("#installed-table", DataTable).cursor_row,
                    screen.query_one("#installed-table", DataTable).row_count,
                )

    async def test_invalid_side_row_stays_visible_and_ctrl_side_keys_repair_it(self) -> None:
        invalid = mod("Broken", "unknown")
        invalid.side_error = "side must be client, server, or both; got 'unknown'"
        valid = mod("Valid", "server")
        with (
            patch.object(
                huroshiki.core,
                "project_config",
                return_value={"display_name": "Demo"},
            ),
            patch.object(huroshiki.core, "list_mods", return_value=[invalid, valid]),
            patch.object(huroshiki.core, "set_installed_mod_side") as set_side,
        ):
            app = _InstalledTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                table = screen.query_one("#installed-table", DataTable)
                row = table.get_row_at(0)
                self.assertEqual(row[1], "Broken")
                self.assertEqual((row[2], row[3]), ("?", "?"))
                self.assertEqual(row[5], "mods/broken.pw.toml")

                await pilot.press("c", "s")
                await pilot.pause()
                set_side.assert_not_called()

                await pilot.press("ctrl+c", "ctrl+s", "b")
                await pilot.pause()

                self.assertEqual(
                    [call.args[2:] for call in set_side.call_args_list],
                    [(True, False), (False, True), (True, True)],
                )

    async def test_project_files_q_clears_zero_result_filter_from_input(self) -> None:
        files = [
            core.TemplateInfo("common", Path("a.toml"), Path("/a.toml"), 1),
            core.TemplateInfo("server", Path("b.toml"), Path("/b.toml"), 2),
        ]
        with (
            patch.object(huroshiki.core, "project_info", return_value=project("demo")),
            patch.object(huroshiki.core, "list_templates", return_value=files),
        ):
            app = _FilesTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                search = screen.query_one("#template-search", Input)
                search.value = "missing"
                screen.reload_templates("missing")
                search.focus()

                self.assertEqual(
                    screen.query_one("#template-table", DataTable).row_count,
                    0,
                )
                await pilot.press("q")
                await pilot.pause()
                self.assertEqual(search.value, "")
                self.assertEqual(len(screen.visible_templates), 2)
                self.assertIs(
                    screen.focused, screen.query_one("#template-table", DataTable)
                )
                self.assertLess(
                    screen.query_one("#template-table", DataTable).cursor_row,
                    screen.query_one("#template-table", DataTable).row_count,
                )

    async def test_error_row_shows_details_and_reloads_repaired_project(self) -> None:
        broken = project("broken", error="mods/broken.pw.toml: invalid TOML")
        repaired = project("broken")
        with patch.object(
            huroshiki.core,
            "list_projects",
            side_effect=[[broken], [repaired]],
        ):
            app = _MainTestApp()
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()

                self.assertIsInstance(app.screen, huroshiki.MessageModal)
                message = app.screen.query_one("#modal-message", Static)
                self.assertIn("mods/broken.pw.toml", str(message.content))
                self.assertIsNone(app.opened)

                await pilot.press("escape")
                await pilot.press("r")
                await pilot.pause()
                self.assertIsNone(app.screen.visible_projects[0].error)

                await pilot.press("enter")
                self.assertEqual(app.opened, "pack:broken")

    async def test_error_pack_row_can_open_recovery_safe_content_files(self) -> None:
        broken = project("broken", error="pack.yaml: invalid YAML")
        files = [core.TemplateInfo("common", Path("notes.txt"), Path("/notes.txt"), 4)]
        with (
            patch.object(huroshiki.core, "list_projects", return_value=[broken]),
            patch.object(huroshiki.core, "project_info", return_value=broken),
            patch.object(huroshiki.core, "list_templates", return_value=files),
        ):
            app = _MainTestApp()
            async with app.run_test() as pilot:
                await pilot.press("t")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateScreen)
                self.assertEqual(app.screen.visible_templates, files)


if __name__ == "__main__":
    unittest.main()
