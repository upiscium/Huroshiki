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

# Keep source preparation expectations separate from immutable published history.
CURRENT_SOURCE_VERSION = "0.3.0-rc.2"
CURRENT_RELEASE_DATE = "2026-09-08"
CURRENT_RELEASE_TAG = f"v{CURRENT_SOURCE_VERSION}"
CURRENT_RELEASE_SCOPE = (
    "bound manifests before reuse",
    "pack.toml[index].hash",
    "metafile = false",
    "<publication_root>/client",
    "primary_error",
    "bounded and redacted",
    "public_pack_url",
    "no configuration-schema migration",
)
LATEST_PUBLISHED_VERSION = "0.3.0-rc.1"
LATEST_PUBLISHED_DATE = "2026-09-03"
LATEST_PUBLISHED_TAG = f"v{LATEST_PUBLISHED_VERSION}"
LATEST_PUBLISHED_RELEASE_SCOPE = (
    "Pack Copy migration",
    "Template Copy migration",
    "Installed MOD version browser",
    "packctl publish",
    "publication-uncertain",
)
HISTORICAL_VERSION = "0.2.0-rc.5"
HISTORICAL_DATE = "2026-08-03"
HISTORICAL_PREVIOUS_VERSION = "0.2.0-rc.4"
HISTORICAL_PREVIOUS_DATE = "2026-08-02"

VERSION_NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION_CORE = rf"{VERSION_NUMBER}\.{VERSION_NUMBER}\.{VERSION_NUMBER}"
VERSION_RE = re.compile(
    rf"^(?:{VERSION_CORE}-rc\.{VERSION_NUMBER}\.dev|{VERSION_CORE}-dev|"
    rf"{VERSION_CORE}-rc\.{VERSION_NUMBER}|{VERSION_CORE})$"
)


