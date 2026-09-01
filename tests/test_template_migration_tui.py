from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

import huroshiki
import huroshiki_core as core


class TemplateMigrationTuiTest(unittest.TestCase):
    def test_blocked_exact_and_global_conflicts_have_no_action(self) -> None:
        for reason in ("dependency-exact-blocked", "ownerless-dependency-exact"):
            item = Mock(reason_code=reason, version_intent_issue=None)
            self.assertTrue(huroshiki.TemplateCopyMigrationScreen._blocked(item))

        root_exact = Mock(
            reason_code="version-intent-blocked",
            version_intent_issue="exact root intent",
        )
        self.assertFalse(huroshiki.TemplateCopyMigrationScreen._blocked(root_exact))

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
        self.assertIn("https://example.invalid/mod.jar", text)

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

    def test_unsupported_or_version_blocked_roots_are_not_replaceable(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        for supported, issue in ((False, None), (True, "exact intent")):
            item = Mock(reason_code="ordinary", version_intent_issue=issue, replacement_supported=supported)
            self.assertTrue(screen._blocked(item) or not supported or issue is not None)

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

    def test_unavailable_or_version_issue_allows_remove_but_not_replace(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen._current_conflict = Mock()
        app = Mock()
        for supported, issue in ((False, None), (True, "exact-version")):
            item = Mock(source_index=1, canonical_identity="modrinth:x", source_selector="x",
                        reason_code="ordinary", replacement_supported=supported, version_intent_issue=issue)
            screen._current_conflict.return_value = item
            screen.choices = {}
            screen._render_conflicts = Mock()
            screen.toggle_remove()
            self.assertEqual(screen.choices[1].action, "remove")
            with patch.object(huroshiki.TemplateCopyMigrationScreen, "app", app):
                screen.replace()
        app.push_screen.assert_not_called()

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
        screen.navigation_pending = False
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
        self.assertIn("temporarily unavailable", status.update.call_args.args[0])
        screen.session.discard.assert_not_called()

    def test_target_opens_only_after_definitive_publish(self) -> None:
        screen = huroshiki.TemplateCopyMigrationScreen.__new__(huroshiki.TemplateCopyMigrationScreen)
        screen.owner = Mock(source_key="template:base", target_key="template:new", error=None, thread=None,
                            done=threading.Event(), cleanup_done=threading.Event())
        screen.owner.done.set()
        screen.session = Mock(state="published")
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


if __name__ == "__main__":
    unittest.main()
