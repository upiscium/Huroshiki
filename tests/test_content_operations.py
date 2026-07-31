from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch

import content_operations
import huroshiki_core as core
import overlay_policy
import packctl


class ContentOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.state = self.root / ".huroshiki"
        self.pack = self.packs / "demo"
        self.content = self.pack / "content"
        self.templates.mkdir(parents=True)
        (self.pack / "source").mkdir(parents=True)
        (self.pack / "pack.yaml").write_text("id: demo\n", encoding="utf-8")
        for side in ("common", "client", "server"):
            (self.content / side).mkdir(parents=True)
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

    def discard(self, plan: core.ContentChangePlan) -> None:
        if plan._project_lock is not None:
            core.discard_content_plan(plan, deadline=time.monotonic() + 1)

    def test_listing_reports_metadata_text_kind_category_and_invalid_entries(self) -> None:
        text = self.content / "common/kubejs/startup_scripts/demo.js"
        text.parent.mkdir(parents=True)
        text.write_text("console.log('demo')\n", encoding="utf-8")
        text.chmod(0o755)
        binary = self.content / "client/resourcepacks/demo.bin"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"\x00\xffbinary")
        link = self.content / "server/config-link"
        link.symlink_to("missing")

        entries = core.list_content_entries("pack:demo")
        by_key = {(entry.side, entry.relative_path): entry for entry in entries}
        text_entry = by_key[("common", Path("kubejs/startup_scripts/demo.js"))]
        self.assertEqual(text_entry.kind, "file")
        self.assertEqual(text_entry.digest, hashlib.sha256(text.read_bytes()).hexdigest())
        self.assertEqual(text_entry.text_kind, "utf8")
        self.assertEqual(text_entry.category, "kubejs")
        self.assertTrue(text_entry.executable)
        self.assertEqual(text_entry.mode, 0o755)
        self.assertEqual(by_key[("client", Path("resourcepacks/demo.bin"))].text_kind, "binary")
        self.assertEqual(by_key[("client", Path("resourcepacks"))].kind, "directory")
        self.assertEqual(by_key[("server", Path("config-link"))].kind, "invalid")
        self.assertTrue(by_key[("server", Path("config-link"))].errors)
        self.assertTrue(all(entry.side == "client" for entry in core.list_content_entries("pack:demo", "client")))
        with self.assertRaisesRegex(core.ContentOperationError, "Content side"):
            core.list_content_entries("pack:demo", "invalid")

    def test_binary_read_is_bounded_and_template_is_rejected(self) -> None:
        target = self.content / "common/config/data.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\xff\x00payload")
        file = core.read_content_file("pack:demo", "common", "config/data.bin")
        self.assertEqual(file.contents, b"\xff\x00payload")
        self.assertEqual(file.entry.text_kind, "binary")
        with self.assertRaisesRegex(core.ContentOperationError, "read limit"):
            core.read_content_file(
                "pack:demo", "common", "config/data.bin", max_bytes=2
            )
        template = self.templates / "base"
        template.mkdir()
        (template / "template.yaml").write_text("id: base\n", encoding="utf-8")
        with self.assertRaisesRegex(
            core.HuroshikiError,
            "currently available only for packs",
        ):
            core.list_content_entries("template:base")

    def test_listing_and_snapshot_stream_large_files_with_bounded_probe_memory(self) -> None:
        target = self.content / "client/resourcepacks/large.bin"
        target.parent.mkdir(parents=True)
        size = 32 * 1024 * 1024
        with target.open("wb") as handle:
            handle.truncate(size)
        requested_reads: list[int] = []
        original_read = overlay_policy.os.read

        def track_read(descriptor: int, count: int) -> bytes:
            requested_reads.append(count)
            return original_read(descriptor, count)

        tracemalloc.start()
        try:
            with patch.object(
                content_operations,
                "read_overlay_bytes",
                side_effect=AssertionError("listing must not materialize file bytes"),
            ), patch.object(overlay_policy.os, "read", side_effect=track_read):
                entries = core.list_content_entries("pack:demo")
                snapshot = core.content_snapshot("pack:demo")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        entry = next(
            item
            for item in entries
            if item.relative_path == Path("resourcepacks/large.bin")
        )
        self.assertEqual(entry.size, size)
        self.assertIsNotNone(entry.digest)
        self.assertTrue(
            any(
                item.relative_path == Path("resourcepacks/large.bin")
                and item.digest == entry.digest
                for item in snapshot.entries
            )
        )
        inspection = overlay_policy.inspect_overlay_file(
            self.content,
            "client",
            "resourcepacks/large.bin",
            probe_bytes=content_operations.CONTENT_TEXT_PROBE_BYTES,
        )
        self.assertEqual(len(inspection.text_probe), 64 * 1024)
        self.assertEqual(inspection.text_kind, "binary")
        self.assertLessEqual(max(requested_reads), 1024 * 1024)
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_streaming_text_classification_covers_full_file_and_probe_boundary(self) -> None:
        config = self.content / "common/config"
        config.mkdir(parents=True)
        prefix = b"a" * content_operations.CONTENT_TEXT_PROBE_BYTES
        (config / "late-invalid.txt").write_bytes(prefix + b"\xff")
        (config / "late-nul.txt").write_bytes(prefix + b"\0")
        (config / "incomplete-eof.txt").write_bytes(prefix + b"\xc3")
        (config / "split-valid.txt").write_bytes(
            prefix[:-1] + "é".encode("utf-8") + b"tail"
        )

        by_path = {
            entry.relative_path: entry
            for entry in core.list_content_entries("pack:demo", "common")
        }
        self.assertEqual(
            by_path[Path("config/late-invalid.txt")].text_kind,
            "binary",
        )
        self.assertEqual(
            by_path[Path("config/late-nul.txt")].text_kind,
            "binary",
        )
        self.assertEqual(
            by_path[Path("config/incomplete-eof.txt")].text_kind,
            "binary",
        )
        self.assertEqual(
            by_path[Path("config/split-valid.txt")].text_kind,
            "utf8",
        )
        inspection = overlay_policy.inspect_overlay_file(
            self.content,
            "common",
            "config/split-valid.txt",
            probe_bytes=content_operations.CONTENT_TEXT_PROBE_BYTES,
        )
        self.assertEqual(inspection.text_kind, "utf8")
        self.assertEqual(len(inspection.text_probe), 64 * 1024)
        self.assertTrue(inspection.text_probe.endswith(b"\xc3"))

    def test_coherent_browser_result_is_lock_free_and_detects_external_change(self) -> None:
        common = self.content / "common/config/demo.txt"
        client = self.content / "client/config/demo.txt"
        common.parent.mkdir(parents=True)
        client.parent.mkdir(parents=True)
        common.write_text("common", encoding="utf-8")
        client.write_text("client", encoding="utf-8")

        result = core.load_content_browser("pack:demo")
        self.assertEqual(
            {(entry.side, entry.relative_path) for entry in result.entries},
            {
                (entry.side, entry.relative_path)
                for entry in result.snapshot.entries
            },
        )
        self.assertTrue(
            any(
                conflict.kind == "common_client_overlap"
                for conflict in result.conflicts
            )
        )
        self.assertEqual(
            result.conflicts,
            core.analyze_content_conflicts(result.snapshot),
        )
        self.assertFalse((self.state / "transactions").exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

        original_list = content_operations.list_content_entries_at
        calls = 0

        def change_during_load(*args, **kwargs):
            nonlocal calls
            calls += 1
            entries = original_list(*args, **kwargs)
            if calls == 2:
                common.write_text("changed", encoding="utf-8")
            return entries

        with patch.object(
            content_operations,
            "list_content_entries_at",
            side_effect=change_during_load,
        ):
            with self.assertRaisesRegex(
                core.ContentOperationError,
                "changed while loading the browser",
            ):
                core.load_content_browser("pack:demo")

        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(core.ContentOperationCancelled):
            core.load_content_browser("pack:demo", cancel_event=cancelled)
        with self.assertRaises(core.ContentOperationDeadlineExceeded):
            core.load_content_browser("pack:demo", deadline=time.monotonic() - 1)

    def test_path_info_reports_file_directory_invalid_and_all_sides_read_only(self) -> None:
        common = self.content / "common/config/run.sh"
        common.parent.mkdir(parents=True)
        common.write_bytes(b"#!/bin/sh\n")
        common.chmod(0o755)
        client_directory = self.content / "client/resourcepacks/empty"
        client_directory.mkdir(parents=True)
        invalid = self.content / "server/config-link"
        invalid.symlink_to("/outside")
        browser = core.load_content_browser("pack:demo")

        file_info = core.resolve_content_path_info(
            "pack:demo",
            "common",
            "config/run.sh",
            expected_snapshot=browser.snapshot,
        )
        self.assertEqual(file_info.project_key, "pack:demo")
        self.assertEqual(file_info.relative_path, Path("config/run.sh"))
        self.assertEqual(
            file_info.repository_relative_path,
            Path("packs/demo/content/common/config/run.sh"),
        )
        self.assertEqual(file_info.absolute_path, common)
        self.assertEqual(file_info.kind, "file")
        self.assertEqual(file_info.size, len(b"#!/bin/sh\n"))
        self.assertEqual(file_info.mode, 0o755)
        self.assertTrue(file_info.executable)
        self.assertEqual(
            file_info.digest,
            hashlib.sha256(b"#!/bin/sh\n").hexdigest(),
        )
        self.assertEqual(file_info.snapshot_digest, browser.snapshot.digest)
        self.assertEqual(file_info.errors, ())

        directory_info = core.resolve_content_path_info(
            "pack:demo",
            "client",
            "resourcepacks/empty",
            expected_snapshot=browser.snapshot,
        )
        self.assertEqual(directory_info.kind, "directory")
        self.assertEqual(directory_info.size, 0)
        self.assertIsNone(directory_info.digest)

        invalid_info = core.resolve_content_path_info(
            "pack:demo",
            "server",
            "config-link",
            expected_snapshot=browser.snapshot,
        )
        self.assertEqual(invalid_info.kind, "invalid")
        self.assertTrue(invalid_info.errors)
        self.assertEqual(invalid_info.absolute_path, invalid)
        self.assertFalse((self.state / "transactions").exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_path_info_rejects_stale_races_invalid_paths_and_templates(self) -> None:
        target = self.content / "common/config/value.txt"
        target.parent.mkdir(parents=True)
        target.write_text("before", encoding="utf-8")
        browser = core.load_content_browser("pack:demo")
        target.write_text("after", encoding="utf-8")
        with self.assertRaisesRegex(core.ContentPlanStale, "reload Content"):
            core.resolve_content_path_info(
                "pack:demo",
                "common",
                "config/value.txt",
                expected_snapshot=browser.snapshot,
            )

        target.write_text("before", encoding="utf-8")
        browser = core.load_content_browser("pack:demo")
        original_inspect = content_operations.inspect_overlay_entry

        def replace_after_entry_inspection(*args, **kwargs):
            result = original_inspect(*args, **kwargs)
            target.unlink()
            target.write_text("replacement", encoding="utf-8")
            return result

        with patch.object(
            content_operations,
            "inspect_overlay_entry",
            side_effect=replace_after_entry_inspection,
        ):
            with self.assertRaises(core.ContentPlanStale):
                core.resolve_content_path_info(
                    "pack:demo",
                    "common",
                    "config/value.txt",
                    expected_snapshot=browser.snapshot,
                )

        for path in ("../escape", "/absolute", "pack.toml", ".gitkeep", "CON/file"):
            with self.subTest(path=path):
                with self.assertRaises(core.ContentOperationError):
                    core.resolve_content_path_info(
                        "pack:demo",
                        "common",
                        path,
                        expected_snapshot=browser.snapshot,
                    )
        with self.assertRaises(core.ContentPlanStale):
            core.resolve_content_path_info(
                "pack:demo",
                "common",
                "config/value.txt",
                expected_snapshot=core.ContentSnapshot(
                    "pack:other",
                    browser.snapshot.project_identity,
                    browser.snapshot.content_parent_identity,
                    browser.snapshot.content_identity,
                    browser.snapshot.entries,
                    browser.snapshot.digest,
                ),
            )
        with self.assertRaisesRegex(core.HuroshikiError, "only for packs"):
            core.resolve_content_path_info(
                "template:base",
                "common",
                "config/value.txt",
                expected_snapshot=browser.snapshot,
            )

    def test_path_info_deadline_and_cancellation_are_checked_during_resolution(self) -> None:
        target = self.content / "common/config/large.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * (2 * 1024 * 1024))
        browser = core.load_content_browser("pack:demo")
        with patch.object(
            Path,
            "lstat",
            side_effect=AssertionError("filesystem accessed before deadline check"),
        ):
            with self.assertRaises(core.ContentOperationDeadlineExceeded):
                core.resolve_content_path_info(
                    "pack:demo",
                    "common",
                    "config/large.bin",
                    expected_snapshot=browser.snapshot,
                    deadline=time.monotonic() - 1,
                )

        cancelled = threading.Event()
        original_read = overlay_policy.os.read
        reads = 0

        def cancel_during_read(fd: int, count: int) -> bytes:
            nonlocal reads
            data = original_read(fd, count)
            reads += 1
            if reads == 1:
                cancelled.set()
            return data

        with patch.object(overlay_policy.os, "read", side_effect=cancel_during_read):
            with self.assertRaises(core.ContentOperationCancelled):
                core.resolve_content_path_info(
                    "pack:demo",
                    "common",
                    "config/large.bin",
                    expected_snapshot=browser.snapshot,
                    cancel_event=cancelled,
                )

    def test_overlay_scan_checkpoint_precedes_access_and_interrupts_traversal(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaises(core.ContentOperationCancelled):
            core.load_content_browser("pack:demo", cancel_event=cancel_event)
        with self.assertRaises(core.ContentOperationDeadlineExceeded):
            core.load_content_browser("pack:demo", deadline=time.monotonic() - 1)

        cancelled = core.ContentOperationCancelled("cancelled")
        with patch.object(
            Path,
            "lstat",
            side_effect=AssertionError("filesystem access happened before checkpoint"),
        ):
            with self.assertRaises(core.ContentOperationCancelled):
                overlay_policy.scan_content_overlays(
                    self.content,
                    checkpoint=lambda: (_ for _ in ()).throw(cancelled),
                )

        directory = self.content / "common/config"
        directory.mkdir(parents=True)
        for index in range(100):
            (directory / f"item-{index:03}.txt").write_text("value", encoding="utf-8")
        checkpoints = 0

        def interrupt() -> None:
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 20:
                raise core.ContentOperationCancelled("mid-scan cancellation")

        with self.assertRaisesRegex(
            core.ContentOperationCancelled,
            "mid-scan cancellation",
        ):
            overlay_policy.scan_content_overlays(
                self.content,
                checkpoint=interrupt,
            )
        self.assertEqual(checkpoints, 20)

    def test_text_document_preserves_mode_newlines_and_snapshot_digest_cas(self) -> None:
        config = self.content / "common/config"
        config.mkdir(parents=True)
        cases = {
            "lf.txt": ("one\ntwo\n", "lf", b"one\ntwo\n"),
            "crlf.txt": ("one\r\ntwo\r\n", "crlf", b"one\r\ntwo\r\n"),
            "cr.txt": ("one\rtwo\r", "cr", b"one\rtwo\r"),
            "mixed.txt": ("one\r\ntwo\nthree\r", "mixed", b"one\ntwo\nthree\n"),
            "none.txt": ("one", "none", b"one"),
        }
        for name, (text, _, _) in cases.items():
            path = config / name
            path.write_bytes(text.encode("utf-8"))
            path.chmod(0o754)
        browser = core.load_content_browser("pack:demo")
        for name, (_source, policy, encoded) in cases.items():
            document = core.load_content_text_document(
                "pack:demo",
                "common",
                Path("config") / name,
                expected_snapshot=browser.snapshot,
            )
            self.assertEqual(document.newline_policy, policy)
            self.assertEqual(document.mode, 0o754)
            self.assertEqual(document.digest, hashlib.sha256((config / name).read_bytes()).hexdigest())
            self.assertEqual(
                core.encode_content_editor_text(document.text, document.newline_policy),
                encoded,
            )
            self.assertEqual(document.text.endswith("\n"), encoded.endswith((b"\n", b"\r")))

        (config / "lf.txt").write_text("external", encoding="utf-8")
        with self.assertRaisesRegex(core.ContentPlanStale, "reload the browser"):
            core.load_content_text_document(
                "pack:demo",
                "common",
                "config/lf.txt",
                expected_snapshot=browser.snapshot,
            )

    def test_text_document_rejects_binary_directory_large_and_read_replacement(self) -> None:
        config = self.content / "common/config"
        config.mkdir(parents=True)
        binary = config / "binary.bin"
        binary.write_bytes(b"text\0binary")
        large = config / "large.txt"
        large.write_bytes(b"x" * 32)
        text = config / "text.txt"
        text.write_text("before", encoding="utf-8")
        browser = core.load_content_browser("pack:demo")

        for relative, limit, message in (
            ("config/binary.bin", 1024, "Binary or invalid UTF-8"),
            ("config", 1024, "regular Content files"),
            ("config/large.txt", 8, "read limit"),
        ):
            with self.assertRaisesRegex(core.ContentOperationError, message):
                core.load_content_text_document(
                    "pack:demo",
                    "common",
                    relative,
                    expected_snapshot=browser.snapshot,
                    max_bytes=limit,
                )

        original_read = content_operations.read_overlay_bytes

        def replace_after_read(*args, **kwargs):
            result = original_read(*args, **kwargs)
            text.write_text("after", encoding="utf-8")
            return result

        with patch.object(
            content_operations,
            "read_overlay_bytes",
            side_effect=replace_after_read,
        ):
            with self.assertRaisesRegex(core.ContentPlanStale, "reload the browser"):
                core.load_content_text_document(
                    "pack:demo",
                    "common",
                    "config/text.txt",
                    expected_snapshot=browser.snapshot,
                )

    def test_snapshot_is_stable_and_tracks_content_mode_and_empty_directories(self) -> None:
        target = self.content / "common/config/demo.toml"
        target.parent.mkdir(parents=True)
        target.write_text("enabled = true\n", encoding="utf-8")
        empty = self.content / "server/kubejs/empty"
        empty.mkdir(parents=True)
        first = core.content_snapshot("pack:demo")
        second = core.content_snapshot("pack:demo")
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.entries, second.entries)
        self.assertIn(
            ("server", Path("kubejs/empty")),
            {(entry.side, entry.relative_path) for entry in first.entries},
        )
        target.chmod(0o755)
        mode_changed = core.content_snapshot("pack:demo")
        self.assertNotEqual(first.digest, mode_changed.digest)
        target.write_text("enabled = false\n", encoding="utf-8")
        self.assertNotEqual(
            mode_changed.digest,
            core.content_snapshot("pack:demo").digest,
        )
        sample = next(entry for entry in first.entries if entry.digest is not None)
        self.assertEqual(
            sample.portable_identity,
            (sample.side, sample.relative_path.as_posix().casefold()),
        )

    def test_plan_applies_multiple_operations_only_to_staging_and_derives_changes(self) -> None:
        original = self.content / "common/config/original.txt"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"old")
        executable = self.content / "client/kubejs/run.sh"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"#!/bin/sh\n")
        executable.chmod(0o755)
        before = core.content_snapshot("pack:demo")
        operations: tuple[core.ContentOperation, ...] = (
            core.ContentCreateDirectory("common", Path("config/new")),
            core.ContentCreateFile(
                "common", Path("config/new/data.bin"), b"\x00new"
            ),
            core.ContentReplaceFile(
                "common",
                Path("config/original.txt"),
                b"updated",
                expected_digest=hashlib.sha256(b"old").hexdigest(),
            ),
            core.ContentCreateDirectory("server", Path("kubejs")),
            core.ContentMove(
                "client",
                Path("kubejs/run.sh"),
                "server",
                Path("kubejs/run.sh"),
            ),
        )
        plan = core.plan_content_changes(
            "pack:demo",
            operations,
            expected_snapshot=before,
        )
        try:
            self.assertEqual(plan.state, "ready")
            self.assertEqual(original.read_bytes(), b"old")
            self.assertTrue(executable.is_file())
            self.assertEqual(
                (plan.staging_content / "common/config/original.txt").read_bytes(),
                b"updated",
            )
            moved = plan.staging_content / "server/kubejs/run.sh"
            self.assertTrue(moved.is_file())
            self.assertTrue(moved.stat().st_mode & 0o111)
            actions = {change.action for change in plan.changes}
            self.assertTrue({"created", "updated", "moved", "unchanged"} <= actions)
            self.assertTrue(packctl.project_lock_is_active("pack:demo"))
            state_item = next(
                item
                for item in packctl.classify_state()
                if item.path == plan.transaction_root
            )
            self.assertEqual(state_item.project_key, "pack:demo")
            self.assertEqual(state_item.category, "active_transaction")
        finally:
            self.discard(plan)

    def test_operation_validation_and_stale_replace_fail_closed(self) -> None:
        target = self.content / "common/config/value.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"value")
        invalid_operations = (
            core.ContentCreateFile("common", Path("/absolute"), b"x"),
            core.ContentCreateFile("common", Path("../escape"), b"x"),
            core.ContentCreateFile("common", Path("pack.toml"), b"x"),
            core.ContentCreateFile("wrong", Path("file"), b"x"),
            core.ContentCreateFile("common", Path("file"), b"x", mode=0o1000),
        )
        for operation in invalid_operations:
            with self.subTest(operation=operation):
                with self.assertRaises(core.ContentOperationError):
                    core.plan_content_changes("pack:demo", (operation,))
        with self.assertRaisesRegex(core.ContentOperationError, "Duplicate"):
            core.plan_content_changes(
                "pack:demo",
                (
                    core.ContentCreateFile("common", Path("Test.txt"), b"x"),
                    core.ContentCreateFile("common", Path("test.txt"), b"y"),
                ),
            )
        with self.assertRaisesRegex(core.ContentOperationError, "digest changed"):
            core.plan_content_changes(
                "pack:demo",
                (
                    core.ContentReplaceFile(
                        "common",
                        Path("config/value.txt"),
                        b"new",
                        expected_digest="0" * 64,
                    ),
                ),
            )

    def test_directory_delete_move_collision_and_missing_source_are_rejected(self) -> None:
        directory = self.content / "common/config/nonempty"
        directory.mkdir(parents=True)
        (directory / "file").write_text("x", encoding="utf-8")
        existing = self.content / "server/config/existing"
        existing.parent.mkdir(parents=True)
        existing.write_text("x", encoding="utf-8")
        cases = (
            core.ContentDeleteDirectory("common", Path("config/nonempty")),
            core.ContentMove(
                "common", Path("missing"), "server", Path("config/new")
            ),
            core.ContentMove(
                "common", Path("config/nonempty"), "server", Path("config/existing")
            ),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(core.ContentOperationError):
                    core.plan_content_changes("pack:demo", (operation,))

    def test_file_and_empty_directory_deletes_are_planned(self) -> None:
        directory = self.content / "common/config/removable"
        directory.mkdir(parents=True)
        file = directory / "file.txt"
        file.write_text("remove", encoding="utf-8")
        plan = core.plan_content_changes(
            "pack:demo",
            (
                core.ContentDeleteFile(
                    "common", Path("config/removable/file.txt")
                ),
                core.ContentDeleteDirectory(
                    "common", Path("config/removable")
                ),
            ),
        )
        try:
            self.assertFalse(
                (plan.staging_content / "common/config/removable").exists()
            )
            self.assertEqual(
                [change.action for change in plan.changes].count("deleted"),
                2,
            )
        finally:
            self.discard(plan)

    def test_portable_and_cross_side_conflicts_match_overlay_priority(self) -> None:
        common = self.content / "common/config/Test.toml"
        common.parent.mkdir(parents=True)
        common.write_text("common", encoding="utf-8")
        client = self.content / "client/config/test.toml"
        client.parent.mkdir(parents=True)
        client.write_text("client", encoding="utf-8")
        server = self.content / "server/config/Test.toml"
        server.parent.mkdir(parents=True)
        server.write_text("server", encoding="utf-8")
        plan = core.plan_content_changes("pack:demo", ())
        try:
            kinds = {conflict.kind for conflict in plan.conflicts}
            self.assertIn("common_client_overlap", kinds)
            self.assertIn("common_server_overlap", kinds)
            self.assertIn("client_server_divergence", kinds)
            self.assertTrue(all(conflict.severity == "warning" for conflict in plan.conflicts))
        finally:
            self.discard(plan)

        (self.content / "common/config/Case.txt").write_text("a", encoding="utf-8")
        (self.content / "common/config/case.txt").write_text("b", encoding="utf-8")
        collision = core.plan_content_changes("pack:demo", ())
        try:
            self.assertEqual(collision.state, "failed")
            self.assertTrue(
                any(
                    conflict.kind == "portable_collision"
                    and conflict.severity == "error"
                    for conflict in collision.conflicts
                )
            )
            with self.assertRaises(core.ContentOperationError):
                core.apply_content_changes(collision)
        finally:
            self.discard(collision)

    def test_unicode_portable_collision_and_cross_side_type_conflict_are_fatal(self) -> None:
        first = self.content / "common/config/é.txt"
        second = self.content / "common/config/é.txt"
        first.parent.mkdir(parents=True)
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        unicode_plan = core.plan_content_changes("pack:demo", ())
        try:
            self.assertEqual(unicode_plan.state, "failed")
            self.assertTrue(
                any(
                    conflict.kind == "portable_collision"
                    for conflict in unicode_plan.conflicts
                )
            )
        finally:
            self.discard(unicode_plan)
        first.unlink()
        second.unlink()

        (self.content / "common/config/type").mkdir()
        client = self.content / "client/config/type"
        client.parent.mkdir(parents=True)
        client.write_text("file", encoding="utf-8")
        type_plan = core.plan_content_changes("pack:demo", ())
        try:
            self.assertEqual(type_plan.state, "failed")
            self.assertTrue(
                any(
                    conflict.kind == "cross_side_type_conflict"
                    and conflict.severity == "error"
                    for conflict in type_plan.conflicts
                )
            )
        finally:
            self.discard(type_plan)

    def test_planning_cancellation_deadline_and_unsafe_entries_release_lock(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(core.ContentOperationCancelled):
            core.plan_content_changes("pack:demo", (), cancel_event=cancelled)
        with self.assertRaises(core.ContentOperationDeadlineExceeded):
            core.plan_content_changes(
                "pack:demo", (), deadline=time.monotonic() - 1
            )
        fifo = self.content / "common/config.fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(core.ContentOperationError, "unsafe or invalid"):
            core.plan_content_changes("pack:demo", ())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_large_create_cancel_and_replace_deadline_interrupt_chunk_writes(self) -> None:
        config = self.content / "common/config"
        config.mkdir(parents=True)
        original_apply = content_operations._apply_operation
        original_write = overlay_policy.os.write

        def run_case(*, replace: bool) -> None:
            active = False
            interrupted = False
            cancel_event = threading.Event()
            deadline = time.monotonic() + 100
            existing = config / "value.bin"
            if replace:
                existing.write_bytes(b"old")

            def activate(staging, operation, checkpoint):
                nonlocal active
                active = True
                return original_apply(staging, operation, checkpoint)

            def interrupt_write(descriptor: int, contents) -> int:
                nonlocal interrupted
                written = original_write(descriptor, contents)
                if active and not interrupted:
                    interrupted = True
                    if replace:
                        deadline_expired.set()
                    else:
                        cancel_event.set()
                return written

            deadline_expired = threading.Event()
            original_monotonic = content_operations.time.monotonic

            def monotonic() -> float:
                return deadline + 1 if deadline_expired.is_set() else original_monotonic()

            operation: core.ContentOperation
            if replace:
                operation = core.ContentReplaceFile(
                    "common",
                    Path("config/value.bin"),
                    b"r" * (3 * 1024 * 1024),
                )
                expected_error = core.ContentOperationDeadlineExceeded
            else:
                operation = core.ContentCreateFile(
                    "common",
                    Path("config/value.bin"),
                    b"c" * (3 * 1024 * 1024),
                )
                expected_error = core.ContentOperationCancelled
            before_roots = set((self.state / "transactions").glob("*"))
            with patch.object(
                content_operations,
                "_apply_operation",
                side_effect=activate,
            ), patch.object(
                overlay_policy.os,
                "write",
                side_effect=interrupt_write,
            ), patch.object(
                content_operations.time,
                "monotonic",
                side_effect=monotonic,
            ):
                with self.assertRaises(expected_error):
                    core.plan_content_changes(
                        "pack:demo",
                        (operation,),
                        cancel_event=cancel_event,
                        deadline=deadline,
                    )
            transaction_root = next(
                path
                for path in (self.state / "transactions").glob("*")
                if path not in before_roots
            )
            staged = transaction_root / "staging-content/common/config/value.bin"
            if replace:
                self.assertEqual(staged.read_bytes(), b"old")
                self.assertEqual(existing.read_bytes(), b"old")
            else:
                self.assertFalse(staged.exists())
                self.assertFalse(existing.exists())
            self.assertFalse(
                any(staged.parent.glob(".value.bin.huroshiki-tmp-*"))
            )
            self.assertFalse(packctl.project_lock_is_active("pack:demo"))

        run_case(replace=False)
        run_case(replace=True)

    def test_replace_cancel_after_file_exchange_restores_staging_entry(self) -> None:
        target = self.content / "common/config/value.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        cancel_event = threading.Event()
        original_renameat2 = overlay_policy._renameat2
        exchanged = False

        def cancel_after_exchange(*args):
            nonlocal exchanged
            result = original_renameat2(*args)
            if args[4] == overlay_policy._RENAME_EXCHANGE and not exchanged:
                exchanged = True
                cancel_event.set()
            return result

        before_roots = set((self.state / "transactions").glob("*"))
        with patch.object(
            overlay_policy,
            "_renameat2",
            side_effect=cancel_after_exchange,
        ):
            with self.assertRaises(core.ContentOperationCancelled):
                core.plan_content_changes(
                    "pack:demo",
                    (
                        core.ContentReplaceFile(
                            "common", Path("config/value.bin"), b"new"
                        ),
                    ),
                    cancel_event=cancel_event,
                )
        transaction_root = next(
            path
            for path in (self.state / "transactions").glob("*")
            if path not in before_roots
        )
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(
            (
                transaction_root
                / "staging-content/common/config/value.bin"
            ).read_bytes(),
            b"old",
        )
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_create_cancel_after_file_publication_restores_missing_entry(self) -> None:
        parent = self.content / "common/config"
        parent.mkdir(parents=True)
        cancel_event = threading.Event()
        original_renameat2 = overlay_policy._renameat2
        published = False

        def cancel_after_publish(*args):
            nonlocal published
            result = original_renameat2(*args)
            if args[4] == overlay_policy._RENAME_NOREPLACE and not published:
                published = True
                cancel_event.set()
            return result

        before_roots = set((self.state / "transactions").glob("*"))
        with patch.object(
            overlay_policy,
            "_renameat2",
            side_effect=cancel_after_publish,
        ):
            with self.assertRaises(core.ContentOperationCancelled):
                core.plan_content_changes(
                    "pack:demo",
                    (
                        core.ContentCreateFile(
                            "common", Path("config/new.bin"), b"new"
                        ),
                    ),
                    cancel_event=cancel_event,
                )
        transaction_root = next(
            path
            for path in (self.state / "transactions").glob("*")
            if path not in before_roots
        )
        self.assertFalse((parent / "new.bin").exists())
        self.assertFalse(
            (
                transaction_root / "staging-content/common/config/new.bin"
            ).exists()
        )
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_plan_holds_exclusive_project_lock_for_its_lifetime(self) -> None:
        plan = core.plan_content_changes("pack:demo", ())
        try:
            with self.assertRaisesRegex(
                core.ContentOperationError,
                "is locked",
            ):
                core.plan_content_changes("pack:demo", ())
            with self.assertRaisesRegex(core.HuroshikiError, "is locked"):
                core.PackTransaction.create("pack:demo")
        finally:
            self.discard(plan)
        replacement = core.plan_content_changes("pack:demo", ())
        self.discard(replacement)

    def test_single_file_compatibility_remains_lightweight(self) -> None:
        core.create_template("pack:demo", "common", "config/demo.txt")
        core.write_template_text(
            "pack:demo", "common", "config/demo.txt", "hello"
        )
        self.assertEqual(
            core.read_template_text("pack:demo", "common", "config/demo.txt"),
            "hello",
        )
        listed = core.list_templates("pack:demo")
        self.assertEqual(
            [(entry.target, entry.relative_path) for entry in listed],
            [("common", Path("config/demo.txt"))],
        )
        core.delete_template("pack:demo", "common", "config/demo.txt")
        self.assertFalse(self.state.joinpath("transactions").exists())

    def test_content_tree_copy_detects_source_and_destination_replacement(self) -> None:
        source_file = self.content / "common/config/source.txt"
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"source")

        staging = self.root / "staging-source-race"
        original_read = overlay_policy.os.read
        replaced = False

        def replace_source(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            data = original_read(descriptor, size)
            if data and not replaced:
                replaced = True
                source_file.rename(source_file.with_suffix(".old"))
                source_file.write_bytes(b"external")
            return data

        with patch.object(overlay_policy.os, "read", side_effect=replace_source):
            with self.assertRaisesRegex(
                overlay_policy.OverlayPolicyError,
                "source changed",
            ):
                overlay_policy.copy_content_tree(self.content, staging)
        self.assertEqual(source_file.read_bytes(), b"external")

        source_file.write_bytes(b"source")
        destination = self.root / "staging-destination-race"
        displaced = self.root / "displaced-staging"
        original_write = overlay_policy.os.write
        destination_replaced = False

        def replace_destination(descriptor: int, contents: bytes) -> int:
            nonlocal destination_replaced
            written = original_write(descriptor, contents)
            if written and not destination_replaced:
                destination_replaced = True
                destination.rename(displaced)
                destination.mkdir()
                (destination / "external.txt").write_bytes(b"external")
            return written

        with patch.object(overlay_policy.os, "write", side_effect=replace_destination):
            with self.assertRaisesRegex(
                overlay_policy.OverlayPolicyError,
                "destination changed|staging root changed",
            ):
                overlay_policy.copy_content_tree(self.content, destination)
        self.assertEqual((destination / "external.txt").read_bytes(), b"external")


if __name__ == "__main__":
    unittest.main()