def version_kind(version: str) -> str:
    if VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"invalid version: {version}")
    if re.search(r"-rc\.[0-9]+\.dev$", version):
        return "post-RC development"
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
            CURRENT_SOURCE_VERSION: "release-candidate",
            "0.3.0-rc.1.dev": "post-RC development",
            "1.2.3": "stable",
            "1.2.3-dev": "development",
        }
        rejected = (
            "v0.3.0-dev",
            "0.3-dev",
            "0.3.0-dev.1",
            "0.3.0-rc",
            "0.3.0-rc.dev",
            "0.3.0-rc.x",
            "0.3.0-rc.0foo",
            "0.3.0-rc.1dev",
            "0.3.0-rc.1.dev.1",
            "0.3.0+build",
            "01.2.3-dev",
            "01.2.3-rc.1.dev",
            "1.02.3-rc.1",
            "1.2.3-rc.01",
            "1.2.3-rc.01.dev",
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
        self.assertRegex(CURRENT_SOURCE_VERSION, VERSION_RE)
        self.assertEqual(version_kind(CURRENT_SOURCE_VERSION), "release-candidate")
        self.assertEqual(VERSION, CURRENT_SOURCE_VERSION)
        source = (SCRIPTS / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(source, CURRENT_SOURCE_VERSION)
        self.assertEqual(source, VERSION)

    def test_flake_uses_runtime_version_source(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        self.assertIn("builtins.readFile ./shared/scripts/VERSION", flake)
        self.assertNotIn(f'version = "{CURRENT_SOURCE_VERSION}"', flake)
        self.assertGreaterEqual(flake.count("inherit version;"), 2)

    def test_cli_versions(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        for script, expected in (
            ("huroshiki.py", f"huroshiki {CURRENT_SOURCE_VERSION}"),
            ("packctl.py", f"packctl {CURRENT_SOURCE_VERSION}"),
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

    def test_current_source_release_metadata_is_deterministic_and_consistent(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith("# Changelog\n\n## Unreleased\n"))
        self.assertEqual(unreleased_payload(changelog), "")
        current = release_block(changelog, CURRENT_SOURCE_VERSION, CURRENT_RELEASE_DATE)
        self.assertTrue(current.strip(), "current release block must have a payload")
        self.assertIn(
            f"## {CURRENT_SOURCE_VERSION} - {CURRENT_RELEASE_DATE}\n", changelog
        )

        release_path = ROOT / "docs" / "releases" / f"{CURRENT_RELEASE_TAG}.md"
        self.assertTrue(release_path.is_file())
        release_notes = release_path.read_text(encoding="utf-8")
        self.assertTrue(release_notes.startswith(f"# Huroshiki {CURRENT_RELEASE_TAG}\n"))
        self.assertIn(f"Release date: {CURRENT_RELEASE_DATE}", release_notes)
        self.assertIn(
            f"compare/{LATEST_PUBLISHED_TAG}...{CURRENT_RELEASE_TAG}", release_notes
        )
        release_material = " ".join(release_notes.split()).lower()
        for phrase in CURRENT_RELEASE_SCOPE:
            with self.subTest(scope=phrase):
                self.assertIn(phrase.lower(), release_material)
        for issue in (190, 192, 194, 195):
            with self.subTest(issue=issue):
                self.assertIn(f"#{issue}", release_notes)
        self.assertIn("## Manual production smoke evidence", release_notes)
        self.assertIn(
            "These facts record the observed smoke evidence only",
            release_notes,
        )
        self.assertIn(
            "did not explicitly confirm the descriptor-level `index.toml` hash or\n"
            "`metafile = false` assertions",
            release_notes,
        )
        self.assertIn(
            "Automated #192 tests provide deterministic coverage",
            release_notes,
        )

    def test_historical_published_rc1_metadata_is_immutable(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        historical = release_block(
            changelog, LATEST_PUBLISHED_VERSION, LATEST_PUBLISHED_DATE
        )
        self.assertTrue(historical.strip(), "historical release block must have a payload")
        self.assertIn(
            f"## {LATEST_PUBLISHED_VERSION} - {LATEST_PUBLISHED_DATE}\n", changelog
        )

        release_path = ROOT / "docs" / "releases" / f"{LATEST_PUBLISHED_TAG}.md"
        self.assertTrue(release_path.is_file())
        release_notes = release_path.read_text(encoding="utf-8")
        self.assertTrue(release_notes.startswith(f"# Huroshiki {LATEST_PUBLISHED_TAG}\n"))
        self.assertIn(f"Release date: {LATEST_PUBLISHED_DATE}", release_notes)
        self.assertIn(
            f"compare/v0.2.0-rc.5...{LATEST_PUBLISHED_TAG}", release_notes
        )
        self.assertIn(f"{LATEST_PUBLISHED_VERSION} - {LATEST_PUBLISHED_DATE}", changelog)
        release_material = " ".join(release_notes.split()).lower()
        for phrase in LATEST_PUBLISHED_RELEASE_SCOPE:
            with self.subTest(scope=phrase):
                self.assertIn(phrase.lower(), release_material)

    def test_historical_published_rc5_metadata_is_immutable(self) -> None:
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
        self.assertIn(CURRENT_SOURCE_VERSION, readme_words)
        self.assertRegex(
            readme_words,
            rf"{re.escape(CURRENT_SOURCE_VERSION)}.*prepared.*not been published",
        )
        self.assertIn(f"{LATEST_PUBLISHED_TAG}", readme_words)
        self.assertIn(
            f"nix run github:upiscium/Huroshiki/{LATEST_PUBLISHED_TAG}", readme_words
        )
        self.assertRegex(
            readme_words,
            rf"latest(?: actually)? published.*{re.escape(LATEST_PUBLISHED_TAG)}",
        )
        self.assertIn(
            f"github:upiscium/Huroshiki/{LATEST_PUBLISHED_TAG}",
            readme,
        )
        self.assertNotRegex(
            readme_words, rf"nix run github:[^\s`)]*{re.escape(CURRENT_RELEASE_TAG)}"
        )
        self.assertNotRegex(
            readme_words, rf"github:upiscium/Huroshiki/{re.escape(CURRENT_RELEASE_TAG)}"
        )


if __name__ == "__main__":
    unittest.main()
