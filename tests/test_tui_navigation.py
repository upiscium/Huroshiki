from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext, redirect_stderr
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input, TextArea

import huroshiki
import huroshiki_core as core
from packwiz_parser import MenuItem, ParserEvent


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

    def __init__(self) -> None:
        self.changes: list[str] = []

    def staged_mods(self) -> list[core.ModInfo]:
        return []


class _Operation:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancelled = False
        self.cancel_deadline: float | None = None

    def cancel(self, *, deadline=None) -> None:
        self.cancelled = True
        self.cancel_deadline = deadline
        self.done.set()


class _ResolvedOperation(_Operation):
    def run(self) -> core.AddOperationResult:
        self.done.set()
        return core.AddOperationResult(
            0,
            (Path("mods/root.pw.toml"),),
            Path("raw.log"),
            Path("output.log"),
            Path("events.log"),
            "staged",
        )


class _PackwizCleanupOperation(core.PackwizAddOperation):
    def __init__(
        self,
        *,
        done: bool,
        incomplete: bool,
        complete_on_cancel: bool = False,
    ) -> None:
        self.done = threading.Event()
        if done:
            self.done.set()
        self.cancelled = False
        self.cancel_deadline: float | None = None
        self.cleanup_error: BaseException | None = None
        self.termination_result = (
            core.ProcessTerminationResult(False, False, True)
            if incomplete
            else None
        )
        self.termination_incomplete = incomplete
        self.complete_on_cancel = complete_on_cancel

    def cancel(self, *, deadline=None):
        self.cancelled = True
        self.cancel_deadline = deadline
        if self.complete_on_cancel:
            self.termination_result = core.ProcessTerminationResult(True, True, True)
            self.termination_incomplete = False
        return self.termination_result


