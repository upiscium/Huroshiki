from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, call, patch

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
        self.version_catalog_workers: dict[str, huroshiki.VersionCatalogWorker] = {}
        self._version_catalog_generation: dict[str, int] = {}
        self._shutting_down = False

    def on_mount(self) -> None:
        self.selected_project = "pack:demo"
        self.push_screen(huroshiki.InstalledModDetailsScreen("pack:demo", self.mod))

    def open_list(self, _project_key: str) -> None:
        pass

    def open_mod_version_browser(self, project_key: str, mod: core.ModInfo) -> None:
        self.switch_screen(huroshiki.InstalledModVersionBrowserScreen(project_key, mod))


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


def intent_status(
    *,
    selection: str = "user",
    installed: str | None = "E5f6G7h8",
    selected: str | None = "E5f6G7h8",
    locked: bool | None = False,
    reason: str | None = "test",
    override_status: str | None = "active",
) -> core.ModVersionIntentStatus:
    return core.ModVersionIntentStatus(
        "modrinth:A1b2C3d4", selection, installed, selected, locked, reason,
        override_status,
    )


def intent_preview(action: str) -> core.ModVersionIntentPreview:
    new_selection = "automatic" if action == "automatic" else "user"
    return core.ModVersionIntentPreview(
        "modrinth:A1b2C3d4", "E5f6G7h8", "E5f6G7h8", "user", new_selection,
        False if action == "automatic" else (False if action == "pin" else True),
        None if action == "automatic" else (True if action == "pin" else False),
        "test", "active", (),
    )


def candidate(
    artifact_id: str,
    *,
    version: str = "1.0",
    release_type: str = "release",
    filename: str = "example.jar",
) -> core.ModVersionCandidate:
    return core.ModVersionCandidate(
        "modrinth",
        core.canonical_modrinth_id("A1b2C3d4"),
        core.canonical_modrinth_id(artifact_id),
        version,
        filename,
        ("1.21.1",), ("fabric",), release_type, "2026-01-02T03:04:05Z",
    )


def candidate_view(
    artifact_id: str,
    *,
    current: bool = False,
    selected: bool = False,
    pinned: bool = False,
    compatible: bool = True,
    notes: tuple[str, ...] = (),
) -> core.ModVersionCandidateView:
    return core.ModVersionCandidateView(
        candidate(artifact_id), current, selected, pinned, compatible, notes
    )


