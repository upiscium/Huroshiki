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

# Keep current development expectations separate from immutable published history.
CURRENT_SOURCE_VERSION = "0.3.1-dev"
PUBLISHED_STABLE_VERSION = "0.3.0"
PUBLISHED_STABLE_DATE = "2026-09-11"
PUBLISHED_STABLE_TAG = f"v{PUBLISHED_STABLE_VERSION}"
PUBLISHED_STABLE_RELEASE_SCOPE = (
    "first stable release",
    "no functional runtime or Publish changes",
    "<publication_root>/client",
    "metafile = false",
    "primary_error",
    "public_pack_url",
    "v0.3.0-rc.2...v0.3.0",
)
HISTORICAL_RC2_VERSION = "0.3.0-rc.2"
HISTORICAL_RC2_DATE = "2026-09-08"
HISTORICAL_RC2_TAG = f"v{HISTORICAL_RC2_VERSION}"
HISTORICAL_RC2_RELEASE_SCOPE = (
    "bound manifests before reuse",
    "pack.toml[index].hash",
    "metafile = false",
    "<publication_root>/client",
    "primary_error",
    "bounded and redacted",
    "public_pack_url",
    "no configuration-schema migration",
)
HISTORICAL_RC1_VERSION = "0.3.0-rc.1"
HISTORICAL_RC1_DATE = "2026-09-03"
HISTORICAL_RC1_TAG = f"v{HISTORICAL_RC1_VERSION}"
HISTORICAL_RC1_RELEASE_SCOPE = (
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
            CURRENT_SOURCE_VERSION: "development",
            PUBLISHED_STABLE_VERSION: "stable",
            "0.3.0-rc.2.dev": "post-RC development",
            HISTORICAL_RC2_VERSION: "release-candidate",
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
        self.assertEqual(version_kind(CURRENT_SOURCE_VERSION), "development")
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

    def test_current_post_stable_development_metadata_is_deterministic(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith("# Changelog\n\n## Unreleased\n"))
        self.assertEqual(unreleased_payload(changelog), "")
        self.assertNotIn(f"## {CURRENT_SOURCE_VERSION} - ", changelog)
        self.assertNotIn("## 0.3.1 - ", changelog)
        development_release_path = (
            ROOT / "docs" / "releases" / f"v{CURRENT_SOURCE_VERSION}.md"
        )
        future_release_path = ROOT / "docs" / "releases" / "v0.3.1.md"
        self.assertFalse(development_release_path.exists())
        self.assertFalse(future_release_path.exists())

    def test_published_stable_metadata_is_immutable(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        stable = release_block(
            changelog, PUBLISHED_STABLE_VERSION, PUBLISHED_STABLE_DATE
        )
        self.assertTrue(stable.strip(), "historical stable release block must have a payload")
        self.assertIn(
            f"## {PUBLISHED_STABLE_VERSION} - {PUBLISHED_STABLE_DATE}\n", changelog
        )

        release_path = ROOT / "docs" / "releases" / f"{PUBLISHED_STABLE_TAG}.md"
        self.assertTrue(release_path.is_file())
        release_notes = release_path.read_text(encoding="utf-8")
        self.assertTrue(release_notes.startswith(f"# Huroshiki {PUBLISHED_STABLE_TAG}\n"))
        self.assertIn(f"Release date: {PUBLISHED_STABLE_DATE}", release_notes)
        self.assertIn(
            f"compare/{HISTORICAL_RC2_TAG}...{PUBLISHED_STABLE_TAG}", release_notes
        )
        release_material = " ".join(release_notes.split()).lower()
        for phrase in PUBLISHED_STABLE_RELEASE_SCOPE:
            with self.subTest(scope=phrase):
                self.assertIn(phrase.lower(), release_material)

        # The checked-in notes intentionally preserve the release-preparation state.
        self.assertIn(
            "The `v0.3.0` annotated tag and GitHub Release do\nnot yet exist",
            release_notes,
        )
        normalized = " ".join(release_notes.split())
        for evidence in (
            "common smoke Content was present in both server and client variants",
            "server-only and client-only smoke Content was present only on its intended side",
            "each observed Content index record had `metafile = false`",
            "each index SHA-256 matched the physical file",
            "Wrong-side smoke entries were absent",
            "This completes the production Content-descriptor gate",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, normalized)
        self.assertIn(
            "This preparation does not claim that final per-entry production check has passed based on deterministic repository tests alone",
            normalized,
        )

    def test_historical_published_rc2_metadata_is_immutable(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        rc2 = release_block(changelog, HISTORICAL_RC2_VERSION, HISTORICAL_RC2_DATE)
        self.assertTrue(rc2.strip(), "historical release block must have a payload")
        self.assertIn(
            f"## {HISTORICAL_RC2_VERSION} - {HISTORICAL_RC2_DATE}\n", changelog
        )

        release_path = ROOT / "docs" / "releases" / f"{HISTORICAL_RC2_TAG}.md"
        self.assertTrue(release_path.is_file())
        release_notes = release_path.read_text(encoding="utf-8")
        self.assertTrue(release_notes.startswith(f"# Huroshiki {HISTORICAL_RC2_TAG}\n"))
        self.assertIn(f"Release date: {HISTORICAL_RC2_DATE}", release_notes)
        self.assertIn(
            f"compare/{HISTORICAL_RC1_TAG}...{HISTORICAL_RC2_TAG}", release_notes
        )
        release_material = " ".join(release_notes.split()).lower()
        for phrase in HISTORICAL_RC2_RELEASE_SCOPE:
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
            changelog, HISTORICAL_RC1_VERSION, HISTORICAL_RC1_DATE
        )
        self.assertTrue(historical.strip(), "historical release block must have a payload")
        self.assertIn(
            f"## {HISTORICAL_RC1_VERSION} - {HISTORICAL_RC1_DATE}\n", changelog
        )

        release_path = ROOT / "docs" / "releases" / f"{HISTORICAL_RC1_TAG}.md"
        self.assertTrue(release_path.is_file())
        release_notes = release_path.read_text(encoding="utf-8")
        self.assertTrue(release_notes.startswith(f"# Huroshiki {HISTORICAL_RC1_TAG}\n"))
        self.assertIn(f"Release date: {HISTORICAL_RC1_DATE}", release_notes)
        self.assertIn(
            f"compare/v0.2.0-rc.5...{HISTORICAL_RC1_TAG}", release_notes
        )
        self.assertIn(f"{HISTORICAL_RC1_VERSION} - {HISTORICAL_RC1_DATE}", changelog)
        release_material = " ".join(release_notes.split()).lower()
        for phrase in HISTORICAL_RC1_RELEASE_SCOPE:
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
            rf"current main/source version is `{re.escape(CURRENT_SOURCE_VERSION)}`",
        )
        self.assertIn(PUBLISHED_STABLE_TAG, readme_words)
        self.assertRegex(
            readme_words,
            rf"latest published stable release is `{re.escape(PUBLISHED_STABLE_TAG)}`",
        )
        stable_release_reference = (
            r"github:upiscium/Huroshiki/v0\.3\.0(?:[\s`)]|$)"
        )
        self.assertRegex(readme, stable_release_reference)
        self.assertNotIn("latest published prerelease remains", readme_words)
        self.assertNotIn(
            "the `v0.3.0` tag and GitHub Release have not yet been published",
            readme_words,
        )
        future_release_reference = (
            r"github:upiscium/Huroshiki/v0\.3\.1(?:[\s`)]|$)"
        )
        self.assertNotRegex(readme, future_release_reference)
        self.assertNotIn("latest published stable release is `v0.3.1`", readme_words)


if __name__ == "__main__":
    unittest.main()
