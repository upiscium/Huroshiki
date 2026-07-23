from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki
import packctl


class StateManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.state = self.root / ".huroshiki"
        self.packs.mkdir()
        self.templates.mkdir()
        self.stack = ExitStack()
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
            self.stack.enter_context(patch.object(packctl, name, value))

    def tearDown(self) -> None:
        self.stack.close()
        self.temp.cleanup()

    def create_pack(self, project_id: str = "demo") -> Path:
        root = self.packs / project_id
        (root / "source").mkdir(parents=True)
        (root / "pack.yaml").write_text(f"id: {project_id}\n", encoding="utf-8")
        return root

    def test_delete_restore_preserves_local_and_ignored_files(self) -> None:
        root = self.create_pack()
        (root / "pack.local.yaml").write_text("secret: local\n", encoding="utf-8")
        (root / "source" / "working.tmp").write_bytes(b"uncommitted")

        entry = packctl.trash_project("pack", "demo")

        self.assertFalse(root.exists())
        self.assertEqual(len(packctl.list_trash()), 1)
        self.assertEqual((entry.path / "pack.local.yaml").read_text(), "secret: local\n")
        self.assertEqual((entry.path / "source" / "working.tmp").read_bytes(), b"uncommitted")

        restored = packctl.restore_trash(entry.name)
        self.assertEqual(restored, root)
        self.assertEqual((root / "pack.local.yaml").read_text(), "secret: local\n")
        self.assertEqual((root / "source" / "working.tmp").read_bytes(), b"uncommitted")
        self.assertEqual(packctl.list_trash(), [])

    def test_restore_detects_project_conflict(self) -> None:
        self.create_pack()
        entry = packctl.trash_project("pack", "demo")
        self.create_pack()

        with self.assertRaisesRegex(packctl.ConfigError, "already exists"):
            packctl.restore_trash(entry.name)

        self.assertTrue(entry.path.is_dir())

    def test_explicit_purge_reports_items_and_bytes(self) -> None:
        root = self.create_pack()
        (root / "payload.bin").write_bytes(b"123456789")
        entry = packctl.trash_project("pack", "demo")

        count, total = packctl.purge_trash(name=entry.name)

        self.assertEqual(count, 1)
        self.assertGreaterEqual(total, 9)
        self.assertFalse(entry.path.exists())

    def test_purge_without_selector_is_rejected(self) -> None:
        with self.assertRaisesRegex(packctl.ConfigError, "requires"):
            packctl.purge_trash()

    def test_cleanup_dry_run_and_project_filter_report_bytes(self) -> None:
        first = self.state / "logs" / "pack-demo" / "old"
        second = self.state / "logs" / "pack-other" / "old"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / "session.txt").write_bytes(b"demo-bytes")
        (second / "session.txt").write_bytes(b"other-bytes")
        old = 1_600_000_000
        os.utime(first, (old, old))
        os.utime(second, (old, old))

        report = packctl.clean_state(
            older_than_days=0,
            project_key="pack:demo",
            now=old + 100,
        )

        self.assertTrue(report.dry_run)
        self.assertEqual(report.removed_count, 0)
        self.assertEqual(len(report.selected), 1)
        self.assertEqual(report.selected[0].project_key, "pack:demo")
        self.assertGreaterEqual(report.selected[0].bytes, len(b"demo-bytes"))
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_cleanup_keep_filter_retains_newest_item(self) -> None:
        project = self.state / "logs" / "pack-demo"
        old = project / "old"
        new = project / "new"
        old.mkdir(parents=True)
        new.mkdir()
        os.utime(old, (100, 100))
        os.utime(new, (200, 200))

        report = packctl.clean_state(older_than_days=0, keep=1, now=300)

        self.assertEqual([item.path for item in report.selected], [old])

    def test_apply_removes_inactive_state_but_protects_active_lock(self) -> None:
        transactions = self.state / "transactions"
        inactive = transactions / "pack-other-inactive"
        active = transactions / "pack-demo-active"
        inactive.mkdir(parents=True)
        active.mkdir()
        (inactive / ".completed").touch()
        project_lock = packctl.ProjectLock("pack:demo", "transaction").acquire()
        lock_path = project_lock.path
        global_lock = self.state / "manager.lock"
        global_lock.touch()
        try:
            report = packctl.clean_state(apply=True, older_than_days=0)
        finally:
            project_lock.release()

        self.assertFalse(inactive.exists())
        self.assertTrue(active.exists())
        self.assertTrue(lock_path.exists())
        self.assertTrue(global_lock.exists())
        self.assertEqual(report.removed_count, 1)
        active_items = [item for item in report.items if item.path == active]
        self.assertEqual(active_items[0].category, "active_transaction")
        self.assertTrue(active_items[0].active)

    def test_symlinked_state_root_cannot_write_or_remove_external_files(self) -> None:
        external = self.root / "external"
        external.mkdir()
        marker = external / "keep"
        marker.write_text("safe", encoding="utf-8")
        self.state.symlink_to(external, target_is_directory=True)
        self.create_pack()

        with self.assertRaisesRegex(packctl.ConfigError, "symlink"):
            packctl.trash_project("pack", "demo")
        with self.assertRaisesRegex(packctl.ConfigError, "symlink"):
            packctl.clean_state(apply=True, older_than_days=0)

        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")
        self.assertTrue((self.packs / "demo").is_dir())

    def test_apply_aborts_when_previewed_cleanup_candidates_change(self) -> None:
        old = self.state / "logs" / "pack-demo" / "old"
        old.mkdir(parents=True)
        (old / "session.txt").write_text("before", encoding="utf-8")
        preview = packctl.clean_state(older_than_days=0, now=2_000_000_000)
        (old / "session.txt").write_text("after and larger", encoding="utf-8")

        with self.assertRaisesRegex(packctl.ConfigError, "changed after preview"):
            packctl.clean_state(
                apply=True,
                older_than_days=0,
                now=2_000_000_000,
                expected=preview.selected,
            )

        self.assertTrue(old.exists())

    def test_tui_applies_exact_previewed_cleanup_selection(self) -> None:
        selected = (
            packctl.StateItem("log", self.state / "logs" / "old", None, 1, 2),
        )
        result = packctl.StateCleanupReport(selected, selected, 1, 2, False)

        class App:
            def notify(self, *args, **kwargs) -> None:
                pass

        class Screen:
            app = App()

            def reload(self) -> None:
                pass

        with patch.object(huroshiki.core, "clean_state", return_value=result) as clean:
            huroshiki.StateScreen.cleanup_confirmed(Screen(), selected, True)

        clean.assert_called_once_with(apply=True, expected=selected)


if __name__ == "__main__":
    unittest.main()
