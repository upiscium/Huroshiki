from __future__ import annotations

from dataclasses import replace
import unittest
import threading
import time
from unittest.mock import patch

import pack_migration
import pack_migration_resolution
import packctl
from tests import test_pack_migration_core as migration_fixture
from tests import test_pack_migration_resolution as resolution_fixture


class PackMigrationPublicationTest(unittest.TestCase):
    """Focused public-boundary tests using, but not inheriting, the core fixture."""

    def setUp(self) -> None:
        self.fixture = migration_fixture.PackMigrationCoreTest(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def resolved_input(self):
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        resolution = plan.resolution
        assert resolution is not None
        plan._state = "resolved"
        plan._validation_token = None
        return plan, resolution

    @staticmethod
    def acknowledgements(plan: object) -> tuple[str, ...]:
        return tuple(
            warning.code
            for warning in plan.warnings
            if warning.acknowledgement_required
        )

    def test_public_ready_apply_and_replay_rejection(self) -> None:
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        published = pack_migration.apply_pack_copy_migration_at(
            plan._public_test_handoff
        )
        self.assertEqual(published.project_key, "pack:next")
        with self.assertRaises(pack_migration.PackMigrationPublicationError):
            pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)

    def test_raw_plan_wrong_handoff_and_acknowledgement_rejection(self) -> None:
        plan = self.fixture.plan()
        with self.assertRaises(pack_migration.PackMigrationPublicationError):
            pack_migration.apply_pack_copy_migration_at(plan)  # type: ignore[arg-type]
        self.fixture.make_ready(plan)
        resolution = plan.resolution
        assert resolution is not None
        plan._state = "resolved"
        required = tuple(
            warning.code
            for warning in plan.warnings
            if warning.acknowledgement_required
        )
        for acknowledgements in ((), required + ("unknown",), required + required[:1]):
            with self.assertRaises(pack_migration.PackMigrationPublicationError):
                pack_migration.prepare_pack_migration_publication(
                    plan,
                    resolution,
                    acknowledged_warning_codes=acknowledgements,
                )

    def test_incomplete_resolution_states_are_rejected(self) -> None:
        variants = (
            {"state": "resolution-required"},
            {"unresolved_roots": (object(),)},
            {"provenance_required": True},
            {"path_collisions": ("mods/a.pw.toml",)},
            {"filename_collisions": ("duplicate.jar",)},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                plan, resolution = self.resolved_input()
                changed = replace(resolution, **changes)
                plan.resolution = changed
                with self.assertRaises(pack_migration.PackMigrationPublicationError):
                    pack_migration.prepare_pack_migration_publication(
                        plan,
                        changed,
                        acknowledged_warning_codes=self.acknowledgements(plan),
                    )
                pack_migration.discard_pack_migration_plan(plan)

    def test_wrong_target_stale_attempt_and_wrong_plan_are_rejected(self) -> None:
        plan, resolution = self.resolved_input()
        wrong_target = replace(plan.target, target_id="other")
        changed = replace(resolution, target=wrong_target)
        plan.resolution = changed
        with self.assertRaises(pack_migration.PackMigrationPublicationError):
            pack_migration.prepare_pack_migration_publication(
                plan, changed,
                acknowledged_warning_codes=self.acknowledgements(plan),
            )
        pack_migration.discard_pack_migration_plan(plan)

        plan, resolution = self.resolved_input()
        changed = replace(resolution, resolution_attempt=resolution.resolution_attempt + 1)
        plan.resolution = changed
        with self.assertRaises(pack_migration.PackMigrationStale):
            pack_migration.prepare_pack_migration_publication(
                plan, changed,
                acknowledged_warning_codes=self.acknowledgements(plan),
            )
        pack_migration.discard_pack_migration_plan(plan)

        first, first_resolution = self.resolved_input()
        pack_migration.discard_pack_migration_plan(first)
        second = self.fixture.plan()
        second.resolution = first_resolution
        second._state = "resolved"
        with self.assertRaises(pack_migration.PackMigrationStale):
            pack_migration.prepare_pack_migration_publication(
                second, first_resolution,
                acknowledged_warning_codes=self.acknowledgements(second),
            )
        pack_migration.discard_pack_migration_plan(second)

    def test_source_detached_staging_and_lock_staleness_are_rejected(self) -> None:
        cases = ("source", "detached", "resolved-source")
        for changed in cases:
            with self.subTest(changed=changed):
                plan, resolution = self.resolved_input()
                if changed == "source":
                    path = self.fixture.pack / "pack.yaml"
                elif changed == "detached":
                    path = plan.source_snapshot_root / "pack.yaml"
                else:
                    path = plan.target_staging_root / "source" / "pack.toml"
                original = path.read_bytes()
                path.write_bytes(original + b"\n# changed\n")
                with self.assertRaises(pack_migration.PackMigrationError):
                    pack_migration.prepare_pack_migration_publication(
                        plan, resolution,
                        acknowledged_warning_codes=self.acknowledgements(plan),
                    )
                if changed == "source":
                    path.write_bytes(original)
                pack_migration.discard_pack_migration_plan(plan)

        plan, resolution = self.resolved_input()
        plan._lock_set.release()
        with self.assertRaises(pack_migration.PackMigrationPublicationError):
            pack_migration.prepare_pack_migration_publication(
                plan, resolution,
                acknowledged_warning_codes=self.acknowledgements(plan),
            )

    def test_target_existing_and_wrong_plan_are_rejected(self) -> None:
        first = self.fixture.plan()
        self.fixture.make_ready(first)
        # The target appearance check is descriptor-bound and occurs before
        # rename; no existing target is ever clobbered.
        (self.fixture.packs / "next").mkdir()
        try:
            with self.assertRaises(pack_migration.PackMigrationPublicationError):
                pack_migration.apply_pack_copy_migration_at(first._public_test_handoff)
            self.assertEqual(list((self.fixture.packs / "next").iterdir()), [])
        finally:
            (self.fixture.packs / "next").rmdir()
            pack_migration.discard_pack_migration_plan(first)

    def test_warning_details_become_stale_after_ready(self) -> None:
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        plan.warnings = tuple(
            warning if warning.code != "resolver-pending" else type(warning)(
                warning.code, warning.message + " changed", warning.relative_path,
                warning.acknowledgement_required,
            )
            for warning in plan.warnings
        )
        with self.assertRaises(pack_migration.PackMigrationPublicationError):
            pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        pack_migration.discard_pack_migration_plan(plan)

    def test_cleanup_failure_preserves_target_and_retry_verifies_it(self) -> None:
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        original = pack_migration._remove_directory_contents
        with patch.object(
            pack_migration,
            "_remove_directory_contents",
            side_effect=pack_migration.PackMigrationCleanupError("cleanup blocked"),
        ):
            with self.assertRaises(pack_migration.PackMigrationCleanupError):
                pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        self.assertTrue((self.fixture.packs / "next").is_dir())
        self.assertTrue(pack_migration.packctl.project_lock_is_active("pack:demo"))
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(pack_migration.PackMigrationCancelled):
            pack_migration.retry_pack_migration_cleanup(
                plan._public_test_handoff, cancel_event=cancelled
            )
        with self.assertRaises(pack_migration.PackMigrationDeadlineExceeded):
            pack_migration.retry_pack_migration_cleanup(
                plan._public_test_handoff, deadline=time.monotonic() - 1
            )
        with patch.object(pack_migration, "_remove_directory_contents", side_effect=original):
            pack_migration.retry_pack_migration_cleanup(plan._public_test_handoff)
        self.assertEqual(plan.state, "applied")

    def test_cancellation_and_deadline_are_checked_before_publication(self) -> None:
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(pack_migration.PackMigrationCancelled):
            pack_migration.apply_pack_copy_migration_at(
                plan._public_test_handoff, cancel_event=cancelled
            )
        pack_migration.discard_pack_migration_plan(plan)
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        with self.assertRaises(pack_migration.PackMigrationDeadlineExceeded):
            pack_migration.apply_pack_copy_migration_at(
                plan._public_test_handoff, deadline=time.monotonic() - 1
            )
        pack_migration.discard_pack_migration_plan(plan)

    def test_callback_exceptions_are_swallowed_and_source_is_unchanged(self) -> None:
        plan = self.fixture.plan()
        source_before = self.fixture.snapshot().snapshot_digest
        self.fixture.make_ready(plan)
        phases: list[str] = []

        def callback(progress: object) -> None:
            phases.append(getattr(progress, "phase", "invalid"))
            raise RuntimeError("observer failure")

        pack_migration.apply_pack_copy_migration_at(
            plan._public_test_handoff, progress=callback
        )
        self.assertIn("publishing", phases)
        self.assertIn("verifying", phases)
        self.assertIn("cleaning-up", phases)
        self.assertEqual(self.fixture.snapshot().snapshot_digest, source_before)

    def test_ambiguous_rename_retains_target_ownership_for_retry(self) -> None:
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        with patch.object(
            packctl,
            "renameat2",
            side_effect=OSError("rename outcome unavailable"),
        ):
            with self.assertRaises(pack_migration.PackMigrationPublicationError):
                pack_migration.apply_pack_copy_migration_at(
                    plan._public_test_handoff
                )
        self.assertTrue(plan.transaction_root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        pack_migration.discard_pack_migration_plan(plan)


class PackMigrationResolverPublicationIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = resolution_fixture.PackMigrationResolutionTest(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_real_resolver_result_prepares_and_publishes(self) -> None:
        plan = self.fixture.plan()
        source_before = pack_migration.snapshot_pack_migration_source_at(
            "pack:demo", self.fixture.pack, self.fixture.root
        ).snapshot_digest
        with patch.object(
            packctl,
            "init_packwiz_project",
            side_effect=self.fixture.fake_init,
        ), patch.object(
            resolution_fixture.core,
            "resolve_mod_closure",
            side_effect=self.fixture.fake_closure,
        ), patch.object(
            resolution_fixture.core,
            "resolve_project_selector",
            side_effect=self.fixture.fake_selector,
        ), patch.object(packctl, "run_packwiz"):
            resolved = pack_migration_resolution.resolve_pack_migration_plan_at(
                plan,
                repository_root=self.fixture.root,
                state_root=self.fixture.state,
            )

        self.assertEqual(resolved.state, "resolved")
        assert resolved.target_source_snapshot is not None
        self.assertEqual(
            resolved.target_source_snapshot.root,
            plan.target_staging_root / "source",
        )
        (plan.target_staging_root / "pack.yaml").write_text(
            """id: next
display_name: Next
enabled: true
distribution:
  rsync_target: host:/packs/next
minecraft_server:
  ssh_host: minecraft
  stack_dir: /stacks/next
  service: next
""",
            encoding="utf-8",
        )
        publication = pack_migration.prepare_pack_migration_publication(
            plan,
            resolved,
            acknowledged_warning_codes=tuple(
                warning.code
                for warning in plan.warnings
                if warning.acknowledgement_required
            ),
        )
        whole_staging = pack_migration.scan_pack_migration_source(
            plan.target_staging_root, checkpoint=lambda: None
        )
        self.assertEqual(
            publication._token.staging_snapshot_digest,
            whole_staging.snapshot_digest,
        )
        self.assertEqual(
            publication._token.staging_content_digest,
            whole_staging.content_digest,
        )
        self.assertNotEqual(
            publication._token.staging_snapshot_digest,
            resolved.target_source_snapshot.snapshot_digest,
        )
        published = pack_migration.apply_pack_copy_migration_at(publication)

        self.assertEqual(published.project_key, "pack:next")
        self.assertEqual(plan.state, "applied")
        self.assertEqual(
            pack_migration.snapshot_pack_migration_source_at(
                "pack:demo", self.fixture.pack, self.fixture.root
            ).snapshot_digest,
            source_before,
        )


if __name__ == "__main__":
    unittest.main()
