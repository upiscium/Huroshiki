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

    def test_pinned_parent_prevents_replacement_directory_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "project"
            root.mkdir()
            with packctl.open_config_directory(root) as directory:
                snapshot = packctl.read_config_snapshot(directory, "settings.yaml")
                pinned = parent / "pinned"
                root.rename(pinned)
                root.mkdir()

                packctl._write_yaml_atomic(
                    directory,
                    {"value": "new"},
                    expected_snapshot=snapshot,
                    guard_snapshots=(snapshot,),
                )

            self.assertEqual(
                (pinned / "settings.yaml").read_text(encoding="utf-8"),
                "value: new\n",
            )
            self.assertFalse((root / "settings.yaml").exists())
            self.assertEqual(
                stat.S_IMODE((pinned / "settings.yaml").stat().st_mode),
                0o600,
            )

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
            recovery = list(root.glob(".pack.local.yaml.huroshiki-*.tmp"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), "value: new\n")
            self.assertEqual(committed.read_text(encoding="utf-8"), "id: external\n")

    def test_exchange_mismatch_rolls_back_external_change(self) -> None:
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
                        "staged configuration retained",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            self.assertEqual(target.read_text(encoding="utf-8"), "value: external\n")
            recovery = list(root.glob(".settings.yaml.huroshiki-*.tmp"))
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
                exchanges = 0

                def race(old_fd, old_name, new_fd, new_name, flags):
                    nonlocal exchanges
                    if flags == packctl.RENAME_EXCHANGE:
                        exchanges += 1
                        if exchanges == 1:
                            target.write_text("value: external\n", encoding="utf-8")
                        else:
                            raise OSError("rollback unavailable")
                    return renameat2(old_fd, old_name, new_fd, new_name, flags)

                with patch.object(packctl, "renameat2", side_effect=race):
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "external configuration is preserved",
                    ):
                        packctl._write_yaml_atomic(
                            directory,
                            {"value": "new"},
                            expected_snapshot=snapshot,
                            guard_snapshots=(snapshot,),
                        )

            recovery = list(root.glob(".settings.yaml.huroshiki-*.tmp"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(
                recovery[0].read_text(encoding="utf-8"),
                "value: external\n",
            )

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
            recovery = list(root.glob(".settings.yaml.huroshiki-*.tmp"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding="utf-8"), "value: new\n")

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
                        "value: original\n",
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
