from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

from huroshiki_version import VERSION


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "shared" / "scripts"


class ReleaseMetadataTest(unittest.TestCase):
    def test_runtime_version_source(self) -> None:
        self.assertRegex(VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$")
        self.assertEqual(
            (SCRIPTS / "VERSION").read_text(encoding="utf-8").strip(),
            VERSION,
        )

    def test_flake_uses_runtime_version_source(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        self.assertIn("builtins.readFile ./shared/scripts/VERSION", flake)
        self.assertNotIn(f'version = "{VERSION}"', flake)
        self.assertGreaterEqual(flake.count("inherit version;"), 2)

    def test_cli_versions(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        for script, expected in (
            ("huroshiki.py", f"huroshiki {VERSION}"),
            ("packctl.py", f"packctl {VERSION}"),
        ):
            with self.subTest(script=script):
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / script), "--version"],
                    cwd=ROOT,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_release_documents_match_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        release_notes = (
            ROOT / "docs" / "releases" / f"v{VERSION}.md"
        ).read_text(encoding="utf-8")
        self.assertIn(f"## {VERSION} - 2026-07-30", changelog)
        self.assertTrue(release_notes.startswith(f"# Huroshiki v{VERSION}\n"))
        self.assertIn(
            f"compare/v0.1.0...v{VERSION}",
            release_notes,
        )


if __name__ == "__main__":
    unittest.main()
