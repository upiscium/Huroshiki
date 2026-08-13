from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

import huroshiki
import huroshiki_core as core
from textual.app import App


class _VersionApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, mod: core.ModInfo) -> None:
        super().__init__()
        self.mod = mod
        self.transactions: dict[str, core.PackTransaction] = {}
        self.update_apply_workers: dict[str, object] = {}
        self.exact_version_workers: dict[str, object] = {}
        self._shutting_down = False

    def on_mount(self) -> None:
        self.selected_project = "pack:demo"
        self.push_screen(huroshiki.InstalledModDetailsScreen("pack:demo", self.mod))

    def open_list(self, _project_key: str) -> None:
        pass


def mod_info(project_id: str = "A1b2C3d4") -> core.ModInfo:
    return core.ModInfo(
        Path("mods/example.pw.toml"),
        "example",
        "Example MOD",
        "MR",
        project_id,
        "example.jar",
        True,
        False,
    )


def preview() -> core.ModVersionSelectionPreview:
    return core.ModVersionSelectionPreview(
        "modrinth:A1b2C3d4",
        Path("mods/example.pw.toml"),
        "Example MOD",
        "modrinth",
        "1.0",
        "OldVer01",
        "2.0",
        "E5f6G7h8",
        (core.UpdateChange(Path("mods/example.pw.toml"), b"old", b"new"),),
        1,
        0,
        ("modrinth:Added001",),
        (),
        "modrinth:A1b2C3d4",
        "E5f6G7h8",
        False,
    )


def completed_discard(transaction, error: BaseException | None = None):
    operation = MagicMock()
    operation.transaction = transaction
    operation.done = threading.Event()
    operation.done.set()
    if error is not None:
        operation.raise_for_error.side_effect = error
    return operation


class InstalledModVersionTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_details_exposes_role_and_exact_version_input(self) -> None:
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, huroshiki.InstalledModDetailsScreen)
                details = screen.query_one("#mod-version-details", huroshiki.Static)
                self.assertIn("Canonical project ID: A1b2C3d4", str(details.render()))
                await pilot.pause(0.1)
                self.assertIn("Role: Explicit root", str(details.render()))
                self.assertIn("Select version", screen.help_text)
                artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                self.assertEqual(artifact.placeholder, "Exact file/version ID")

    async def test_prepare_runs_off_loop_shows_progress_and_cancel_does_not_apply(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.discard.side_effect = lambda **_: setattr(transaction, "active", False)
        worker_threads: list[int] = []

        def prepare(selection, *, progress, **_kwargs):
            worker_threads.append(threading.get_ident())
            progress(core.ModVersionSelectionProgress("resolving", "Resolving exact MOD"))
            return preview()

        transaction.prepare_exact_mod_version.side_effect = prepare
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Dependency"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            caller = threading.get_ident()
            async with app.run_test() as pilot:
                screen = app.screen
                artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                artifact.value = "E5f6G7h8"
                await pilot.press("enter")
                await pilot.pause(0.2)
                status = str(
                    screen.query_one("#mod-version-status", huroshiki.Static).render()
                )
                self.assertIn("Artifact ID: OldVer01 -> E5f6G7h8", status)
                self.assertIn("modrinth:Added001", status)
                self.assertTrue(worker_threads)
                self.assertNotEqual(worker_threads[0], caller)
                self.assertIs(app.transactions["pack:demo"], transaction)
                screen.cancel_and_navigate(None)
                await pilot.pause(0.2)
                transaction.apply.assert_not_called()

    async def test_apply_publishes_verified_preview_in_worker(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.apply.side_effect = lambda: setattr(transaction, "active", False)
        apply_threads: list[int] = []

        def apply(**_kwargs) -> None:
            apply_threads.append(threading.get_ident())
            transaction.active = False

        transaction.apply.side_effect = apply
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            caller = threading.get_ident()
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                    artifact.value = "E5f6G7h8"
                    await pilot.press("enter")
                    await pilot.pause(0.2)
                    screen.apply_preview()
                    await pilot.pause(0.2)
                    transaction.apply.assert_called_once()
                    self.assertIs(
                        transaction.apply.call_args.kwargs["cancel_event"],
                        screen.cancel_event,
                    )
                    self.assertEqual(
                        transaction.apply.call_args.kwargs["deadline"], screen.deadline
                    )
                    self.assertNotEqual(apply_threads[0], caller)
                    open_list.assert_called_once_with("pack:demo")

    async def test_back_during_prepare_cancels_then_waits_for_discard(self) -> None:
        started = threading.Event()
        transaction = MagicMock()
        transaction.active = True

        def prepare(*_args, cancel_event, **_kwargs):
            started.set()
            cancel_event.wait(2)
            raise core.ExactModVersionCancelled("cancelled")

        transaction.prepare_exact_mod_version.side_effect = prepare
        transaction.discard.side_effect = lambda **_: setattr(transaction, "active", False)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Dependency"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                    artifact.value = "E5f6G7h8"
                    await pilot.press("enter")
                    self.assertTrue(started.wait(1))
                    screen.cancel_and_navigate(lambda: app.open_list("pack:demo"))
                    self.assertFalse(open_list.called)
                    await pilot.pause(0.3)
                    self.assertTrue(open_list.called)

    async def test_prepare_failure_leaves_detail_screen_usable(self) -> None:
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Dependency"
        ), patch.object(
            core.PackTransaction,
            "create",
            side_effect=core.HuroshikiError("resolver unavailable"),
        ):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                screen = app.screen
                artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                artifact.value = "E5f6G7h8"
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertIs(app.screen, screen)
                self.assertIsNone(screen.prepare_thread)
                self.assertIsNone(screen.transaction)
                self.assertIn(
                    "resolver unavailable",
                    str(
                        screen.query_one(
                            "#mod-version-status", huroshiki.Static
                        ).render()
                    ),
                )
                artifact.value = "E5f6G7h8"

    async def test_incomplete_discard_blocks_navigation_and_retains_transaction(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        discard = MagicMock()
        discard.transaction = transaction
        discard.done = threading.Event()
        discard.done.set()
        discard.raise_for_error.side_effect = core.TransactionDiscardIntegrityError(
            "cleanup incomplete"
        )
        transaction.begin_discard.return_value = discard
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                    artifact.value = "E5f6G7h8"
                    await pilot.press("enter")
                    await pilot.pause(0.2)
                    screen.cancel_and_navigate(lambda: app.open_list("pack:demo"))
                    await pilot.pause(0.2)
                    open_list.assert_not_called()
                    self.assertIs(screen.transaction, transaction)
                    self.assertIs(app.transactions["pack:demo"], transaction)
                    self.assertIn(
                        "cleanup incomplete",
                        str(
                            screen.query_one(
                                "#mod-version-status", huroshiki.Static
                            ).render()
                        ),
                    )

    async def test_apply_failure_invalidates_preview_and_returns_to_input(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.apply.side_effect = core.HuroshikiError("apply verification failed")
        transaction.begin_discard.return_value = completed_discard(transaction)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                screen = app.screen
                artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                artifact.value = "E5f6G7h8"
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertIsNotNone(screen.preview)

                screen.apply_preview()
                await pilot.pause(0.3)

                transaction.apply.assert_called_once()
                self.assertIsNone(screen.preview)
                self.assertIsNone(screen.transaction)
                self.assertNotIn("pack:demo", app.transactions)
                self.assertIn(
                    "apply verification failed",
                    str(
                        screen.query_one(
                            "#mod-version-status", huroshiki.Static
                        ).render()
                    ),
                )
                screen.apply_preview()
                transaction.apply.assert_called_once()
                artifact.value = "E5f6G7h8"

    async def test_apply_failure_incomplete_discard_retains_only_cleanup_ownership(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.apply.side_effect = core.HuroshikiError("apply failed")
        transaction.begin_discard.return_value = completed_discard(
            transaction,
            core.TransactionDiscardIntegrityError("cleanup incomplete"),
        )
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Dependency"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                    artifact.value = "E5f6G7h8"
                    await pilot.press("enter")
                    await pilot.pause(0.2)

                    screen.apply_preview()
                    await pilot.pause(0.3)

                    self.assertIsNone(screen.preview)
                    self.assertIs(screen.transaction, transaction)
                    self.assertIs(app.transactions["pack:demo"], transaction)
                    self.assertIn(
                        "cleanup incomplete",
                        str(
                            screen.query_one(
                                "#mod-version-status", huroshiki.Static
                            ).render()
                        ),
                    )
                    open_list.assert_not_called()
                    screen.apply_preview()
                    transaction.apply.assert_called_once()

    async def test_preview_review_time_does_not_consume_apply_deadline(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.apply.side_effect = lambda **_: setattr(transaction, "active", False)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list"):
                async with app.run_test() as pilot:
                    screen = app.screen
                    artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                    artifact.value = "E5f6G7h8"
                    await pilot.press("enter")
                    await pilot.pause(0.2)
                    prepare_deadline = screen.deadline
                    prepare_event = screen.cancel_event
                    assert prepare_deadline is not None

                    advanced = prepare_deadline + 60.0
                    with patch.object(huroshiki.time, "monotonic", return_value=advanced):
                        screen.apply_preview()
                    await pilot.pause(0.2)

                    apply_call = transaction.apply.call_args
                    self.assertIsNot(
                        apply_call.kwargs["cancel_event"], prepare_event
                    )
                    self.assertGreater(apply_call.kwargs["deadline"], advanced)
                    self.assertNotEqual(
                        apply_call.kwargs["deadline"], prepare_deadline
                    )

    async def test_cancel_during_apply_uses_fresh_event_and_waits_for_cleanup(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.begin_discard.return_value = completed_discard(transaction)
        apply_started = threading.Event()
        apply_stopped = threading.Event()
        apply_events: list[threading.Event] = []

        def apply(*, cancel_event, **_kwargs):
            apply_events.append(cancel_event)
            apply_started.set()
            cancel_event.wait(2)
            apply_stopped.set()
            raise core.ExactModVersionCancelled("apply cancelled")

        transaction.apply.side_effect = apply
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                    artifact.value = "E5f6G7h8"
                    await pilot.press("enter")
                    await pilot.pause(0.2)
                    prepare_event = screen.cancel_event

                    screen.apply_preview()
                    self.assertTrue(apply_started.wait(1))
                    self.assertIsNot(apply_events[0], prepare_event)
                    screen.cancel_and_navigate(lambda: app.open_list("pack:demo"))
                    self.assertTrue(apply_events[0].is_set())
                    open_list.assert_not_called()
                    await pilot.pause(0.3)

                    self.assertTrue(apply_stopped.is_set())
                    open_list.assert_called_once_with("pack:demo")

    async def test_apply_worker_start_failure_invalidates_and_discards(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.begin_discard.return_value = completed_discard(transaction)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                screen = app.screen
                artifact = screen.query_one("#mod-version-artifact", huroshiki.Input)
                artifact.value = "E5f6G7h8"
                await pilot.press("enter")
                await pilot.pause(0.2)

                worker_registered_before_start = False

                def fail_start():
                    nonlocal worker_registered_before_start
                    worker_registered_before_start = (
                        app.update_apply_workers.get("pack:demo", (None,))[0]
                        is screen.apply_thread
                    )
                    raise RuntimeError("start failed")

                with patch.object(
                    threading.Thread, "start", side_effect=fail_start
                ):
                    screen.apply_preview()
                await pilot.pause(0.2)

                self.assertTrue(worker_registered_before_start)
                self.assertNotIn("pack:demo", app.update_apply_workers)
                self.assertIsNone(screen.preview)
                self.assertIsNone(screen.transaction)
                transaction.apply.assert_not_called()
                self.assertIn(
                    "start failed",
                    str(
                        screen.query_one(
                            "#mod-version-status", huroshiki.Static
                        ).render()
                    ),
                )


class InstalledModProvenanceTest(unittest.TestCase):
    def test_manifest_distinguishes_root_and_dependency(self) -> None:
        root = core.PackRootRecord("modrinth", "A1b2C3d4", "client")
        with patch.object(core, "project_source", return_value=Path("/pack/source")), patch.object(
            core, "read_pack_root_manifest", return_value=(root,)
        ):
            self.assertEqual(
                core.installed_mod_provenance("pack:demo", mod_info()), "Explicit root"
            )
            self.assertEqual(
                core.installed_mod_provenance("pack:demo", mod_info("Z9y8X7w6")),
                "Dependency",
            )


if __name__ == "__main__":
    unittest.main()
