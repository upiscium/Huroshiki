from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import call, patch

import huroshiki_core as core
from pack_migration import PackMigrationChange, PackMigrationWarning
from pack_migration_roots import PackMigrationRootCandidate


class PackCopyMigrationSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.event = threading.Event()
        self.deadline = time.monotonic() + 60
        self.target = core.PackMigrationTarget(
            "next", "Next", "1.21.4", "fabric", "0.16.0"
        )
        self.snapshot = SimpleNamespace(
            snapshot_digest="a" * 64,
            minecraft_version="1.20.1",
            loader="forge",
            loader_version="47.2.0",
        )
        self.plan = SimpleNamespace(
            publication_lifecycle="precommit",
            warnings=(PackMigrationWarning("confirm", "Review config", None, True),),
            copied_files=3,
            copied_directories=2,
            copied_bytes=42,
            skipped_paths=(Path("dist"),),
            changes=(PackMigrationChange("target-config", None, "Minecraft changed"),),
        )
        self.resolution = self._resolution()

    def _resolution(self, **changes: object) -> SimpleNamespace:
        values = {
            "state": "resolved",
            "provenance_required": False,
            "root_candidates": (),
            "roots": (),
            "resolved_roots": (),
            "unresolved_roots": (),
            "dependency_delta": SimpleNamespace(
                added=(), removed=(), updated=(), unchanged=(), side_changed=(),
                identity_changed=(), path_changed=(), filename_changed=(),
            ),
            "side_changes": (),
            "identity_changes": (),
            "path_collisions": (),
            "filename_collisions": (),
            "provider_warnings": (),
            "url_compatibility": (),
            "version_intent_facts": SimpleNamespace(
                overrides=(), automatic_identities=()
            ),
            "version_intent_issues": (),
            "resolution_attempt": 0,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_one_event_deadline_and_exact_authorities_reach_every_phase(self) -> None:
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        publication = object()
        published = object()
        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ) as snapshot, patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ) as plan, patch.object(
            core, "resolve_pack_migration_plan", return_value=self.resolution
        ) as resolve, patch.object(
            core, "prepare_pack_migration_publication", return_value=publication
        ) as prepare, patch.object(
            core, "apply_pack_migration_publication", return_value=published
        ) as apply:
            session.start()
            preview = session.preview()
            session.prepare_publication(("confirm",))
            self.assertIs(session.publish(), published)

        self.assertEqual(preview.source_snapshot_digest, "a" * 64)
        self.assertIs(snapshot.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(snapshot.call_args.kwargs["deadline"], self.deadline)
        self.assertIs(plan.call_args.kwargs["expected_snapshot"], self.snapshot)
        self.assertIs(plan.call_args.kwargs["cancel_event"], self.event)
        self.assertIs(resolve.call_args.args[0], self.plan)
        self.assertIs(resolve.call_args.kwargs["cancel_event"], self.event)
        self.assertIs(prepare.call_args.args[0], self.plan)
        self.assertIs(prepare.call_args.args[1], self.resolution)
        self.assertIs(apply.call_args.args[0], publication)
        self.assertIs(apply.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(session.state, "published")

    def test_preview_is_deterministic_and_does_not_replan(self) -> None:
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ) as snapshot, patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ) as plan, patch.object(
            core, "resolve_pack_migration_plan", return_value=self.resolution
        ) as resolve:
            session.start()
            first = session.preview()
            second = session.preview()
        self.assertEqual(first, second)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(resolve.call_count, 1)
        output = "\n".join(core.format_pack_copy_migration_preview(first))
        self.assertIn("Source snapshot: aaaaaaaaaaaaaaaa", output)
        self.assertNotIn("/tmp/", output)

    def test_progress_callback_can_observe_session_without_blocking(self) -> None:
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        observed: list[str] = []

        def resolve(_plan, **kwargs):
            kwargs["progress"](
                SimpleNamespace(phase="resolving-roots", message="Resolving")
            )
            return self.resolution

        def progress(_value: object) -> None:
            observed.append(session.view.state)

        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ), patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ), patch.object(
            core, "resolve_pack_migration_plan", side_effect=resolve
        ):
            session.start(progress=progress)
        self.assertEqual(observed, ["planning"])
        self.assertEqual(session.view.progress_message, "Resolving")

    def test_provenance_selection_continues_same_plan_and_owner_controls(self) -> None:
        candidate = PackMigrationRootCandidate(
            "modrinth:abc", "modrinth", "abc", "file", "1.0", "both",
            Path("mods/example.pw.toml"), "example.jar",
        )
        first_resolution = self._resolution(
            state="resolution-required",
            provenance_required=True,
            root_candidates=(candidate,),
            unresolved_roots=(
                SimpleNamespace(
                    source_root=candidate,
                    reason_code="root-provenance-required",
                    message="Select roots",
                    retryable=True,
                    replacement_supported=False,
                ),
            ),
        )
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ) as snapshot, patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ) as plan, patch.object(
            core, "resolve_pack_migration_plan",
            return_value=first_resolution,
        ) as resolve, patch.object(
            core, "select_pack_migration_roots", return_value=self.resolution
        ) as select, patch.object(
            core, "commit_pack_migration_root_selection"
        ) as commit, patch.object(
            core, "discard_pack_migration_plan"
        ) as discard:
            view = session.start()
            self.assertEqual(view.state, "provenance-required")
            view = session.select_root_candidates(
                (("mods/example.pw.toml", "modrinth:abc"),)
            )
            session.discard(deadline=321.0)
        self.assertEqual(view.state, "resolved")
        selection = select.call_args.args[1][0]
        self.assertEqual(selection.source_metadata_path, Path("mods/example.pw.toml"))
        self.assertIs(select.call_args.args[0], self.plan)
        self.assertIs(select.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(select.call_args.kwargs["deadline"], self.deadline)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(resolve.call_count, 1)
        commit.assert_not_called()
        discard.assert_called_once_with(self.plan, deadline=321.0)

    def test_conflicts_use_current_request_and_exact_result(self) -> None:
        unresolved = SimpleNamespace(
            source_root=SimpleNamespace(
                canonical_identity="modrinth:old",
                source_side="both",
                source_metadata_path=Path("mods/old.pw.toml"),
            ),
            reason_code="no-compatible-file",
            message="No compatible file",
            retryable=False,
            replacement_supported=True,
        )
        self.resolution = self._resolution(
            state="resolution-required", unresolved_roots=(unresolved,)
        )
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        choice = core.PackMigrationRootResolution("modrinth:old", "remove")
        request = object()
        result = SimpleNamespace(
            resolution_plan=self._resolution(),
            removed_roots=(), replaced_roots=(),
        )
        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ), patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ), patch.object(
            core, "resolve_pack_migration_plan", return_value=self.resolution
        ), patch.object(
            core, "create_pack_migration_resolution_request", return_value=request
        ) as create, patch.object(
            core, "resolve_pack_migration_conflicts", return_value=result
        ) as resolve:
            session.start()
            view = session.resolve_conflicts((choice,))
        self.assertEqual(view.state, "resolved")
        self.assertEqual(create.call_args, call(self.plan, (choice,)))
        self.assertIs(resolve.call_args.args[0], self.plan)
        self.assertIs(resolve.call_args.args[1], request)
        self.assertIs(resolve.call_args.kwargs["cancel_event"], self.event)

    def test_committed_cleanup_uses_only_retry_with_fresh_controls(self) -> None:
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        publication = object()
        published = object()

        def fail_after_commit(*_args: object, **_kwargs: object) -> object:
            self.plan.publication_lifecycle = "committed"
            raise core.PackMigrationCleanupError("cleanup blocked")

        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ), patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ), patch.object(
            core, "resolve_pack_migration_plan", return_value=self.resolution
        ), patch.object(
            core, "prepare_pack_migration_publication", return_value=publication
        ), patch.object(
            core, "apply_pack_migration_publication", side_effect=fail_after_commit
        ), patch.object(
            core, "retry_pack_migration_cleanup", return_value=published
        ) as retry, patch.object(core, "discard_pack_migration_plan") as discard:
            session.start()
            session.prepare_publication(("confirm",))
            with self.assertRaises(core.PackMigrationCleanupError):
                session.publish()
            self.assertEqual(session.state, "cleanup-pending")
            cleanup_deadline = time.monotonic() + 10
            self.assertIs(session.retry_cleanup(deadline=cleanup_deadline), published)
        self.assertEqual(session.state, "published")
        self.assertIs(retry.call_args.args[0], publication)
        self.assertIsNot(retry.call_args.kwargs["cancel_event"], self.event)
        self.assertEqual(retry.call_args.kwargs["deadline"], cleanup_deadline)
        discard.assert_not_called()

    def test_uncertain_publication_retains_owner_and_rejects_discard(self) -> None:
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        publication = object()

        def fail_uncertain(*_args: object, **_kwargs: object) -> object:
            self.plan.publication_lifecycle = "uncertain"
            raise core.PackMigrationPublicationError("outcome uncertain")

        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ), patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ), patch.object(
            core, "resolve_pack_migration_plan", return_value=self.resolution
        ), patch.object(
            core, "prepare_pack_migration_publication", return_value=publication
        ), patch.object(
            core, "apply_pack_migration_publication", side_effect=fail_uncertain
        ), patch.object(core, "discard_pack_migration_plan") as discard:
            session.start()
            session.prepare_publication(("confirm",))
            with self.assertRaises(core.PackMigrationPublicationError):
                session.publish()
            self.assertEqual(session.state, "publication-uncertain")
            with self.assertRaisesRegex(core.PackMigrationError, "outcome is uncertain"):
                session.discard()
        discard.assert_not_called()

    def test_precommit_discard_failure_is_retryable_without_replanning(self) -> None:
        session = core.PackCopyMigrationSession(
            "pack:demo", self.target, self.event, self.deadline
        )
        with patch.object(
            core, "snapshot_pack_migration_source", return_value=self.snapshot
        ), patch.object(
            core, "plan_pack_copy_migration", return_value=self.plan
        ) as plan, patch.object(
            core, "resolve_pack_migration_plan", return_value=self.resolution
        ), patch.object(
            core,
            "discard_pack_migration_plan",
            side_effect=(core.PackMigrationCleanupError("blocked"), None),
        ) as discard:
            session.start()
            with self.assertRaises(core.PackMigrationCleanupError):
                session.discard(deadline=123.0)
            self.assertEqual(session.state, "cleanup-pending")
            session.discard(deadline=456.0)
        self.assertEqual(session.state, "discarded")
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(
            [item.kwargs["deadline"] for item in discard.call_args_list],
            [123.0, 456.0],
        )


if __name__ == "__main__":
    unittest.main()
