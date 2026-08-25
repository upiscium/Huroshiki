from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
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

    def test_writer_rejects_every_noncanonical_model(self) -> None:
        for entry, pattern in (
            (ModVersionOverride("curseforge", "1", "2", 1), "boolean"),
            (ModVersionOverride("curseforge", "1", "2", "true"), "boolean"),
            (ModVersionOverride("url", "one", "two"), "provider"),
            (ModVersionOverride("curseforge", "slug", "2"), "positive decimal"),
            (ModVersionOverride("curseforge", "1", "0"), "positive decimal"),
            (ModVersionOverride("modrinth", "short", "Ef56Gh78"), "8-character"),
            (ModVersionOverride("curseforge", "1", "2", False, "bad\nreason"), "control"),
        ):
            with self.subTest(entry=entry), self.assertRaisesRegex(
                ModVersionOverrideError, pattern
            ):
                serialize_mod_version_overrides((entry,))

    def test_every_successful_serialization_parses(self) -> None:
        entries = (
            ModVersionOverride("curseforge", "1", "2", False),
            ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78", True, "Reason"),
        )
        serialized = serialize_mod_version_overrides(entries)
        self.assertEqual(parse_mod_version_overrides(serialized).entries, entries)

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
        with self.assertRaisesRegex(core.HuroshikiError, "automatically selected"):
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

    def test_pin_is_blocked_by_active_transaction_operation(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2"),)
        )
        transaction = self.transaction()
        transaction._operation = object()
        before = (self.source / ".huroshiki-version-overrides.json").read_bytes()
        with self.assertRaisesRegex(core.HuroshikiError, "active transaction operation"):
            transaction.set_mod_version_pin("curseforge:1")
        self.assertEqual(
            (self.source / ".huroshiki-version-overrides.json").read_bytes(), before
        )

    def test_pin_write_failure_is_normalized_and_preserves_manifest(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2"),)
        )
        transaction = self.transaction()
        before = (self.source / ".huroshiki-version-overrides.json").read_bytes()
        with patch(
            "pack_migration_roots.packctl.renameat2",
            side_effect=OSError("injected write failure"),
        ), self.assertRaisesRegex(core.HuroshikiError, "injected write failure"):
            transaction.set_mod_version_pin("curseforge:1")
        self.assertEqual(
            (self.source / ".huroshiki-version-overrides.json").read_bytes(), before
        )

    def test_pin_requires_canonical_ignore_without_partial_mutation(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2"),)
        )
        self.source.joinpath(".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        transaction = self.transaction()
        manifest_before = self.source.joinpath(
            ".huroshiki-version-overrides.json"
        ).read_bytes()
        ignore_before = self.source.joinpath(".packwizignore").read_bytes()
        with self.assertRaisesRegex(core.HuroshikiError, "canonically excluded"):
            transaction.set_mod_version_pin("curseforge:1")
        self.assertEqual(
            self.source.joinpath(".huroshiki-version-overrides.json").read_bytes(),
            manifest_before,
        )
        self.assertEqual(self.source.joinpath(".packwizignore").read_bytes(), ignore_before)

    def test_automatic_preview_removes_only_target_intent(self) -> None:
        write_mod_version_overrides(
            self.source,
            (
                ModVersionOverride("curseforge", "1", "2", True, "Compatibility"),
                ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78", False),
            ),
        )
        transaction = self.transaction()
        metadata_before = self.metadata.read_bytes()
        preview = transaction.prepare_mod_version_automatic("curseforge:1")
        self.assertEqual(preview.old_selection, "user")
        self.assertEqual(preview.new_selection, "automatic")
        self.assertEqual(preview.installed_artifact_id, "2")
        self.assertEqual(preview.selected_artifact_id, "2")
        self.assertTrue(preview.old_locked)
        self.assertIsNone(preview.new_locked)
        self.assertEqual(preview.reason, "Compatibility")
        self.assertEqual(preview.override_status, "active")
        self.assertEqual(self.metadata.read_bytes(), metadata_before)
        self.assertEqual(
            read_mod_version_overrides(self.source),
            (ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78", False),),
        )

    def test_automatic_rejects_drifted_and_stale_without_mutation(self) -> None:
        for identity, override, expected_status in (
            (
                "curseforge:1",
                ModVersionOverride("curseforge", "1", "3", True),
                "drifted",
            ),
            (
                "modrinth:Ab12Cd34",
                ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78", False),
                "stale",
            ),
        ):
            with self.subTest(status=expected_status):
                write_mod_version_overrides(self.source, (override,))
                transaction = self.transaction()
                before = core._file_content_snapshot(self.source)
                with self.assertRaisesRegex(
                    core.HuroshikiError,
                    f"{expected_status}.*re-select the exact artifact",
                ) as raised:
                    transaction.prepare_mod_version_automatic(identity)
                self.assertNotIn("Return to Automatic", str(raised.exception))
                self.assertEqual(core._file_content_snapshot(self.source), before)
                self.assertEqual(
                    read_mod_version_overrides(self.source), (override,)
                )
                self.assertFalse(transaction._source_mutation_recorded)
                self.assertFalse(transaction._version_override_mutated)
                self.assertFalse(transaction._intent_only_mutation)

    def test_automatic_missing_override_is_stable_noop(self) -> None:
        transaction = self.transaction()
        before = core._file_content_snapshot(self.source)
        preview = transaction.prepare_mod_version_automatic("curseforge:1")
        self.assertEqual(preview.old_selection, "automatic")
        self.assertEqual(preview.new_selection, "automatic")
        self.assertEqual(preview.installed_artifact_id, "2")
        self.assertEqual(preview.changes, ())
        self.assertEqual(core._file_content_snapshot(self.source), before)

    def test_automatic_malformed_manifest_fails_without_mutation(self) -> None:
        manifest = self.source / ".huroshiki-version-overrides.json"
        manifest.write_bytes(b"{not-json")
        before = core._file_content_snapshot(self.source)
        with self.assertRaisesRegex(core.HuroshikiError, "invalid JSON"):
            self.transaction().prepare_mod_version_automatic("curseforge:1")
        self.assertEqual(core._file_content_snapshot(self.source), before)

    def test_automatic_write_failure_preserves_previous_manifest(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", True),)
        )
        manifest = self.source / ".huroshiki-version-overrides.json"
        before = manifest.read_bytes()
        with patch(
            "pack_migration_roots.packctl.renameat2",
            side_effect=OSError("injected automatic write failure"),
        ), self.assertRaisesRegex(core.HuroshikiError, "injected automatic write failure"):
            self.transaction().prepare_mod_version_automatic("curseforge:1")
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(self.metadata.read_text().count("file-id = 2"), 1)

    def test_pin_unpin_preview_preserves_artifact_and_reason(self) -> None:
        write_mod_version_overrides(
            self.source,
            (ModVersionOverride("curseforge", "1", "2", False, "Original"),),
        )
        transaction = self.transaction()
        pinned = transaction.prepare_mod_version_pin(
            "curseforge:1", locked=True, reason="Replacement"
        )
        self.assertEqual(pinned.selected_artifact_id, "2")
        self.assertFalse(pinned.old_locked)
        self.assertTrue(pinned.new_locked)
        self.assertEqual(pinned.reason, "Replacement")
        unpinned = transaction.prepare_mod_version_pin(
            "curseforge:1", locked=False
        )
        self.assertTrue(unpinned.old_locked)
        self.assertFalse(unpinned.new_locked)
        self.assertEqual(unpinned.reason, "Replacement")

    def test_pin_unpin_reject_drifted_and_stale_intent(self) -> None:
        for identity, override, expected_status in (
            (
                "curseforge:1",
                ModVersionOverride("curseforge", "1", "3", False),
                "drifted",
            ),
            (
                "modrinth:Ab12Cd34",
                ModVersionOverride("modrinth", "Ab12Cd34", "Ef56Gh78", True),
                "stale",
            ),
        ):
            with self.subTest(status=expected_status):
                write_mod_version_overrides(self.source, (override,))
                before = core._file_content_snapshot(self.source)
                with self.assertRaisesRegex(
                    core.HuroshikiError,
                    f"{expected_status}.*re-select the exact artifact",
                ) as raised:
                    self.transaction().prepare_mod_version_pin(
                        identity, locked=not override.locked
                    )
                self.assertNotIn("Return to Automatic", str(raised.exception))
                self.assertEqual(core._file_content_snapshot(self.source), before)

    def test_intent_only_refresh_state_is_monotonic_in_mixed_transactions(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", False),)
        )
        metadata_path = Path("mods/demo.pw.toml")

        metadata_then_intent = self.transaction()
        metadata_then_intent.set_side(metadata_path, True, False)
        metadata_then_intent.prepare_mod_version_pin("curseforge:1", locked=True)
        self.assertFalse(metadata_then_intent._intent_only_mutation)

        self.metadata.write_text(
            self.metadata.read_text().replace('side = "client"', 'side = "both"'),
            encoding="utf-8",
        )
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", False),)
        )
        intent_then_metadata = self.transaction()
        intent_then_metadata.prepare_mod_version_pin("curseforge:1", locked=True)
        intent_then_metadata.set_side(metadata_path, True, False)
        self.assertFalse(intent_then_metadata._intent_only_mutation)

    def test_intent_prepare_honors_cancellation_and_deadline(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", False),)
        )
        before = core._file_content_snapshot(self.source)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(core.ExactModVersionCancelled):
            self.transaction().prepare_mod_version_automatic(
                "curseforge:1", cancel_event=cancelled
            )
        with self.assertRaises(core.ExactModVersionDeadlineExceeded):
            self.transaction().prepare_mod_version_pin(
                "curseforge:1", deadline=time.monotonic() - 1
            )
        self.assertEqual(core._file_content_snapshot(self.source), before)

    def test_intent_cancellation_after_manifest_exchange_rolls_back(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", True),)
        )
        transaction = self.transaction()
        before = core._file_content_snapshot(self.source)
        cancelled = threading.Event()
        original_rename = __import__("pack_migration_roots").packctl.renameat2

        def rename_then_cancel(*args, **kwargs):
            result = original_rename(*args, **kwargs)
            cancelled.set()
            return result

        with patch(
            "pack_migration_roots.packctl.renameat2",
            side_effect=rename_then_cancel,
        ), self.assertRaises(core.ExactModVersionCancelled):
            transaction.prepare_mod_version_automatic(
                "curseforge:1", cancel_event=cancelled
            )
        self.assertEqual(core._file_content_snapshot(self.source), before)
        self.assertFalse(transaction._source_mutation_recorded)
        self.assertFalse(transaction._version_override_mutated)

    def test_intent_rollback_cleanup_has_fresh_bounded_deadline(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", True),)
        )
        transaction = self.transaction()
        contents = self.source.joinpath(
            ".huroshiki-version-overrides.json"
        ).read_bytes()
        with self.assertRaisesRegex(
            core.HuroshikiError, "rollback cleanup deadline exceeded"
        ):
            transaction._restore_version_intent_manifest(
                contents, deadline=time.monotonic() - 1
            )


if __name__ == "__main__":
    unittest.main()
