from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input, TextArea

import huroshiki
import huroshiki_core as core


def mod(side: str = "both") -> core.ModInfo:
    client, server = core.flags_from_side(side)
    return core.ModInfo(
        relative_path=Path("mods/example.pw.toml"),
        slug="example",
        name="Example",
        provider="MR",
        project_id="example",
        filename="example.jar",
        client=client,
        server=server,
    )


class _InstallTransaction:
    active = True

    def __init__(self) -> None:
        self.item = mod()
        self.calls: list[tuple[bool, bool]] = []

    def staged_mods(self) -> list[core.ModInfo]:
        return [self.item]

    def set_side(self, relative_path: Path, client: bool, server: bool) -> None:
        self.calls.append((client, server))
        self.item.client = client
        self.item.server = server


class _ScreenApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen, transaction=None) -> None:
        super().__init__()
        self.initial_screen = screen
        self.transactions = {}
        if transaction is not None:
            self.transactions["pack:demo"] = transaction

    def on_mount(self) -> None:
        self.push_screen(self.initial_screen)

    def get_transaction(self, project_key: str):
        return self.transactions[project_key]


class SideControlInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_install_defaults_use_ctrl_keys_and_bare_keys_do_nothing(self) -> None:
        with patch.object(huroshiki.core, "project_config", return_value={"display_name": "Demo"}):
            app = _ScreenApp(huroshiki.InstallScreen("pack:demo"))
            async with app.run_test() as pilot:
                results = app.screen.query_one("#search-results-table", DataTable)
                results.focus()
                await pilot.press("c", "s")
                await pilot.pause()
                self.assertEqual((app.screen.default_client, app.screen.default_server), (True, True))

                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertEqual((app.screen.default_client, app.screen.default_server), (False, True))
                await pilot.press("ctrl+s", "b")
                await pilot.pause()
                self.assertEqual((app.screen.default_client, app.screen.default_server), (True, True))

    async def test_install_staged_changes_use_ctrl_keys(self) -> None:
        transaction = _InstallTransaction()
        with patch.object(huroshiki.core, "project_config", return_value={"display_name": "Demo"}):
            app = _ScreenApp(huroshiki.InstallScreen("pack:demo"), transaction)
            async with app.run_test() as pilot:
                app.screen.query_one("#staged-table", DataTable).focus()
                await pilot.press("ctrl+c", "b", "ctrl+s", "b")
                await pilot.pause()
                self.assertEqual(
                    transaction.calls,
                    [(False, True), (True, True), (True, False), (True, True)],
                )

    async def test_install_search_input_keeps_text_and_does_not_mutate_sides(self) -> None:
        with patch.object(huroshiki.core, "project_config", return_value={"display_name": "Demo"}):
            app = _ScreenApp(huroshiki.InstallScreen("pack:demo"))
            async with app.run_test() as pilot:
                search = app.screen.query_one("#mod-search", Input)
                search.focus()
                await pilot.press("c", "s", "ctrl+c", "ctrl+s")
                await pilot.pause()
                self.assertEqual(search.value, "cs")
                self.assertEqual((app.screen.default_client, app.screen.default_server), (True, True))

    async def test_pack_and_template_mod_lists_use_ctrl_keys_only(self) -> None:
        for project_key in ("pack:demo", "template:demo"):
            item = mod("unknown")
            item.side_error = "invalid side"
            with (
                patch.object(huroshiki.core, "project_config", return_value={"display_name": "Demo"}),
                patch.object(huroshiki.core, "list_mods", return_value=[item]),
                patch.object(huroshiki.core, "set_installed_mod_side") as set_side,
            ):
                app = _ScreenApp(huroshiki.InstalledModsScreen(project_key))
                async with app.run_test() as pilot:
                    await pilot.press("c", "s")
                    await pilot.pause()
                    set_side.assert_not_called()
                    await pilot.press("ctrl+c", "ctrl+s", "b")
                    await pilot.pause()
                    self.assertEqual(
                        [call.args[2:] for call in set_side.call_args_list],
                        [(True, False), (False, True), (True, True)],
                    )

    async def test_mod_list_input_focus_does_not_mutate_sides(self) -> None:
        item = mod()
        with (
            patch.object(huroshiki.core, "project_config", return_value={"display_name": "Demo"}),
            patch.object(huroshiki.core, "list_mods", return_value=[item]),
            patch.object(huroshiki.core, "set_installed_mod_side") as set_side,
        ):
            app = _ScreenApp(huroshiki.InstalledModsScreen("pack:demo"))
            async with app.run_test() as pilot:
                search = app.screen.query_one("#installed-search", Input)
                search.focus()
                await pilot.press("c", "s", "ctrl+c", "ctrl+s")
                await pilot.pause()
                self.assertEqual(search.value, "cs")
                set_side.assert_not_called()

    async def test_editor_ctrl_s_remains_save_and_text_input(self) -> None:
        project = core.ProjectInfo(
            "pack", "demo", "Demo", "1.21.1", "neoforge", "21.1.0", True
        )
        file = core.TemplateInfo("common", Path("notes.txt"), Path("/notes.txt"), 4)
        with (
            patch.object(huroshiki.core, "project_info", return_value=project),
            patch.object(huroshiki.core, "read_template_text", return_value="old"),
            patch.object(huroshiki.core, "write_template_text") as write,
        ):
            app = _ScreenApp(huroshiki.TemplateEditorScreen("pack:demo", file))
            async with app.run_test() as pilot:
                editor = app.screen.query_one("#template-editor", TextArea)
                editor.load_text("new")
                await pilot.press("ctrl+s")
                await pilot.pause()
                write.assert_called_once_with("pack:demo", "common", Path("notes.txt"), "new")


if __name__ == "__main__":
    unittest.main()
