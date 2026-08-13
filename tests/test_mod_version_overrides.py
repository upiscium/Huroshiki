from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
from mod_version_overrides import (
    ModVersionOverride,
    ModVersionOverrideError,
    VERSION_OVERRIDE_MANIFEST_MAX_BYTES,
    parse_mod_version_overrides,
    read_mod_version_overrides,
    serialize_mod_version_overrides,
    write_mod_version_overrides,
)


class ModVersionOverrideManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_missing_manifest_is_empty(self) -> None:
        self.assertEqual(read_mod_version_overrides(self.source), ())

    def test_canonical_round_trip_and_deterministic_ordering(self) -> None:
        entries = (
            ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78", False),
            ModVersionOverride(
                "curseforge", "309927", "6529130", True, "Compatibility"
            ),
        )
        write_mod_version_overrides(self.source, entries)
        contents = (self.source / ".huroshiki-version-overrides.json").read_bytes()
        self.assertEqual(read_mod_version_overrides(self.source), tuple(reversed(entries)))
        self.assertEqual(contents, serialize_mod_version_overrides(entries))
        self.assertLess(contents.index(b"curseforge:309927"), contents.index(b"modrinth:Ab12Cd34"))

    def test_strict_schema_and_entry_validation(self) -> None:
        valid = {
            "schema": 1,
            "mods": {
                "curseforge:309927": {
                    "artifact_id": "6529130",
                    "selection": "user",
                    "locked": False,
                }
            },
        }
        mutations = (
            ({**valid, "extra": True}, "unknown"),
            ({**valid, "schema": 2}, "schema"),
            ({**valid, "schema": True}, "schema"),
            ({"schema": 1, "mods": {"curseforge:slug": next(iter(valid["mods"].values()))}}, "positive decimal"),
            ({"schema": 1, "mods": {"modrinth:short": next(iter(valid["mods"].values()))}}, "8-character"),
        )
        for value, pattern in mutations:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ModVersionOverrideError, pattern
            ):
                parse_mod_version_overrides(json.dumps(value).encode())

        for field, value, pattern in (
            ("artifact_id", "0", "positive decimal"),
            ("selection", "automatic", "selection"),
            ("locked", 1, "boolean"),
            ("reason", 3, "string"),
            ("reason", "bad\nreason", "control"),
            ("extra", True, "unknown"),
        ):
            changed = json.loads(json.dumps(valid))
            changed["mods"]["curseforge:309927"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                ModVersionOverrideError, pattern
            ):
                parse_mod_version_overrides(json.dumps(changed).encode())

    def test_duplicate_json_identity_and_oversize_are_rejected(self) -> None:
        duplicate = b'{"schema":1,"mods":{"curseforge:1":{"artifact_id":"2","selection":"user","locked":false},"curseforge:1":{"artifact_id":"3","selection":"user","locked":false}}}'
        with self.assertRaisesRegex(ModVersionOverrideError, "Duplicate"):
            parse_mod_version_overrides(duplicate)
        with self.assertRaisesRegex(ModVersionOverrideError, "size"):
            parse_mod_version_overrides(b" " * (VERSION_OVERRIDE_MANIFEST_MAX_BYTES + 1))
        with patch("mod_version_overrides.json.loads", side_effect=RecursionError):
            with self.assertRaisesRegex(ModVersionOverrideError, "invalid JSON"):
                parse_mod_version_overrides(b"{}")

    def test_symlink_manifest_is_rejected(self) -> None:
        target = self.source / "target"
        target.write_text("{}", encoding="utf-8")
        (self.source / ".huroshiki-version-overrides.json").symlink_to(target)
        with self.assertRaises(ModVersionOverrideError):
            read_mod_version_overrides(self.source)

    def test_atomic_replacement_failure_preserves_old_bytes_and_cleans_temp(self) -> None:
        old = (ModVersionOverride("curseforge", "1", "2"),)
        write_mod_version_overrides(self.source, old)
        path = self.source / ".huroshiki-version-overrides.json"
        before = path.read_bytes()
        with patch("pack_migration_roots.packctl.renameat2", side_effect=OSError("fail")):
            with self.assertRaises(OSError):
                write_mod_version_overrides(
                    self.source, (ModVersionOverride("curseforge", "1", "3"),)
                )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            [item for item in self.source.iterdir() if item.name.endswith(".tmp")], []
        )

    def test_read_rejects_replaced_source_root(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2"),)
        )
        with patch(
            "mod_version_overrides.scan_pack_migration_source",
            wraps=__import__("mod_version_overrides").scan_pack_migration_source,
        ) as scan:
            original = scan(self.source, checkpoint=lambda: None)
            scan.return_value = original
            replacement = self.source.with_name("replacement")
            self.source.rename(replacement)
            self.source.mkdir()
            with self.assertRaisesRegex(ModVersionOverrideError, "replaced"):
                read_mod_version_overrides(self.source)


class ModVersionOverrideCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        self.metadata = self.source / "mods" / "demo.pw.toml"
        self.metadata.write_text(
            '''name = "Demo"\nfilename = "demo.jar"\nside = "both"\n[download]\nurl = "https://example.invalid/demo.jar"\n[update.curseforge]\nproject-id = 1\nfile-id = 2\n''',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def transaction(self) -> core.PackTransaction:
        return core.PackTransaction(
            "pack:demo",
            self.source.parent,
            self.source,
            core.metadata_digest_snapshot(self.source),
            core.metadata_content_snapshot(self.source),
        )

    def test_inspect_active_drifted_stale_and_duplicate(self) -> None:
        write_mod_version_overrides(
            self.source,
            (
                ModVersionOverride("curseforge", "1", "2"),
                ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78"),
            ),
        )
        statuses = core.inspect_mod_version_overrides(self.source)
        self.assertEqual([item.status for item in statuses], ["active", "stale"])
        self.metadata.write_text(self.metadata.read_text().replace("file-id = 2", "file-id = 3"))
        self.assertEqual(core.inspect_mod_version_overrides(self.source)[0].status, "drifted")
        alias = self.source / "mods" / "alias.pw.toml"
        alias.write_bytes(self.metadata.read_bytes())
        with self.assertRaisesRegex(core.HuroshikiError, "ambiguous"):
            core.inspect_mod_version_overrides(self.source)

    def test_pin_requires_existing_user_override_and_preserves_artifact(self) -> None:
        transaction = self.transaction()
        with self.assertRaisesRegex(core.HuroshikiError, "existing user selection"):
            transaction.set_mod_version_pin("curseforge:1")
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", False),)
        )
        pinned = transaction.set_mod_version_pin(
            "curseforge:1", locked=True, reason="Compatibility"
        )
        self.assertTrue(pinned.locked)
        self.assertEqual(pinned.artifact_id, "2")
        unpinned = transaction.set_mod_version_pin("curseforge:1", locked=False)
        self.assertFalse(unpinned.locked)
        self.assertEqual(unpinned.reason, "Compatibility")


if __name__ == "__main__":
    unittest.main()
