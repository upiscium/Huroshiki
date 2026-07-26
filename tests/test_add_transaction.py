from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


def metadata(name: str, project_id: str) -> str:
    return f'''name = "{name}"
filename = "{project_id}.jar"
side = "both"
[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/{project_id}.jar"
[update.modrinth]
mod-id = "{project_id}"
version = "v1"
'''


class AddTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_bytes(b'name = "Demo"\n')
        (self.source / "index.toml").write_bytes(b"original index\n")
        self.config = self.packs / "demo" / "pack.yaml"
        self.config.write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "STATE_ROOT", self.root / ".huroshiki"),
        ]
        for item in self.patches:
            item.start()
        self.key = core.project_key("pack", "demo")

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def snapshot(self) -> dict[Path, bytes | str]:
        snapshot: dict[Path, bytes | str] = {}
        for path in sorted(self.source.rglob("*")):
            relative = path.relative_to(self.source)
            if path.is_symlink():
                snapshot[relative] = f"symlink:{path.readlink()}"
            elif path.is_file():
                snapshot[relative] = path.read_bytes()
            elif path.is_dir():
                snapshot[relative] = "directory"
        return snapshot

    @staticmethod
    def completed(command: list[str], returncode: int = 0):
        return subprocess.CompletedProcess(command, returncode)

    def install_files(self, cwd: Path) -> None:
        (cwd / "mods/root.pw.toml").write_text(
            metadata("Root", "root"), encoding="utf-8"
        )
        (cwd / "mods/dependency.pw.toml").write_text(
            metadata("Dependency", "dependency"), encoding="utf-8"
        )
        (cwd / "pack.toml").write_bytes(b"staged pack\n")
        (cwd / "index.toml").write_bytes(b"staged index\n")

    def assert_unlocked(self) -> None:
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_success_applies_root_dependencies_and_packwiz_files_together(self) -> None:
        original = self.snapshot()
        command_directories: list[Path] = []

        def run(command, *, cwd, **_):
            self.assertEqual(self.snapshot(), original)
            self.assertNotEqual(cwd, self.source)
            command_directories.append(cwd)
            if command == ["packwiz", "modrinth", "add", "example"]:
                self.install_files(cwd)
            elif command == ["packwiz", "refresh"]:
                (cwd / "index.toml").write_bytes(b"refreshed index\n")
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            result = core.add_mod_transactionally(
                self.key, "modrinth", "example", "client"
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(set(command_directories)), 1)
        self.assertIn('side = "client"', (self.source / "mods/root.pw.toml").read_text())
        self.assertIn(
            'side = "client"',
            (self.source / "mods/dependency.pw.toml").read_text(),
        )
        self.assertEqual((self.source / "pack.toml").read_bytes(), b"staged pack\n")
        self.assertEqual((self.source / "index.toml").read_bytes(), b"refreshed index\n")
        self.assert_unlocked()

    def test_add_and_refresh_failures_leave_real_tree_unchanged(self) -> None:
        for failure in ("add", "refresh"):
            with self.subTest(failure=failure):
                original = self.snapshot()

                def run(command, *, cwd, **_):
                    if command[:3] == ["packwiz", "modrinth", "add"]:
                        self.install_files(cwd)
                        return self.completed(command, 7 if failure == "add" else 0)
                    if command == ["packwiz", "refresh"]:
                        (cwd / "index.toml").write_bytes(b"partial refresh\n")
                        return self.completed(command, 9)
                    raise AssertionError(command)

                with patch.object(core.subprocess, "run", side_effect=run):
                    if failure == "add":
                        self.assertEqual(
                            core.add_mod_transactionally(
                                self.key, "modrinth", "example", "both"
                            ),
                            7,
                        )
                    else:
                        with self.assertRaises(core.HuroshikiError):
                            core.add_mod_transactionally(
                                self.key, "modrinth", "example", "both"
                            )
                self.assertEqual(self.snapshot(), original)
                self.assert_unlocked()

    def test_side_write_failure_leaves_real_tree_unchanged(self) -> None:
        original = self.snapshot()

        def run(command, *, cwd, **_):
            self.install_files(cwd)
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run), patch.object(
            packctl, "set_side_file", side_effect=OSError("side write failed")
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "side write failed"):
                core.add_mod_transactionally(
                    self.key, "modrinth", "example", "both"
                )
        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()

    def test_keyboard_interrupt_during_add_or_refresh_discards_and_unlocks(self) -> None:
        for interrupted_command in ("add", "refresh"):
            with self.subTest(command=interrupted_command):
                original = self.snapshot()

                def run(command, *, cwd, **_):
                    if command[:3] == ["packwiz", "modrinth", "add"]:
                        self.install_files(cwd)
                        if interrupted_command == "add":
                            raise KeyboardInterrupt
                    elif command == ["packwiz", "refresh"]:
                        (cwd / "index.toml").write_bytes(b"partial refresh\n")
                        raise KeyboardInterrupt
                    return self.completed(command)

                with patch.object(core.subprocess, "run", side_effect=run):
                    with self.assertRaises(KeyboardInterrupt):
                        core.add_mod_transactionally(
                            self.key, "modrinth", "example", "both"
                        )
                self.assertEqual(self.snapshot(), original)
                self.assert_unlocked()

    def test_external_source_or_configuration_change_aborts_apply(self) -> None:
        for external_change in ("source", "config"):
            with self.subTest(change=external_change):
                (self.source / "index.toml").write_bytes(b"original index\n")
                self.config.write_text(
                    "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
                )

                def run(command, *, cwd, **_):
                    if command[:3] == ["packwiz", "modrinth", "add"]:
                        self.install_files(cwd)
                    elif command == ["packwiz", "refresh"]:
                        if external_change == "source":
                            (self.source / "index.toml").write_bytes(b"external index\n")
                        else:
                            self.config.write_text("external: true\n", encoding="utf-8")
                    return self.completed(command)

                with patch.object(core.subprocess, "run", side_effect=run):
                    with self.assertRaisesRegex(core.HuroshikiError, "changed"):
                        core.add_mod_transactionally(
                            self.key, "modrinth", "example", "server"
                        )
                self.assertFalse((self.source / "mods/root.pw.toml").exists())
                if external_change == "source":
                    self.assertEqual(
                        (self.source / "index.toml").read_bytes(), b"external index\n"
                    )
                else:
                    self.assertEqual(self.config.read_text(), "external: true\n")
                self.assert_unlocked()


if __name__ == "__main__":
    unittest.main()
