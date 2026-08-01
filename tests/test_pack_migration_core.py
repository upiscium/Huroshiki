from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import pack_migration
import packctl
from pack_migration_resolution import PackMigrationResolutionPlan
from pack_migration_resolution import PackMigrationDependencyDelta
from pack_tree_policy import scan_pack_migration_source


PACK_TOML = '''name = "Demo"
author = "Test"
pack-format = "packwiz:1.1.0"

[versions]
minecraft = "1.21.1"
neoforge = "21.1.1"
'''


class PackMigrationCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.state = self.root / ".huroshiki"
        self.pack = self.packs / "demo"
        self.source = self.pack / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.pack / "content" / "common" / "kubejs" / "server_scripts").mkdir(
            parents=True
        )
        (self.pack / "content" / "client").mkdir()
        (self.pack / "content" / "server").mkdir()
        self.templates.mkdir()
        (self.pack / "pack.yaml").write_text(
            """id: demo
display_name: Demo
enabled: true
distribution:
  rsync_target: host:/packs/demo
minecraft_server:
  ssh_host: minecraft
  stack_dir: /stacks/demo
  service: demo
""",
            encoding="utf-8",
        )
        (self.pack / "profiles.yaml").write_text("profiles: {}\n", encoding="utf-8")
        (self.source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (self.source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (self.source / "mods" / "example.pw.toml").write_text(
            '''name = "Example"
filename = "example.jar"
side = "both"
[download]
url = "https://example.invalid/example.jar"
hash-format = "sha256"
hash = "00"
''',
            encoding="utf-8",
        )
        script = self.pack / "content" / "common" / "kubejs" / "server_scripts" / "x.js"
        script.write_text("event => event\n", encoding="utf-8")
        (self.pack / "dist").mkdir()
        self.stack = ExitStack()
        for module in (packctl, core):
            for name, value in (
                ("ROOT", self.root),
                ("PACKS", self.packs),
                ("TEMPLATES", self.templates),
                ("STATE_ROOT", self.state),
                ("TRANSACTION_ROOT", self.state / "transactions"),
                ("LOG_ROOT", self.state / "logs"),
                ("TRASH_ROOT", self.state / "trash"),
                ("DEPLOY_SNAPSHOT_ROOT", self.state / "deploy-snapshots"),
            ):
                if hasattr(module, name):
                    self.stack.enter_context(patch.object(module, name, value))

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def target(self) -> pack_migration.PackMigrationTarget:
        return pack_migration.PackMigrationTarget(
            "next",
            "Next",
            "1.21.4",
            "NeoForge",  # type: ignore[arg-type]
            "21.4.1",
        )

    def snapshot(self) -> pack_migration.PackMigrationSourceSnapshot:
        return pack_migration.snapshot_pack_migration_source_at(
            "pack:demo", self.pack, self.root
        )

    def plan(self) -> pack_migration.PackMigrationPlan:
        return pack_migration.plan_pack_copy_migration_at(
            "pack:demo",
            self.pack,
            self.packs / "next",
            self.state / "transactions",
            self.target(),
            expected_snapshot=self.snapshot(),
            repository_root=self.root,
            state_root=self.state,
        )

    def make_ready(self, plan: pack_migration.PackMigrationPlan) -> None:
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
        (plan.target_staging_root / "source" / "pack.toml").write_text(
            PACK_TOML.replace("Demo", "Next")
            .replace("1.21.1", "1.21.4")
            .replace("21.1.1", "21.4.1"),
            encoding="utf-8",
        )
        target_scan = scan_pack_migration_source(
            plan.target_staging_root / "source", checkpoint=lambda: None
        )
        resolution = PackMigrationResolutionPlan(
            plan.source_snapshot.snapshot_digest,
            plan.target,
            (), (), (), (), PackMigrationDependencyDelta(), (), (), (), (), (), (),
            target_scan, "resolved", False, 0,
        )
        plan.resolution = resolution
        plan._state = "resolved"
        plan._public_test_handoff = pack_migration.prepare_pack_migration_publication(
            plan,
            resolution,
            acknowledged_warning_codes=tuple(
                warning.code for warning in plan.warnings if warning.acknowledgement_required
            ),
        )

    def test_snapshot_has_semantic_digests_and_no_absolute_path_dependency(self) -> None:
        snapshot = self.snapshot()
        self.assertFalse(snapshot.validation_errors)
        self.assertEqual(snapshot.project_identity, snapshot._tree_scan.root_identity)
        self.assertIsNotNone(snapshot.pack_toml_digest)
        self.assertTrue(snapshot.source_tree_digest)
        self.assertTrue(snapshot.content_tree_digest)
        self.assertTrue(snapshot.provider_metadata_digest)
        self.assertNotIn(str(self.root), snapshot.snapshot_digest)

    def test_plan_stages_detached_copy_and_explicit_warnings(self) -> None:
        before = self.snapshot().snapshot_digest
        plan = self.plan()
        try:
            self.assertEqual(plan.state, "staged")
            self.assertTrue((plan.source_snapshot_root / "source" / "pack.toml").is_file())
            self.assertTrue((plan.target_staging_root / "content" / "common").is_dir())
            self.assertFalse((plan.target_staging_root / "dist").exists())
            codes = {warning.code for warning in plan.warnings}
            self.assertIn("dist-skipped", codes)
            self.assertIn("kubejs-compatibility-unknown", codes)
            self.assertIn("resolver-pending", codes)
            payload = json.loads((plan.transaction_root / "plan.json").read_text())
            self.assertFalse(Path(payload["transaction"]).is_absolute())
            self.assertNotIn(str(self.root), json.dumps(payload))
            self.assertEqual(self.snapshot().snapshot_digest, before)
        finally:
            pack_migration.discard_pack_migration_plan(plan)
        self.assertEqual(plan.state, "discarded")
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))
        self.assertFalse(packctl.project_lock_is_active("pack:next"))

    def test_staged_and_direct_state_mutation_cannot_apply(self) -> None:
        plan = self.plan()
        try:
            with self.assertRaises(AttributeError):
                plan.state = "ready"  # type: ignore[misc]
            plan._state = "ready"
            with self.assertRaisesRegex(pack_migration.PackMigrationError, "publication requires"):
                pack_migration.apply_pack_copy_migration_at(plan)
        finally:
            plan._state = "staged"
            pack_migration.discard_pack_migration_plan(plan)

    def test_apply_atomically_publishes_validated_target(self) -> None:
        plan = self.plan()
        self.make_ready(plan)
        published = pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        self.assertEqual(plan.state, "applied")
        self.assertEqual(published.project_key, "pack:next")
        self.assertTrue((self.packs / "next" / "source" / "pack.toml").is_file())
        self.assertFalse(plan.transaction_root.exists())
        self.assertTrue(self.pack.is_dir())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_source_or_staging_change_blocks_apply_and_retains_plan(self) -> None:
        for changed in ("source", "staging"):
            with self.subTest(changed=changed):
                plan = self.plan()
                self.make_ready(plan)
                if changed == "source":
                    (self.pack / "pack.yaml").write_text(
                        (self.pack / "pack.yaml").read_text() + "note: changed\n",
                        encoding="utf-8",
                    )
                else:
                    (plan.target_staging_root / "extra.txt").write_text(
                        "changed", encoding="utf-8"
                    )
                with self.assertRaises(pack_migration.PackMigrationStale):
                    pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
                self.assertFalse((self.packs / "next").exists())
                if changed == "source":
                    text = (self.pack / "pack.yaml").read_text()
                    (self.pack / "pack.yaml").write_text(
                        text.replace("note: changed\n", ""), encoding="utf-8"
                    )
                pack_migration.discard_pack_migration_plan(plan)

    def test_target_appearance_race_is_rejected(self) -> None:
        plan = self.plan()
        self.make_ready(plan)
        (self.packs / "next").mkdir()
        try:
            with self.assertRaisesRegex(
                pack_migration.PackMigrationPublicationError, "appeared"
            ):
                pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        finally:
            (self.packs / "next").rmdir()
            pack_migration.discard_pack_migration_plan(plan)

    def test_rename_exception_after_publish_is_verified_not_guessed(self) -> None:
        plan = self.plan()
        self.make_ready(plan)
        original = packctl.renameat2

        def publish_then_raise(*args: object) -> None:
            original(*args)  # type: ignore[arg-type]
            raise OSError("uncertain syscall result")

        with patch.object(packctl, "renameat2", side_effect=publish_then_raise):
            published = pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        self.assertEqual(published.project_key, "pack:next")
        self.assertEqual(plan.state, "applied")

    def test_rename_failure_retains_transaction_and_locks_for_discard(self) -> None:
        plan = self.plan()
        self.make_ready(plan)
        with patch.object(packctl, "renameat2", side_effect=OSError("rename failed")):
            with self.assertRaises(pack_migration.PackMigrationPublicationError):
                pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        self.assertEqual(plan.state, "failed")
        self.assertTrue(plan.transaction_root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        pack_migration.discard_pack_migration_plan(plan)

    def test_post_publication_validation_failure_cannot_discard(self) -> None:
        plan = self.plan()
        self.make_ready(plan)
        original = pack_migration.snapshot_pack_migration_source_at
        calls = 0

        def fail_published_snapshot(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise pack_migration.PackMigrationStale("published scan failed")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(
            pack_migration,
            "snapshot_pack_migration_source_at",
            side_effect=fail_published_snapshot,
        ):
            with self.assertRaises(pack_migration.PackMigrationStale):
                pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        self.assertTrue(plan._publication_committed)
        self.assertTrue((self.packs / "next").is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        with self.assertRaisesRegex(pack_migration.PackMigrationError, "cannot be discarded"):
            pack_migration.discard_pack_migration_plan(plan)
        published = pack_migration.retry_pack_migration_cleanup(plan._public_test_handoff)
        self.assertEqual(published.project_key, "pack:next")
        self.assertEqual(plan.state, "applied")

    def test_discard_cleanup_failure_retains_locks_and_retries(self) -> None:
        plan = self.plan()
        original = pack_migration._remove_directory_contents
        with patch.object(
            pack_migration,
            "_remove_directory_contents",
            side_effect=pack_migration.PackMigrationCleanupError("blocked"),
        ):
            with self.assertRaises(pack_migration.PackMigrationCleanupError):
                pack_migration.discard_pack_migration_plan(plan)
        self.assertEqual(plan.state, "failed")
        self.assertTrue(plan.transaction_root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        with patch.object(
            pack_migration,
            "_remove_directory_contents",
            side_effect=original,
        ):
            pack_migration.discard_pack_migration_plan(plan)
        self.assertEqual(plan.state, "discarded")

    def test_publication_lock_release_failure_retains_plan_diagnostic(self) -> None:
        plan = self.plan()
        self.make_ready(plan)
        original = pack_migration._release_plan_locks
        with patch.object(
            pack_migration,
            "_release_plan_locks",
            side_effect=pack_migration.PackMigrationCleanupError("unlock failed"),
        ):
            with self.assertRaisesRegex(
                pack_migration.PackMigrationCleanupError, "unlock failed"
            ):
                pack_migration.apply_pack_copy_migration_at(plan._public_test_handoff)
        self.assertTrue((plan.transaction_root / "plan.json").is_file())
        self.assertEqual(
            [path.name for path in plan.transaction_root.iterdir()], ["plan.json"]
        )
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        with patch.object(
            pack_migration,
            "_release_plan_locks",
            side_effect=original,
        ):
            pack_migration.retry_pack_migration_cleanup(plan._public_test_handoff)
        self.assertEqual(plan.state, "applied")

    def test_transaction_parent_symlink_replacement_cannot_escape(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        original = packctl.make_state_directory

        def replace_parent(*args: object, **kwargs: object) -> Path:
            result = original(*args, **kwargs)  # type: ignore[arg-type]
            result.rmdir()
            result.symlink_to(outside, target_is_directory=True)
            return result

        with patch.object(packctl, "make_state_directory", side_effect=replace_parent):
            with self.assertRaises(OSError):
                self.plan()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_snapshot_checks_cancellation_before_filesystem_access(self) -> None:
        event = threading.Event()
        event.set()
        missing = self.packs / "missing"
        with self.assertRaises(pack_migration.PackMigrationCancelled):
            pack_migration.snapshot_pack_migration_source_at(
                "pack:missing", missing, self.root, cancel_event=event
            )

    def test_canonical_multi_lock_avoids_reverse_deadlock(self) -> None:
        acquired: list[tuple[str, ...]] = []
        first = packctl.acquire_project_locks(
            ("pack:next", "pack:demo"),
            deadline=time.monotonic() + 1,
            cancel_event=None,
        )
        acquired.append(tuple(lock.project_key for lock in first.locks))
        first.release()
        second = packctl.acquire_project_locks(
            ("pack:demo", "pack:next"),
            deadline=time.monotonic() + 1,
            cancel_event=None,
        )
        acquired.append(tuple(lock.project_key for lock in second.locks))
        second.release()
        self.assertEqual(acquired[0], acquired[1])

    def test_partial_lock_release_retains_failed_owner_for_retry(self) -> None:
        lock_set = packctl.acquire_project_locks(
            ("pack:demo", "pack:next"),
            deadline=time.monotonic() + 1,
            cancel_event=None,
        )
        failed_lock = lock_set.locks[-1]
        original = failed_lock.release
        with patch.object(failed_lock, "release", side_effect=OSError("unlock failed")):
            with self.assertRaises(packctl.ProjectLockSetError):
                lock_set.release()
        self.assertEqual(lock_set.owned_keys, ("pack:next",))
        self.assertTrue(packctl.project_lock_is_active("pack:next"))
        original()
        lock_set.release()
        self.assertFalse(lock_set.owned)

    def test_state_classification_observes_target_only_partial_lock(self) -> None:
        plan = self.plan()
        source_lock = next(
            lock for lock in plan._lock_set.locks if lock.project_key == "pack:demo"
        )
        source_lock.release()
        items = packctl.classify_state()
        item = next(item for item in items if item.path == plan.transaction_root)
        self.assertEqual(item.category, "active_transaction")
        pack_migration.discard_pack_migration_plan(plan)

    def test_core_wrappers_derive_roots_and_create_staged_plan(self) -> None:
        snapshot = core.snapshot_pack_migration_source("pack:demo")
        plan = core.plan_pack_copy_migration(
            "pack:demo", self.target(), expected_snapshot=snapshot
        )
        self.assertEqual(plan.target_root, self.packs / "next")
        self.assertEqual(plan.state, "staged")
        core.discard_pack_migration_plan(plan)

    def test_existing_transaction_conflicts_with_copy_migration(self) -> None:
        transaction = core.PackTransaction.create("pack:demo")
        try:
            with self.assertRaisesRegex(packctl.ConfigError, "deadline"):
                packctl.acquire_project_locks(
                    ("pack:demo", "pack:next"),
                    deadline=time.monotonic() + 0.03,
                    cancel_event=None,
                )
        finally:
            transaction.discard()


if __name__ == "__main__":
    unittest.main()
