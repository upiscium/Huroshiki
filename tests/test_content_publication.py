from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import content_operations
import huroshiki_core as core
import packctl


@unittest.skipUnless(sys.platform == "linux", "atomic directory exchange requires Linux")
class ContentPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.state = self.root / ".huroshiki"
        self.pack = self.packs / "demo"
        self.content = self.pack / "content"
        self.templates.mkdir(parents=True)
        source = self.pack / "source"
        source.mkdir(parents=True)
        (source / "pack.toml").write_text(
            'name = "Demo"\nauthor = "tester"\nversion = "1"\n'
            'pack-format = "packwiz:1.1.0"\n',
            encoding="utf-8",
        )
        (source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (self.pack / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n"
            "minecraft:\n  version: 1.21.1\n  loader: neoforge\n"
            "  loader_version: 21.1.0\n",
            encoding="utf-8",
        )
        for side in ("common", "client", "server"):
            (self.content / side).mkdir(parents=True)
        self.original = self.content / "common/config/original.txt"
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b"original")
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
        self.temp.cleanup()

    def plan(self, *operations: core.ContentOperation) -> core.ContentChangePlan:
        return core.plan_content_changes("pack:demo", operations)

    def finish(self, plan: core.ContentChangePlan) -> None:
        if plan._project_lock is not None:
            core.discard_content_plan(plan, deadline=time.monotonic() + 1)

    def test_atomic_exchange_applies_and_retains_exact_original(self) -> None:
        before_identity = self.content.stat().st_ino
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        staging_identity = plan.staging_content.stat().st_ino
        core.apply_content_changes(plan)
        self.assertEqual(plan.state, "applied")
        self.assertEqual(self.original.read_bytes(), b"published")
        self.assertEqual(self.content.stat().st_ino, staging_identity)
        self.assertEqual(plan.retained_original_content.stat().st_ino, before_identity)
        self.assertEqual(
            (plan.retained_original_content / "common/config/original.txt").read_bytes(),
            b"original",
        )
        self.assertTrue((plan.transaction_root / ".completed").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))
        state_item = next(
            item
            for item in packctl.classify_state()
            if item.path == plan.transaction_root
        )
        self.assertEqual(state_item.project_key, "pack:demo")
        self.assertEqual(state_item.category, "completed_transaction")
        core.discard_content_plan(plan)

    def test_apply_rejects_external_file_content_root_and_staging_changes(self) -> None:
        scenarios = ("file", "root", "staging")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                plan = self.plan(
                    core.ContentCreateFile(
                        "common", Path(f"config/{scenario}.txt"), b"planned"
                    )
                )
                try:
                    if scenario == "file":
                        self.original.write_bytes(b"external")
                    elif scenario == "root":
                        replacement = self.pack / "replacement-content"
                        replacement.mkdir()
                        self.content.rename(self.pack / "old-content")
                        replacement.rename(self.content)
                    else:
                        (plan.staging_content / "common/config/staged.txt").write_bytes(
                            b"tampered"
                        )
                    with self.assertRaises(core.ContentPlanStale):
                        core.apply_content_changes(plan)
                    self.assertTrue(packctl.project_lock_is_active("pack:demo"))
                finally:
                    self.finish(plan)
                if scenario == "file":
                    self.original.write_bytes(b"original")
                elif scenario == "root":
                    current = self.content
                    current.rename(self.pack / "external-content")
                    (self.pack / "old-content").rename(self.content)

    def test_missing_content_publication_uses_noreplace_and_rejects_race(self) -> None:
        for child in sorted(self.content.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        self.content.rmdir()
        plan = self.plan(
            core.ContentCreateDirectory("common", Path("config")),
            core.ContentCreateFile("common", Path("config/new.txt"), b"new")
        )
        core.apply_content_changes(plan)
        self.assertEqual(
            (self.content / "common/config/new.txt").read_bytes(), b"new"
        )
        self.assertFalse(plan.retained_original_content.exists())

        self.content.rename(self.pack / "published-content")
        second = self.plan(
            core.ContentCreateDirectory("common", Path("config")),
            core.ContentCreateFile("common", Path("config/race.txt"), b"race")
        )
        self.content.mkdir()
        with self.assertRaises(core.ContentPlanStale):
            core.apply_content_changes(second)
        self.assertTrue(self.content.is_dir())
        self.finish(second)

    def test_exchange_failure_is_fail_closed_and_discard_releases_lock(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        with patch.object(
            content_operations.packctl,
            "renameat2",
            side_effect=OSError("exchange unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "exchange unavailable"):
                core.apply_content_changes(plan)
        self.assertEqual(self.original.read_bytes(), b"original")
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        self.finish(plan)
        self.assertEqual(plan.state, "discarded")

    def test_apply_cancellation_and_deadline_leave_real_content_and_lock_intact(self) -> None:
        for cancelled, deadline in (
            (True, None),
            (False, time.monotonic() - 1),
        ):
            with self.subTest(cancelled=cancelled):
                plan = self.plan(
                    core.ContentReplaceFile(
                        "common", Path("config/original.txt"), b"published"
                    )
                )
                event = None
                if cancelled:
                    event = threading.Event()
                    event.set()
                with self.assertRaises(
                    (
                        core.ContentOperationCancelled
                        if cancelled
                        else core.ContentOperationDeadlineExceeded
                    )
                ):
                    core.apply_content_changes(
                        plan,
                        cancel_event=event,
                        deadline=deadline,
                    )
                self.assertEqual(self.original.read_bytes(), b"original")
                self.assertTrue(packctl.project_lock_is_active("pack:demo"))
                self.finish(plan)

    def test_post_exchange_deadline_uses_fresh_cleanup_deadline(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        execution_deadline = time.monotonic() + 100
        original_checkpoint = content_operations._checkpoint
        expired = False

        def expire_after_exchange(cancel_event, deadline):
            nonlocal expired
            if (
                plan._publication_active
                and deadline == execution_deadline
                and not expired
            ):
                expired = True
                raise core.ContentOperationDeadlineExceeded(
                    "Content operation deadline exceeded"
                )
            return original_checkpoint(cancel_event, deadline)

        with patch.object(
            content_operations,
            "_checkpoint",
            side_effect=expire_after_exchange,
        ):
            with self.assertRaises(core.ContentOperationDeadlineExceeded):
                core.apply_content_changes(plan, deadline=execution_deadline)

        self.assertTrue(expired)
        self.assertEqual(self.original.read_bytes(), b"original")
        self.assertEqual(
            (plan.retained_failed_content / "common/config/original.txt").read_bytes(),
            b"published",
        )
        self.assertFalse(plan._publication_active)
        self.assertIsNone(plan.cleanup_error)
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        self.finish(plan)

    def test_post_exchange_cleanup_deadline_exhaustion_retries_discard(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        execution_deadline = time.monotonic() + 100
        original_checkpoint = content_operations._checkpoint
        expired = False

        def expire_after_exchange(cancel_event, deadline):
            nonlocal expired
            if (
                plan._publication_active
                and deadline == execution_deadline
                and not expired
            ):
                expired = True
                raise core.ContentOperationDeadlineExceeded(
                    "Content operation deadline exceeded"
                )
            return original_checkpoint(cancel_event, deadline)

        with patch.object(
            content_operations,
            "_checkpoint",
            side_effect=expire_after_exchange,
        ), patch.object(
            content_operations,
            "CONTENT_CLEANUP_TIMEOUT_SECONDS",
            0.0,
        ):
            with self.assertRaises(core.ContentCleanupError):
                core.apply_content_changes(plan, deadline=execution_deadline)

        self.assertEqual(self.original.read_bytes(), b"published")
        self.assertTrue(plan._publication_active)
        self.assertIsNotNone(plan.cleanup_error)
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        retry = plan.retry_discard(deadline=time.monotonic() + 1)
        self.assertTrue(retry.done.wait(1))
        retry.raise_for_error()
        self.assertEqual(self.original.read_bytes(), b"original")
        self.assertFalse(plan._publication_active)
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_committed_lock_release_failure_is_observable_and_retryable(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        lock = plan._project_lock
        assert lock is not None
        original_release = lock.release
        with patch.object(lock, "release", side_effect=OSError("lock busy")):
            with self.assertRaisesRegex(core.ContentCleanupError, "lock release failed"):
                core.apply_content_changes(plan)
        self.assertEqual(self.original.read_bytes(), b"published")
        self.assertTrue(plan._publication_committed)
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        with patch.object(lock, "release", side_effect=original_release):
            core.discard_content_plan(plan, deadline=time.monotonic() + 1)
        self.assertEqual(plan.state, "applied")
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_failure_after_exchange_rolls_back_without_losing_either_tree(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        real_renameat2 = packctl.renameat2
        calls = 0

        def exchange_then_fail(*args):
            nonlocal calls
            calls += 1
            real_renameat2(*args)
            if calls == 1:
                raise RuntimeError("after exchange")

        with patch.object(content_operations.packctl, "renameat2", side_effect=exchange_then_fail):
            with self.assertRaisesRegex(RuntimeError, "after exchange"):
                core.apply_content_changes(plan)
        self.assertEqual(self.original.read_bytes(), b"original")
        self.assertEqual(
            (plan.retained_failed_content / "common/config/original.txt").read_bytes(),
            b"published",
        )
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        self.finish(plan)

    def test_exchange_race_restores_external_content_without_publishing_plan(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        original_tree = self.pack / "original-before-race"
        external_tree = self.pack / "external-before-exchange"
        for side in ("common", "client", "server"):
            (external_tree / side).mkdir(parents=True)
        external_file = external_tree / "common/config/external.txt"
        external_file.parent.mkdir(parents=True)
        external_file.write_bytes(b"external")
        real_renameat2 = packctl.renameat2
        injected = False

        def replace_before_exchange(*args):
            nonlocal injected
            if args[4] == packctl.RENAME_EXCHANGE and not injected:
                injected = True
                self.content.rename(original_tree)
                external_tree.rename(self.content)
            return real_renameat2(*args)

        with patch.object(
            content_operations.packctl,
            "renameat2",
            side_effect=replace_before_exchange,
        ):
            with self.assertRaisesRegex(
                core.ContentOperationError,
                "Original Content verification failed",
            ):
                core.apply_content_changes(plan)

        self.assertEqual(
            (self.content / "common/config/external.txt").read_bytes(),
            b"external",
        )
        self.assertFalse((self.content / "common/config/original.txt").exists())
        self.assertTrue((plan.staging_content / "common/config/original.txt").is_file())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        self.finish(plan)

    def test_rollback_failure_retains_lock_and_retry_discard_recovers(self) -> None:
        plan = self.plan(
            core.ContentReplaceFile(
                "common", Path("config/original.txt"), b"published"
            )
        )
        real_renameat2 = packctl.renameat2
        calls = 0

        def fail_after_publish_and_rollback(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                real_renameat2(*args)
                raise RuntimeError("verify failed")
            raise OSError("rollback exchange failed")

        with patch.object(
            content_operations.packctl,
            "renameat2",
            side_effect=fail_after_publish_and_rollback,
        ):
            with self.assertRaises(core.ContentCleanupError):
                core.apply_content_changes(plan)
        self.assertEqual(self.original.read_bytes(), b"published")
        self.assertIsNotNone(plan.cleanup_error)
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))

        retry = plan.retry_discard(deadline=time.monotonic() + 1)
        self.assertTrue(retry.done.wait(1))
        retry.raise_for_error()
        self.assertEqual(self.original.read_bytes(), b"original")
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_retained_rename_and_marker_failures_roll_back(self) -> None:
        for failure in ("retained", "marker"):
            with self.subTest(failure=failure):
                plan = self.plan(
                    core.ContentReplaceFile(
                        "common", Path("config/original.txt"), b"published"
                    )
                )
                if failure == "retained":
                    real_renameat2 = packctl.renameat2

                    def rename(*args):
                        if args[3] == "retained-original-content":
                            raise OSError("retained rename failed")
                        return real_renameat2(*args)

                    context = patch.object(
                        content_operations.packctl, "renameat2", side_effect=rename
                    )
                else:
                    original_touch = Path.touch

                    def touch(path: Path, *args, **kwargs):
                        if path.name == ".completed":
                            raise OSError("marker failed")
                        return original_touch(path, *args, **kwargs)

                    context = patch.object(Path, "touch", autospec=True, side_effect=touch)
                with context:
                    with self.assertRaises((OSError, core.ContentOperationError)):
                        core.apply_content_changes(plan)
                self.assertEqual(self.original.read_bytes(), b"original")
                self.assertTrue(packctl.project_lock_is_active("pack:demo"))
                self.finish(plan)

    def test_discard_is_bounded_non_daemon_retryable_and_idempotent(self) -> None:
        plan = self.plan(
            core.ContentCreateFile("common", Path("config/new.txt"), b"new")
        )
        expired = plan.begin_discard(deadline=time.monotonic() - 1)
        expired.run()
        with self.assertRaises(core.ContentCleanupError):
            expired.raise_for_error()
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        retry = plan.retry_discard(deadline=time.monotonic() + 1)
        self.assertIsNotNone(retry._thread)
        assert retry._thread is not None
        self.assertFalse(retry._thread.daemon)
        self.assertTrue(retry.done.wait(1))
        retry.raise_for_error()
        self.assertEqual(plan.state, "discarded")
        self.assertTrue((plan.transaction_root / ".completed").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))
        core.discard_content_plan(plan)

    def test_discard_begin_is_bounded_while_plan_is_busy(self) -> None:
        plan = self.plan(
            core.ContentCreateFile("common", Path("config/new.txt"), b"new")
        )
        acquired = threading.Event()
        release = threading.Event()

        def hold_plan() -> None:
            with plan._lock:
                acquired.set()
                release.wait(1)

        worker = threading.Thread(target=hold_plan, daemon=False)
        worker.start()
        self.assertTrue(acquired.wait(1))
        started = time.monotonic()
        discard = plan.begin_discard(deadline=started + 0.02)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)
        self.assertTrue(discard.done.is_set())
        with self.assertRaisesRegex(core.ContentCleanupError, "remained busy"):
            discard.raise_for_error()
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        release.set()
        worker.join(1)
        self.finish(plan)

    def test_planner_overlap_matches_existing_build_copy_order(self) -> None:
        for side, value in (
            ("common", b"common"),
            ("client", b"client"),
            ("server", b"server"),
        ):
            target = self.content / side / "config/value.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        plan = self.plan()
        try:
            kinds = {conflict.kind for conflict in plan.conflicts}
            self.assertTrue(
                {"common_client_overlap", "common_server_overlap"} <= kinds
            )
            for side, expected in (("client", b"client"), ("server", b"server")):
                destination = self.root / f"build-{side}"
                destination.mkdir()
                scan = packctl.copy_content_overlays(
                    plan.staging_content,
                    ("common", side),
                    destination,
                )
                self.assertEqual(scan.issues, ())
                self.assertEqual(
                    (destination / "config/value.txt").read_bytes(), expected
                )
        finally:
            self.finish(plan)


if __name__ == "__main__":
    unittest.main()
