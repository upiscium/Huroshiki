from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
from pathlib import Path
import threading
import time
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

    def __init__(self) -> None:
        self.changes: list[str] = []

    def staged_mods(self) -> list[core.ModInfo]:
        return []


class _Operation:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
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


class _InstallTransaction(_Transaction):
    source = Path("/fake/source")

    def __init__(self) -> None:
        super().__init__()
        self.resolved_calls: list[dict[str, object]] = []

    def begin_resolved_add(self, **kwargs):
        self.resolved_calls.append(kwargs)
        return _ResolvedOperation()

class _UpdateTransaction:
    def __init__(self) -> None:
        self.discarded = False

    def prepare_updates(self, **_) -> list[core.UpdateCandidate]:
        return []

    def discard(self) -> None:
        self.discarded = True


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

    async def test_curseforge_name_search_is_rejected_before_worker(self) -> None:
        transaction = _InstallTransaction()
        with self.patches(), patch.object(
            huroshiki.core, "search_provider_projects"
        ) as search:
            screen = huroshiki.InstallScreen("pack:demo")
            screen.provider = "curseforge"
            app = _NavigationApp(screen)
            app.transactions["pack:demo"] = transaction
            async with app.run_test() as pilot:
                field = screen.query_one("#mod-search", Input)
                field.value = "Create"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("numeric CurseForge", str(screen.query_one("#packwiz-status").render()))
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
