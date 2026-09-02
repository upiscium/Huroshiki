from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import huroshiki
import huroshiki_core as core


class TemplateMigrationTuiTest(unittest.TestCase):

    def test_owner_keys_are_source_and_target(self) -> None:
        owner = huroshiki.TemplateCopyMigrationOwner(
            "template:source", "template:target", Mock(), threading.Event(), 10.0
        )
        self.assertEqual(owner.keys, ("template:source", "template:target"))

    def test_target_modal_uses_distinct_reference_loader_field(self) -> None:
        self.assertEqual(huroshiki.TemplateCopyMigrationTargetModal.FIELD_IDS[-1], "template-migration-reference")

    def test_embedded_url_userinfo_is_redacted_from_tui_text(self) -> None:
        text = huroshiki.TemplateCopyMigrationScreen._safe_text(
            "selector=(https://user:password@example.invalid/mod.jar)"
        )
        self.assertNotIn("user:password", text)
        self.assertIn("example.invalid/mod.jar", text)

    def test_publish_uses_exact_preview_and_does_not_resolve(self) -> None:
        session = Mock()
        preview = Mock(required_warnings=("warn-1",))
        owner = huroshiki.TemplateCopyMigrationOwner(
            "template:source", "template:target", session, threading.Event(), 10.0
        )
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner, screen.session, screen.preview = owner, session, preview
        screen._publish()
        session.prepare_publication.assert_called_once_with(("warn-1",), expected_preview=preview)
        session.publish.assert_called_once_with()
        session.resolve_choices.assert_not_called()
        self.assertTrue(owner.done.is_set())

    def test_replace_follows_typed_core_authority_not_reason_spelling(self) -> None:
        for reason in ("ordinary", "version-looking-but-replaceable", "ownerless-exact-looking"):
            self.assertFalse(huroshiki.TemplateCopyMigrationScreen._replace_allowed(
                Mock(reason_code=reason, replacement_supported=False)
            ))
            self.assertTrue(huroshiki.TemplateCopyMigrationScreen._replace_allowed(
                Mock(reason_code=reason, replacement_supported=True)
            ))

    def test_menu_action_opens_template_copy_migration(self) -> None:
        screen = huroshiki.ProjectScreen.__new__(huroshiki.ProjectScreen)
        screen.project_key = "template:base"
        screen.project = Mock(kind="template", display_name="Base")
        screen.actions = ("Migrate / Copy version",)
        app = Mock()
        screen.query_one = Mock(return_value=Mock())
        screen.current_index = Mock(return_value=0)
        with patch.object(huroshiki.ProjectScreen, "app", app):
            screen.run_selected()
        app.open_template_copy_migration.assert_called_once_with("template:base")

    def test_render_conflict_contains_typed_selector_identity_and_action(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        item = Mock(source_index=4, source_selector="https://modrinth.test/a", canonical_identity="modrinth:a",
                    side="both", reason_code="identity-collision", retryable=True,
                    replacement_supported=True, version_intent_issue=None, message="choose replacement")
        screen.conflicts = [item]
        screen.choices = {4: core.TemplateMigrationRootResolution(4, "replace", "curseforge", "12345")}
        table = Mock()
        screen.session = Mock(view=Mock(collision_facts=()))
        screen.query_one = Mock(return_value=table)
        screen._render_conflicts()
        row = table.add_row.call_args.args
        self.assertIn("Replace → curseforge:12345", row[0])
        self.assertIn("selector=https://modrinth.test/a", row[1])
        self.assertIn("identity=modrinth:a", row[1])

    def test_remove_and_replace_use_full_selector_then_rerender(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        item = Mock(source_index=2, source_selector="old", canonical_identity="modrinth:old",
                    reason_code="ordinary", replacement_supported=True, version_intent_issue=None)
        screen.conflicts, screen.choices = [item], {}
        screen._current_conflict = Mock(return_value=item)
        screen._render_conflicts = Mock()
        screen.toggle_remove()
        self.assertEqual(screen.choices[2].action, "remove")
        screen._replacement(item, ("modrinth", "https://modrinth.test/project/real"))
        self.assertEqual(screen.choices[2].replacement_project_id, "https://modrinth.test/project/real")
        self.assertEqual(screen.choices[2].replacement_provider, "modrinth")
        self.assertEqual(screen._render_conflicts.call_count, 2)

    def test_conflict_actions_are_ignored_while_resolution_worker_runs(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.phase = "resolving"
        screen.toggle_remove = Mock()
        screen.replace = Mock()
        screen.details = Mock()
        for key in ("space", "p", "d"):
            event = Mock(key=key)
            screen.on_key(event)
            event.stop.assert_not_called()
        screen.toggle_remove.assert_not_called()
        screen.replace.assert_not_called()
        screen.details.assert_not_called()

    def test_replace_action_is_hidden_only_when_core_marks_it_unsupported(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen._current_conflict = Mock()
        app = Mock()
        for supported in (False, True):
            item = Mock(source_index=1, canonical_identity="modrinth:x", source_selector="x",
                        reason_code="version-looking", replacement_supported=supported,
                        version_intent_issue="diagnostic text")
            screen._current_conflict.return_value = item
            screen.choices = {}
            screen._render_conflicts = Mock()
            screen.toggle_remove()
            self.assertEqual(screen.choices[1].action, "remove")
            with patch.object(huroshiki.TemplateCopyMigrationScreen, "app", app):
                screen.replace()
        app.push_screen.assert_called_once()

    def test_ownerless_dependency_issue_is_global_and_has_no_action_row(self) -> None:
        target = core.TemplateMigrationTarget(
            "target", "Target", "1.21.1", "fabric", "0.16"
        )
        issue = core.TemplateVersionIntentIssue(
            "modrinth", "dependency", "artifact", "dependency",
            "version-intent-blocked", "ownerless dependency", (),
        )
        view = core.TemplateCopyMigrationView(
            "resolution-required", "source", target, None, None, None,
            (), (), (), (), (), (), (), (), (issue,), (), (),
            1, "c" * 64, "precommit", False, False, None,
        )
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.session = Mock(view=view)
        screen.conflicts = list(screen.session.view.unresolved_roots)
        self.assertEqual(screen.conflicts, [])
        requirements = "\n".join(core.format_template_copy_migration_requirements(view))
        self.assertIn("Template blocker: ownerless dependency exact intent", requirements)

    def test_query_secrets_are_redacted_in_conflict_details_and_warning_modal(self) -> None:
        secret_url = "https://user:pass@example.invalid/mod.jar?token=TOPSECRET&access_token=ACCESSSECRET&normal=value"
        item = Mock(
            source_index=1, source_selector=secret_url, canonical_identity="modrinth:x",
            side="both", reason_code="ordinary", retryable=True,
            replacement_supported=True, version_intent_issue=None, message=secret_url,
        )
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.conflicts, screen.choices = [item], {}
        screen.session = Mock(view=Mock(collision_facts=()))
        table = Mock()
        screen.query_one = Mock(return_value=table)
        screen._render_conflicts()
        conflict_text = " ".join(str(arg) for arg in table.add_row.call_args.args)
        self.assertNotIn("TOPSECRET", conflict_text)
        self.assertNotIn("ACCESSSECRET", conflict_text)
        self.assertNotIn("user:pass", conflict_text)
        self.assertIn("normal=value", conflict_text)

        screen._current_conflict = Mock(return_value=item)
        app = Mock()
        with patch.object(huroshiki.TemplateCopyMigrationScreen, "app", app):
            screen.details()
        detail_text = " ".join(app.push_screen.call_args.args[0].lines)
        self.assertNotIn("TOPSECRET", detail_text)
        self.assertNotIn("ACCESSSECRET", detail_text)
        self.assertNotIn("user:pass", detail_text)
        self.assertIn("normal=value", detail_text)

        preview = Mock(required_warnings=(secret_url,))
        screen.preview, screen.phase = preview, "preview"
        screen._warnings_confirmed = Mock()
        with patch.object(huroshiki.TemplateCopyMigrationScreen, "app", app):
            screen.advance()
        warning_modal = app.push_screen.call_args.args[0]
        warning_text = " ".join(warning_modal.lines)
        self.assertNotIn("TOPSECRET", warning_text)
        self.assertNotIn("ACCESSSECRET", warning_text)
        self.assertNotIn("user:pass", warning_text)
        self.assertIn("normal=value", warning_text)

    def test_worker_is_named_non_daemon_and_preserves_one_event_deadline(self) -> None:
        owner = Mock(thread=None, done=threading.Event(), error=None,
                     source_key="template:base", cancel_event=threading.Event(),
                     deadline=time.monotonic() + 20)
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = owner
        screen.session = Mock()
        finished = threading.Event()
        screen._start_worker("start", finished.set)
        self.assertTrue(finished.wait(1))
        self.assertIsNotNone(owner.thread)
        self.assertFalse(owner.thread.daemon)
        self.assertTrue(owner.thread.name.startswith("huroshiki-template-migration-start-template-base"))
        self.assertIs(screen.owner.cancel_event, owner.cancel_event)
        self.assertIsNotNone(owner.deadline)
        owner.thread.join(1)

    def test_resolved_preview_uses_shared_formatter_and_warning_publish_preview(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = Mock(source_key="template:base", target_key="template:new", error=None)
        screen.session = Mock(state="resolved")
        screen.owner.thread = None
        screen.owner.done = threading.Event(); screen.owner.done.set()
        screen.owner.cleanup_done = threading.Event()
        screen.navigation_pending = False
        screen.phase = "starting"
        preview = Mock(required_warnings=("url-warning",))
        screen.session.preview.return_value = preview
        status = Mock()
        screen.query_one = Mock(return_value=status)
        with patch.object(core, "format_template_copy_migration_preview", return_value=("shared preview",)):
            screen._poll()
        self.assertEqual(screen.phase, "preview")
        self.assertIn("shared preview", status.update.call_args.args[0])
        screen.preview = preview
        screen.owner.done.clear()
        screen._publish()
        screen.session.prepare_publication.assert_called_with(("url-warning",), expected_preview=preview)

    def test_retryable_resolution_error_retains_session_for_another_choice(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        conflict = Mock(source_index=1, reason_code="temporary", version_intent_scope=None)
        screen.owner = Mock(
            error=RuntimeError("resolver temporarily unavailable"),
            thread=None,
            done=threading.Event(),
            cleanup_done=threading.Event(),
        )
        screen.owner.done.set()
        screen.session = Mock(
            state="resolution-required",
            view=Mock(unresolved_roots=(conflict,), collision_facts=()),
        )
        screen.phase = "resolving"
        screen.navigation_pending = False
        screen.choices = {99: Mock()}
        screen._render_conflicts = Mock()
        status = Mock()
        screen.query_one = Mock(return_value=status)
        with patch.object(
            core,
            "format_template_copy_migration_requirements",
            return_value=("requirements",),
        ):
            screen._poll()
        self.assertEqual(screen.phase, "conflicts")
        self.assertIsNone(screen.owner.error)
        self.assertEqual(screen.choices, {})
        self.assertIn("temporarily unavailable", status.update.call_args.args[0])
        screen.session.discard.assert_not_called()

    def test_poll_preserves_choices_for_unchanged_conflict_view(self) -> None:
        conflict = Mock(source_index=1)
        choice = core.TemplateMigrationRootResolution(1, "remove")
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = Mock(error=None, thread=None, done=threading.Event())
        screen.session = Mock(
            state="resolution-required",
            view=Mock(unresolved_roots=(conflict,), collision_facts=()),
        )
        screen.phase = "conflicts"
        screen.navigation_pending = False
        screen.choices = {1: choice}
        screen._render_conflicts = Mock()
        screen.query_one = Mock(return_value=Mock())
        with patch.object(
            core,
            "format_template_copy_migration_requirements",
            return_value=("requirements",),
        ):
            screen._poll()
        self.assertEqual(screen.choices, {1: choice})

    def test_target_opens_only_after_definitive_publish(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = Mock(source_key="template:base", target_key="template:new", error=None, thread=None,
                            done=threading.Event(), cleanup_done=threading.Event())
        screen.owner.done.set()
        screen.session = Mock(state="published")
        screen.phase = "publishing"
        screen.navigation_pending = False
        app = Mock()
        with patch.object(huroshiki.TemplateCopyMigrationScreen, "app", app):
            screen._poll()
        app.open_project.assert_called_once_with("template:new")

        app.reset_mock()
        screen.session.state = "publication-uncertain"
        screen.query_one = Mock(return_value=Mock())
        with patch.object(huroshiki.TemplateCopyMigrationScreen, "app", app):
            screen._poll()
        app.open_project.assert_not_called()

    def test_cancel_waits_for_cleanup_and_cleanup_failure_retains_without_replan(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = Mock(source_key="template:base", target_key="template:new", thread=None,
                            done=threading.Event(), cleanup_done=threading.Event(), cancel_event=threading.Event())
        screen.session = Mock(state="resolved", view=Mock(publication_lifecycle="precommit"))
        screen.navigation_pending = False
        screen._start_worker = Mock()
        screen.leave()
        self.assertTrue(screen.navigation_pending); screen.session.cancel.assert_called_once()
        screen.session.discard.side_effect = RuntimeError("cleanup blocked")
        screen._cleanup()
        self.assertIsNotNone(screen.owner.cleanup_error)
        screen.session.resolve_choices.assert_not_called()

    def test_committed_retry_and_uncertain_shutdown_never_discard(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = Mock(source_key="template:base", target_key="template:new", thread=None,
                            done=threading.Event(), cleanup_done=threading.Event(), cancel_event=threading.Event())
        screen.session = Mock(state="cleanup-pending", view=Mock(publication_lifecycle="committed"))
        screen._start_worker = Mock()
        screen.retry_cleanup()
        screen._start_worker.assert_called_once()
        screen.session.view.publication_lifecycle = "uncertain"
        screen.session.state = "publication-uncertain"
        screen._cleanup()
        screen.session.discard.assert_not_called()


class FakeTemplateMigrationSession:
    instances: list["FakeTemplateMigrationSession"] = []
    block_start = False
    discard_error: BaseException | None = None
    lifecycle = "precommit"
    start_error: BaseException | None = None
    block_discard = False

    def __init__(self, source_id, target, cancel_event, deadline):
        self.source_id, self.target = source_id, target
        self.cancel_event, self.deadline = cancel_event, deadline
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.discard_entered = threading.Event()
        self.release_discard = threading.Event()
        self.start_calls = self.discard_calls = self.resolve_calls = 0
        self.discard_threads: list[threading.Thread] = []
        self.discard_deadlines: list[float | None] = []
        self._state = "new"
        self.instances.append(self)

    @property
    def state(self):
        return self._state

    @property
    def view(self):
        return SimpleNamespace(
            publication_lifecycle=type(self).lifecycle,
            unresolved_roots=(), collision_facts=(), required_warnings=(),
        )

    def start(self):
        self.start_calls += 1
        self.start_entered.set()
        if type(self).block_start:
            self.release_start.wait(2)
        if type(self).start_error is not None:
            raise type(self).start_error
        if self.cancel_event.is_set():
            self._state = "cancelled"
            raise RuntimeError("cancelled")
        self._state = "resolved"

    def preview(self):
        return SimpleNamespace(required_warnings=())

    def cancel(self):
        self.cancel_event.set()

    def discard(self, *, deadline=None):
        self.discard_calls += 1
        self.discard_deadlines.append(deadline)
        self.discard_threads.append(threading.current_thread())
        self.discard_entered.set()
        if type(self).block_discard:
            self.release_discard.wait(2)
        if self.discard_error is not None:
            self._state = "cleanup-pending"
            raise self.discard_error
        self._state = "discarded"

    def retry_cleanup(self, *, deadline=None):
        return self.discard(deadline=deadline)

    def resolve_choices(self, choices):
        self.resolve_calls += 1


class TemplateMigrationRealTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeTemplateMigrationSession.instances.clear()
        FakeTemplateMigrationSession.block_start = False
        FakeTemplateMigrationSession.discard_error = None
        FakeTemplateMigrationSession.lifecycle = "precommit"
        FakeTemplateMigrationSession.start_error = None
        FakeTemplateMigrationSession.block_discard = False

    def _values(self, project_id="target"):
        return {
            "template_id": project_id, "display_name": "Target",
            "minecraft": "1.21.1", "loader": "fabric", "reference": "0.16.10",
        }

    @staticmethod
    def _notification_text(app):
        return "\n".join(
            str(getattr(notification, "message", notification))
            for notification in app._notifications
        )

    async def test_precommit_start_failure_discards_before_redacted_notification_and_source_navigation(self):
        FakeTemplateMigrationSession.start_error = RuntimeError(
            "planning failed: https://user:pass@example.invalid/mod.jar?token=TOPSECRET"
        )
        FakeTemplateMigrationSession.block_discard = True
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.HuroshikiApp, "open_project"
        ) as open_project:
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_template_copy_migration("template:source", self._values())
                session = FakeTemplateMigrationSession.instances[0]
                await pilot.pause(0.1)
                self.assertTrue(session.discard_entered.wait(1))
                open_project.assert_not_called()
                self.assertIsNot(session.discard_threads[0], threading.current_thread())
                self.assertIsNotNone(session.discard_deadlines[0])
                self.assertEqual(set(app.template_copy_migration_owners), {"template:source", "template:target"})
                session.release_discard.set()
                await pilot.pause(0.2)
                open_project.assert_called_once_with("template:source")
                self.assertEqual(app.template_copy_migration_owners, {})
                notifications = self._notification_text(app)
                self.assertIn("planning failed", notifications)
                self.assertNotIn("user:pass", notifications)
                self.assertNotIn("TOPSECRET", notifications)

    async def test_target_already_exists_failure_reports_collision_without_overwrite_and_returns_source(self):
        FakeTemplateMigrationSession.start_error = RuntimeError(
            "target already exists; collision: refusing overwrite"
        )
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.HuroshikiApp, "open_project"
        ) as open_project:
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_template_copy_migration("template:source", self._values())
                await pilot.pause(0.2)
                open_project.assert_called_once_with("template:source")
                diagnostic = self._notification_text(app)
                self.assertIn("collision", diagnostic)
                self.assertIn("refusing overwrite", diagnostic)
                self.assertEqual(app.template_copy_migration_owners, {})

    async def test_target_form_path_has_one_session_and_responsive_named_worker(self):
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.core, "format_template_copy_migration_preview", return_value=("preview",)
        ), patch.object(huroshiki.HuroshikiApp, "project_is_usable", return_value=True):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app.open_template_copy_migration("template:source")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.TemplateCopyMigrationTargetModal)
                for field, value in zip(
                    huroshiki.TemplateCopyMigrationTargetModal.FIELD_IDS,
                    ("target", "Target", "1.21.1", "fabric", "0.16.10"),
                    strict=True,
                ):
                    app.screen.query_one(f"#{field}", huroshiki.Input).value = value
                await pilot.press("ctrl+enter")
                await pilot.pause(0.15)
                self.assertEqual(len(FakeTemplateMigrationSession.instances), 1)
                session = FakeTemplateMigrationSession.instances[0]
                owner = app.screen.owner
                self.assertIs(owner.cancel_event, session.cancel_event)
                self.assertEqual(owner.deadline, session.deadline)
                self.assertIsNotNone(owner.thread)
                self.assertFalse(owner.thread.daemon)
                self.assertTrue(owner.thread.name.startswith("huroshiki-template-migration-start-template-source"))
                self.assertEqual(session.start_calls, 1)

    async def test_cancel_joins_operation_then_cleans_off_loop_before_navigation(self):
        FakeTemplateMigrationSession.block_start = True
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.core, "format_template_copy_migration_preview", return_value=("preview",)
        ), patch.object(huroshiki.HuroshikiApp, "open_project") as open_project:
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_template_copy_migration("template:source", self._values())
                session = FakeTemplateMigrationSession.instances[0]
                await pilot.pause(0.05)
                app.screen.leave()
                await pilot.pause(0.05)
                self.assertEqual(session.discard_calls, 0)
                self.assertFalse(open_project.called)
                session.release_start.set()
                await pilot.pause(0.25)
                self.assertEqual(session.discard_calls, 1)
                self.assertEqual(len(session.discard_threads), 1)
                self.assertIsNot(session.discard_threads[0], threading.current_thread())
                open_project.assert_called_once_with("template:source")

    async def test_cleanup_failure_retains_keys_and_retry_does_not_restart_or_resolve(self):
        FakeTemplateMigrationSession.start_error = RuntimeError(
            "operation failed: https://user:pass@example.invalid/mod.jar?token=TOPSECRET"
        )
        FakeTemplateMigrationSession.discard_error = RuntimeError(
            "cleanup blocked: https://user:pass@example.invalid/cleanup.jar?access_token=ACCESSSECRET"
        )
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.core, "format_template_copy_migration_preview", return_value=("preview",)
        ), patch.object(huroshiki.HuroshikiApp, "open_project") as open_project:
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_template_copy_migration("template:source", self._values())
                await pilot.pause(0.1)
                screen = app.screen
                await pilot.pause(0.1)
                session = FakeTemplateMigrationSession.instances[0]
                self.assertEqual(set(app.template_copy_migration_owners), {"template:source", "template:target"})
                self.assertEqual((session.start_calls, session.resolve_calls), (1, 0))
                status = app.screen.query_one("#template-migration-status")
                visible = str(status.render())
                self.assertIn("Original migration failure", visible)
                self.assertIn("Cleanup pending/retry required", visible)
                self.assertNotIn("user:pass", visible)
                self.assertNotIn("TOPSECRET", visible)
                self.assertNotIn("ACCESSSECRET", visible)
                FakeTemplateMigrationSession.discard_error = None
                screen.retry_cleanup()
                await pilot.pause(0.2)
                self.assertEqual((session.start_calls, session.resolve_calls), (1, 0))
                self.assertEqual(session.discard_calls, 2)
                open_project.assert_called_once_with("template:source")
                self.assertEqual(app.template_copy_migration_owners, {})
                notifications = self._notification_text(app)
                self.assertIn("operation failed", notifications)
                self.assertNotIn("TOPSECRET", notifications)

    async def test_uncertain_publication_never_discards_or_navigates(self):
        FakeTemplateMigrationSession.lifecycle = "uncertain"
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.core, "format_template_copy_migration_preview", return_value=("preview",)
        ), patch.object(huroshiki.HuroshikiApp, "open_project") as open_project:
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app._start_template_copy_migration("template:source", self._values())
                await pilot.pause(0.1)
                app.screen.leave()
                await pilot.pause(0.2)
                session = FakeTemplateMigrationSession.instances[0]
                self.assertEqual(session.discard_calls, 0)
                open_project.assert_not_called()
                self.assertEqual(set(app.template_copy_migration_owners), {"template:source", "template:target"})

    async def test_second_migration_using_source_or_target_is_rejected(self):
        with patch.object(huroshiki.core, "TemplateCopyMigrationSession", FakeTemplateMigrationSession), patch.object(
            huroshiki.core, "format_template_copy_migration_preview", return_value=("preview",)
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test():
                app._start_template_copy_migration("template:source", self._values())
                with patch.object(app, "notify") as notify:
                    app._start_template_copy_migration("template:source", self._values("other"))
                    app._start_template_copy_migration("template:other", self._values("target"))
                self.assertEqual(len(FakeTemplateMigrationSession.instances), 1)
                self.assertEqual(notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
