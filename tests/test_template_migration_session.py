from __future__ import annotations

from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import call, patch

import huroshiki_core as core


class TemplateCopyMigrationSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.event = threading.Event()
        self.deadline = time.monotonic() + 60
        self.target = core.TemplateMigrationTarget(
            "next", "Next", "1.21.4", "fabric", "0.16.0"
        )
        self.root = core.TemplateRootIntent(0, "Example", "modrinth", "abc", "both")
        self.snapshot = SimpleNamespace(
            snapshot_digest="a" * 64,
            target=SimpleNamespace(
                minecraft_version="1.20.1",
                loader="forge",
                reference_loader_version="47.2.0",
            ),
            roots=(self.root,),
        )
        self.plan = SimpleNamespace(
            plan_digest="b" * 64,
            publication_lifecycle="precommit",
            roots=(self.root,),
        )
        self.resolution = self._resolution()

    def _resolution(self, **changes: object) -> SimpleNamespace:
        values = {
            "status": "resolved",
            "ordered_roots": (self.root,),
            "resolved": (SimpleNamespace(source_index=0, artifact_id="file", classification="updated"),),
            "unresolved": (),
            "ordered_root_facts": (SimpleNamespace(source_index=0, target_canonical_identity="modrinth:abc"),),
            "warnings": ("confirm",),
            "url_evidence": (),
            "version_intent_facts": (),
            "version_intent_issues": (),
            "collisions": (),
            "removed_roots": (),
            "replaced_roots": (),
            "resolution_attempt": 1,
            "digest": "c" * 64,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def _session(self) -> core.TemplateCopyMigrationSession:
        return core.TemplateCopyMigrationSession(
            "demo", self.target, self.event, self.deadline
        )

    def _started(self, *, resolution=None) -> core.TemplateCopyMigrationSession:
        session = self._session()
        with patch.object(core, "snapshot_template_migration_source", return_value=self.snapshot), \
             patch.object(core, "plan_template_copy_migration", return_value=self.plan), \
             patch.object(core, "resolve_template_migration_plan", return_value=resolution or self.resolution):
            session.start()
        return session

    def _retained_precommit_plan(self, cleanup_error: BaseException):
        state = SimpleNamespace(
            publication_state="not-published",
            committed=False,
            cleanup_error=cleanup_error,
        )
        return core.TemplateMigrationPlan(
            "demo", self.target, "a" * 64, "b" * 64, (self.root,), state
        )

    def test_one_event_deadline_and_exact_authorities_reach_every_phase(self) -> None:
        session = self._session()
        publication, published = object(), object()
        with patch.object(core, "snapshot_template_migration_source", return_value=self.snapshot) as snapshot, \
             patch.object(core, "plan_template_copy_migration", return_value=self.plan) as plan, \
             patch.object(core, "resolve_template_migration_plan", return_value=self.resolution) as resolve, \
             patch.object(core, "prepare_template_migration_publication", return_value=publication) as prepare, \
             patch.object(core, "apply_template_migration_publication", return_value=published) as apply:
            session.start()
            preview = session.preview()
            session.prepare_publication(("confirm",), expected_preview=preview)
            self.assertIs(session.publish(), published)
        self.assertEqual(preview.source_snapshot_digest, "a" * 64)
        self.assertIs(snapshot.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(snapshot.call_args.kwargs["deadline"], self.deadline)
        self.assertIs(plan.call_args.kwargs["expected_snapshot"], self.snapshot)
        self.assertIs(plan.call_args.kwargs["cancel_event"], self.event)
        self.assertIs(resolve.call_args.args[0], self.plan)
        self.assertIs(resolve.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(prepare.call_args.args[:2], (self.plan, self.resolution))
        self.assertIs(apply.call_args.args[0], publication)
        self.assertEqual(session.state, "published")

    def test_resolution_required_conflict_resolves_without_replan_and_view_is_real(self) -> None:
        unresolved = SimpleNamespace(
            source_index=0, source_selector="modrinth:abc", canonical_identity="modrinth:abc",
            code="identity-collision", detail="Choose a result", retry=True,
            replacement_supported=True, version_issue=None,
        )
        required = self._resolution(status="resolution-required", resolved=(), unresolved=(unresolved,))
        resolved = self._resolution(resolution_attempt=2, digest="d" * 64)
        outcome = SimpleNamespace(resolution=resolved)
        choice = core.TemplateMigrationRootResolution(0, "remove")
        session = self._session()
        with patch.object(core, "snapshot_template_migration_source", return_value=self.snapshot), \
             patch.object(core, "plan_template_copy_migration", return_value=self.plan) as plan, \
             patch.object(core, "resolve_template_migration_plan", return_value=required), \
             patch.object(core, "create_template_migration_resolution_request", return_value=object()) as create, \
             patch.object(core, "resolve_template_migration_conflicts", return_value=outcome) as resolve:
            view = session.start()
            self.assertEqual(view.state, "resolution-required")
            request = session.create_resolution_request((choice,))
            view = session.resolve_conflicts(request)
        self.assertEqual(view.state, "resolved")
        self.assertEqual(view.unresolved_roots, ())
        self.assertEqual(view.updated_roots[0].artifact_id, "file")
        self.assertEqual(plan.call_count, 1)
        create.assert_called_once_with(self.plan, (choice,))
        self.assertIs(resolve.call_args.args[0], self.plan)
        self.assertIs(resolve.call_args.args[1], request)
        self.assertIs(resolve.call_args.kwargs["cancel_event"], self.event)

    def test_preview_is_deterministic_and_invokes_no_resolver_or_network(self) -> None:
        session = self._started()
        with patch.object(core, "resolve_template_migration_plan", side_effect=AssertionError), \
             patch.object(core, "resolve_template_migration_conflicts", side_effect=AssertionError), \
             patch("urllib.request.urlopen", side_effect=AssertionError):
            first, second = session.preview(), session.preview()
        self.assertEqual(first, second)
        self.assertEqual(first.plan_digest, "b" * 64)

    def test_view_exposes_replacement_selector_and_canonical_identities(self) -> None:
        replacement = core.TemplateMigrationReplacedRoot(
            self.root,
            core.TemplateRootIntent(0, "Example", "modrinth", "New00001", "both"),
            "new-project",
            "modrinth:Old00001",
            "modrinth:New00001",
            False,
        )
        session = self._started(resolution=self._resolution(replaced_roots=(replacement,)))
        view = session.preview().view.replaced_roots[0]
        self.assertEqual(view.old_identity, "modrinth:Old00001")
        self.assertEqual(view.replacement_selector, "new-project")
        self.assertEqual(view.new_identity, "modrinth:New00001")

    def test_precommit_conflict_failure_preserves_request_for_retry(self) -> None:
        unresolved = SimpleNamespace(
            source_index=0, source_selector="abc", canonical_identity="modrinth:abc",
            code="no-compatible-file", detail="retry", retry=True,
            replacement_supported=True, version_issue=None,
        )
        required = self._resolution(status="resolution-required", resolved=(), unresolved=(unresolved,))
        resolved = self._resolution(resolution_attempt=2, digest="d" * 64)
        self.plan.resolution = required
        request = object()
        session = self._session()
        with patch.object(core, "snapshot_template_migration_source", return_value=self.snapshot), \
             patch.object(core, "plan_template_copy_migration", return_value=self.plan), \
             patch.object(core, "resolve_template_migration_plan", return_value=required), \
             patch.object(core, "resolve_template_migration_conflicts", side_effect=(
                 core.TemplateMigrationOperationError("temporary resolver failure"),
                 SimpleNamespace(resolution=resolved),
             )) as resolve:
            session.start()
            with self.assertRaises(core.TemplateMigrationOperationError):
                session.resolve_conflicts(request)
            self.assertEqual(session.state, "resolution-required")
            self.assertIn("temporary", session.view.error_message)
            session.resolve_conflicts(request)
        self.assertEqual(session.state, "resolved")
        self.assertEqual([item.args[1] for item in resolve.call_args_list], [request, request])

    def test_stale_preview_is_rejected_and_current_view_is_preserved(self) -> None:
        session = self._started()
        preview = session.preview()
        stale = core.TemplateCopyMigrationPreview(
            preview.view, preview.source_snapshot_digest, "x" * 64,
            preview.resolution_attempt, preview.resolution_digest,
        )
        with patch.object(core, "prepare_template_migration_publication") as prepare:
            with self.assertRaisesRegex(core.TemplateMigrationOperationError, "stale"):
                session.prepare_publication(expected_preview=stale)
        prepare.assert_not_called()
        self.assertEqual(session.state, "failed")

    def test_concurrent_operation_rejected_and_cancel_is_shared(self) -> None:
        session = self._session()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked(*_args, **_kwargs):
            entered.set(); release.wait(2)
            raise core.TemplateMigrationOperationError("template migration cancelled")
        with patch.object(core, "snapshot_template_migration_source", side_effect=blocked), \
             patch.object(core, "plan_template_copy_migration", return_value=self.plan):
            def run():
                try: session.start()
                except BaseException as error: errors.append(error)
            worker = threading.Thread(target=run)
            worker.start(); self.assertTrue(entered.wait(2))
            with self.assertRaises(core.TemplateMigrationOperationError): session.start()
            session.cancel(); self.assertTrue(session.cancel_event.is_set())
            release.set(); worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], core.TemplateMigrationOperationError)
        self.assertEqual(session.state, "cancelled")

    def test_planning_failure_with_retained_cleanup_is_cleanup_pending(self) -> None:
        session = self._session()
        cleanup_error = core.TemplateMigrationOperationError("cleanup retained")
        retained = self._retained_precommit_plan(cleanup_error)
        planning_error = core.TemplateMigrationPlanningError("planning failed", retained)
        with patch.object(
            core, "snapshot_template_migration_source", return_value=self.snapshot
        ) as snapshot, patch.object(
            core, "plan_template_copy_migration", side_effect=planning_error
        ) as plan, patch.object(
            core, "resolve_template_migration_plan"
        ) as resolve, patch.object(
            core, "discard_template_migration_plan"
        ) as discard, patch.object(
            core, "prepare_template_migration_publication"
        ) as prepare, patch.object(
            core, "apply_template_migration_publication"
        ) as publish:
            with self.assertRaises(core.TemplateMigrationPlanningError):
                session.start()
            self.assertEqual(session.state, "cleanup-pending")
            self.assertTrue(session.view.cleanup_pending)
            self.assertEqual(session.view.publication_lifecycle, "precommit")
            session.discard(deadline=123.0)
        self.assertEqual(session.state, "discarded")
        snapshot.assert_called_once()
        plan.assert_called_once()
        resolve.assert_not_called()
        prepare.assert_not_called()
        publish.assert_not_called()
        discard.assert_called_once_with(retained, deadline=123.0)

    def test_planning_failure_without_retained_owner_is_failed(self) -> None:
        session = self._session()
        with patch.object(
            core, "snapshot_template_migration_source", return_value=self.snapshot
        ), patch.object(
            core, "plan_template_copy_migration", side_effect=RuntimeError("planning failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "planning failed"):
                session.start()
        self.assertEqual(session.state, "failed")
        self.assertFalse(session.view.cleanup_pending)

    def test_cancel_with_retained_cleanup_prioritizes_cleanup_pending(self) -> None:
        session = self._session()
        retained = self._retained_precommit_plan(
            core.TemplateMigrationOperationError("cleanup retained")
        )
        planning_error = core.TemplateMigrationPlanningError("cancelled", retained)
        def fail_plan(*_args, **_kwargs):
            session.cancel()
            raise planning_error
        with patch.object(
            core, "snapshot_template_migration_source", return_value=self.snapshot
        ), patch.object(core, "plan_template_copy_migration", side_effect=fail_plan):
            with self.assertRaises(core.TemplateMigrationPlanningError):
                session.start()
        self.assertTrue(session.cancel_event.is_set())
        self.assertEqual(session.state, "cleanup-pending")
        self.assertTrue(session.view.cleanup_pending)
        self.assertEqual(session.view.publication_lifecycle, "precommit")

    def test_precommit_discard_failure_retries_without_replanning(self) -> None:
        session = self._started()
        with patch.object(core, "discard_template_migration_plan", side_effect=(core.TemplateMigrationOperationError("blocked"), None)) as discard:
            with self.assertRaises(core.TemplateMigrationOperationError): session.discard(deadline=123.0)
            self.assertEqual(session.state, "cleanup-pending")
            session.discard(deadline=456.0)
        self.assertEqual(session.state, "discarded")
        self.assertEqual([x.kwargs["deadline"] for x in discard.call_args_list], [123.0, 456.0])

    def test_committed_cleanup_retry_uses_fresh_event_without_republish_or_resolve(self) -> None:
        session = self._started()
        publication, published = object(), object()
        self.plan.publication_lifecycle = "precommit"
        with patch.object(core, "prepare_template_migration_publication", return_value=publication), \
             patch.object(core, "apply_template_migration_publication", side_effect=lambda p: setattr(self.plan, "publication_lifecycle", "committed") or (_ for _ in ()).throw(core.TemplateMigrationOperationError("blocked"))), \
             patch.object(core, "retry_template_migration_cleanup", return_value=published) as retry, \
             patch.object(core, "resolve_template_migration_plan") as resolve:
            session.prepare_publication()
            with self.assertRaises(core.TemplateMigrationOperationError): session.publish()
            self.assertEqual(session.state, "cleanup-pending")
            deadline = time.monotonic() + 10
            self.assertIs(session.retry_cleanup(deadline=deadline), published)
        self.assertEqual(session.state, "published")
        self.assertIsNot(retry.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(retry.call_args.kwargs["deadline"], deadline)
        resolve.assert_not_called()

    def test_uncertain_publication_rejects_discard(self) -> None:
        session = self._started()
        publication = object()
        def uncertain(_publication):
            self.plan.publication_lifecycle = "uncertain"
            raise core.TemplateMigrationOperationError("uncertain")
        with patch.object(core, "prepare_template_migration_publication", return_value=publication), \
             patch.object(core, "apply_template_migration_publication", side_effect=uncertain), \
             patch.object(core, "discard_template_migration_plan") as discard:
            session.prepare_publication()
            with self.assertRaises(core.TemplateMigrationOperationError): session.publish()
            self.assertEqual(session.state, "publication-uncertain")
            with self.assertRaisesRegex(core.TemplateMigrationOperationError, "uncertain"):
                session.discard()
        discard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
