from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from huroshiki_version import VERSION


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "shared" / "scripts"

# Keep current development metadata separate from published historical metadata.  In
# particular, preparing rc.1 should only require changing these current expectations.
CURRENT_VERSION = "0.3.0-dev"
HISTORICAL_VERSION = "0.2.0-rc.5"
HISTORICAL_DATE = "2026-08-03"
HISTORICAL_PREVIOUS_VERSION = "0.2.0-rc.4"
HISTORICAL_PREVIOUS_DATE = "2026-08-02"

VERSION_NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION_CORE = rf"{VERSION_NUMBER}\.{VERSION_NUMBER}\.{VERSION_NUMBER}"
VERSION_RE = re.compile(
    rf"^(?:{VERSION_CORE}-dev|{VERSION_CORE}-rc\.{VERSION_NUMBER}|{VERSION_CORE})$"
)


def version_kind(version: str) -> str:
    if VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"invalid version: {version}")
    if version.endswith("-dev"):
        return "development"
    if "-rc." in version:
        return "release-candidate"
    return "stable"


def unreleased_payload(changelog: str) -> str:
    match = re.search(
        r"^## Unreleased\n(?P<body>.*?)(?=^## [0-9])",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("missing Unreleased section before release history")
    return match.group("body").strip()


def release_block(document: str, version: str, date: str) -> str:
    heading = f"## {version} - {date}"
    match = re.search(
        rf"^{re.escape(heading)}\n(?P<body>.*?)(?=^## (?:[0-9]|\Z))",
        document,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing release heading: {heading}")
    return match.group("body")


class ReleaseMetadataTest(unittest.TestCase):
    def test_allowed_version_forms_are_strict(self) -> None:
        allowed = {
            "0.3.0-dev": "development",
            "0.2.0-rc.5": "release-candidate",
            "1.2.3": "stable",
        }
        rejected = (
            "v0.3.0-dev",
            "0.3-dev",
            "0.3.0-dev.1",
            "0.3.0-rc",
            "0.3.0-rc.x",
            "0.3.0-rc.0foo",
            "0.3.0+build",
            "01.2.3-dev",
            "1.02.3-rc.1",
            "1.2.3-rc.01",
            "latest",
            "",
        )
        for version, kind in allowed.items():
            with self.subTest(version=version):
                self.assertRegex(version, VERSION_RE)
                self.assertEqual(version_kind(version), kind)
        for version in rejected:
            with self.subTest(version=version):
                self.assertIsNone(VERSION_RE.fullmatch(version))
                with self.assertRaises(ValueError):
                    version_kind(version)

    def test_current_version_source_and_runtime_parity(self) -> None:
        self.assertRegex(CURRENT_VERSION, VERSION_RE)
        self.assertEqual(VERSION, CURRENT_VERSION)
        source = (SCRIPTS / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(source, CURRENT_VERSION)
        self.assertEqual(source, VERSION)

    def test_flake_uses_runtime_version_source(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        self.assertIn("builtins.readFile ./shared/scripts/VERSION", flake)
        self.assertNotIn(f'version = "{CURRENT_VERSION}"', flake)
        self.assertGreaterEqual(flake.count("inherit version;"), 2)

    def test_cli_versions(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        for script, expected in (
            ("huroshiki.py", f"huroshiki {CURRENT_VERSION}"),
            ("packctl.py", f"packctl {CURRENT_VERSION}"),
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

    def test_current_development_metadata(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith("# Changelog\n\n## Unreleased\n"))
        self.assertEqual(version_kind(CURRENT_VERSION), "development")
        self.assertTrue(
            unreleased_payload(changelog),
            "development Unreleased section must have a payload",
        )
        self.assertFalse(
            (ROOT / "docs" / "releases" / f"v{CURRENT_VERSION}.md").exists()
        )

    def test_historical_rc5_metadata_is_immutable(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        release_path = ROOT / "docs" / "releases" / f"v{HISTORICAL_VERSION}.md"
        self.assertTrue(release_path.is_file())
        release_notes = release_path.read_text(encoding="utf-8")
        self.assertRegex(HISTORICAL_VERSION, VERSION_RE)
        rc5 = release_block(changelog, HISTORICAL_VERSION, HISTORICAL_DATE)
        rc4 = release_block(changelog, HISTORICAL_PREVIOUS_VERSION, HISTORICAL_PREVIOUS_DATE)

        self.assertTrue(release_notes.startswith(f"# Huroshiki v{HISTORICAL_VERSION}\n"))
        self.assertIn(f"Release date: {HISTORICAL_DATE}", release_notes)
        self.assertIn(
            "compare/v0.2.0-rc.4...v0.2.0-rc.5",
            release_notes,
        )
        self.assertIn("legacy Packs without", rc4)
        historical_claims = " ".join((rc5 + release_notes).split()).lower()
        for phrase in (
            "metadata:curseforge",
            "java -cp",
            "link.infra.packwiz.installer.Main",
            "RequiresBootstrap",
            "fail closed",
            "bounded process-output",
            "artifact identity",
            "side = \"both\"",
            "positive numeric project ID",
            "no live network-backed CurseForge metadata materialization",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), historical_claims)
        rc4_claims = " ".join(rc4.split()).lower()
        for phrase in (
            "strict declared SHA-256",
            "verified materialized SHA-256",
            "target-loader MOD ID/version set",
            "unions sides",
        ):
            self.assertIn(phrase.lower(), rc4_claims)

    def test_readme_current_and_published_version_guidance(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_words = " ".join(readme.split())
        self.assertRegex(readme_words, r"current main .*development version `0\.3\.0-dev`")
        self.assertIn("latest published release example is `v0.2.0-rc.5`", readme_words)
        self.assertIn(
            "github:upiscium/Huroshiki/v0.2.0-rc.5",
            readme,
        )
        self.assertNotRegex(readme.lower(), r"rc\.5[^\n]*(?:future|uncreated)")
        self.assertNotIn("github:upiscium/Huroshiki/v0.3.0-dev", readme)
        self.assertNotIn("v0.3.0-rc.1", readme)


if __name__ == "__main__":
    unittest.main()
