from __future__ import annotations

from dataclasses import replace
import shutil
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

    def test_byte_identical_target_replacement_does_not_match_handoff(self) -> None:
        plan = self.fixture.plan()
        self.fixture.make_ready(plan)
        token = plan._validation_token
        assert token is not None
        replacement = self.fixture.packs / "replacement"
        shutil.copytree(plan.target_staging_root, replacement)
        try:
            snapshot = pack_migration.snapshot_pack_migration_source_at(
                "pack:next", replacement, self.fixture.root
            )
            self.assertEqual(
                snapshot._tree_scan.content_digest,
                token.staging_content_digest,
            )
            self.assertNotEqual(snapshot.project_identity, token.staging_identity)
            self.assertFalse(pack_migration._matches_validated_target(snapshot, token))
        finally:
            shutil.rmtree(replacement)
            pack_migration.discard_pack_migration_plan(plan)

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
        changed_code = next(
            warning.code
            for warning in plan.warnings
            if warning.acknowledgement_required
        )
        plan.warnings = tuple(
            warning if warning.code != changed_code else type(warning)(
                warning.code, warning.message + " changed", warning.relative_path,
                warning.acknowledgement_required,
            )
            for warning in plan.warnings
        )
        with self.assertRaises(pack_migration.PackMigrationPublicationError):
            pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        pack_migration.discard_pack_migration_plan(plan)

    def test_every_resolution_semantic_field_is_bound_to_handoff(self) -> None:
        plan, resolution = self.resolved_input()
        variants = {
            "roots": resolution.roots + (object(),),
            "root_candidates": resolution.root_candidates + (object(),),
            "resolved_roots": resolution.resolved_roots + (object(),),
            "unresolved_roots": resolution.unresolved_roots + (object(),),
            "dependency_delta": replace(
                resolution.dependency_delta, added=(object(),)
            ),
            "side_changes": (("a", "b", "client"),),
            "identity_changes": (("a", "b"),),
            "path_collisions": ("mods/new.pw.toml",),
            "filename_collisions": ("new.jar",),
            "provider_warnings": ("provider changed",),
            "url_compatibility": (("a", object()),),
            "target_source_snapshot": replace(
                resolution.target_source_snapshot, snapshot_digest="changed"
            ),
            "provenance_required": True,
            "resolution_attempt": resolution.resolution_attempt + 1,
        }
        try:
            for field, value in variants.items():
                with self.subTest(field=field):
                    changed = replace(resolution, **{field: value})
                    plan.resolution = changed
                    with self.assertRaises(pack_migration.PackMigrationError):
                        pack_migration.apply_pack_migration_publication(
                            plan._public_test_handoff
                        )
                    plan.resolution = resolution
        finally:
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
        snapshot = resolution_fixture.core.snapshot_pack_migration_source("pack:demo")
        source_before = snapshot.snapshot_digest
        source_pack_yaml_before = (self.fixture.pack / "pack.yaml").read_bytes()
        plan = resolution_fixture.core.plan_pack_copy_migration(
            "pack:demo",
            self.fixture.target(),
            expected_snapshot=snapshot,
        )
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
            resolved = resolution_fixture.core.resolve_pack_migration_plan(plan)

        self.assertEqual(resolved.state, "resolved")
        self.assertFalse(
            {"resolver-pending", "url-provider-compatibility-pending"}
            & {warning.code for warning in plan.warnings}
        )
        assert resolved.target_source_snapshot is not None
        self.assertEqual(
            resolved.target_source_snapshot.root,
            plan.target_staging_root / "source",
        )
        target_config = (plan.target_staging_root / "pack.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("id: next", target_config)
        self.assertIn("display_name: Next", target_config)
        self.assertIn("enabled: true", target_config)
        self.assertIn("url_max_jar_size_bytes: 123456", target_config)
        self.assertNotIn("rsync_target:", target_config)
        self.assertNotIn("public_pack_url:", target_config)
        self.assertNotIn("minecraft_server:", target_config)
        publication = resolution_fixture.core.prepare_pack_migration_publication(
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
        published = resolution_fixture.core.apply_pack_migration_publication(publication)

        self.assertEqual(published.project_key, "pack:next")
        self.assertFalse(published.validation_errors)
        self.assertEqual(plan.state, "applied")
        self.assertEqual(
            packctl.project_versions(self.fixture.packs / "next" / "source"),
            ("1.21.4", "fabric", "0.16.0"),
        )
        published_config = (self.fixture.packs / "next" / "pack.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("id: next", published_config)
        self.assertIn("display_name: Next", published_config)
        self.assertNotIn("rsync_target:", published_config)
        self.assertNotIn("public_pack_url:", published_config)
        self.assertNotIn("minecraft_server:", published_config)
        self.assertEqual(
            resolution_fixture.core.snapshot_pack_migration_source(
                "pack:demo"
            ).snapshot_digest,
            source_before,
        )
        self.assertEqual(
            (self.fixture.pack / "pack.yaml").read_bytes(), source_pack_yaml_before
        )


if __name__ == "__main__":
    unittest.main()
