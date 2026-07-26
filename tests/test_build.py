from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import overlay_policy
import packctl


PACK_TOML = '''name = "Demo"
author = "tester"
version = "0.1.0"
pack-format = "packwiz:1.1.0"
'''


class TransactionalBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.pack_root = self.packs / "demo"
        (self.pack_root / "source" / "mods").mkdir(parents=True)
        (self.pack_root / "source" / "pack.toml").write_text(
            PACK_TOML, encoding="utf-8"
        )
        (self.pack_root / "source" / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (self.pack_root / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        for target in ("client", "server"):
            old = self.pack_root / "dist" / target
            old.mkdir(parents=True)
            (old / "old.bin").write_bytes(b"previous-" + target.encode())

        self.patches = [
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def dist_snapshot(self) -> dict[Path, bytes]:
        dist = self.pack_root / "dist"
        return {
            path.relative_to(dist): path.read_bytes()
            for path in sorted(dist.rglob("*"))
            if path.is_file()
        }

    def write_metadata(self, side: str) -> None:
        (self.pack_root / "source" / "mods" / "demo.pw.toml").write_text(
            f'name = "Demo MOD"\nfilename = "demo.jar"\nside = "{side}"\n',
            encoding="utf-8",
        )

    def test_invalid_side_preserves_dist(self) -> None:
        self.write_metadata("unknown")
        before = self.dist_snapshot()

        with patch.object(packctl, "run") as run:
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertEqual(self.dist_snapshot(), before)
        run.assert_not_called()

    def test_missing_and_empty_sides_use_the_same_validation_as_unknown(self) -> None:
        metadata = self.pack_root / "source" / "mods" / "demo.pw.toml"
        for side_line in ("", 'side = ""\n'):
            metadata.write_text(
                'name = "Demo MOD"\nfilename = "demo.jar"\n' + side_line,
                encoding="utf-8",
            )
            before = self.dist_snapshot()
            with patch.object(packctl, "run") as run:
                result = packctl.build_pack("demo")
            self.assertEqual(result, 1)
            self.assertEqual(self.dist_snapshot(), before)
            run.assert_not_called()

    def test_refresh_failure_preserves_entire_dist(self) -> None:
        self.write_metadata("both")
        before = self.dist_snapshot()

        def refresh(command: list[str], *, cwd: Path | None = None) -> None:
            assert cwd is not None
            (cwd / "refreshed").write_text("partial", encoding="utf-8")
            if cwd.name == "server":
                raise subprocess.CalledProcessError(1, command)

        with patch.object(packctl, "run", side_effect=refresh):
            with self.assertRaises(subprocess.CalledProcessError):
                packctl.build_pack("demo")

        self.assertEqual(self.dist_snapshot(), before)

    def test_success_replaces_client_and_server_together(self) -> None:
        self.write_metadata("both")
        common = self.pack_root / "content" / "common"
        common.mkdir(parents=True)
        (common / "new.txt").write_text("new build", encoding="utf-8")

        with patch.object(packctl, "run"):
            result = packctl.build_pack("demo")

        self.assertEqual(result, 0)
        for target in ("client", "server"):
            output = self.pack_root / "dist" / target
            self.assertFalse((output / "old.bin").exists())
            self.assertEqual((output / "new.txt").read_text(), "new build")
            self.assertTrue((output / "mods" / "demo.pw.toml").is_file())

    def test_overlay_symlink_stops_before_secret_copy_and_preserves_dist(self) -> None:
        self.write_metadata("both")
        secret = self.root / "secret.txt"
        secret.write_text("private data", encoding="utf-8")
        link = self.pack_root / "content" / "common" / "secret.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(secret)
        before = self.dist_snapshot()
        stderr = StringIO()

        with patch.object(packctl, "run") as run, redirect_stderr(stderr):
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertEqual(self.dist_snapshot(), before)
        self.assertIn(f"content/common/secret.txt: symlink is not allowed -> {secret}", stderr.getvalue())
        self.assertNotIn(b"private data", self.dist_snapshot().values())
        run.assert_not_called()

    def test_reserved_overlay_stops_before_replacing_dist(self) -> None:
        self.write_metadata("both")
        reserved = self.pack_root / "content" / "server" / "nested" / "index.toml"
        reserved.parent.mkdir(parents=True)
        reserved.write_text("malicious", encoding="utf-8")
        before = self.dist_snapshot()

        with patch.object(packctl, "run") as run:
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertEqual(self.dist_snapshot(), before)
        run.assert_not_called()

    def test_directory_to_external_symlink_race_never_copies_secret(self) -> None:
        self.write_metadata("both")
        clean = self.pack_root / "content" / "common" / "clean"
        clean.mkdir(parents=True)
        (clean / "ordinary.txt").write_text("ordinary", encoding="utf-8")
        external = self.root / "external"
        external.mkdir()
        secret = external / "secret.txt"
        secret.write_text("private data", encoding="utf-8")
        parked = clean.with_name("clean-parked")
        before = self.dist_snapshot()
        original_open = overlay_policy._open_directory
        swapped = False

        def racing_open(name: str, parent_fd: int) -> int:
            nonlocal swapped
            if name == "clean" and not swapped:
                swapped = True
                clean.rename(parked)
                clean.symlink_to(external, target_is_directory=True)
            return original_open(name, parent_fd)

        with patch.object(overlay_policy, "_open_directory", side_effect=racing_open), patch.object(
            packctl, "run"
        ) as run:
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertEqual(self.dist_snapshot(), before)
        self.assertNotIn(b"private data", self.dist_snapshot().values())
        run.assert_not_called()

    def test_destination_directory_replacement_never_writes_external(self) -> None:
        self.write_metadata("both")
        overlay = self.pack_root / "content" / "common" / "mods" / "overlay.txt"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("overlay data", encoding="utf-8")
        external = self.root / "external-destination"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("unchanged", encoding="utf-8")
        before = self.dist_snapshot()
        original_open = overlay_policy._open_destination_directory
        replaced = False

        def replace_after_open(name, destination_fd, relative, issues):
            nonlocal replaced
            opened_fd = original_open(name, destination_fd, relative, issues)
            if name == "mods" and opened_fd is not None and not replaced:
                replaced = True
                destination = Path(os.readlink(f"/proc/self/fd/{destination_fd}"))
                child = destination / name
                child.rename(destination / "mods-parked")
                child.symlink_to(external, target_is_directory=True)
            return opened_fd

        with patch.object(
            overlay_policy,
            "_open_destination_directory",
            side_effect=replace_after_open,
        ), patch.object(packctl, "run") as run:
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertEqual(self.dist_snapshot(), before)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((external / "overlay.txt").exists())
        run.assert_not_called()

    def test_preexisting_destination_symlink_never_writes_external(self) -> None:
        self.write_metadata("both")
        overlay = self.pack_root / "content" / "common" / "config" / "settings.txt"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("overlay data", encoding="utf-8")
        external = self.root / "external-config"
        external.mkdir()
        settings = external / "settings.txt"
        settings.write_text("unchanged", encoding="utf-8")
        before = self.dist_snapshot()
        original_copy = packctl.copy_metadata

        def copy_with_packwiz_symlink(source: Path, destination: Path) -> None:
            original_copy(source, destination)
            (destination / "config").symlink_to(external, target_is_directory=True)

        with patch.object(
            packctl, "copy_metadata", side_effect=copy_with_packwiz_symlink
        ), patch.object(packctl, "run") as run:
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertEqual(self.dist_snapshot(), before)
        self.assertEqual(settings.read_text(encoding="utf-8"), "unchanged")
        run.assert_not_called()

    def test_keyboard_interrupt_during_swap_restores_dist(self) -> None:
        self.write_metadata("both")
        before = self.dist_snapshot()
        real_replace = Path.replace

        def interrupt_staged_swap(path: Path, target: Path):
            if path.name == "dist" and path.parent.name.startswith(".build-dist-"):
                raise KeyboardInterrupt
            return real_replace(path, target)

        with (
            patch.object(packctl, "run"),
            patch.object(Path, "replace", interrupt_staged_swap),
        ):
            with self.assertRaises(KeyboardInterrupt):
                packctl.build_pack("demo")

        self.assertEqual(self.dist_snapshot(), before)

    def test_invalid_side_prints_exact_side_for_guidance(self) -> None:
        self.write_metadata("unknown")
        stderr = StringIO()

        with patch.object(packctl, "run"), redirect_stderr(stderr):
            result = packctl.build_pack("demo")

        self.assertEqual(result, 1)
        self.assertIn("  - mods/demo.pw.toml has no valid side\n", stderr.getvalue())
        self.assertIn(
            "Use: packctl side demo mods/<name>.pw.toml client|server|both\n",
            stderr.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
