from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
from pack_migration_roots import (
    PackMigrationRootError,
    PackRootRecord,
    extract_pack_migration_roots,
    extract_pack_migration_root_candidates,
    identify_pack_metadata_by_slug,
    read_pack_root_manifest,
    write_pack_root_manifest,
)
from pack_tree_policy import scan_pack_migration_source


class PackMigrationRootsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        (self.source / "mods" / "root.pw.toml").write_text(
            '''name = "Root"
filename = "root.jar"
side = "both"
[download]
url = "https://cdn.modrinth.com/root.jar"
[update.modrinth]
mod-id = "root-project"
version = "root-version"
''',
            encoding="utf-8",
        )
        (self.source / "mods" / "dependency.pw.toml").write_text(
            '''name = "Dependency"
filename = "dependency.jar"
side = "both"
[download]
url = "https://cdn.modrinth.com/dependency.jar"
[update.modrinth]
mod-id = "dependency-project"
version = "dependency-version"
''',
            encoding="utf-8",
        )
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "root-project", "both"),),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def extract(self):
        scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
        return extract_pack_migration_roots(
            self.source,
            expected_identity=scan.root_identity,
            expected_snapshot_digest=scan.snapshot_digest,
            checkpoint=lambda: None,
        )

    def test_extracts_explicit_root_and_excludes_dependency(self) -> None:
        roots = self.extract()
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].canonical_identity, "modrinth:root-project")
        self.assertEqual(roots[0].source_file_id, "root-version")
        self.assertEqual(roots[0].source_side, "both")
        self.assertEqual(roots[0].source_metadata_path, Path("mods/root.pw.toml"))

    def test_extracts_numeric_curseforge_and_url_roots(self) -> None:
        (self.source / "mods" / "curse.pw.toml").write_text(
            '''name = "Curse"
filename = "curse.jar"
side = "server"
[download]
url = "https://example.invalid/curse.jar"
[update.curseforge]
project-id = 123
file-id = 456
''',
            encoding="utf-8",
        )
        (self.source / "mods" / "url.pw.toml").write_text(
            '''name = "URL"
filename = "url.jar"
side = "client"
[download]
url = "https://example.invalid/url.jar"
[huroshiki]
project-id = "url-mod"
version = "1.0"
''',
            encoding="utf-8",
        )
        write_pack_root_manifest(
            self.source,
            (
                PackRootRecord("modrinth", "root-project", "both"),
                PackRootRecord("curseforge", "123", "server"),
                PackRootRecord("url", "url-mod", "client"),
            ),
        )
        roots = self.extract()
        self.assertEqual(
            [root.canonical_identity for root in roots],
            ["curseforge:123", "modrinth:root-project", "url:url-mod"],
        )
        self.assertEqual(roots[0].source_file_id, "456")
        self.assertEqual(roots[2].source_download_url, "https://example.invalid/url.jar")

    def test_legacy_url_metadata_is_candidate_without_inferred_identity(self) -> None:
        (self.source / "mods" / "root.pw.toml").unlink()
        (self.source / "mods" / "dependency.pw.toml").unlink()
        (self.source / ".huroshiki-roots.json").unlink()
        (self.source / "mods" / "legacy-url.pw.toml").write_text(
            '''name = "Legacy URL"
filename = "legacy.jar"
side = "both"
[download]
url = "https://example.invalid/legacy.jar"
''',
            encoding="utf-8",
        )
        scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
        candidates = extract_pack_migration_root_candidates(
            self.source,
            expected_identity=scan.root_identity,
            expected_snapshot_digest=scan.snapshot_digest,
            checkpoint=lambda: None,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "url")
        self.assertIsNone(candidates[0].canonical_identity)
        self.assertEqual(candidates[0].source_metadata_path, Path("mods/legacy-url.pw.toml"))

    def test_missing_manifest_and_duplicate_identity_fail_closed(self) -> None:
        (self.source / ".huroshiki-roots.json").unlink()
        scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
        with self.assertRaisesRegex(PackMigrationRootError, "provenance"):
            extract_pack_migration_roots(
                self.source,
                expected_identity=scan.root_identity,
                expected_snapshot_digest=scan.snapshot_digest,
                checkpoint=lambda: None,
            )

        (self.source / ".huroshiki-roots.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "roots": [
                        {"provider": "modrinth", "project_id": "root-project", "side": "both"},
                        {"provider": "modrinth", "project_id": "root-project", "side": "both"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
        with self.assertRaisesRegex(PackMigrationRootError, "Duplicate root identity"):
            extract_pack_migration_roots(
                self.source,
                expected_identity=scan.root_identity,
                expected_snapshot_digest=scan.snapshot_digest,
                checkpoint=lambda: None,
            )

    def test_rejects_invalid_side_curseforge_id_and_stale_snapshot(self) -> None:
        for provider, project_id, side, pattern in (
            ("modrinth", "root-project", "invalid", "invalid side"),
            ("curseforge", "slug", "both", "numeric"),
        ):
            with self.subTest(provider=provider):
                (self.source / ".huroshiki-roots.json").write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "roots": [
                                {
                                    "provider": provider,
                                    "project_id": project_id,
                                    "side": side,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
                with self.assertRaisesRegex(PackMigrationRootError, pattern):
                    extract_pack_migration_roots(
                        self.source,
                        expected_identity=scan.root_identity,
                        expected_snapshot_digest=scan.snapshot_digest,
                        checkpoint=lambda: None,
                    )

        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "root-project", "both"),),
        )
        scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
        (self.source / "mods" / "root.pw.toml").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(PackMigrationRootError, "changed"):
            extract_pack_migration_roots(
                self.source,
                expected_identity=scan.root_identity,
                expected_snapshot_digest=scan.snapshot_digest,
                checkpoint=lambda: None,
            )

    def test_rejects_hardlinked_metadata(self) -> None:
        os.link(
            self.source / "mods" / "root.pw.toml",
            self.source / "mods" / "alias.pw.toml",
        )
        scan = scan_pack_migration_source(self.source, checkpoint=lambda: None)
        self.assertTrue(scan.entries[-1].errors or any(entry.errors for entry in scan.entries))

    def test_identifies_canonical_identity_from_selected_metadata(self) -> None:
        self.assertEqual(
            identify_pack_metadata_by_slug(self.source, "root"),
            "modrinth:root-project",
        )
        self.assertIsNone(identify_pack_metadata_by_slug(self.source, "missing"))

    def test_transaction_side_unstage_and_remove_keep_manifest_consistent(self) -> None:
        baseline = core.metadata_digest_snapshot(self.source)
        baseline_contents = core.metadata_content_snapshot(self.source)
        baseline_roots = read_pack_root_manifest(self.source)
        transaction = core.PackTransaction(
            project_key="pack:demo",
            root=self.source.parent,
            source=self.source,
            baseline=baseline,
            baseline_contents=baseline_contents,
            root_manifest_baseline=baseline_roots,
        )
        transaction.set_side(Path("mods/root.pw.toml"), True, False)
        self.assertEqual(read_pack_root_manifest(self.source)[0].side, "client")

        changed = (self.source / "mods" / "root.pw.toml").read_text().replace(
            'version = "root-version"', 'version = "new-version"'
        )
        (self.source / "mods" / "root.pw.toml").write_text(changed, encoding="utf-8")
        transaction.unstage(Path("mods/root.pw.toml"))
        self.assertEqual(
            (self.source / "mods" / "root.pw.toml").read_bytes(),
            baseline_contents[Path("mods/root.pw.toml")],
        )
        self.assertEqual(read_pack_root_manifest(self.source), baseline_roots)

        def remove(*_: object, **__: object) -> None:
            (self.source / "mods" / "root.pw.toml").unlink()

        with patch.object(core, "_run_noninteractive_packwiz", side_effect=remove):
            transaction.remove_mods(["root"])
        self.assertEqual(read_pack_root_manifest(self.source), ())


if __name__ == "__main__":
    unittest.main()
