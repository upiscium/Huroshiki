from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Input, Static

import huroshiki
import huroshiki_core as core


PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo Pack",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.1",
    enabled=True,
)
PREVIEW = core.LoaderMigrationPreview(
    "pack:demo",
    "1.21.1",
    "neoforge",
    "21.1.1",
    "21.1.2",
    (
        core.UpdateChange(Path("index.toml"), b"old", b"new"),
        core.UpdateChange(Path("pack.toml"), b"old", b"new"),
    ),
    ("URL MOD compatibility cannot be verified",),
)


class FakeLoaderOperation:
    def __init__(self, *, delayed_cleanup: bool = False) -> None:
        self.done = threading.Event()
        self.started = threading.Event()
        self.release = threading.Event()
        self.error: BaseException | None = None
        self.cancelled = False
        self.preview: core.LoaderMigrationPreview | None = None
        self.progress: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.delayed_cleanup = delayed_cleanup
        self.cancel_calls = 0
        self.discard_calls = 0
        self.apply_calls = 0

    def run(self) -> None:
        self.started.set()
        self.progress.put("Running Packwiz migration")
        if self.delayed_cleanup:
            self.release.wait(3)
        if self.cancel_calls:
            self.cancelled = True
        else:
            self.preview = PREVIEW
        self.done.set()

    def cancel(self) -> None:
        self.cancel_calls += 1

    def discard(self) -> None:
        self.discard_calls += 1

    def apply(self) -> None:
        self.apply_calls += 1

    def drain_progress(self) -> tuple[str, ...]:
        values: list[str] = []
        while True:
            try:
                values.append(self.progress.get_nowait())
            except queue.Empty:
                return tuple(values)


class _VersionsTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self) -> None:
        super().__init__()
        self.settings_opens = 0
        self.versions_opens = 0

    def on_mount(self) -> None:
        self.push_screen(huroshiki.SettingsScreen("pack:demo"))

    def open_versions(self, project_key: str) -> None:
        self.versions_opens += 1
        self.switch_screen(huroshiki.VersionsScreen(project_key))

    def open_settings(self, project_key: str) -> None:
        self.settings_opens += 1
        self.switch_screen(huroshiki.SettingsScreen(project_key))

    def open_deployment_settings(self, project_key: str) -> None:
        raise AssertionError("unexpected Deployment navigation")

    def open_client_distribution_settings(self, project_key: str) -> None:
        raise AssertionError("unexpected Client Distribution navigation")

    def open_project(self, project_key: str) -> bool:
        return True


class LoaderMigrationTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_settings_versions_navigation_and_read_only_identity(self) -> None:
        with patch.object(core, "project_info", return_value=PROJECT):
            app = _VersionsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "j", "enter")
                await pilot.pause()

                self.assertIsInstance(app.screen, huroshiki.VersionsScreen)
                self.assertEqual(
                    app.screen.query_one("#loader-version-input", Input).value,
                    "21.1.1",
                )
                readonly = [
                    str(widget.content)
                    for widget in app.screen.query(".readonly-setting").results(Static)
                ]
                self.assertEqual(readonly, ["1.21.1", "neoforge"])

    async def test_preview_modal_cancel_discards_operation(self) -> None:
        operation = FakeLoaderOperation()
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "LoaderMigrationOperation", return_value=operation),
        ):
            app = _VersionsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "j", "enter", "ctrl+s")
                await pilot.pause(0.15)

                modal = app.screen
                self.assertIsInstance(modal, huroshiki.ConfirmModal)
                self.assertIn(
                    "Loader version: 21.1.1 -> 21.1.2",
                    "\n".join(modal.lines),
                )
                self.assertIn("pack.toml", "\n".join(modal.lines))
                self.assertIn("URL MOD compatibility", "\n".join(modal.lines))

                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(operation.discard_calls, 1)
                self.assertEqual(operation.apply_calls, 0)
                self.assertIsInstance(app.screen, huroshiki.VersionsScreen)

    async def test_confirmed_preview_applies_and_reloads_versions(self) -> None:
        operation = FakeLoaderOperation()
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "LoaderMigrationOperation", return_value=operation),
        ):
            app = _VersionsTestApp()
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.press("j", "j", "enter", "ctrl+s")
                    await pilot.pause(0.15)
                    await pilot.press("enter")
                    await pilot.pause()

                    self.assertEqual(operation.apply_calls, 1)
                    self.assertEqual(operation.discard_calls, 0)
                    self.assertEqual(app.versions_opens, 2)
                    self.assertIsInstance(app.screen, huroshiki.VersionsScreen)

    async def test_escape_waits_for_cancel_cleanup_before_navigation(self) -> None:
        operation = FakeLoaderOperation(delayed_cleanup=True)
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "LoaderMigrationOperation", return_value=operation),
        ):
            app = _VersionsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "j", "enter", "ctrl+s")
                self.assertTrue(operation.started.wait(2))
                screen = app.screen
                self.assertFalse(screen.operation_thread.daemon)

                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertEqual(operation.cancel_calls, 1)
                self.assertEqual(app.settings_opens, 0)
                self.assertIs(app.screen, screen)

                operation.release.set()
                await pilot.pause(0.15)
                self.assertTrue(operation.done.is_set())
                self.assertEqual(app.settings_opens, 1)
                self.assertIsInstance(app.screen, huroshiki.SettingsScreen)

    async def test_thread_start_failure_cancels_operation(self) -> None:
        operation = FakeLoaderOperation()
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "LoaderMigrationOperation", return_value=operation),
            patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")),
        ):
            app = _VersionsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "j", "enter", "ctrl+s")
                await pilot.pause()

                self.assertEqual(operation.cancel_calls, 1)
                self.assertIsNone(app.screen.operation)
                self.assertFalse(
                    app.screen.query_one("#loader-version-input", Input).disabled
                )


if __name__ == "__main__":
    unittest.main()
