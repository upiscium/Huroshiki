from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY_ROOT / "shared" / "scripts"
LAUNCHER = SCRIPTS / "huroshiki-launcher.sh"


class LauncherTest(unittest.TestCase):
    def test_launcher_is_directly_executable(self) -> None:
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        result = subprocess.run(
            [str(LAUNCHER), "--help"],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "HUROSHIKI_PYTHON": sys.executable},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_launcher_discovers_repository_but_uses_its_own_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            installed = temporary / "installed"
            installed.mkdir()
            shutil.copy2(LAUNCHER, installed / LAUNCHER.name)
            (installed / "huroshiki.py").write_text(
                "import os, sys\nprint(os.environ['HUROSHIKI_ROOT'], *sys.argv[1:])\n",
                encoding="utf-8",
            )

            root = temporary / "repository"
            repository_script = root / "shared" / "scripts" / "huroshiki.py"
            repository_script.parent.mkdir(parents=True)
            repository_script.write_text("raise SystemExit('wrong source')\n")
            (root / "flake.nix").touch()
            nested = root / "nested" / "directory"
            nested.mkdir(parents=True)

            env = os.environ.copy()
            env.pop("HUROSHIKI_ROOT", None)
            env["HUROSHIKI_PYTHON"] = sys.executable
            result = subprocess.run(
                ["bash", str(installed / LAUNCHER.name), "--probe"],
                cwd=nested,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"{root} --probe\n")

    def test_source_launcher_preserves_explicit_environment_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            env = os.environ.copy()
            env["HUROSHIKI_ROOT"] = str(root)
            env["HUROSHIKI_PYTHON"] = sys.executable
            result = subprocess.run(
                ["bash", str(LAUNCHER), "--help"],
                cwd="/",
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--root PATH", result.stdout)


if __name__ == "__main__":
    unittest.main()