def catalog(
    *views: core.ModVersionCandidateView,
    selection: str = "user",
    selected: str | None = "new-id",
    missing: bool = False,
) -> core.ModVersionCandidateCatalog:
    return core.ModVersionCandidateCatalog(
        "modrinth:A1b2C3d4", "1.21.1", "fabric", tuple(views),
        intent_status(selection=selection, selected=selected), missing,
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
    async def asyncSetUp(self) -> None:
        intent_patcher = patch.object(
            core, "installed_mod_version_intent", return_value=intent_status()
        )
        self.intent_mock = intent_patcher.start()
        self.addAsyncCleanup(intent_patcher.stop)

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

    async def test_details_displays_automatic_and_all_user_intent_states(self) -> None:
        cases = (
            ("automatic", None, "Selection: Automatic", "Pin: N/A"),
            ("user", False, "Pin: Unlocked", "Status: active"),
            ("user", True, "Pin: Locked", "Status: active"),
            ("user", False, "Status: drifted", "Selected artifact: E5f6G7h8"),
            ("user", True, "Status: stale", "Installed artifact: <missing>"),
        )
        for selection, locked, expected, also_expected in cases:
            self.intent_mock.return_value = intent_status(
                selection=selection,
                locked=None if selection == "automatic" else locked,
                installed=None if also_expected.endswith("<missing>") else "E5f6G7h8",
                override_status=(None if selection == "automatic" else
                                 ("drifted" if expected.endswith("drifted") else
                                  "stale" if expected.endswith("stale") else "active")),
            )
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                details = app.screen.query_one("#mod-version-details", huroshiki.Static)
                rendered = str(details.render())
                self.assertIn(expected, rendered)
                self.assertIn(also_expected, rendered)

    async def test_navigation_waits_for_cancellable_detail_loading_worker(self) -> None:
        started = threading.Event()
        stopped = threading.Event()

        def provenance(*_args, cancel_event, **_kwargs):
            started.set()
            cancel_event.wait(2)
            stopped.set()
            raise core.ExactModVersionCancelled("detail loading cancelled")

        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", side_effect=provenance
        ):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    self.assertTrue(started.wait(1))
                    screen.cancel_and_navigate(lambda: app.open_list("pack:demo"))
                    open_list.assert_not_called()
                    await pilot.pause(0.2)
                    self.assertTrue(stopped.is_set())
                    open_list.assert_called_once_with("pack:demo")

    async def test_intent_controls_availability_and_no_duplicate_transaction_ownership(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_mod_version_pin.return_value = intent_preview("pin")
        transaction.prepare_mod_version_automatic.return_value = intent_preview("automatic")
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                screen = app.screen
                await pilot.pause(0.1)
                screen.start_intent_prepare("pin")
                screen.start_intent_prepare("pin")
                await pilot.pause(0.2)
                pin_call = transaction.prepare_mod_version_pin.call_args
                self.assertEqual(pin_call.args, ("modrinth:A1b2C3d4",))
                self.assertTrue(pin_call.kwargs["locked"])
                self.assertIs(pin_call.kwargs["cancel_event"], screen.cancel_event)
                self.assertEqual(pin_call.kwargs["deadline"], screen.deadline)
                self.assertIs(app.transactions["pack:demo"], screen.transaction)
                self.assertIn("Selection: User exact -> User exact", str(
                    screen.query_one("#mod-version-status", huroshiki.Static).render()
                ))
                screen.cancel_and_navigate(None)
                await pilot.pause(0.2)
                self.assertIsNone(screen.transaction)
                self.assertNotIn("pack:demo", app.transactions)

    async def test_ctrl_r_ctrl_k_ctrl_u_dispatch_available_intent_actions(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.prepare_mod_version_automatic.return_value = intent_preview("automatic")
        transaction.prepare_mod_version_pin.return_value = intent_preview("pin")
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                screen = app.screen
                await pilot.pause(0.1)
                with patch.object(screen, "start_intent_prepare") as start:
                    await pilot.press("ctrl+r")
                    await pilot.press("ctrl+k")
                    await pilot.press("ctrl+u")
                self.assertEqual(
                    [call.args[0] for call in start.call_args_list],
                    ["automatic", "pin", "unpin"],
                )

    async def test_automatic_rejects_drifted_and_stale_before_transaction(self) -> None:
        for override_status in ("drifted", "stale"):
            with self.subTest(status=override_status):
                self.intent_mock.return_value = intent_status(
                    override_status=override_status
                )
                with patch.object(core, "project_config", return_value={}), patch.object(
                    core, "installed_mod_provenance", return_value="Explicit root"
                ), patch.object(core.PackTransaction, "create") as create:
                    app = _VersionApp(mod_info())
                    async with app.run_test() as pilot:
                        screen = app.screen
                        await pilot.pause(0.1)
                        screen.start_intent_prepare("automatic")
                        await pilot.pause()
                        create.assert_not_called()
                        self.assertIsNone(screen.transaction)
                        self.assertIsNone(screen.prepare_thread)

    async def test_automatic_pin_and_unpin_preview_apply_without_refresh(self) -> None:
        for action, locked, status in (
            ("automatic", None, intent_status()),
            ("pin", False, intent_status(locked=False)),
            ("unpin", True, intent_status(locked=True)),
        ):
            self.intent_mock.return_value = status
            transaction = MagicMock()
            transaction.active = True
            transaction.prepare_mod_version_automatic.return_value = intent_preview(action)
            transaction.prepare_mod_version_pin.return_value = intent_preview(action)
            transaction.apply.side_effect = lambda **_: setattr(transaction, "active", False)
            with patch.object(core, "project_config", return_value={}), patch.object(
                core, "installed_mod_provenance", return_value="Explicit root"
            ), patch.object(core.PackTransaction, "create", return_value=transaction):
                app = _VersionApp(mod_info())
                with patch.object(app, "open_list"):
                    async with app.run_test() as pilot:
                        screen = app.screen
                        await pilot.pause(0.1)
                        screen.start_intent_prepare(action)
                        await pilot.pause(0.2)
                        self.assertIsInstance(screen.preview, core.ModVersionIntentPreview)
                        prepare_event = screen.cancel_event
                        prepare_deadline = screen.deadline
                        screen.apply_preview()
                        await pilot.pause(0.2)
                        self.assertEqual(transaction.apply.call_args.kwargs["refresh"], False)
                        if action == "automatic":
                            intent_call = (
                                transaction.prepare_mod_version_automatic.call_args
                            )
                        else:
                            intent_call = transaction.prepare_mod_version_pin.call_args
                            self.assertEqual(
                                intent_call.kwargs["locked"], action == "pin"
                            )
                        self.assertEqual(
                            intent_call.args, ("modrinth:A1b2C3d4",)
                        )
                        self.assertIs(
                            intent_call.kwargs["cancel_event"], prepare_event
                        )
                        self.assertEqual(
                            intent_call.kwargs["deadline"], prepare_deadline
                        )

    async def test_intent_navigation_waits_for_worker_and_discard_cleanup(self) -> None:
        started = threading.Event()
        release = threading.Event()
        worker_threads: list[int] = []
        transaction = MagicMock()
        transaction.active = True

        def prepare(_identity, **_kwargs):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(2)
            return intent_preview("automatic")

        transaction.prepare_mod_version_automatic.side_effect = prepare
        transaction.begin_discard.return_value = completed_discard(transaction)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            caller = threading.get_ident()
            with patch.object(app, "open_list") as open_list:
                async with app.run_test() as pilot:
                    screen = app.screen
                    await pilot.pause(0.1)
                    screen.start_intent_prepare("automatic")
                    self.assertTrue(started.wait(1))
                    screen.cancel_and_navigate(lambda: app.open_list("pack:demo"))
                    open_list.assert_not_called()
                    release.set()
                    await pilot.pause(0.3)
                    open_list.assert_called_once_with("pack:demo")
                    transaction.begin_discard.assert_called_once_with()
                    self.assertNotEqual(worker_threads[0], caller)

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
                        app.exact_version_workers.get("pack:demo", (None,))[0]
                        is screen.apply_thread
                    )
                    raise RuntimeError("start failed")

                with patch.object(
                    threading.Thread, "start", side_effect=fail_start
                ):
                    screen.apply_preview()
                await pilot.pause(0.2)

                self.assertTrue(worker_registered_before_start)
                self.assertNotIn("pack:demo", app.exact_version_workers)
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

    async def test_modrinth_details_advertises_catalog_and_preserves_exact_id(self) -> None:
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                screen = app.screen
                prompt = screen.query_one("#mod-version-prompt", huroshiki.Static)
                self.assertIn("Compatible versions: Ctrl+V", str(prompt.render()))
                self.assertEqual(
                    screen.query_one("#mod-version-artifact", huroshiki.Input).placeholder,
                    "Exact file/version ID",
                )
                self.assertIn("Canonical project ID: A1b2C3d4", str(
                    screen.query_one("#mod-version-details", huroshiki.Static).render()
                ))

    async def test_curseforge_catalog_fallback_does_not_start_worker_or_transaction(self) -> None:
        mod = core.ModInfo(Path("mods/cf.pw.toml"), "cf", "Curse MOD", "curseforge", "12345", "cf.jar", True, False)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Dependency"
        ), patch.object(core, "list_mod_version_candidates") as listing, patch.object(
            core.PackTransaction, "create"
        ) as create:
            app = _VersionApp(mod)
            async with app.run_test() as pilot:
                await pilot.pause(0.1)
                screen = app.screen
                screen.action_browse_versions()
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertFalse(app.version_catalog_workers)
                listing.assert_not_called()
                create.assert_not_called()
                self.assertIn("unavailable", str(screen.query_one("#mod-version-prompt", huroshiki.Static).render()))

    async def test_url_catalog_fallback_does_not_start_worker_or_transaction(self) -> None:
        mod = core.ModInfo(
            Path("mods/url.pw.toml"),
            "url",
            "URL MOD",
            "url",
            "url-mod",
            "url.jar",
            True,
            False,
        )
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Explicit root"
        ), patch.object(core, "list_mod_version_candidates") as listing, patch.object(
            core.PackTransaction, "create"
        ) as create:
            app = _VersionApp(mod)
            async with app.run_test() as pilot:
                await pilot.pause(0.1)
                screen = app.screen
                screen.action_browse_versions()
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertFalse(app.version_catalog_workers)
                listing.assert_not_called()
                create.assert_not_called()

    async def test_catalog_load_is_named_non_daemon_and_passes_cancel_deadline(self) -> None:
        seen: dict[str, object] = {}
        loaded = catalog(candidate_view("NewId001"))

        def listing(project, identity, *, include_prerelease, cancel_event, deadline):
            seen.update(project=project, identity=identity, prerelease=include_prerelease,
                        cancel=cancel_event, deadline=deadline, thread=threading.current_thread())
            return loaded

        with patch.object(core, "list_mod_version_candidates", side_effect=listing):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.switch_screen(huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info()))
                await pilot.pause(0.2)
                self.assertEqual(seen["project"], "pack:demo")
                self.assertEqual(seen["identity"], "modrinth:A1b2C3d4")
                self.assertFalse(seen["prerelease"])
                self.assertIsInstance(seen["cancel"], threading.Event)
                self.assertGreater(float(seen["deadline"]), 0)
                thread = seen["thread"]
                self.assertFalse(thread.daemon)
                self.assertTrue(thread.name.startswith("huroshiki-version-catalog-pack:demo"))

    async def test_catalog_does_not_overlap_same_pack_exact_worker(self) -> None:
        blocker_done = threading.Event()
        blocker_cancel = threading.Event()
        blocker = MagicMock()
        with patch.object(core, "list_mod_version_candidates") as listing:
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.exact_version_workers["pack:demo"] = (
                    blocker,
                    blocker_done,
                    blocker_cancel,
                    lambda: None,
                )
                app.switch_screen(
                    huroshiki.InstalledModVersionBrowserScreen(
                        "pack:demo", mod_info()
                    )
                )
                await pilot.pause(0.1)
                listing.assert_not_called()
                self.assertIn(
                    "Wait for the installed MOD operation",
                    str(
                        app.screen.query_one(
                            "#mod-version-catalog-status", huroshiki.Static
                        ).render()
                    ),
                )
                app.exact_version_workers.pop("pack:demo", None)

    async def test_catalog_renders_states_order_and_missing_warning(self) -> None:
        views = (
            candidate_view("Curr0001", current=True),
            candidate_view("Selc0001", selected=True, pinned=True),
            candidate_view("Drft0001", selected=True),
        )
        loaded = catalog(*views, missing=True)
        with patch.object(core, "list_mod_version_candidates", return_value=loaded):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.switch_screen(huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info()))
                await pilot.pause(0.2)
                screen = app.screen
                text = str(screen.query_one("#mod-version-catalog-status", huroshiki.Static).render())
                self.assertIn("Selection: User exact", text)
                self.assertIn("Stored selected artifact is not present", text)
                table = screen.query_one("#mod-version-catalog-table", huroshiki.DataTable)
                self.assertEqual([str(screen.catalog.candidates[i].candidate.artifact_id) for i in range(3)],
                                 ["Curr0001", "Selc0001", "Drft0001"])
                self.assertEqual(table.row_count, 3)
                self.assertIn("C", str(table.get_row_at(0)[0]))
                self.assertIn("S/P", str(table.get_row_at(1)[0]))
                self.assertIn("S", str(table.get_row_at(2)[0]))

    async def test_catalog_renders_automatic_current_without_selected_state(self) -> None:
        loaded = catalog(
            candidate_view("Auto0001", current=True),
            selection="automatic",
            selected=None,
        )
        with patch.object(core, "list_mod_version_candidates", return_value=loaded):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.switch_screen(
                    huroshiki.InstalledModVersionBrowserScreen(
                        "pack:demo", mod_info()
                    )
                )
                await pilot.pause(0.2)
                screen = app.screen
                status = str(
                    screen.query_one(
                        "#mod-version-catalog-status", huroshiki.Static
                    ).render()
                )
                self.assertIn("Selection: Automatic", status)
                row_state = str(
                    screen.query_one(
                        "#mod-version-catalog-table", huroshiki.DataTable
                    ).get_row_at(0)[0]
                )
                self.assertEqual(row_state, "C")

    async def test_empty_catalog_is_recoverable_and_prerelease_reload_preserves_selection(self) -> None:
        calls: list[bool] = []
        results = [
            catalog(),
            catalog(candidate_view("Keep0001"), candidate_view("Othr0001")),
            catalog(candidate_view("Othr0001"), candidate_view("Keep0001")),
        ]

        def listing(*_args, include_prerelease, **_kwargs):
            calls.append(include_prerelease)
            return results.pop(0)

        with patch.object(core, "list_mod_version_candidates", side_effect=listing):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.switch_screen(huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info()))
                await pilot.pause(0.15)
                screen = app.screen
                self.assertIn("No compatible versions found", str(screen.query_one("#mod-version-catalog-status", huroshiki.Static).render()))
                screen.action_toggle_prerelease()
                await pilot.pause(0.15)
                self.assertEqual(calls, [False, True])
                self.assertTrue(screen.include_prerelease)
                self.assertEqual(screen._selected_artifact(), "Keep0001")
                screen.action_toggle_prerelease()
                await pilot.pause(0.15)
                self.assertEqual(calls, [False, True, False])
                self.assertFalse(screen.include_prerelease)
                self.assertEqual(screen._selected_artifact(), "Keep0001")

    async def test_candidate_details_exposes_metadata_flags_and_notes(self) -> None:
        view = candidate_view("Cand0001", current=True, selected=True, pinned=True,
                             compatible=True, notes=("loader match",))
        app = _VersionApp(mod_info())
        async with app.run_test() as pilot:
            app.push_screen(huroshiki.InstalledModVersionCandidateScreen(
                "pack:demo", mod_info(), catalog(view), view
            ))
            await pilot.pause()
            rendered = str(app.screen.query_one("#mod-version-candidate-details", huroshiki.Static).render())
            for expected in ("Provider: modrinth", "Project ID: A1b2C3d4", "Artifact ID: Cand0001",
                             "Version: 1.0", "Filename: example.jar", "Channel: release",
                             "Published: 2026-01-02T03:04:05Z", "Minecraft: 1.21.1",
                             "Loaders: fabric", "Current: yes", "Selected: yes", "Pinned: yes",
                             "Compatible: yes", "Note: loader match"):
                self.assertIn(expected, rendered)

    async def test_candidate_selection_uses_details_exact_selection_authority(self) -> None:
        view = candidate_view("Cand0001")
        app = _VersionApp(mod_info())
        async with app.run_test() as pilot:
            app.push_screen(huroshiki.InstalledModVersionCandidateScreen("pack:demo", mod_info(), catalog(view), view))
            await pilot.pause()
            with patch.object(huroshiki.InstalledModDetailsScreen, "start_exact_selection") as start:
                app.screen.action_select_version()
                await pilot.pause()
                start.assert_called_once_with(view.candidate.as_exact_selection())
                self.assertIsInstance(app.screen, huroshiki.InstalledModDetailsScreen)
                self.assertFalse(app.transactions)

    async def test_browser_selection_is_read_only_until_existing_details_prepare(self) -> None:
        view = candidate_view("Cand0001")
        loaded = catalog(view)
        transaction = MagicMock(active=True)
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.apply.side_effect = lambda **_: setattr(transaction, "active", False)
        with patch.object(core, "list_mod_version_candidates", return_value=loaded), patch.object(
            core, "project_config", return_value={}
        ), patch.object(core, "installed_mod_provenance", return_value="Dependency"), patch.object(
            core.PackTransaction, "create", return_value=transaction
        ):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.switch_screen(huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info()))
                await pilot.pause(0.2)
                screen = app.screen
                self.assertFalse(app.transactions)
                self.assertFalse(transaction.apply.called)
                screen.on_data_table_row_selected(MagicMock(data_table=screen.query_one("#mod-version-catalog-table", huroshiki.DataTable)))
                await pilot.pause()
                candidate_screen = app.screen
                self.assertFalse(transaction.apply.called)
                candidate_screen.action_select_version()
                await pilot.pause(0.1)
                details = app.screen
                self.assertIsInstance(details, huroshiki.InstalledModDetailsScreen)
                await pilot.pause(0.2)
                self.assertTrue(details.prepare_thread is not None or transaction.prepare_exact_mod_version.called)
                self.assertEqual(transaction.prepare_exact_mod_version.call_args.args[0], view.candidate.as_exact_selection())
                self.assertFalse(transaction.apply.called)
                details.apply_preview()
                await pilot.pause(0.2)
                transaction.apply.assert_called_once()
                self.assertNotIn("pack:demo", app.transactions)

    async def test_candidate_preview_cancel_discards_without_applying(self) -> None:
        view = candidate_view("Cncl0001")
        transaction = MagicMock(active=True)
        transaction.prepare_exact_mod_version.return_value = preview()
        transaction.begin_discard.return_value = completed_discard(transaction)
        with patch.object(core, "project_config", return_value={}), patch.object(
            core, "installed_mod_provenance", return_value="Dependency"
        ), patch.object(core.PackTransaction, "create", return_value=transaction):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.push_screen(
                    huroshiki.InstalledModVersionCandidateScreen(
                        "pack:demo", mod_info(), catalog(view), view
                    )
                )
                await pilot.pause()
                app.screen.action_select_version()
                await pilot.pause(0.3)
                details = app.screen
                self.assertIsInstance(details, huroshiki.InstalledModDetailsScreen)
                self.assertIs(app.transactions["pack:demo"], transaction)
                details.cancel_and_navigate(None)
                await pilot.pause(0.2)
                transaction.apply.assert_not_called()
                transaction.begin_discard.assert_called_once_with()
                self.assertNotIn("pack:demo", app.transactions)

    async def test_catalog_cancel_defers_back_until_worker_terminates_without_transaction(self) -> None:
        started, release = threading.Event(), threading.Event()
        def listing(*_args, cancel_event, **_kwargs):
            started.set()
            release.wait(2)
            if cancel_event.is_set():
                raise core.ExactModVersionCancelled("cancelled")
            return catalog(candidate_view("Late0001"))
        with patch.object(core, "list_mod_version_candidates", side_effect=listing):
            app = _VersionApp(mod_info())
            with patch.object(app, "open_list") as destination:
                async with app.run_test() as pilot:
                    await pilot.pause(0.15)
                    app.switch_screen(huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info()))
                    await pilot.pause(0.1)
                    screen = app.screen
                    self.assertTrue(started.is_set())
                    screen.action_back()
                    destination.assert_not_called()
                    release.set()
                    await pilot.pause(0.25)
                    self.assertIsInstance(app.screen, huroshiki.InstalledModDetailsScreen)
                    self.assertFalse(app.transactions)

    async def test_catalog_deadline_failure_is_recoverable_without_transaction(self) -> None:
        with patch.object(core, "list_mod_version_candidates", side_effect=core.ExactModVersionDeadlineExceeded("deadline")):
            app = _VersionApp(mod_info())
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                app.switch_screen(huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info()))
                await pilot.pause(0.2)
                screen = app.screen
                self.assertIn("deadline", str(screen.query_one("#mod-version-catalog-status", huroshiki.Static).render()))
                self.assertFalse(app.transactions)
                screen.action_reload()
                self.assertIsNotNone(screen.thread)

    async def test_stale_catalog_completion_cannot_replace_newer_generation(self) -> None:
        app = _VersionApp(mod_info())
        with patch.object(core, "list_mod_version_candidates", return_value=catalog(candidate_view("Init0001"))):
            async with app.run_test() as pilot:
                screen = huroshiki.InstalledModVersionBrowserScreen("pack:demo", mod_info())
                app.push_screen(screen)
                await pilot.pause(0.1)
                new_catalog = catalog(candidate_view("Neww0001"))
                screen.generation = 2
                fake_thread = MagicMock()
                fake_thread.is_alive.return_value = False
                screen.active_worker = huroshiki.VersionCatalogWorker(fake_thread, threading.Event(), threading.Event(), 1.0, 2)
                app.version_catalog_workers["pack:demo"] = screen.active_worker
                screen._load_results[1] = (catalog(candidate_view("Oldd0001")), None)
                screen._load_results[2] = (new_catalog, None)
                screen.done.set()
                screen._poll_load()
                self.assertEqual(screen.catalog.candidates[0].candidate.artifact_id, "Neww0001")
                self.assertNotEqual(screen.catalog.candidates[0].candidate.artifact_id, "Oldd0001")

    async def test_stale_exact_completion_cannot_clear_newer_worker(self) -> None:
        app = _VersionApp(mod_info())
        async with app.run_test() as pilot:
            await pilot.pause(0.15)
            screen = app.screen
            old_thread = MagicMock()
            newer_thread = MagicMock()
            newer_worker = (
                newer_thread,
                threading.Event(),
                threading.Event(),
                lambda: None,
            )
            screen.prepare_thread = old_thread
            screen.prepare_done.set()
            screen.prepare_error = core.HuroshikiError("old worker failed")
            app.exact_version_workers["pack:demo"] = newer_worker
            screen._poll_prepare()
            self.assertIs(app.exact_version_workers["pack:demo"], newer_worker)

    async def test_catalog_worker_shutdown_cancels_joins_and_removes_non_daemon_worker(self) -> None:
        stopped = threading.Event()
        cancel = threading.Event()
        def run():
            cancel.wait(2)
            stopped.set()
        thread = threading.Thread(target=run, name="catalog-test", daemon=False)
        done = threading.Event()
        worker = huroshiki.VersionCatalogWorker(thread, done, cancel, 99.0, 1)
        def finish():
            stopped.wait(2)
            done.set()
        threading.Thread(target=finish, daemon=False).start()
        app = huroshiki.HuroshikiApp()
        app.version_catalog_workers["pack:demo"] = worker
        thread.start()
        app.on_unmount()
        self.assertTrue(cancel.is_set())
        self.assertTrue(stopped.is_set())
        self.assertFalse(thread.is_alive())
        self.assertNotIn("pack:demo", app.version_catalog_workers)

    async def test_exact_worker_shutdown_cancels_joins_non_daemon_worker(self) -> None:
        stopped = threading.Event()
        cancel = threading.Event()
        done = threading.Event()

        def run() -> None:
            cancel.wait(2)
            stopped.set()
            done.set()

        thread = threading.Thread(target=run, name="exact-test", daemon=False)
        app = huroshiki.HuroshikiApp()
        app.exact_version_workers["pack:demo"] = (
            thread,
            done,
            cancel,
            lambda: None,
        )
        thread.start()
        app.on_unmount()
        self.assertTrue(cancel.is_set())
        self.assertTrue(stopped.is_set())
        self.assertFalse(thread.is_alive())


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
