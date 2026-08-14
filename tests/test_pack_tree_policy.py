from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pack_tree_policy import (
    PackTreePolicyError,
    copy_pack_tree_snapshot,
    scan_pack_migration_source,
)


class PackTreePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source-pack"
        (self.source / "source" / "mods").mkdir(parents=True)
        (self.source / "content" / "common" / "empty").mkdir(parents=True)
        (self.source / "pack.yaml").write_text("id: source\n", encoding="utf-8")
        binary = self.source / "source" / "mods" / "binary.dat"
        binary.write_bytes(b"\x00\xffpayload")
        binary.chmod(0o751)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def checkpoint() -> None:
        return None

    def test_scan_and_copy_preserve_content_modes_and_empty_directories(self) -> None:
        scan = scan_pack_migration_source(self.source, checkpoint=self.checkpoint)
        self.assertFalse([entry for entry in scan.entries if entry.errors])

        destination = self.root / "destination"
        result = copy_pack_tree_snapshot(
            scan,
            destination,
            include=(Path("pack.yaml"), Path("source"), Path("content")),
            checkpoint=self.checkpoint,
        )

        copied = destination / "source" / "mods" / "binary.dat"
        self.assertEqual(copied.read_bytes(), b"\x00\xffpayload")
        self.assertEqual(copied.stat().st_mode & 0o777, 0o751)
        self.assertTrue((destination / "content" / "common" / "empty").is_dir())
        self.assertEqual(result.copied_files, 2)
        self.assertNotEqual(scan.snapshot_digest, result.scan.snapshot_digest)

    def test_rejects_symlink_hardlink_fifo_and_case_collision(self) -> None:
        cases = ("symlink", "hardlink", "fifo", "collision")
        for case in cases:
            with self.subTest(case=case):
                path = self.source / "unsafe"
                if case == "symlink":
                    path.symlink_to("pack.yaml")
                elif case == "hardlink":
                    os.link(self.source / "pack.yaml", path)
                elif case == "fifo":
                    os.mkfifo(path)
                else:
                    (self.source / "Case").write_text("a", encoding="utf-8")
                    (self.source / "case").write_text("b", encoding="utf-8")
                scan = scan_pack_migration_source(self.source, checkpoint=self.checkpoint)
                self.assertTrue(any(entry.errors for entry in scan.entries))
                with self.assertRaises(PackTreePolicyError):
                    copy_pack_tree_snapshot(
                        scan,
                        self.root / f"copy-{case}",
                        include=(Path("pack.yaml"),),
                        checkpoint=self.checkpoint,
                    )
                if case == "symlink":
                    path.unlink()
                elif case == "hardlink":
                    path.unlink()
                elif case == "fifo":
                    path.unlink()
                else:
                    (self.source / "Case").unlink()
                    (self.source / "case").unlink()

    def test_cancellation_during_hashing_stops_scan(self) -> None:
        (self.source / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        cancelled = threading.Event()
        calls = 0

        def checkpoint() -> None:
            nonlocal calls
            calls += 1
            if calls > 8:
                cancelled.set()
            if cancelled.is_set():
                raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            scan_pack_migration_source(self.source, checkpoint=checkpoint)

    def test_optional_scan_entry_and_byte_limits_fail_before_excess_hashing(self) -> None:
        with self.assertRaisesRegex(PackTreePolicyError, "entry scan limit"):
            scan_pack_migration_source(
                self.source,
                checkpoint=self.checkpoint,
                max_entries=1,
            )
        with self.assertRaisesRegex(PackTreePolicyError, "byte scan limit"):
            scan_pack_migration_source(
                self.source,
                checkpoint=self.checkpoint,
                max_total_file_bytes=1,
            )

    def test_byte_limit_is_enforced_against_stream_growth(self) -> None:
        isolated = self.root / "bounded-source"
        isolated.mkdir()
        (isolated / "one.bin").write_bytes(b"x")
        with patch("pack_tree_policy.os.read", return_value=b"xx"):
            with self.assertRaisesRegex(PackTreePolicyError, "byte scan limit"):
                scan_pack_migration_source(
                    isolated,
                    checkpoint=self.checkpoint,
                    max_total_file_bytes=1,
                )

    def test_source_mutation_after_scan_is_rejected(self) -> None:
        scan = scan_pack_migration_source(self.source, checkpoint=self.checkpoint)
        (self.source / "pack.yaml").write_text("id: changed\n", encoding="utf-8")
        with self.assertRaisesRegex(PackTreePolicyError, "changed"):
            copy_pack_tree_snapshot(
                scan,
                self.root / "copy-stale",
                include=(Path("pack.yaml"),),
                checkpoint=self.checkpoint,
            )


if __name__ == "__main__":
    unittest.main()
