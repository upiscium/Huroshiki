from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Input, TextArea

import huroshiki
import huroshiki_core as core


PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)
BROKEN_PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="broken",
    display_name="Broken",
    minecraft="",
    loader="",
    loader_version="",
    enabled=True,
    error="pack.yaml: invalid YAML",
)
FILE = core.TemplateInfo("common", Path("notes.txt"), Path("/notes.txt"), 4)


class _Transaction:
    active = True

    def staged_mods(self) -> list[core.ModInfo]:
        return []


class _Operation:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self.done.set()


class _UpdateTransaction:
    def prepare_updates(self) -> list[core.UpdateCandidate]:
        return []

    def discard(self) -> None:
        pass


class _NavigationApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen) -> None:
        super().__init__()
        self.initial_screen = screen
        self.transactions: dict[str, object] = {}

    def on_mount(self) -> None:
        self.push_screen(self.initial_screen)

    def go_main(self) -> None:
        self.switch_screen(huroshiki.MainMenuScreen())

    def open_project(self, project_key: str) -> None:
        self.switch_screen(huroshiki.ProjectScreen(project_key))

    def open_list(self, project_key: str) -> None:
        self.switch_screen(huroshiki.InstalledModsScreen(project_key))

    def get_transaction(self, project_key: str):
        return self.transactions[project_key]


class ProjectChildNavigationTest(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def patches(self):
        patches = (
            patch.object(huroshiki.core, "project_info", side_effect=lambda key: BROKEN_PROJECT if key == "pack:broken" else PROJECT),
            patch.object(huroshiki.core, "project_config", return_value={"display_name": "Demo"}),
            patch.object(huroshiki.core, "project_actions", return_value=[]),
            patch.object(huroshiki.core, "list_projects", return_value=[]),
            patch.object(huroshiki.core, "list_mods", return_value=[]),
            patch.object(huroshiki.core, "list_templates", return_value=[FILE]),
            patch.object(huroshiki.core, "read_template_text", return_value="clean"),
        )
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            yield

    async def assert_escape_opens_project(self, screen) -> None:
        app = _NavigationApp(screen)
        async with app.run_test() as pilot:
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
            self.assertEqual(app.screen.project_key, "pack:demo")

    async def test_install_installed_and_files_escape_to_exact_project(self) -> None:
        with self.patches():
            await self.assert_escape_opens_project(huroshiki.InstallScreen("pack:demo"))
            await self.assert_escape_opens_project(huroshiki.InstalledModsScreen("pack:demo"))
            await self.assert_escape_opens_project(huroshiki.TemplateScreen("pack:demo"))

    async def test_escape_from_child_input_focus_still_opens_project(self) -> None:
        with self.patches():
            for screen, input_id in (
                (huroshiki.InstallScreen("pack:demo"), "#mod-search"),
                (huroshiki.InstalledModsScreen("pack:demo"), "#installed-search"),
                (huroshiki.TemplateScreen("pack:demo"), "#template-search"),
            ):
                app = _NavigationApp(screen)
                async with app.run_test() as pilot:
                    screen.query_one(input_id, Input).focus()
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
                    self.assertEqual(app.screen.project_key, "pack:demo")

    async def test_update_escape_discards_and_opens_project(self) -> None:
        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
            return_value=_UpdateTransaction(),
        ):
            app = _NavigationApp(huroshiki.UpdateScreen("pack:demo"))
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
                    self.assertEqual(app.screen.project_key, "pack:demo")

    async def test_project_escape_stays_main_and_main_escape_stays_main(self) -> None:
        with self.patches():
            app = _NavigationApp(huroshiki.ProjectScreen("pack:demo"))
            async with app.run_test() as pilot:
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.MainMenuScreen)
                screen = app.screen
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, screen)

    async def test_clean_editor_escape_opens_project_files(self) -> None:
        with self.patches():
            app = _NavigationApp(huroshiki.TemplateEditorScreen("pack:demo", FILE))
            async with app.run_test() as pilot:
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateScreen)
                self.assertEqual(app.screen.project_key, "pack:demo")

    async def test_dirty_editor_requires_confirmation_then_opens_files(self) -> None:
        with self.patches():
            app = _NavigationApp(huroshiki.TemplateEditorScreen("pack:demo", FILE))
            async with app.run_test() as pilot:
                editor = app.screen.query_one("#template-editor", TextArea)
                editor.load_text("dirty")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)

                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateEditorScreen)

                await pilot.press("escape", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateScreen)
                self.assertEqual(app.screen.project_key, "pack:demo")

    async def test_broken_recovery_editor_retains_main_parent(self) -> None:
        with self.patches():
            files = huroshiki.TemplateScreen(
                "pack:broken", recovery_parent_main=True
            )
            app = _NavigationApp(files)
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateEditorScreen)
                self.assertTrue(app.screen.recovery_parent_main)

                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateScreen)
                self.assertTrue(app.screen.recovery_parent_main)

                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.MainMenuScreen)

    async def test_install_escape_cancels_before_navigation_and_preserves_staging(self) -> None:
        with self.patches():
            transaction = _Transaction()
            operation = _Operation()
            app = _NavigationApp(huroshiki.InstallScreen("pack:demo"))
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                app.screen.operation = operation
                await pilot.press("escape")
                await pilot.pause()
                self.assertTrue(operation.cancelled)
                self.assertIs(app.transactions["pack:demo"], transaction)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

    async def test_install_sibling_navigation_cancels_and_preserves_staging(self) -> None:
        with self.patches():
            transaction = _Transaction()
            operation = _Operation()
            app = _NavigationApp(huroshiki.InstallScreen("pack:demo"))
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                app.screen.operation = operation
                app.screen.query_one("#search-results-table").focus()
                await pilot.press("l")
                await pilot.pause()
                self.assertTrue(operation.cancelled)
                self.assertIs(app.transactions["pack:demo"], transaction)
                self.assertIsInstance(app.screen, huroshiki.InstalledModsScreen)


if __name__ == "__main__":
    unittest.main()
