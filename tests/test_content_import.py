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
import overlay_policy
import packctl


class ContentImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pack = self.root / "packs/demo"
        self.content = self.pack / "content"
        (self.pack / "source").mkdir(parents=True)
        (self.pack / "pack.yaml").write_text("id: demo\n", encoding="utf-8")
        (self.root / "templates").mkdir()
        for side in ("common", "client", "server"):
            (self.content / side).mkdir(parents=True)
        self.imports = self.root / "local-imports"
        self.imports.mkdir()
        self.state = self.root / ".huroshiki"
        self.stack = ExitStack()
        for module in (packctl, core):
            for name, value in (
                ("ROOT", self.root),
                ("PACKS", self.root / "packs"),
                ("TEMPLATES", self.root / "templates"),
                ("STATE_ROOT", self.state),
                ("TRANSACTION_ROOT", self.state / "transactions"),
            ):
                if hasattr(module, name):
                    self.stack.enter_context(patch.object(module, name, value))

    def tearDown(self) -> None:
        self.stack.close()
        self.temp.cleanup()

    def snapshot(self) -> core.ContentSnapshot:
        return core.content_snapshot("pack:demo")

    def request(
        self,
        source: Path,
        target: str,
        *,
        side: str = "common",
        placement: str = "file",
        policy: str = "reject",
    ) -> core.ContentImportRequest:
        inspected = core.inspect_content_import_source(source)
        return core.ContentImportRequest(
            inspected,
            side,
            Path(target),
            placement,  # type: ignore[arg-type]
            policy,  # type: ignore[arg-type]
        )

    def finish(self, plan: core.ContentChangePlan) -> None:
        if plan._project_lock is not None:
            core.discard_content_plan(plan, deadline=time.monotonic() + 2)

    @unittest.skipUnless(sys.platform == "linux", "atomic publication requires Linux")
    def test_streams_binary_file_preserves_mode_and_applies_existing_plan(self) -> None:
        source = self.imports / "large.bin"
        source.write_bytes((b"\x00\xffpayload" * 200000) + b"end")
        source.chmod(0o751)
        requested: list[int] = []
        original_read = overlay_policy.os.read

        def tracked_read(descriptor: int, count: int) -> bytes:
            requested.append(count)
            return original_read(descriptor, count)

        with patch.object(overlay_policy.os, "read", side_effect=tracked_read):
            plan = core.plan_content_import(
                "pack:demo",
                self.request(source, "resourcepacks/large.bin", side="client"),
                expected_snapshot=self.snapshot(),
            )
        self.assertEqual(plan.state, "ready")
        self.assertIsNotNone(plan.import_summary)
        assert plan.import_summary is not None
        self.assertEqual(plan.import_summary.files, 1)
        self.assertLessEqual(max(requested), 1024 * 1024)
        core.apply_content_changes(plan)
        target = self.content / "client/resourcepacks/large.bin"
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o751)

    @unittest.skipUnless(sys.platform == "linux", "atomic publication requires Linux")
    def test_apply_uses_transaction_copy_not_external_source(self) -> None:
        source = self.imports / "detached.bin"
        source.write_bytes(b"planned")
        plan = core.plan_content_import(
            "pack:demo",
            self.request(source, "resourcepacks/detached.bin", side="client"),
            expected_snapshot=self.snapshot(),
        )
        self.assertTrue(
            all(
                isinstance(
                    operation,
                    (core.ContentCreateFile, core.ContentCreateDirectory),
                )
                for operation in plan.operations
            )
        )
        self.assertTrue((plan.transaction_root / "import-source").is_file())
        source.write_bytes(b"changed after planning")
        source.unlink()
        core.apply_content_changes(plan)
        self.assertEqual(
            (self.content / "client/resourcepacks/detached.bin").read_bytes(),
            b"planned",
        )

    @unittest.skipUnless(sys.platform == "linux", "atomic publication requires Linux")
    def test_directory_import_retains_empty_directories_and_merges_replacements(self) -> None:
        source = self.imports / "tree"
        (source / "empty").mkdir(parents=True)
        (source / "nested").mkdir()
        (source / "nested/new.txt").write_text("new", encoding="utf-8")
        existing = self.content / "common/config/nested/new.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("old", encoding="utf-8")
        plan = core.plan_content_import(
            "pack:demo",
            self.request(
                source,
                "config",
                placement="directory",
                policy="merge-and-replace-files",
            ),
            expected_snapshot=self.snapshot(),
        )
        assert plan.import_summary is not None
        self.assertIn(Path("config/nested/new.txt"), plan.import_summary.updated)
        core.apply_content_changes(plan)
        self.assertEqual(existing.read_text(encoding="utf-8"), "new")
        self.assertTrue((self.content / "common/config/empty").is_dir())

    @unittest.skipUnless(sys.platform == "linux", "atomic publication requires Linux")
    def test_directory_import_preserves_read_only_modes(self) -> None:
        source = self.imports / "readonly"
        nested = source / "nested"
        nested.mkdir(parents=True)
        (nested / "value.txt").write_text("value", encoding="utf-8")
        nested.chmod(0o555)
        source.chmod(0o555)
        plan = core.plan_content_import(
            "pack:demo",
            self.request(source, "readonly", placement="directory"),
            expected_snapshot=self.snapshot(),
        )
        core.apply_content_changes(plan)
        self.assertEqual((self.content / "common/readonly").stat().st_mode & 0o777, 0o555)
        self.assertEqual(
            (self.content / "common/readonly/nested").stat().st_mode & 0o777,
            0o555,
        )

    def test_all_overwrite_policies_and_type_collisions(self) -> None:
        source_file = self.imports / "new.txt"
        source_file.write_text("new", encoding="utf-8")
        target = self.content / "common/config/value.txt"
        target.parent.mkdir(parents=True)
        target.write_text("old", encoding="utf-8")
        for policy, ready in (
            ("reject", False),
            ("replace-files", True),
            ("merge-directories", False),
            ("merge-and-replace-files", True),
        ):
            with self.subTest(policy=policy):
                plan = core.plan_content_import(
                    "pack:demo",
                    self.request(source_file, "config/value.txt", policy=policy),
                    expected_snapshot=self.snapshot(),
                )
                self.assertEqual(plan.state == "ready", ready)
                assert plan.import_summary is not None
                self.assertEqual(bool(plan.import_summary.conflicts), not ready)
                self.finish(plan)

        directory_target = self.content / "common/config/directory"
        directory_target.mkdir()
        plan = core.plan_content_import(
            "pack:demo",
            self.request(source_file, "config/directory", policy="replace-files"),
            expected_snapshot=self.snapshot(),
        )
        self.assertEqual(plan.state, "failed")
        assert plan.import_summary is not None
        self.assertRegex(plan.import_summary.conflicts[0], "type collision")
        self.finish(plan)

    def test_inspection_rejects_relative_unsafe_hardlinked_and_colliding_sources(self) -> None:
        with self.assertRaisesRegex(core.ContentOperationError, "absolute path"):
            core.inspect_content_import_source("relative.txt")

        unsafe = self.imports / "unsafe"
        unsafe.mkdir()
        (unsafe / "target").write_text("x", encoding="utf-8")
        (unsafe / "link").symlink_to("target")
        snapshot = core.inspect_content_import_source(unsafe)
        self.assertEqual(snapshot.source_kind, "directory")
        self.assertTrue(any("symlink" in error for error in snapshot.validation_errors))

        plan = core.plan_content_import(
            "pack:demo",
            core.ContentImportRequest(
                snapshot,
                "common",
                Path("unsafe"),
                "directory",
                "reject",
            ),
            expected_snapshot=self.snapshot(),
        )
        self.assertEqual(plan.state, "failed")
        assert plan.import_summary is not None
        self.assertTrue(plan.import_summary.rejected)
        self.finish(plan)

        hardlinks = self.imports / "hardlinks"
        hardlinks.mkdir()
        first = hardlinks / "first"
        first.write_text("x", encoding="utf-8")
        os.link(first, hardlinks / "second")
        snapshot = core.inspect_content_import_source(hardlinks)
        self.assertTrue(any("hard-linked" in error for error in snapshot.validation_errors))

        collision = self.imports / "collision"
        collision.mkdir()
        (collision / "Name.txt").write_text("a", encoding="utf-8")
        (collision / "name.txt").write_text("b", encoding="utf-8")
        snapshot = core.inspect_content_import_source(collision)
        self.assertTrue(any("portable source path collision" in error for error in snapshot.validation_errors))

    def test_rejects_special_entries_reserved_targets_and_self_reference(self) -> None:
        if hasattr(os, "mkfifo"):
            fifo_root = self.imports / "fifo"
            fifo_root.mkdir()
            os.mkfifo(fifo_root / "pipe")
            snapshot = core.inspect_content_import_source(fifo_root)
            self.assertTrue(any("special" in error for error in snapshot.validation_errors))

        source = self.imports / "value"
        source.write_text("value", encoding="utf-8")
        with self.assertRaisesRegex(core.ContentOperationError, "Packwiz-owned"):
            core.plan_content_import(
                "pack:demo",
                self.request(source, "index.toml"),
                expected_snapshot=self.snapshot(),
            )
        live_request = self.request(
            self.content / "common", "copied", placement="directory"
        )
        with self.assertRaisesRegex(core.ContentOperationError, "live Content tree"):
            core.plan_content_import(
                "pack:demo", live_request, expected_snapshot=self.snapshot()
            )

    def test_revalidates_source_and_expected_target_snapshot(self) -> None:
        source = self.imports / "value.txt"
        source.write_text("before", encoding="utf-8")
        request = self.request(source, "config/value.txt")
        baseline = self.snapshot()
        source.write_text("after", encoding="utf-8")
        with self.assertRaisesRegex(core.ContentOperationError, "changed after inspection"):
            core.plan_content_import(
                "pack:demo", request, expected_snapshot=baseline
            )

        request = self.request(source, "config/value.txt")
        (self.content / "common/external.txt").write_text("change", encoding="utf-8")
        with self.assertRaises(core.ContentPlanStale):
            core.plan_content_import(
                "pack:demo", request, expected_snapshot=baseline
            )

    def test_detects_source_change_during_streaming_copy(self) -> None:
        source = self.imports / "changing.bin"
        source.write_bytes(b"x" * (3 * 1024 * 1024))
        request = self.request(source, "changing.bin")
        original_read = overlay_policy.os.read
        reads = 0

        def mutate_during_copy(descriptor: int, count: int) -> bytes:
            nonlocal reads
            result = original_read(descriptor, count)
            reads += 1
            if reads == 5:
                with source.open("ab") as handle:
                    handle.write(b"changed")
            return result

        with patch.object(overlay_policy.os, "read", side_effect=mutate_during_copy):
            with self.assertRaisesRegex(core.ContentOperationError, "changed while copying"):
                core.plan_content_import(
                    "pack:demo", request, expected_snapshot=self.snapshot()
                )
        self.assertFalse((self.content / "common/changing.bin").exists())

    def test_staging_parent_replacement_cannot_redirect_import_write(self) -> None:
        source = self.imports / "race.bin"
        source.write_bytes(b"race payload")
        original_stream = overlay_policy._stream_verified_import_file

        for policy in ("reject", "replace-files"):
            with self.subTest(policy=policy):
                target = self.content / "common/config/race.bin"
                target.parent.mkdir(parents=True, exist_ok=True)
                if policy == "replace-files":
                    target.write_bytes(b"original")
                elif target.exists():
                    target.unlink()
                outside = self.root / f"outside-{policy}"
                outside.mkdir()
                stream_calls = 0

                def replace_parent(source_fd, output_fd, expected, checkpoint):
                    nonlocal stream_calls
                    stream_calls += 1
                    if stream_calls == 2:
                        temporary = Path(os.readlink(f"/proc/self/fd/{output_fd}"))
                        parent = temporary.parent
                        retained = parent.with_name(parent.name + "-pinned")
                        parent.rename(retained)
                        parent.symlink_to(outside, target_is_directory=True)
                    return original_stream(
                        source_fd,
                        output_fd,
                        expected,
                        checkpoint,
                    )

                with patch.object(
                    overlay_policy,
                    "_stream_verified_import_file",
                    side_effect=replace_parent,
                ):
                    with self.assertRaises(core.ContentOperationError):
                        core.plan_content_import(
                            "pack:demo",
                            self.request(
                                source,
                                "config/race.bin",
                                policy=policy,
                            ),
                            expected_snapshot=self.snapshot(),
                        )
                self.assertEqual(list(outside.iterdir()), [])
                if policy == "replace-files":
                    self.assertEqual(target.read_bytes(), b"original")
                else:
                    self.assertFalse(target.exists())

    def test_planning_cleanup_failure_returns_owned_retryable_plan(self) -> None:
        source = self.imports / "cleanup.txt"
        source.write_text("cleanup", encoding="utf-8")
        request = self.request(source, "cleanup.txt")
        original_release = packctl.ProjectLock.release
        with patch.object(
            content_operations,
            "copy_import_source",
            side_effect=core.ContentOperationError("copy failed"),
        ), patch.object(
            packctl.ProjectLock,
            "release",
            side_effect=OSError("lock busy"),
        ):
            plan = core.plan_content_import(
                "pack:demo",
                request,
                expected_snapshot=self.snapshot(),
            )
        self.assertEqual(plan.state, "failed")
        self.assertIsNotNone(plan.cleanup_error)
        self.assertIsNotNone(plan._project_lock)
        assert plan.import_summary is not None
        self.assertRegex(plan.import_summary.rejected[0], "cleanup failed")
        with patch.object(packctl.ProjectLock, "release", original_release):
            core.discard_content_plan(plan, deadline=time.monotonic() + 1)
        self.assertEqual(plan.state, "discarded")
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_cancellation_and_deadline_checkpoint_before_access_and_during_copy(self) -> None:
        source = self.imports / "large"
        source.write_bytes(b"x" * (3 * 1024 * 1024))
        with patch.object(Path, "lstat", side_effect=AssertionError("accessed")):
            with self.assertRaises(core.ContentOperationDeadlineExceeded):
                core.inspect_content_import_source(
                    source, deadline=time.monotonic() - 1
                )

        request = self.request(source, "large")
        event = threading.Event()
        reads = 0
        original_read = overlay_policy.os.read

        def cancel_during_read(descriptor: int, count: int) -> bytes:
            nonlocal reads
            result = original_read(descriptor, count)
            reads += 1
            if reads == 2:
                event.set()
            return result

        with patch.object(overlay_policy.os, "read", side_effect=cancel_during_read):
            with self.assertRaises(core.ContentOperationCancelled):
                core.plan_content_import(
                    "pack:demo",
                    request,
                    expected_snapshot=self.snapshot(),
                    cancel_event=event,
                )
        self.assertFalse((self.content / "common/large").exists())


if __name__ == "__main__":
    unittest.main()