class _PackwizMenuOperation(core.PackwizAddOperation):
    """Small PTY substitute that exposes the menu through the UI callback."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancel_event = threading.Event()
        self.cancelled = False
        self.cancel_deadline = None
        self.cleanup_error = None
        self.termination_result = None
        self.termination_incomplete = False
        self.on_event = None
        self.menu_selection: list[int] = []
        self.menu_cancelled = False
        self.menu_ready = threading.Event()
        self.release = threading.Event()

    def run(self) -> core.AddOperationResult:
        assert self.on_event is not None
        self.on_event(
            ParserEvent(
                "search_results",
                "Choose a number",
                (MenuItem(4, "Create"), MenuItem(9, "Create (beta)")),
            )
        )
        self.menu_ready.set()
        self.release.wait(2)
        self.done.set()
        return core.AddOperationResult(
            0,
            (),
            Path("raw.log"),
            Path("output.log"),
            Path("events.log"),
            "staged",
        )

    def send_selection(self, index: int) -> None:
        self.menu_selection.append(index)
        self.release.set()

    def cancel_menu(self) -> None:
        self.menu_cancelled = True
        self.cancel_event.set()

    def cancel(self, *, deadline=None) -> None:
        self.cancelled = True
        self.cancel_deadline = deadline
        self.release.set()


class _InstallTransaction(_Transaction):
    source = Path("/fake/source")

    def __init__(self) -> None:
        super().__init__()
        self.add_calls: list[dict[str, object]] = []
        self.resolved_calls: list[dict[str, object]] = []
        self.queued_operations: list[object] = []
        self._operation: object | None = None

    def _next_operation(self):
        operation = (
            self.queued_operations.pop(0)
            if self.queued_operations
            else _ResolvedOperation()
        )
        self._operation = operation
        return operation

    def begin_add(self, provider, selector, **kwargs):
        self.add_calls.append(
            {"provider": provider, "selector": selector, **kwargs}
        )
        operation = self._next_operation()
        if "on_event" in kwargs:
            operation.on_event = kwargs["on_event"]
        return operation

    def begin_resolved_add(self, **kwargs):
        self.resolved_calls.append(kwargs)
        return self._next_operation()


class _BlockingInstallOperation(core.ResolvedAddOperation):
    def __init__(self, transaction: _InstallTransaction) -> None:
        self.transaction = transaction
        self.done = threading.Event()
        self.cancel_event = threading.Event()
        self.cancelled = False
        self.cancel_deadline: float | None = None
        self.cleanup_error: BaseException | None = None
        self.termination_result = None
        self.termination_incomplete = False
        self.result: core.AddOperationResult | None = None
        self.started = threading.Event()
        self.cleanup_started = threading.Event()
        self.release_cleanup = threading.Event()

    def run(self) -> core.AddOperationResult:
        self.started.set()
        self.cancel_event.wait(2)
        self.cleanup_started.set()
        self.release_cleanup.wait(2)
        self.transaction._operation = None
        self.result = core.AddOperationResult(
            130,
            (),
            Path("raw.log"),
            Path("output.log"),
            Path("events.log"),
            "Install operation was cancelled",
            True,
        )
        self.done.set()
        return self.result

    def cancel(self, *, deadline=None) -> None:
        self.cancelled = True
        self.cancel_deadline = deadline
        self.cancel_event.set()

    def abort_before_start(self, error, *, cancelled=False) -> bool:
        self.cancelled = cancelled
        self.transaction._operation = None
        self.result = core.AddOperationResult(
            130 if cancelled else 1,
            (),
            Path("raw.log"),
            Path("output.log"),
            Path("events.log"),
            str(error),
            cancelled,
        )
        self.done.set()
        return True


class _UpdateTransaction:
    def __init__(self) -> None:
        self.discarded = False
        self.discard_operation = _DiscardOperation(self)

    def prepare_updates(self, **_) -> list[core.UpdateCandidate]:
        return []

    def discard(self) -> None:
        self.discarded = True

    def begin_discard(self):
        return self.discard_operation


class _DiscardOperation:
    def __init__(self, transaction, *, complete_on_start: bool = True) -> None:
        self.transaction = transaction
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.complete_on_start = complete_on_start
        self.starts = 0

    def start(self) -> None:
        self.starts += 1
        if self.complete_on_start:
            self.done.set()

    def raise_for_error(self) -> None:
        if self.error is not None:
            raise self.error


class _RegistryTransaction:
    active = True

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.discard_operation = _DiscardOperation(self, complete_on_start=False)
        self.discard_operation.error = error
        self.discard_deadlines: list[float | None] = []
        self.discard_error = error

    def begin_discard(self):
        return self.discard_operation

    def discard(self, *, deadline=None) -> None:
        self.discard_deadlines.append(deadline)
        if self.discard_error is not None:
            raise self.discard_error


class _BlockingUpdateTransaction(_UpdateTransaction):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def prepare_updates(self, *, cancel_event, on_progress, **_) -> list[core.UpdateCandidate]:
        self.started.set()
        on_progress(core.UpdateProgress("normalizing", 0, 1))
        cancel_event.wait(2)
        on_progress(core.UpdateProgress("cancelled", 0, 1))
        raise core.UpdatePreparationCancelled("cancelled")


class _PreparedUpdateTransaction(_UpdateTransaction):
    def __init__(self) -> None:
        super().__init__()
        self.prepare_controls: tuple[threading.Event, float] | None = None
        self.apply_controls: tuple[threading.Event | None, float | None] | None = None
        self.selected: set[Path] = set()

    def prepare_updates(self, *, cancel_event, deadline, **_) -> list[core.UpdateCandidate]:
        self.prepare_controls = (cancel_event, deadline)
        return [
            core.UpdateCandidate(
                "modrinth:first",
                Path("mods/first.pw.toml"),
                "first",
                "First",
                "modrinth",
                "v1",
                "v2",
                "update",
                (core.UpdateChange(Path("mods/first.pw.toml"), b"old", b"new"),),
            )
        ]

    def select_updates(self, selected: set[Path], **_) -> None:
        self.selected = set(selected)

    def apply(self, *, cancel_event=None, deadline=None) -> None:
        self.apply_controls = (cancel_event, deadline)


class _NavigationApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen) -> None:
        super().__init__()
        self.update_apply_workers = {}
        self._shutting_down = False
        self.initial_screen = screen
        self.transactions: dict[str, object] = {}

    def on_mount(self) -> None:
        self.push_screen(self.initial_screen)

    def go_main(self) -> None:
        self.switch_screen(huroshiki.MainMenuScreen())

    def open_project(self, project_key: str) -> bool:
        self.switch_screen(huroshiki.ProjectScreen(project_key))
        return True

    def open_list(self, project_key: str) -> None:
        self.switch_screen(huroshiki.InstalledModsScreen(project_key))

    def open_update(self, project_key: str) -> None:
        self.switch_screen(huroshiki.UpdateScreen(project_key))

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

    async def test_update_apply_reuses_preparation_controls(self) -> None:
        transaction = _PreparedUpdateTransaction()
        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
            return_value=transaction,
        ):
            screen = huroshiki.UpdateScreen("pack:demo")
            app = _NavigationApp(screen)
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.pause(0.15)
                    self.assertIsNotNone(transaction.prepare_controls)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                    await pilot.press("enter")
                    await pilot.pause(0.15)

        self.assertEqual(transaction.apply_controls, transaction.prepare_controls)
        self.assertEqual(transaction.selected, {Path("mods/first.pw.toml")})

    async def test_update_navigation_waits_for_discard_and_rejects_duplicates(self) -> None:
        transaction = _PreparedUpdateTransaction()
        transaction.discard_operation = _DiscardOperation(
            transaction,
            complete_on_start=False,
        )
        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
            return_value=transaction,
        ):
            screen = huroshiki.UpdateScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                await pilot.press("l")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertEqual(transaction.discard_operation.starts, 1)
                await pilot.press("i")
                self.assertEqual(transaction.discard_operation.starts, 1)

                transaction.discard_operation.done.set()
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, huroshiki.InstalledModsScreen)

    async def test_update_discard_failure_stays_on_screen(self) -> None:
        transaction = _PreparedUpdateTransaction()
        transaction.discard_operation = _DiscardOperation(
            transaction,
            complete_on_start=False,
        )
        transaction.discard_operation.error = core.TransactionDiscardIntegrityError(
            "cleanup failed"
        )
        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
            return_value=transaction,
        ):
            screen = huroshiki.UpdateScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                await pilot.press("l")
                transaction.discard_operation.done.set()
                await pilot.pause(0.1)
                self.assertIs(app.screen, screen)
                self.assertIs(screen.transaction, transaction)
                self.assertIs(app.transactions["pack:demo"], transaction)

    async def test_registry_retains_transaction_until_async_discard_succeeds(self) -> None:
        transaction = _RegistryTransaction()
        called: list[str] = []
        with self.patches():
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app.transactions["pack:demo"] = transaction
                app.discard_transaction("pack:demo", lambda: called.append("done"))
                await pilot.pause()
                self.assertIs(app.transactions["pack:demo"], transaction)
                self.assertEqual(called, [])

                transaction.discard_operation.done.set()
                await pilot.pause(0.1)
                self.assertNotIn("pack:demo", app.transactions)
                self.assertEqual(called, ["done"])

    async def test_registry_discard_failure_keeps_owner_and_skips_destination(self) -> None:
        error = core.TransactionDiscardIntegrityError("cleanup failed")
        transaction = _RegistryTransaction(error=error)
        called: list[str] = []
        with self.patches():
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app.transactions["pack:demo"] = transaction
                app.discard_transaction("pack:demo", lambda: called.append("done"))
                transaction.discard_operation.done.set()
                await pilot.pause(0.1)
                self.assertIs(app.transactions["pack:demo"], transaction)
                self.assertEqual(called, [])
                transaction.discard_error = None

    async def test_shutdown_uses_one_deadline_and_retains_failures(self) -> None:
        successful = _RegistryTransaction()
        failed = _RegistryTransaction(
            error=core.TransactionDiscardIntegrityError("cleanup failed")
        )
        app = huroshiki.HuroshikiApp()
        app.transactions = {
            "pack:success": successful,
            "pack:failed": failed,
        }
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            app.on_unmount()

        self.assertNotIn("pack:success", app.transactions)
        self.assertIs(app.transactions["pack:failed"], failed)
        self.assertEqual(successful.discard_deadlines, failed.discard_deadlines)
        self.assertIn("pack:failed", stderr.getvalue())
        self.assertIn("cleanup failed", stderr.getvalue())

    async def test_remove_transaction_does_not_pop_before_discard_success(self) -> None:
        error = core.TransactionDiscardIntegrityError("cleanup failed")
        transaction = _RegistryTransaction(error=error)
        app = huroshiki.HuroshikiApp()
        app.transactions["pack:demo"] = transaction

        with self.assertRaisesRegex(core.TransactionDiscardIntegrityError, "cleanup failed"):
            app.remove_transaction("pack:demo", discard=True)

        self.assertIs(app.transactions["pack:demo"], transaction)

    async def test_project_delete_waits_for_transaction_discard(self) -> None:
        transaction = _RegistryTransaction()
        with self.patches(), patch.object(
            huroshiki.core,
            "delete_project",
            return_value=SimpleNamespace(name="trashed-demo"),
        ) as delete:
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                screen = app.screen
                self.assertIsInstance(screen, huroshiki.MainMenuScreen)
                app.transactions["pack:demo"] = transaction
                screen.delete_confirmed("pack:demo", True)
                await pilot.pause()
                delete.assert_not_called()
                self.assertIs(app.transactions["pack:demo"], transaction)

                transaction.discard_operation.done.set()
                await pilot.pause(0.1)
                delete.assert_called_once_with("pack:demo")
                self.assertNotIn("pack:demo", app.transactions)

    async def test_update_worker_cancel_blocks_other_navigation_and_cleans_up(self) -> None:
        transaction = _BlockingUpdateTransaction()
        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
            return_value=transaction,
        ):
            screen = huroshiki.UpdateScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                self.assertTrue(transaction.started.wait(1))
                self.assertIsNotNone(screen.operation)
                await pilot.press("l", "space", "enter")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                await pilot.press("escape")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
        self.assertTrue(transaction.discarded)

    async def test_update_thread_start_failure_does_not_create_transaction(self) -> None:
        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
        ) as create, patch.object(
            threading.Thread, "start", side_effect=RuntimeError("start failed")
        ):
            screen = huroshiki.UpdateScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause()

        create.assert_not_called()
        self.assertTrue(screen.operation.done.is_set())

    async def test_update_escape_cancels_transaction_copy_before_navigation(self) -> None:
        started = threading.Event()

        def create(_, *, checkpoint):
            started.set()
            while True:
                checkpoint()
                time.sleep(0.01)

        with self.patches(), patch.object(
            huroshiki.core.PackTransaction,
            "create",
            side_effect=create,
        ):
            screen = huroshiki.UpdateScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                self.assertTrue(started.wait(1))
                await pilot.press("escape")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

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

    async def test_child_falls_back_to_main_if_project_becomes_invalid(self) -> None:
        current = {"project": PROJECT}
        with self.patches(), patch.object(
            huroshiki.core,
            "project_info",
            side_effect=lambda _: current["project"],
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app.switch_screen(huroshiki.InstalledModsScreen("pack:demo"))
                await pilot.pause()
                current["project"] = BROKEN_PROJECT

                await pilot.press("escape")
                await pilot.pause()

                self.assertIsInstance(app.screen, huroshiki.MainMenuScreen)
                self.assertIsNone(app.selected_project)

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

    async def test_install_waits_for_delayed_rollback_before_sibling_navigation(self) -> None:
        class DelayedOperation(_Operation):
            def cancel(self) -> None:
                self.cancelled = True

        with self.patches():
            transaction = _Transaction()
            operation = DelayedOperation()
            install = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(install)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                install.operation = operation
                install.query_one("#search-results-table").focus()
                await pilot.press("l", "p", "l")
                await pilot.pause()
                self.assertTrue(operation.cancelled)
                self.assertIs(app.screen, install)
                transaction.changes.append("rollback")
                operation.done.set()
                await pilot.pause(0.1)

                self.assertIsInstance(app.screen, huroshiki.InstalledModsScreen)
                transaction.changes.append("post-navigation edit")
                self.assertEqual(
                    transaction.changes,
                    ["rollback", "post-navigation edit"],
                )

    async def test_url_checkpoint_worker_keeps_event_loop_responsive_and_defers_escape(self) -> None:
        transaction = _InstallTransaction()
        operation = _BlockingInstallOperation(transaction)
        transaction.queued_operations.append(operation)
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            screen.provider = "url"
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "https://example.invalid/private.jar"
                await pilot.press("enter")
                await pilot.pause(0.05)

                self.assertTrue(operation.started.is_set())
                self.assertEqual(screen.state, "resolving")
                self.assertIs(app.screen, screen)
                assert screen.operation_thread is not None
                self.assertFalse(screen.operation_thread.daemon)

                await pilot.press("escape")
                await pilot.pause(0.05)
                self.assertTrue(operation.cancel_event.is_set())
                self.assertTrue(operation.cleanup_started.is_set())
                self.assertIs(app.screen, screen)
                self.assertEqual(operation.cancel_deadline, screen._navigation_deadline)

                operation.release_cleanup.set()
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

    async def test_resolved_checkpoint_worker_defers_navigation_until_cleanup(self) -> None:
        transaction = _InstallTransaction()
        operation = _BlockingInstallOperation(transaction)
        transaction.queued_operations.append(operation)
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "mr:canonical"
                await pilot.press("enter")
                await pilot.pause(0.05)
                self.assertTrue(operation.started.is_set())
                self.assertIs(app.screen, screen)

                await pilot.press("escape")
                await pilot.pause(0.05)
                self.assertTrue(operation.cancel_event.is_set())
                self.assertIs(app.screen, screen)

                operation.release_cleanup.set()
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

    async def test_install_worker_start_failure_restores_idle_and_allows_retry(self) -> None:
        transaction = _InstallTransaction()
        failed_operation = _BlockingInstallOperation(transaction)
        transaction.queued_operations.append(failed_operation)
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            screen.provider = "url"
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "https://example.invalid/private.jar"
                with patch.object(
                    threading.Thread,
                    "start",
                    side_effect=RuntimeError("start failed"),
                ):
                    await pilot.press("enter")
                    await pilot.pause()

                self.assertTrue(failed_operation.done.is_set())
                self.assertIsNone(transaction._operation)
                self.assertIsNone(screen.operation)
                self.assertIsNone(screen.operation_thread)
                self.assertEqual(screen.state, "idle")
                self.assertFalse(field.disabled)

                field.value = "https://example.invalid/retry.jar"
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertEqual(len(transaction.add_calls), 2)
                self.assertEqual(screen.state, "idle")

    async def test_install_stays_for_already_done_incomplete_pty_cleanup(self) -> None:
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                operation = _PackwizCleanupOperation(done=True, incomplete=True)
                screen.operation = operation
                screen.query_one("#search-results-table").focus()
                await pilot.press("l")
                await pilot.pause()

                self.assertIs(app.screen, screen)
                self.assertIsNone(screen._pending_navigation)

    async def test_install_stays_when_cancelled_pty_cleanup_is_incomplete(self) -> None:
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                operation = _PackwizCleanupOperation(done=False, incomplete=False)
                screen.operation = operation
                screen.query_one("#search-results-table").focus()
                await pilot.press("l")
                await pilot.pause()
                self.assertIsNotNone(operation.cancel_deadline)
                self.assertEqual(operation.cancel_deadline, screen._navigation_deadline)
                self.assertIs(app.screen, screen)

                operation.termination_result = core.ProcessTerminationResult(
                    False,
                    False,
                    True,
                )
                operation.termination_incomplete = True
                operation.done.set()
                await pilot.pause(0.1)

                self.assertIs(app.screen, screen)
                self.assertIsNone(screen._pending_navigation)

    async def test_install_stays_when_checkpoint_handoff_cleanup_failed(self) -> None:
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            async with app.run_test() as pilot:
                operation = _PackwizCleanupOperation(done=True, incomplete=False)
                operation.cleanup_error = OSError("checkpoint handoff stalled")
                screen.operation = operation
                screen.query_one("#search-results-table").focus()
                await pilot.press("l")
                await pilot.pause()

                self.assertIs(app.screen, screen)
                self.assertIsNone(screen._pending_navigation)
                self.assertIn(
                    "cleanup failed",
                    str(screen.query_one("#packwiz-status").render()),
                )

    async def test_install_completion_retains_incomplete_pty_until_navigation_retry(self) -> None:
        transaction = _InstallTransaction()
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                operation = _PackwizCleanupOperation(
                    done=True,
                    incomplete=True,
                    complete_on_cancel=True,
                )
                result = core.AddOperationResult(
                    1,
                    (),
                    Path("raw.log"),
                    Path("output.log"),
                    Path("events.log"),
                    "Install operation deadline exceeded",
                    timed_out=True,
                )
                screen.operation = operation
                screen._operation_finished(operation, result)

                self.assertIs(screen.operation, operation)
                self.assertIs(app.screen, screen)
                self.assertIn(
                    "cleanup was incomplete",
                    str(screen.query_one("#packwiz-status").render()),
                )

                screen.query_one("#search-results-table").focus()
                await pilot.press("l")
                await pilot.pause()

                self.assertIsNotNone(operation.cancel_deadline)
                self.assertIsNone(screen.operation)
                self.assertIsInstance(app.screen, huroshiki.InstalledModsScreen)

    async def test_install_search_shows_canonical_ids_and_resolves_selection(self) -> None:
        projects = (
            core.ProviderProject(
                "modrinth", "canonical-one", "sodium-extra", "Sodium Extra", "First", "A"
            ),
            core.ProviderProject(
                "modrinth", "canonical-two", "other", "Sodium Extra", "Second", "B"
            ),
        )
        transaction = _InstallTransaction()
        with self.patches(), patch.object(
            huroshiki.core.packctl,
            "project_versions",
            return_value=("1.21.1", "neoforge", "21.1.0"),
        ), patch.object(
            huroshiki.core, "search_provider_projects", return_value=projects
        ):
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                search = screen.query_one("#mod-search", Input)
                search.value = "Sodium Extra"
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(screen.state, "showing_results")
                self.assertEqual(
                    [item.project_id for item in screen.search_results],
                    ["canonical-one", "canonical-two"],
                )
                await pilot.press("enter")
                await pilot.pause(0.2)

        self.assertEqual(len(transaction.resolved_calls), 1)
        self.assertEqual(
            transaction.resolved_calls[0]["canonical_project_id"], "canonical-one"
        )

    async def test_bare_single_word_modrinth_input_uses_provider_search(self) -> None:
        projects = (
            core.ProviderProject(
                "modrinth", "canonical", "sodium", "Sodium", "Renderer", "author"
            ),
        )
        transaction = _InstallTransaction()
        with self.patches(), patch.object(
            huroshiki.core.packctl,
            "project_versions",
            return_value=("1.21.1", "neoforge", "21.1.0"),
        ), patch.object(
            huroshiki.core, "search_provider_projects", return_value=projects
        ) as search:
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "sodium"
                await pilot.press("enter")
                await pilot.pause(0.2)

                self.assertEqual(screen.state, "showing_results")
                self.assertEqual(screen.search_results[0].project_id, "canonical")

        self.assertEqual(search.call_args.args[:2], ("modrinth", "sodium"))
        self.assertEqual(transaction.resolved_calls, [])

    async def test_explicit_modrinth_selectors_bypass_provider_search(self) -> None:
        cases = (
            ("mr:sodium", "sodium"),
            ("https://modrinth.com/mod/sodium", "https://modrinth.com/mod/sodium"),
        )
        for query, selector in cases:
            with self.subTest(query=query):
                transaction = _InstallTransaction()
                with self.patches(), patch.object(
                    huroshiki.core, "search_provider_projects"
                ) as search:
                    screen = huroshiki.InstallScreen("pack:demo")
                    app = _NavigationApp(screen)
                    app.transactions["pack:demo"] = transaction
                    async with app.run_test() as pilot:
                        field = screen.query_one("#mod-search", Input)
                        field.value = query
                        await pilot.press("enter")
                        await pilot.pause(0.2)

                search.assert_not_called()
                self.assertEqual(len(transaction.resolved_calls), 1)
                self.assertEqual(transaction.resolved_calls[0]["provider"], "modrinth")
                self.assertEqual(transaction.resolved_calls[0]["selector"], selector)
                self.assertIsNone(
                    transaction.resolved_calls[0]["canonical_project_id"]
                )

    async def test_install_search_cancel_waits_before_navigation(self) -> None:
        transaction = _InstallTransaction()
        started = threading.Event()

        def search(*_, cancel_event, **__):
            started.set()
            cancel_event.wait(2)
            raise core.HuroshikiError("Provider lookup was cancelled")

        with self.patches(), patch.object(
            huroshiki.core.packctl,
            "project_versions",
            return_value=("1.21.1", "neoforge", "21.1.0"),
        ), patch.object(huroshiki.core, "search_provider_projects", side_effect=search):
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "Sodium Extra"
                await pilot.press("enter")
                await pilot.pause(0.05)
                self.assertTrue(started.is_set())
                await pilot.press("escape")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

    async def test_curseforge_input_starts_packwiz_operation(self) -> None:
        transaction = _InstallTransaction()
        with self.patches(), patch.object(
            huroshiki.core, "search_provider_projects"
        ) as search:
            operation = _PackwizMenuOperation()
            transaction.queued_operations.append(operation)
            screen = huroshiki.InstallScreen("pack:demo")
            screen.provider = "curseforge"
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "Create"
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertTrue(operation.menu_ready.wait(1))
                self.assertIs(screen.operation, operation)
                self.assertIsInstance(operation, core.PackwizAddOperation)
                self.assertEqual(transaction.add_calls[0]["provider"], "curseforge")
                search.assert_not_called()
                operation.release.set()

    async def test_packwiz_menu_is_identity_pending_and_selection_uses_exact_index(self) -> None:
        transaction = _InstallTransaction()
        operation = _PackwizMenuOperation()
        transaction.queued_operations.append(operation)
        with self.patches():
            screen = huroshiki.InstallScreen("pack:demo")
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "Create"
                screen.provider = "curseforge"
                await pilot.press("enter")
                self.assertTrue(operation.menu_ready.wait(1))
                await pilot.pause()

                row = screen.query_one("#search-results-table", DataTable).get_row_at(0)
                self.assertEqual(row[0], "Create")
                self.assertEqual(row[2], "pending verification")
                self.assertEqual(row[3], "Packwiz candidate label")
                await pilot.press("j")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(operation.menu_selection, [9])
                self.assertEqual(screen.search_results, [])
                self.assertEqual(transaction.resolved_calls, [])
                operation.release.set()

    async def test_numeric_curseforge_id_uses_packwiz_identity_verification(self) -> None:
        transaction = _InstallTransaction()
        with self.patches(), patch.object(
            huroshiki.core, "search_provider_projects"
        ) as search:
            operation = _PackwizMenuOperation()
            transaction.queued_operations.append(operation)
            screen = huroshiki.InstallScreen("pack:demo")
            screen.provider = "curseforge"
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "000328085"
                await pilot.press("enter")
                await pilot.pause(0.2)
        search.assert_not_called()
        self.assertEqual(transaction.add_calls[0]["selector"], "328085")
        self.assertIsInstance(operation, core.PackwizAddOperation)
        operation.release.set()


if __name__ == "__main__":
    unittest.main()
