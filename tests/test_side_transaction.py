from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


METADATA = b'''name = "Example"\nfilename = "example.jar"\nside = "both"\n'''
PACK_TOML = b'''name = "Demo"\n[versions]\nminecraft = "1.21.1"\nneoforge = "21.1.234"\n'''


class SideTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.source = self.packs / "demo" / "source"
        self.metadata = self.source / "mods" / "example.pw.toml"
        self.metadata.parent.mkdir(parents=True)
        self.metadata.write_bytes(METADATA)
        (self.source / "index.toml").write_bytes(b"original index\n")
        (self.source / "pack.toml").write_bytes(PACK_TOML)
        (self.packs / "demo" / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "PACKS", self.packs),
            patch.object(packctl, "PACKS", self.packs),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def failed_refresh(self, command, **kwargs):
        self.metadata.write_bytes(b"partial metadata")
        (self.source / "index.toml").write_bytes(b"partial index")
        (self.source / "pack.toml").write_bytes(b"partial pack")
        return subprocess.CompletedProcess(command, 1, "", "refresh failed")

    def assert_rolled_back(self) -> None:
        self.assertEqual(self.metadata.read_bytes(), METADATA)
        self.assertEqual((self.source / "index.toml").read_bytes(), b"original index\n")
        self.assertEqual((self.source / "pack.toml").read_bytes(), PACK_TOML)

    def test_cli_side_refresh_failure_rolls_back_files(self) -> None:
        args = argparse.Namespace(
            pack="demo", metadata_file="mods/example.pw.toml", side="server"
        )
        with patch.object(packctl.subprocess, "run", side_effect=self.failed_refresh):
            with self.assertRaisesRegex(packctl.ConfigError, "refresh failed"):
                packctl.cmd_side(args)
        self.assert_rolled_back()

    def test_tui_side_refresh_failure_uses_same_rollback(self) -> None:
        with patch.object(packctl.subprocess, "run", side_effect=self.failed_refresh):
            with self.assertRaisesRegex(core.HuroshikiError, "refresh failed"):
                core.set_installed_mod_side(
                    core.project_key("pack", "demo"),
                    Path("mods/example.pw.toml"),
                    client=True,
                    server=False,
                )
        self.assert_rolled_back()

    def test_keyboard_interrupt_rolls_back_files(self) -> None:
        def interrupted_refresh(command, **kwargs):
            self.metadata.write_bytes(b"partial metadata")
            (self.source / "index.toml").write_bytes(b"partial index")
            (self.source / "pack.toml").write_bytes(b"partial pack")
            raise KeyboardInterrupt

        with patch.object(packctl.subprocess, "run", side_effect=interrupted_refresh):
            with self.assertRaises(KeyboardInterrupt):
                packctl.set_side_and_refresh(self.source, self.metadata, "server")
        self.assert_rolled_back()

    def test_rollback_attempts_every_snapshot_after_restore_failures(self) -> None:
        real_replace = Path.replace

        def fail_two_restores(path: Path, target: Path):
            if "huroshiki-side-rollback" in path.name and target.name in {
                self.metadata.name,
                "index.toml",
            }:
                raise OSError(f"cannot restore {target.name}")
            return real_replace(path, target)

        with (
            patch.object(packctl.subprocess, "run", side_effect=self.failed_refresh),
            patch.object(Path, "replace", fail_two_restores),
        ):
            with self.assertRaises(packctl.ConfigError) as raised:
                packctl.set_side_and_refresh(self.source, self.metadata, "server")

        message = str(raised.exception)
        self.assertIn("cannot restore example.pw.toml", message)
        self.assertIn("cannot restore index.toml", message)
        self.assertEqual((self.source / "pack.toml").read_bytes(), PACK_TOML)


if __name__ == "__main__":
    unittest.main()
