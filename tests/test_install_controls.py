from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input

import huroshiki
import huroshiki_core as core
from packwiz_parser import MenuItem, ParserEvent


class _InstallTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self) -> None:
        super().__init__()
        self.transactions = {}
        self.opened: str | None = None

    def open_project(self, key: str) -> bool:
        self.opened = key
        return True

    def on_mount(self) -> None:
        self.push_screen(huroshiki.InstallScreen("pack:demo"))


class _ActivePackwizOperation(core.PackwizAddOperation):
    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancel_event = threading.Event()
        self.termination_result = None
        self.termination_incomplete = False
        self.menu_cancelled = False
        self.cancel_called = False

    def cancel_menu(self) -> None:
        self.menu_cancelled = True

    def cancel(self, *, deadline=None) -> None:
        self.cancel_called = True
        self.cancel_event.set()
        self.done.set()


class _DeferredActivePackwizOperation(_ActivePackwizOperation):
    def cancel(self, *, deadline=None) -> None:
        self.cancel_called = True
        self.cancel_event.set()


class InstallControlsTest(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_t_toggles_provider_while_search_input_is_focused(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                search = screen.query_one("#mod-search", Input)
                search.focus()

                self.assertEqual(screen.provider, "modrinth")
                await pilot.press("ctrl+t")
                await pilot.pause()
                self.assertEqual(screen.provider, "curseforge")

                await pilot.press("ctrl+t")
                await pilot.pause()
                self.assertEqual(screen.provider, "url")
                self.assertIn("Public URL", search.placeholder)

                await pilot.press("ctrl+t")
                await pilot.pause()
                self.assertEqual(screen.provider, "modrinth")

    async def test_c_discards_visible_provider_search_results(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                screen.search_results = [
                    huroshiki.core.InstallSearchResult(
                        "modrinth", "canonical", "Example MOD", "Details"
                    )
                ]
                screen.refresh_search_results()
                results = screen.query_one("#search-results-table", DataTable)
                results.focus()

                await pilot.press("c")
                await pilot.pause()

                self.assertEqual(screen.search_results, [])
                self.assertEqual(results.row_count, 0)

    async def test_c_discards_active_packwiz_search_menu(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                operation = _ActivePackwizOperation()
                screen.operation = operation
                screen.packwiz_menu_items = [MenuItem(7, "Create")]
                screen.refresh_search_results()
                results = screen.query_one("#search-results-table", DataTable)
                results.focus()

                await pilot.press("c")
                await pilot.pause()

                self.assertTrue(operation.menu_cancelled)
                self.assertEqual(screen.packwiz_menu_items, [])
                self.assertEqual(screen.state, "cancelling")

    async def test_q_navigates_to_project_after_active_packwiz_cleanup(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                operation = _DeferredActivePackwizOperation()
                screen.operation = operation
                results = screen.query_one("#search-results-table", DataTable)
                results.focus()

                await pilot.press("q")
                await pilot.pause()

                self.assertTrue(operation.cancel_called)
                self.assertTrue(operation.cancel_event.is_set())
                self.assertIsNone(app.opened)

                operation.done.set()
                await pilot.pause(0.1)

                self.assertEqual(app.opened, "pack:demo")

    async def test_q_in_search_input_is_included_literally(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                search = screen.query_one("#mod-search", Input)
                search.focus()

                await pilot.press("q")
                await pilot.pause()

                self.assertEqual(search.value, "q")
                self.assertIsNone(app.opened)

    async def test_packwiz_progress_events_use_parser_messages(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test():
                screen = app.screen
                screen.operation = _ActivePackwizOperation()

                screen._handle_packwiz_event(ParserEvent("search_started", "Create"))
                self.assertIn("Create", str(screen.query_one("#packwiz-status").render()))
                screen._handle_packwiz_event(
                    ParserEvent("identity_verified", "Verified CurseForge project ID 328085")
                )
                self.assertIn("328085", str(screen.query_one("#packwiz-status").render()))
                screen._handle_packwiz_event(ParserEvent("diagnostic", "Packwiz warning"))
                self.assertIn(
                    "Packwiz warning", str(screen.query_one("#packwiz-status").render())
                )


if __name__ == "__main__":
    unittest.main()
