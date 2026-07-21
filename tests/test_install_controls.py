from __future__ import annotations

import threading
from pathlib import Path
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input

import huroshiki
from packwiz_parser import MenuItem


class _InstallTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self) -> None:
        super().__init__()
        self.transactions = {}

    def on_mount(self) -> None:
        self.push_screen(huroshiki.InstallScreen("pack:demo"))


class _FakeOperation:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancel_menu_called = False
        self.cancel_called = False

    def cancel_menu(self) -> None:
        self.cancel_menu_called = True

    def cancel(self) -> None:
        self.cancel_called = True
        self.done.set()


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

    async def test_q_discards_visible_search_results_and_cancels_menu(self) -> None:
        with patch.object(
            huroshiki.core,
            "project_config",
            return_value={"display_name": "Demo"},
        ):
            app = _InstallTestApp()
            async with app.run_test() as pilot:
                screen = app.screen
                operation = _FakeOperation()
                screen.operation = operation
                screen.search_results = [
                    MenuItem(index=1, label="Example MOD", is_default=True)
                ]
                screen.refresh_search_results()
                results = screen.query_one("#search-results-table", DataTable)
                results.focus()

                await pilot.press("q")
                await pilot.pause()

                self.assertEqual(screen.search_results, [])
                self.assertEqual(results.row_count, 0)
                self.assertTrue(operation.cancel_menu_called)


if __name__ == "__main__":
    unittest.main()
