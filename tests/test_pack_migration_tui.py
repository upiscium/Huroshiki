from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import huroshiki
import huroshiki_core as core


PROJECT = core.ProjectInfo(
    kind="pack", project_id="demo", display_name="Demo",
    minecraft="1.20.1", loader="forge", loader_version="47.2.0", enabled=True,
)


class FakeMigrationSession:
    instances: list["FakeMigrationSession"] = []
    delay_start = False
    preview_result: object = object()
    initial_state = "resolved"
    initial_candidates: tuple[core.PackCopyMigrationRootCandidateView, ...] = ()
    initial_unresolved: tuple[core.PackCopyMigrationUnresolvedView, ...] = ()

    def __init__(self, source_key, target, cancel_event, deadline) -> None:
        self.source_key = source_key
        self.target = target
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_calls = 0
        self.discard_calls = 0
        self.retry_calls = 0
        self.prepare_calls: list[tuple[str, ...]] = []
        self.publish_calls = 0
        self.root_calls: list[tuple[tuple[str, str], ...]] = []
        self.conflict_calls: list[tuple[core.PackMigrationRootResolution, ...]] = []
        self._state = "new"
        self.instances.append(self)

    @property
    def state(self):
        return self._state

    @property
    def view(self):
        return core.PackCopyMigrationView(
            self._state,
            self.source_key,
            self.target,
            None,
            None,
            self.initial_candidates if self._state == "provenance-required" else (),
            self.initial_unresolved if self._state == "resolution-required" else (),
            (),
            "precommit",
            False,
            False,
            None,
        )

    def start(self, *, progress=None):
        self.started.set()
        if self.delay_start:
            self.release.wait(3)
        if self.cancel_event.is_set():
            self._state = "cancelled"
            raise core.PackMigrationCancelled("cancelled")
        self._state = self.initial_state
        return self.view

    def preview(self):
        return self.preview_result

    def select_root_candidates(self, selections, *, progress=None):
        self.root_calls.append(tuple(selections))
        self._state = "resolved"
        return self.view

    def resolve_conflicts(self, choices, *, progress=None):
        self.conflict_calls.append(tuple(choices))
        self._state = "resolved"
        return self.view

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancel_event.set()

    def discard(self, *, deadline=None) -> None:
        self.discard_calls += 1
        self._state = "discarded"

    def retry_cleanup(self, *, deadline=None, progress=None):
        self.retry_calls += 1
        self._state = "published"
        return object()

    def prepare_publication(self, acknowledgements=(), *, progress=None):
        self.prepare_calls.append(tuple(acknowledgements))
        self._state = "ready"
        return self.view

    def publish(self, *, progress=None):
        self.publish_calls += 1
        self._state = "published"
        return object()


class PackMigrationTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeMigrationSession.instances.clear()
        FakeMigrationSession.delay_start = False
        FakeMigrationSession.preview_result = object()
        FakeMigrationSession.initial_state = "resolved"
        FakeMigrationSession.initial_candidates = ()
        FakeMigrationSession.initial_unresolved = ()

    async def test_target_creates_one_control_pair_and_named_non_daemon_worker(self) -> None:
        values = {
            "project_id": "next",
            "display_name": "Next",
            "minecraft": "1.21.4",
            "loader": "fabric",
            "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ), patch.object(
            huroshiki.core, "format_pack_copy_migration_preview", return_value=("preview",)
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                await pilot.pause(0.15)
                screen = app.screen
                self.assertIsInstance(screen, huroshiki.PackCopyMigrationScreen)
                session = FakeMigrationSession.instances[-1]
                owner = screen.owner
                self.assertIs(owner.cancel_event, session.cancel_event)
                self.assertEqual(owner.deadline, session.deadline)
                self.assertIsNotNone(owner.thread)
                self.assertFalse(owner.thread.daemon)
                self.assertTrue(owner.thread.name.startswith("huroshiki-pack-migration-start-pack-demo"))
                self.assertEqual(screen.phase, "preview")

    async def test_root_selection_is_explicit_and_snapshot_before_worker(self) -> None:
        FakeMigrationSession.initial_state = "provenance-required"
        FakeMigrationSession.initial_candidates = (
            core.PackCopyMigrationRootCandidateView(
                "mods/a.pw.toml",
                "modrinth:a",
                "modrinth",
                "a",
                None,
                None,
                "both",
                "mods/a.pw.toml",
                "a.jar",
            ),
        )
        values = {
            "project_id": "next", "display_name": "Next", "minecraft": "1.21.4",
            "loader": "fabric", "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ), patch.object(
            huroshiki.core, "format_pack_copy_migration_preview", return_value=("preview",)
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                await pilot.pause(0.15)
                screen = app.screen
                self.assertEqual(screen.phase, "roots")
                await pilot.press("space", "enter")
                deadline = time.monotonic() + 2
                while screen.phase != "preview" and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                session = FakeMigrationSession.instances[-1]
                self.assertEqual(
                    session.root_calls,
                    [(("mods/a.pw.toml", "modrinth:a"),)],
                )

    async def test_conflict_remove_uses_typed_current_choice(self) -> None:
        FakeMigrationSession.initial_state = "resolution-required"
        FakeMigrationSession.initial_unresolved = (
            core.PackCopyMigrationUnresolvedView(
                "modrinth:old",
                "both",
                "no-compatible-file",
                "No compatible file",
                False,
                True,
                "mods/old.pw.toml",
            ),
        )
        values = {
            "project_id": "next", "display_name": "Next", "minecraft": "1.21.4",
            "loader": "fabric", "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ), patch.object(
            huroshiki.core, "format_pack_copy_migration_preview", return_value=("preview",)
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                await pilot.pause(0.15)
                screen = app.screen
                self.assertEqual(screen.phase, "conflicts")
                await pilot.press("space", "enter")
                deadline = time.monotonic() + 2
                while screen.phase != "preview" and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                session = FakeMigrationSession.instances[-1]
                choice = session.conflict_calls[0][0]
                self.assertEqual(choice.source_identity, "modrinth:old")
                self.assertEqual(choice.action, "remove")

    async def test_required_warnings_have_explicit_step_before_exact_publish(self) -> None:
        FakeMigrationSession.preview_result = type(
            "Preview",
            (),
            {
                "required_warning_codes": ("review-config",),
                "warnings": (
                    core.PackCopyMigrationWarningView(
                        "review-config", "Review copied configuration", None, True
                    ),
                ),
            },
        )()
        values = {
            "project_id": "next",
            "display_name": "Next",
            "minecraft": "1.21.4",
            "loader": "fabric",
            "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ), patch.object(
            huroshiki.core, "format_pack_copy_migration_preview", return_value=("preview",)
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                await pilot.pause(0.15)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                self.assertIn("Acknowledge every", app.screen.dialog_title)

                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                self.assertEqual(app.screen.dialog_title, "Confirm copy migration")
                session = FakeMigrationSession.instances[-1]
                self.assertEqual(session.prepare_calls, [])

                await pilot.press("enter")
                deadline = time.monotonic() + 2
                while session.publish_calls == 0 and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertEqual(session.prepare_calls, [("review-config",)])
                self.assertEqual(session.publish_calls, 1)

    async def test_escape_cancels_waits_then_discards_before_navigation(self) -> None:
        FakeMigrationSession.delay_start = True
        values = {
            "project_id": "next",
            "display_name": "Next",
            "minecraft": "1.21.4",
            "loader": "fabric",
            "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                await pilot.pause()
                screen = app.screen
                session = FakeMigrationSession.instances[-1]
                self.assertTrue(session.started.wait(1))

                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertIs(app.screen, screen)
                self.assertEqual(session.cancel_calls, 1)
                self.assertEqual(session.discard_calls, 0)

                session.release.set()
                deadline = time.monotonic() + 2
                while app.screen is screen and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertEqual(session.discard_calls, 1)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
                self.assertNotIn("pack:demo", app.pack_copy_migration_owners)
                self.assertNotIn("pack:next", app.pack_copy_migration_owners)


if __name__ == "__main__":
    unittest.main()
