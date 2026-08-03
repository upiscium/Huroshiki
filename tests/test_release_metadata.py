from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
import re

from huroshiki_version import VERSION


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "shared" / "scripts"


class ReleaseMetadataTest(unittest.TestCase):
    def test_runtime_version_source(self) -> None:
        self.assertRegex(VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$")
        self.assertEqual(VERSION, "0.2.0-rc.5")
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

    def _extract_release_block(self, changelog: str, heading: str) -> str:
        pattern = rf"## {re.escape(heading)}\n(?:(?:.|\n)*?)(?=\n## [0-9]|\Z)"
        match = re.search(pattern, changelog, re.MULTILINE)
        self.assertIsNotNone(match, f"missing release heading: {heading}")
        assert match is not None
        return match.group(0)

    def test_release_documents_match_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        release_note_path = ROOT / "docs" / "releases" / f"v{VERSION}.md"
        self.assertTrue(release_note_path.is_file())
        release_notes = release_note_path.read_text(encoding="utf-8")

        rc5_heading = f"## {VERSION} - 2026-08-03"
        self.assertTrue(
            changelog.startswith(
                "# Changelog\n\n## Unreleased\n"
            ),
            "Unreleased section is missing",
        )
        unreleased_payload = (
            changelog.split("## Unreleased", 1)[1]
            .split(rc5_heading, 1)[0]
            .strip()
        )
        self.assertTrue(
            not unreleased_payload.strip(),
            "Unreleased section should be empty",
        )
        rc5_changelog = self._extract_release_block(changelog, f"{VERSION} - 2026-08-03").split("\n", 1)[1]
        rc4_changelog = self._extract_release_block(changelog, "0.2.0-rc.4 - 2026-08-02").split("\n", 1)[1]
        self.assertTrue(release_notes.startswith(f"# Huroshiki v{VERSION}\n"))
        self.assertIn("Release date: 2026-08-03", release_notes)
        self.assertIn("github:upiscium/Huroshiki/v0.2.0-rc.5", readme)
        self.assertIn(
            "compare/v0.2.0-rc.4...v0.2.0-rc.5",
            release_notes,
        )

        release_scope_words = " ".join(
            (f"{unreleased_payload}\n{rc5_changelog}\n{release_notes}").split()
        )
        release_scope_words_lower = release_scope_words.lower()

        self.assertIn("metadata:curseforge", release_scope_words_lower)
        self.assertIn("download.url", release_scope_words_lower)
        self.assertIn("packwiz installer", release_scope_words_lower)
        self.assertIn("java -cp", release_scope_words)
        self.assertIn("link.infra.packwiz.installer.Main", release_scope_words)
        self.assertIn("link.infra.packwiz.installer.main", release_scope_words_lower)
        self.assertIn("requiresbootstrap", release_scope_words_lower)
        self.assertIn("bootstrap updater", release_scope_words_lower)
        self.assertIn("bounded process-output", release_scope_words_lower)
        self.assertIn("artifact identity", release_scope_words_lower)
        self.assertIn("packwiz-installer.jar", release_scope_words)
        self.assertIn("declared hash", release_scope_words_lower)
        self.assertIn("side = \"both\"", release_scope_words)
        self.assertIn("manual-download", release_scope_words)
        self.assertIn("fail closed", release_scope_words_lower)
        self.assertIn(
            "no live network-backed curseforge metadata materialization",
            release_scope_words_lower,
        )
        self.assertIn("positive numeric project id", release_scope_words_lower)
        self.assertIn("positive file id", release_scope_words_lower)
        self.assertIn("candidate project identity", release_scope_words_lower)

        self.assertIn("## Known limitations", release_notes)
        self.assertIn(
            "no api key",
            release_scope_words_lower,
        )
        self.assertIn("bounded direct-download", release_scope_words_lower)
        self.assertIn("manifest", release_scope_words_lower)
        self.assertIn("loopback", release_scope_words_lower)
        self.assertIn("sha-256", release_scope_words_lower)
        self.assertIn("artifact", release_scope_words_lower)
        self.assertIn("Packwiz Installer entrypoint and bootstrap handling", release_notes)

        rc4_scope_words = " ".join(rc4_changelog.split())
        self.assertIn("legacy Packs without", rc4_scope_words)
        for provenance in ("explicit", "dependency", "unknown"):
            self.assertIn(provenance, rc4_scope_words)
        self.assertIn("preserves the existing metadata", rc4_scope_words)
        self.assertIn("does not infer or create a root manifest", rc4_scope_words)
        self.assertIn("evidence binding", rc4_scope_words)
        self.assertIn("unions sides", rc4_scope_words)
        self.assertIn("incoming explicit root", rc4_scope_words)
        self.assertIn("fail closed", rc4_scope_words)
        for evidence in (
            "strict declared SHA-256",
            "verified materialized SHA-256",
            "target-loader MOD ID/version set",
        ):
            self.assertIn(evidence, rc4_scope_words)
        self.assertIn("## Known limitations", release_notes)
        self.assertIn(
            "No live network-backed CurseForge metadata materialization was performed in release "
            "verification.",
            " ".join(release_notes.split()),
        )
        self.assertIn("Packwiz-native CurseForge Install", release_notes)
        self.assertIn("no API key is required", release_notes)
        readme_words = " ".join(readme.split())
        self.assertIn("CurseForge uses Packwiz-native interactive search", readme_words)
        self.assertIn("CurseForge API key is unnecessary", readme_words)
        excluded_release_scope = release_scope_words.lower()
        for excluded in (
            "#104",
            "#105",
            "publication manifest planner",
            "pack_publish",
        ):
            self.assertNotIn(excluded, excluded_release_scope)

        for historical in (
            "## 0.2.0-rc.3 - 2026-08-02",
            "## 0.2.0-rc.2 - 2026-08-01",
            "## 0.2.0-rc.1 - 2026-07-30",
        ):
            self.assertIn(historical, changelog)
        for historical_note in (
            "v0.2.0-rc.1.md",
            "v0.2.0-rc.2.md",
            "v0.2.0-rc.3.md",
        ):
            self.assertTrue((ROOT / "docs" / "releases" / historical_note).is_file())
        rc2_changelog = changelog.split("## 0.2.0-rc.2 -", 1)[1].split(
            "## 0.2.0-rc.1 -", 1
        )[0]
        self.assertIn("HUROSHIKI_CURSEFORGE_API_KEY", rc2_changelog)


if __name__ == "__main__":
    unittest.main()
