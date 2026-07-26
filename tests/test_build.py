from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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
