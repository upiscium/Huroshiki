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
        self.assertEqual(VERSION, "0.2.0-rc.3")
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
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        release_note_path = ROOT / "docs" / "releases" / f"v{VERSION}.md"
        self.assertTrue(release_note_path.is_file())
        release_notes = release_note_path.read_text(encoding="utf-8")

        rc3_heading = f"## {VERSION} - 2026-08-02"
        self.assertTrue(
            changelog.startswith(f"# Changelog\n\n## Unreleased\n\n{rc3_heading}\n"),
            "Unreleased must remain empty before the rc.3 entry",
        )
        self.assertTrue(release_notes.startswith(f"# Huroshiki v{VERSION}\n"))
        self.assertIn("Release date: 2026-08-02", release_notes)
        self.assertIn("github:upiscium/Huroshiki/v0.2.0-rc.3", readme)
        self.assertIn(
            "compare/v0.2.0-rc.2...v0.2.0-rc.3",
            release_notes,
        )

        rc3_changelog = changelog.split("## 0.2.0-rc.2 -", 1)[0]
        self.assertIn("Packwiz-native", rc3_changelog)
        self.assertIn("no CurseForge API key is required", rc3_changelog)
        self.assertIn("dependency equivalence", rc3_changelog)
        self.assertIn("Packwiz-native CurseForge Install", release_notes)
        self.assertIn("no API key is required", release_notes)
        self.assertIn("cross-provider dependency equivalence", release_notes)
        excluded_release_scope = f"{rc3_changelog}\n{release_notes}".lower()
        for excluded in (
            "#104",
            "#105",
            "publication manifest planner",
            "pack_publish",
        ):
            self.assertNotIn(excluded, excluded_release_scope)

        self.assertIn("## 0.2.0-rc.2 - 2026-08-01", changelog)
        self.assertIn("## 0.2.0-rc.1 - 2026-07-30", changelog)
        rc2_changelog = changelog.split("## 0.2.0-rc.2 -", 1)[1].split(
            "## 0.2.0-rc.1 -", 1
        )[0]
        self.assertIn("HUROSHIKI_CURSEFORGE_API_KEY", rc2_changelog)


if __name__ == "__main__":
    unittest.main()
