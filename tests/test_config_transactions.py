from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import packctl


TEMPLATE_YAML = b"""id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods: []
"""

PACK_YAML = b"""id: demo
display_name: Demo
enabled: true
distribution:
  rsync_target: host:/packs/demo
minecraft_server:
  ssh_host: dockge
  stack_dir: /srv/demo
  service: minecraft
"""


class ConfigTransactionTest(unittest.TestCase):
    def test_exchange_state_classification_uses_complete_snapshots(self) -> None:
        expected = packctl.ConfigFileSnapshot(
            "settings.yaml", True, 0o600, 1, 10, b"old", "old"
        )
        staged = packctl.ConfigFileSnapshot(
            ".settings.tmp", True, 0o600, 1, 20, b"new", "new"
        )
        target_changed = packctl.ConfigFileSnapshot(
            "settings.yaml", True, 0o600, 1, 20, b"external", "external"
        )
        temporary_changed = packctl.ConfigFileSnapshot(
            ".settings.tmp", True, 0o600, 1, 10, b"external", "external"
        )

        cases = (
            (staged, expected, "intact"),
            (target_changed, expected, "target_changed"),
            (staged, temporary_changed, "temporary_changed"),
            (target_changed, temporary_changed, "both_changed"),
        )
        for target, temporary_snapshot, expected_result in cases:
            with self.subTest(expected_result=expected_result):
                self.assertEqual(
                    packctl.classify_exchange_state(
                        target=target,
                        temporary=temporary_snapshot,
                        staged=staged,
                        expected=expected,
                    ),
                    expected_result,
                )

    def test_snapshot_bytes_are_parsed_without_reopening_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "settings.yaml"
            path.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, path.name)
                path.write_text("value: replacement\n", encoding="utf-8")

                self.assertEqual(
                    packctl.parse_yaml_snapshot(snapshot),
                    {"value": "original"},
                )

    def test_snapshot_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / "settings.yaml")
            with packctl.open_config_directory(root) as directory:
                with self.assertRaisesRegex(packctl.ConfigError, "regular file"):
                    packctl.read_config_snapshot(directory, "settings.yaml")

    def test_snapshot_rejects_symlink_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real.yaml").write_text("value: true\n", encoding="utf-8")
            (root / "linked.yaml").symlink_to(root / "real.yaml")
            (root / "directory.yaml").mkdir()
            with packctl.open_config_directory(root) as directory:
                with self.assertRaisesRegex(packctl.ConfigError, "symlink"):
                    packctl.read_config_snapshot(directory, "linked.yaml")
                with self.assertRaisesRegex(packctl.ConfigError, "regular file"):
                    packctl.read_config_snapshot(directory, "directory.yaml")

    def test_replaced_project_directory_rejects_pinned_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "project"
            root.mkdir()
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, "settings.yaml")
                pinned = parent / "pinned"
                root.rename(pinned)
                root.mkdir()

                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "managed project directory is no longer current",
                ):
                    packctl._write_yaml_atomic(
                        directory,
                        {"value": "new"},
                        expected_snapshot=snapshot,
                        guard_snapshots=(snapshot,),
                    )

            self.assertFalse((pinned / "settings.yaml").exists())
            self.assertFalse((root / "settings.yaml").exists())

    def test_replaced_collection_directory_rejects_pinned_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            collection = repository / "packs"
            project = collection / "demo"
            project.mkdir(parents=True)
            with packctl.open_config_directory(project) as directory:
                snapshot = packctl.read_config_snapshot(directory, "settings.yaml")
                moved = repository / "packs-old"
                collection.rename(moved)
                (collection / "demo").mkdir(parents=True)

                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "managed project directory is no longer current",
                ):
                    packctl._write_yaml_atomic(
                        directory,
                        {"value": "new"},
                        expected_snapshot=snapshot,
                        guard_snapshots=(snapshot,),
                    )

            self.assertFalse((moved / "demo/settings.yaml").exists())
            self.assertFalse((collection / "demo/settings.yaml").exists())

    def test_directory_replacement_after_publication_is_detected_and_rolled_back(self) -> None:
        for target_exists in (False, True):
            with self.subTest(target_exists=target_exists), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary)
                root = parent / "project"
                root.mkdir()
                target = root / "settings.yaml"
                if target_exists:
                    target.write_text("value: original\n", encoding="utf-8")
                with packctl.open_config_directory(root) as directory:
                    snapshot = packctl.read_config_snapshot(directory, target.name)
                    renameat2 = packctl.renameat2
                    replaced = False

                    def race(old_fd, old_name, new_fd, new_name, flags):
                        nonlocal replaced
                        result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                        if not replaced:
                            replaced = True
                            root.rename(parent / "moved-project")
                            root.mkdir()
                        return result

                    with patch.object(packctl, "renameat2", side_effect=race):
                        with self.assertRaisesRegex(
                            packctl.ConfigError,
                            "managed project directory is no longer current",
                        ):
                            packctl._write_yaml_atomic(
                                directory,
                                {"value": "new"},
                                expected_snapshot=snapshot,
                                guard_snapshots=(snapshot,),
                            )

                moved_target = parent / "moved-project/settings.yaml"
                if target_exists:
                    self.assertEqual(
                        moved_target.read_text(encoding="utf-8"),
                        "value: original\n",
                    )
                else:
                    self.assertFalse(moved_target.exists())
                self.assertFalse((root / "settings.yaml").exists())

    def test_missing_target_noreplace_preserves_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2

                def race(old_fd, old_name, new_fd, new_name, flags):
                    if flags == packctl.RENAME_NOREPLACE:
                        target.write_text("external: true\n", encoding="utf-8")
                    return renameat2(old_fd, old_name, new_fd, new_name, flags)

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(packctl.ConfigError, "changed"):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "external: true\n")

    def test_missing_target_companion_race_rolls_back_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            committed = root / "pack.yaml"
            committed.write_text("id: demo\n", encoding="utf-8")
            local = root / "pack.local.yaml"
            with packctl.open_config_directory(root) as directory:
                committed_snapshot = packctl.read_config_snapshot(directory, committed.name)
                local_snapshot = packctl.read_config_snapshot(directory, local.name)
                renameat2 = packctl.renameat2

                def race(old_fd, old_name, new_fd, new_name, flags):
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if flags == packctl.RENAME_NOREPLACE and new_name == local.name:
                        committed.write_text("id: external\n", encoding="utf-8")
                    return result

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(packctl.ConfigError, "changed"):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=local_snapshot,
                            guard_snapshots=(committed_snapshot, local_snapshot),
                        )

            self.assertFalse(local.exists())
            recovery = list(root.glob(".pack.local.yaml.huroshiki-*.staged"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), "value: new\n")
            self.assertEqual(committed.read_text(encoding="utf-8"), "id: external\n")

    def test_exchange_temporary_change_preserves_staged_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                raced = False

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal raced
                    if flags == packctl.RENAME_EXCHANGE and not raced:
                        raced = True
                        target.write_text("value: external\n", encoding="utf-8")
                    return renameat2(old_fd, old_name, new_fd, new_name, flags)

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "staged configuration remains at the canonical path",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: new\n")
            temporary_recovery = list(root.glob(".settings.yaml.huroshiki-*.tmp"))
            self.assertEqual(len(temporary_recovery), 1)
            self.assertEqual(
                temporary_recovery[0].read_text(encoding="utf-8"),
                "value: external\n",
            )
            recovery = list(root.glob(".settings.yaml.huroshiki-*.staged"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), "value: new\n")

    def test_exchange_rollback_failure_keeps_external_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                check_snapshots = packctl._check_config_snapshots
                checks = 0
                failed_restore = False

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal failed_restore
                    if (
                        flags == packctl.RENAME_NOREPLACE
                        and old_name == target.name
                        and new_name.endswith(".rollback")
                    ):
                        failed_restore = True
                        raise OSError("rollback unavailable")
                    return renameat2(old_fd, old_name, new_fd, new_name, flags)

                def fail_companion(directory_arg, snapshots):
                    nonlocal checks
                    checks += 1
                    if checks == 2:
                        raise packctl.ConfigError("companion changed")
                    return check_snapshots(directory_arg, snapshots)

                with patch.object(
                    packctl,
                    "_check_config_snapshots",
                    side_effect=fail_companion,
                ), patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "original and staged recovery artifacts remain",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertTrue(failed_restore)
            recovery = [
                path
                for path in root.iterdir()
                if path.name.startswith(".settings.yaml.huroshiki-")
            ]
            self.assertGreaterEqual(len(recovery), 2)
            self.assertIn("value: original\n", [path.read_text() for path in recovery])
            self.assertIn("value: new\n", [path.read_text() for path in recovery])

    def test_interrupt_after_exchange_rolls_back_without_deleting_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                exchanges = 0

                def interrupt(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal exchanges
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if flags == packctl.RENAME_EXCHANGE:
                        exchanges += 1
                        if exchanges == 1:
                            raise KeyboardInterrupt
                    return result

                with patch.object(packctl, "renameat2", side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: original\n")
            recovery = list(root.glob(".settings.yaml.huroshiki-*.staged"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), "value: new\n")

    def test_post_exchange_external_replacement_remains_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                replaced = False

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal replaced
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if flags == packctl.RENAME_EXCHANGE and not replaced:
                        replaced = True
                        external = root / "external.yaml"
                        external.write_text("value: external\n", encoding="utf-8")
                        os.replace(external, target)
                    return result

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "external configuration remains at the canonical path",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: external\n")
            recovery_contents = {
                path.read_text(encoding="utf-8")
                for path in root.iterdir()
                if path.name.startswith(".settings.yaml.huroshiki-")
            }
            self.assertIn("value: original\n", recovery_contents)
            self.assertIn("value: new\n", recovery_contents)

    def test_post_exchange_in_place_change_remains_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                changed = False

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal changed
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if flags == packctl.RENAME_EXCHANGE and not changed:
                        changed = True
                        target.write_text("value: external\n", encoding="utf-8")
                    return result

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "external configuration remains at the canonical path",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: external\n")
            recovery_contents = {
                path.read_text(encoding="utf-8")
                for path in root.iterdir()
                if path.name.startswith(".settings.yaml.huroshiki-")
            }
            self.assertIn("value: original\n", recovery_contents)
            self.assertIn("value: new\n", recovery_contents)

    def test_post_exchange_temporary_change_preserves_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            temporary_name = ""
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal temporary_name
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if flags == packctl.RENAME_EXCHANGE and not temporary_name:
                        temporary_name = old_name
                        (root / old_name).write_text(
                            "value: external temporary\n", encoding="utf-8"
                        )
                    return result

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(packctl.ConfigError, "canonical path"):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: new\n")
            self.assertEqual(
                (root / temporary_name).read_text(encoding="utf-8"),
                "value: external temporary\n",
            )

    def test_post_exchange_both_changes_preserve_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            temporary_name = ""
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal temporary_name
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if flags == packctl.RENAME_EXCHANGE and not temporary_name:
                        temporary_name = old_name
                        target.write_text("value: external target\n", encoding="utf-8")
                        (root / old_name).write_text(
                            "value: external temporary\n", encoding="utf-8"
                        )
                    return result

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "external configuration remains at the canonical path",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(
                target.read_text(encoding="utf-8"), "value: external target\n"
            )
            self.assertEqual(
                (root / temporary_name).read_text(encoding="utf-8"),
                "value: external temporary\n",
            )

    def test_external_replacement_after_original_restore_keeps_original_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                check_snapshots = packctl._check_config_snapshots
                checks = 0

                def fail_companion(directory_arg, snapshots):
                    nonlocal checks
                    checks += 1
                    if checks == 2:
                        raise packctl.ConfigError("companion changed")
                    return check_snapshots(directory_arg, snapshots)

                def replace_after_restore(old_fd, old_name, new_fd, new_name, flags):
                    result = renameat2(old_fd, old_name, new_fd, new_name, flags)
                    if (
                        flags == packctl.RENAME_NOREPLACE
                        and old_name.endswith(".tmp")
                        and new_name == target.name
                    ):
                        external = root / "external.yaml"
                        external.write_text("value: external\n", encoding="utf-8")
                        os.replace(external, target)
                    return result

                with patch.object(
                    packctl,
                    "_check_config_snapshots",
                    side_effect=fail_companion,
                ), patch.object(
                    packctl,
                    "renameat2",
                    side_effect=replace_after_restore,
                ):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "external configuration remains at the canonical path",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: external\n")
            recovery_contents = {
                path.read_text(encoding="utf-8")
                for path in root.iterdir()
                if path.name.startswith(".settings.yaml.huroshiki-")
            }
            self.assertIn("value: original\n", recovery_contents)
            self.assertIn("value: new\n", recovery_contents)

    def test_external_replacement_during_quarantine_is_relinked_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                renameat2 = packctl.renameat2
                check_snapshots = packctl._check_config_snapshots
                checks = 0
                replaced = False

                def fail_companion(directory_arg, snapshots):
                    nonlocal checks
                    checks += 1
                    if checks == 2:
                        raise packctl.ConfigError("companion changed")
                    return check_snapshots(directory_arg, snapshots)

                def replace_before_quarantine(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal replaced
                    if (
                        flags == packctl.RENAME_NOREPLACE
                        and old_name == target.name
                        and new_name.endswith(".rollback")
                        and not replaced
                    ):
                        replaced = True
                        external = root / "external.yaml"
                        external.write_text("value: external\n", encoding="utf-8")
                        os.replace(external, target)
                    return renameat2(old_fd, old_name, new_fd, new_name, flags)

                with patch.object(
                    packctl,
                    "_check_config_snapshots",
                    side_effect=fail_companion,
                ), patch.object(
                    packctl,
                    "renameat2",
                    side_effect=replace_before_quarantine,
                ):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "external configuration remains at the canonical path",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertTrue(replaced)
            self.assertEqual(target.read_text(encoding="utf-8"), "value: external\n")
            recovery_contents = {
                path.read_text(encoding="utf-8")
                for path in root.iterdir()
                if path.name.startswith(".settings.yaml.huroshiki-")
            }
            self.assertIn("value: original\n", recovery_contents)
            self.assertIn("value: new\n", recovery_contents)

    def test_existing_target_mode_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            target.chmod(0o640)
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                packctl._write_yaml_atomic(
                    directory,
                    {"value": "new"},
                    expected_snapshot=snapshot,
                    guard_snapshots=(snapshot,),
                )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_temporary_tampering_is_rejected_before_commit(self) -> None:
        for target_exists in (False, True):
            with self.subTest(target_exists=target_exists), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "settings.yaml"
                if target_exists:
                    target.write_text("value: original\n", encoding="utf-8")
                with packctl.open_config_directory(root) as directory:
                    snapshot = packctl.read_config_snapshot(directory, target.name)
                    renameat2 = packctl.renameat2
                    tampered = False

                    def race(old_fd, old_name, new_fd, new_name, flags):
                        nonlocal tampered
                        if not tampered:
                            tampered = True
                            (root / old_name).write_text(
                                "value: tampered\n",
                                encoding="utf-8",
                            )
                        return renameat2(old_fd, old_name, new_fd, new_name, flags)

                    with patch.object(packctl, "renameat2", side_effect=race):
                        with self.assertRaisesRegex(
                            packctl.ConfigError,
                            "temporary configuration changed",
                        ):
                            packctl._write_yaml_atomic(
                                directory,
                                {"value": "new"},
                                expected_snapshot=snapshot,
                                guard_snapshots=(snapshot,),
                            )

                if target_exists:
                    self.assertEqual(
                        target.read_text(encoding="utf-8"),
                        "value: tampered\n",
                    )
                else:
                    self.assertFalse(target.exists())

    def test_prospective_pack_rejects_whitespace_deployment_values(self) -> None:
        import yaml

        for field, value in (
            ("rsync_target", " host:/packs/demo"),
            ("ssh_host", " dockge"),
            ("stack_dir", " /srv/demo"),
            ("service", " minecraft"),
        ):
            candidate = yaml.safe_load(PACK_YAML)
            if field == "rsync_target":
                candidate["distribution"][field] = value
            else:
                candidate["minecraft_server"][field] = value
            with self.subTest(field=field), self.assertRaises(packctl.ConfigError):
                packctl.prospective_pack_config("demo", candidate, {})

    def test_unsupported_atomic_rename_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                with patch.object(
                    packctl,
                    "renameat2",
                    side_effect=OSError("renameat2 unavailable"),
                ):
                    with self.assertRaisesRegex(OSError, "renameat2 unavailable"):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: original\n")
            self.assertEqual(list(root.glob(".settings.yaml.huroshiki-*.tmp")), [])

    def test_post_commit_cleanup_and_warning_failures_do_not_fail_transaction(self) -> None:
        class BrokenStderr:
            def write(self, _: str) -> int:
                raise BrokenPipeError("stderr closed")

            def flush(self) -> None:
                raise BrokenPipeError("stderr closed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "settings.yaml"
            target.write_text("value: original\n", encoding="utf-8")
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, target.name)
                with patch.object(
                    packctl.os,
                    "unlink",
                    side_effect=OSError("cleanup unavailable"),
                ), patch.object(packctl.sys, "stderr", BrokenStderr()):
                    packctl._write_yaml_atomic(
                        directory,
                        {"value": "new"},
                        expected_snapshot=snapshot,
                        guard_snapshots=(snapshot,),
                    )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: new\n")

    def test_template_invalid_local_config_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            manifest = template / "template.yaml"
            manifest.write_bytes(TEMPLATE_YAML)
            (template / "template.local.yaml").write_text(
                "mods: []\n",
                encoding="utf-8",
            )
            with patch.object(packctl, "ROOT", root), patch.object(
                packctl, "TEMPLATES", templates
            ):
                with self.assertRaises(packctl.ConfigError):
                    packctl.cmd_set_template_loader_version(
                        type(
                            "Args",
                            (),
                            {"template": "base", "loader_version": "21.1.235"},
                        )()
                    )

            self.assertEqual(manifest.read_bytes(), TEMPLATE_YAML)

    def test_template_local_change_after_validation_rolls_back_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            manifest = template / "template.yaml"
            manifest.write_bytes(TEMPLATE_YAML)
            local = template / "template.local.yaml"
            local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
            renameat2 = packctl.renameat2
            raced = False

            def race(old_fd, old_name, new_fd, new_name, flags):
                nonlocal raced
                if flags == packctl.RENAME_EXCHANGE and not raced:
                    raced = True
                    local.write_text("url_max_jar_size_bytes: 2048\n", encoding="utf-8")
                return renameat2(old_fd, old_name, new_fd, new_name, flags)

            with patch.object(packctl, "ROOT", root), patch.object(
                packctl, "TEMPLATES", templates
            ), patch.object(packctl, "renameat2", side_effect=race):
                with self.assertRaisesRegex(packctl.ConfigError, "changed"):
                    packctl.cmd_set_template_loader_version(
                        type(
                            "Args",
                            (),
                            {"template": "base", "loader_version": "21.1.235"},
                        )()
                    )

            self.assertEqual(manifest.read_bytes(), TEMPLATE_YAML)
            self.assertEqual(
                local.read_text(encoding="utf-8"),
                "url_max_jar_size_bytes: 2048\n",
            )

    def test_pack_committed_change_rejects_local_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packs = root / "packs"
            pack = packs / "demo"
            pack.mkdir(parents=True)
            manifest = pack / "pack.yaml"
            manifest.write_bytes(PACK_YAML)
            local = pack / "pack.local.yaml"
            local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
            renameat2 = packctl.renameat2
            raced = False

            def race(old_fd, old_name, new_fd, new_name, flags):
                nonlocal raced
                if flags == packctl.RENAME_EXCHANGE and not raced:
                    raced = True
                    manifest.write_bytes(PACK_YAML + b"external: true\n")
                return renameat2(old_fd, old_name, new_fd, new_name, flags)

            with patch.object(packctl, "ROOT", root), patch.object(
                packctl, "PACKS", packs
            ), patch.object(packctl, "renameat2", side_effect=race):
                with self.assertRaisesRegex(packctl.ConfigError, "changed"):
                    packctl.cmd_set_url_policy(
                        type(
                            "Args",
                            (),
                            {
                                "kind": "pack",
                                "project": "demo",
                                "max_size": 2048,
                                "allow_private_networks": None,
                            },
                        )()
                    )

            self.assertEqual(
                local.read_text(encoding="utf-8"),
                "url_max_jar_size_bytes: 1024\n",
            )
            self.assertEqual(manifest.read_bytes(), PACK_YAML + b"external: true\n")


if __name__ == "__main__":
    unittest.main()
