from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import threading
import time
import unittest
from unittest.mock import patch

from textual.widgets import DataTable

import huroshiki
import huroshiki_core as core


PROJECT = core.ProjectInfo(
    kind="pack", project_id="demo", display_name="Demo",
    minecraft="1.20.1", loader="forge", loader_version="47.2.0", enabled=True,
)


class FakeMigrationSession:
    instances: list["FakeMigrationSession"] = []
    delay_start = False
    delay_roots = False
    preview_result: object = object()
    initial_state = "resolved"
    initial_candidates: tuple[core.PackCopyMigrationRootCandidateView, ...] = ()
    initial_unresolved: tuple[core.PackCopyMigrationUnresolvedView, ...] = ()
    lifecycle = "precommit"
    start_error: BaseException | None = None
    prepare_error: BaseException | None = None
    publish_error: BaseException | None = None
    discard_error: BaseException | None = None
    block_discard = False

    def __init__(self, source_key, target, cancel_event, deadline) -> None:
        self.source_key = source_key
        self.target = target
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_calls = 0
        self.discard_calls = 0
        self.start_calls = 0
        self.retry_calls = 0
        self.prepare_calls: list[tuple[str, ...]] = []
        self.publish_calls = 0
        self.root_calls: list[tuple[tuple[str, str], ...]] = []
        self.root_started = threading.Event()
        self.root_release = threading.Event()
        self.conflict_calls: list[tuple[core.PackMigrationRootResolution, ...]] = []
        self._state = "new"
        self.lifecycle = type(self).lifecycle
        self.discard_started = threading.Event()
        self.discard_release = threading.Event()
        self.discard_threads: list[threading.Thread] = []
        if not self.block_discard:
            self.discard_release.set()
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
            self.lifecycle,
            False,
            self.lifecycle == "uncertain",
            None,
        )

    def start(self, *, progress=None):
        self.start_calls += 1
        self.started.set()
        if self.delay_start:
            self.release.wait(3)
        if self.cancel_event.is_set():
            self._state = "cancelled"
            raise core.PackMigrationCancelled("cancelled")
        if self.start_error is not None:
            self._state = "failed"
            raise self.start_error
        self._state = self.initial_state
        return self.view

    def preview(self):
        return self.preview_result

    def select_root_candidates(self, selections, *, progress=None):
        self.root_calls.append(tuple(selections))
        self.root_started.set()
        if self.delay_roots:
            self.root_release.wait(3)
        if self.cancel_event.is_set():
            self._state = "cancelled"
            raise core.PackMigrationCancelled("cancelled after root selection")
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
        self.discard_threads.append(threading.current_thread())
        self.discard_started.set()
        self.discard_release.wait(3)
        if self.discard_error is not None:
            raise self.discard_error
        self._state = "discarded"

    def retry_cleanup(self, *, deadline=None, progress=None):
        self.retry_calls += 1
        self._state = "published"
        return object()

    def prepare_publication(self, acknowledgements=(), *, progress=None):
        self.prepare_calls.append(tuple(acknowledgements))
        if self.prepare_error is not None:
            self._state = "failed"
            raise self.prepare_error
        self._state = "ready"
        return self.view

    def publish(self, *, progress=None):
        self.publish_calls += 1
        if self.publish_error is not None:
            self._state = "failed"
            raise self.publish_error
        self._state = "published"
        return object()


class PackMigrationTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeMigrationSession.instances.clear()
        FakeMigrationSession.delay_start = False
        FakeMigrationSession.delay_roots = False
        FakeMigrationSession.preview_result = object()
        FakeMigrationSession.initial_state = "resolved"
        FakeMigrationSession.initial_candidates = ()
        FakeMigrationSession.initial_unresolved = ()
        FakeMigrationSession.lifecycle = "precommit"
        FakeMigrationSession.start_error = None
        FakeMigrationSession.prepare_error = None
        FakeMigrationSession.publish_error = None
        FakeMigrationSession.discard_error = None
        FakeMigrationSession.block_discard = False

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

    async def test_shutdown_cleanup_runs_on_named_non_daemon_worker(self) -> None:
        event_loop_thread = threading.current_thread()
        cancel_event = threading.Event()
        target = core.PackMigrationTarget(
            "next", "Next", "1.21.4", "fabric", "0.16.0"
        )
        session = FakeMigrationSession(
            "pack:demo", target, cancel_event, time.monotonic() + 60
        )
        owner = huroshiki.PackCopyMigrationOwner(
            "pack:demo",
            "pack:next",
            session,
            cancel_event,
            time.monotonic() + 60,
        )
        owner.done.set()
        app = huroshiki.HuroshikiApp()
        async with app.run_test():
            app.pack_copy_migration_owners["pack:demo"] = owner
            app.pack_copy_migration_owners["pack:next"] = owner

        self.assertEqual(session.discard_calls, 1)
        cleanup_thread = session.discard_threads[0]
        self.assertIsNot(cleanup_thread, event_loop_thread)
        self.assertFalse(cleanup_thread.daemon)
        self.assertTrue(
            cleanup_thread.name.startswith(
                "huroshiki-pack-migration-shutdown-cleanup-pack-demo"
            )
        )
        self.assertIs(owner.cleanup_thread, cleanup_thread)

    async def test_shutdown_committed_cleanup_uses_retry_only(self) -> None:
        FakeMigrationSession.lifecycle = "committed"
        cancel_event = threading.Event()
        target = core.PackMigrationTarget(
            "next", "Next", "1.21.4", "fabric", "0.16.0"
        )
        session = FakeMigrationSession(
            "pack:demo", target, cancel_event, time.monotonic() + 60
        )
        owner = huroshiki.PackCopyMigrationOwner(
            "pack:demo", "pack:next", session, cancel_event, time.monotonic() + 60
        )
        owner.published = True
        owner.cleanup_retained = True
        owner.done.set()
        app = huroshiki.HuroshikiApp()
        async with app.run_test():
            app.pack_copy_migration_owners["pack:demo"] = owner
            app.pack_copy_migration_owners["pack:next"] = owner

        self.assertEqual(session.retry_calls, 1)
        self.assertEqual(session.discard_calls, 0)

    async def test_shutdown_uncertain_publication_retains_without_cleanup_guess(self) -> None:
        FakeMigrationSession.lifecycle = "uncertain"
        cancel_event = threading.Event()
        target = core.PackMigrationTarget(
            "next", "Next", "1.21.4", "fabric", "0.16.0"
        )
        session = FakeMigrationSession(
            "pack:demo", target, cancel_event, time.monotonic() + 60
        )
        owner = huroshiki.PackCopyMigrationOwner(
            "pack:demo", "pack:next", session, cancel_event, time.monotonic() + 60
        )
        owner.cleanup_retained = True
        owner.done.set()
        app = huroshiki.HuroshikiApp()
        app.pack_copy_migration_owners["pack:demo"] = owner
        app.pack_copy_migration_owners["pack:next"] = owner
        stderr = StringIO()
        with redirect_stderr(stderr):
            app.on_unmount()

        self.assertEqual(session.retry_calls, 0)
        self.assertEqual(session.discard_calls, 0)
        self.assertTrue(owner.cleanup_retained)
        self.assertIn("cleanup ownership retained", stderr.getvalue())

    async def test_planning_failure_discards_off_loop_before_safe_navigation(self) -> None:
        FakeMigrationSession.start_error = RuntimeError("planning failed")
        FakeMigrationSession.block_discard = True
        values = {
            "project_id": "next", "display_name": "Next", "minecraft": "1.21.4",
            "loader": "fabric", "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                screen = app.screen
                session = FakeMigrationSession.instances[-1]
                deadline = time.monotonic() + 2
                while not session.discard_started.is_set() and time.monotonic() < deadline:
                    await pilot.pause(0.05)

                self.assertTrue(session.discard_started.is_set())
                self.assertIs(app.screen, screen)
                self.assertIn("pack:demo", app.pack_copy_migration_owners)
                self.assertEqual(screen.phase, "failure-cleanup")
                cleanup_thread = session.discard_threads[0]
                self.assertFalse(cleanup_thread.daemon)
                self.assertIn("failure-cleanup", cleanup_thread.name)

                session.discard_release.set()
                deadline = time.monotonic() + 2
                while screen.phase != "failure-cleaned" and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertEqual(screen.phase, "failure-cleaned")
                self.assertIn("Migration failed; target not published.", screen.status)
                self.assertIn("Cleanup completed.", screen.status)
                self.assertNotIn("pack:demo", app.pack_copy_migration_owners)
                self.assertEqual(session.start_calls, 1)

    async def test_precommit_publication_failure_discards_automatically(self) -> None:
        FakeMigrationSession.publish_error = RuntimeError("publication failed before commit")
        FakeMigrationSession.preview_result = type(
            "Preview", (), {"required_warning_codes": (), "warnings": ()}
        )()
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
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                await pilot.press("enter")
                session = FakeMigrationSession.instances[-1]
                migration_screen = app.screen_stack[-1]
                deadline = time.monotonic() + 2
                while (
                    migration_screen.phase != "failure-cleaned"
                    and time.monotonic() < deadline
                ):
                    await pilot.pause(0.05)
                self.assertEqual(session.prepare_calls, [()])
                self.assertEqual(session.publish_calls, 1)
                self.assertEqual(session.discard_calls, 1)
                self.assertEqual(migration_screen.phase, "failure-cleaned")

    async def test_discard_failure_retains_owner_and_retry_never_replans(self) -> None:
        FakeMigrationSession.start_error = RuntimeError("planning failed")
        FakeMigrationSession.discard_error = RuntimeError("cleanup blocked")
        values = {
            "project_id": "next", "display_name": "Next", "minecraft": "1.21.4",
            "loader": "fabric", "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                screen = app.screen
                session = FakeMigrationSession.instances[-1]
                deadline = time.monotonic() + 2
                while session.discard_calls < 1 and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                await pilot.pause(0.1)

                self.assertEqual(screen.phase, "failure-cleanup")
                self.assertTrue(screen.owner.cleanup_retained)
                self.assertIn("Migration cleanup is still pending.", screen.status)
                self.assertIn("pack:demo", app.pack_copy_migration_owners)

                session.discard_error = None
                await pilot.press("r")
                deadline = time.monotonic() + 2
                while screen.phase != "failure-cleaned" and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertEqual(session.discard_calls, 2)
                self.assertEqual(session.start_calls, 1)
                self.assertEqual(session.root_calls, [])
                self.assertEqual(session.conflict_calls, [])
                self.assertEqual(session.prepare_calls, [])
                self.assertEqual(session.publish_calls, 0)
                self.assertEqual(screen.phase, "failure-cleaned")

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
                self.assertIn("explicitly", screen.status)
                self.assertIn("migration-local", screen.status)
                self.assertIn("source Pack unchanged", screen.status)
                self.assertNotIn("commit", screen.status.lower())
                self.assertNotIn("update", screen.status.lower())
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

    async def test_cancel_after_root_selection_waits_then_discards(self) -> None:
        FakeMigrationSession.initial_state = "provenance-required"
        FakeMigrationSession.delay_roots = True
        FakeMigrationSession.initial_candidates = (
            core.PackCopyMigrationRootCandidateView(
                "mods/a.pw.toml", "modrinth:a", "modrinth", "a", None,
                None, "both", "mods/a.pw.toml", "a.jar",
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
                await pilot.press("space", "enter")
                session = FakeMigrationSession.instances[-1]
                deadline = time.monotonic() + 2
                while not session.root_started.is_set() and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertTrue(session.root_started.is_set())
                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertIs(app.screen, screen)
                self.assertEqual(session.cancel_calls, 1)
                self.assertEqual(session.discard_calls, 0)
                session.root_release.set()
                deadline = time.monotonic() + 2
                while app.screen is screen and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertEqual(session.root_calls, [(('mods/a.pw.toml', 'modrinth:a'),)])
                self.assertEqual(session.discard_calls, 1)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

    async def test_conflict_rendering_includes_complete_bounded_typed_facts(self) -> None:
        FakeMigrationSession.initial_state = "resolution-required"
        FakeMigrationSession.initial_unresolved = (
            core.PackCopyMigrationUnresolvedView(
                "modrinth:blocked",
                "client",
                "version-intent-blocked",
                "Exact dependency artifact is unavailable",
                False,
                True,
                "mods/blocked.pw.toml",
                "owner modrinth:root requires exact artifact v1",
            ),
        )
        values = {
            "project_id": "next", "display_name": "Next", "minecraft": "1.21.4",
            "loader": "fabric", "loader_version": "0.16.0",
        }
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "PackCopyMigrationSession", FakeMigrationSession
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_pack_copy_migration("pack:demo", values)
                await pilot.pause(0.15)
                screen = app.screen
                table = screen.query_one("#migration-options", DataTable)
                row = table.get_row_at(0)
                self.assertEqual(str(row[0]), "Blocked")
                facts = str(row[1])
                for expected in (
                    "identity=modrinth:blocked",
                    "side=client",
                    "reason=version-intent-blocked",
                    "metadata_path=mods/blocked.pw.toml",
                    "detail=Exact dependency artifact is unavailable",
                    "retryable=false",
                    "replacement_supported=true",
                    "version_intent_issue=owner modrinth:root requires exact artifact v1",
                ):
                    self.assertIn(expected, facts)
                self.assertNotIn(
                    "remove", str(screen.query_one("#key-help").content).lower()
                )
                self.assertNotIn(
                    "replace", str(screen.query_one("#key-help").content).lower()
                )
                self.assertIn("authoritative", screen.status)
                await pilot.press("space", "p")
                self.assertEqual(screen.conflict_choices, {})

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
                confirmation = " ".join(app.screen.lines)
                self.assertIn("source Pack is never changed", confirmation)
                self.assertIn("successful target", confirmation)
                self.assertNotIn("committed", confirmation.lower())
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
