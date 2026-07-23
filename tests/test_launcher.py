from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "shared" / "scripts" / "huroshiki-launcher.sh"


class LauncherTest(unittest.TestCase):
    def run_launcher(self, cwd: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("HUROSHIKI_ROOT", None)
        env["HUROSHIKI_PYTHON"] = sys.executable
        return subprocess.run(
            ["bash", str(LAUNCHER), "--probe"],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_discovers_repository_without_packs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            script = root / "shared" / "scripts" / "huroshiki.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "import sys\nprint('launched', *sys.argv[1:])\n",
                encoding="utf-8",
            )
            (root / "flake.nix").touch()
            nested = root / "nested" / "directory"
            nested.mkdir(parents=True)

            result = self.run_launcher(nested)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "launched --probe\n")
            self.assertFalse((root / "packs").exists())

    def test_rejects_directory_with_only_huroshiki_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            script = root / "shared" / "scripts" / "huroshiki.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise SystemExit('wrong script')\n", encoding="utf-8")

            result = self.run_launcher(root)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stderr,
                "huroshiki: not inside the MODPACK monorepo\n",
            )


if __name__ == "__main__":
    unittest.main()
