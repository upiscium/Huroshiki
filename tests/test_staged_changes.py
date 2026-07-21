from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


MOD_TOML = '''name = "{name}"
filename = "{slug}.jar"
side = "{side}"
[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/{slug}.jar"
[update.modrinth]
mod-id = "{slug}"
version = "version-id"
'''


class StagedChangesTest(unittest.TestCase):
    def test_unstage_removes_only_selected_new_mod(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            pack_root = packs / "demo"
            source = pack_root / "source"
            mods = source / "mods"
            mods.mkdir(parents=True)
            (source / "pack.toml").write_text("name = \"Demo\"\n", encoding="utf-8")
            (source / "index.toml").write_text(
                'hash-format = "sha256"\n', encoding="utf-8"
            )

            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "STATE_ROOT", root / ".huroshiki"),
                patch.object(
                    core,
                    "TRANSACTION_ROOT",
                    root / ".huroshiki" / "transactions",
                ),
                patch.object(core, "LOG_ROOT", root / ".huroshiki" / "logs"),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
            ]
            for item in patches:
                item.start()
            try:
                transaction = core.PackTransaction.create(
                    core.project_key("pack", "demo")
                )
                first = transaction.source / "mods" / "first.pw.toml"
                second = transaction.source / "mods" / "second.pw.toml"
                first.write_text(
                    MOD_TOML.format(
                        name="First", slug="first", side="both"
                    ),
                    encoding="utf-8",
                )
                second.write_text(
                    MOD_TOML.format(
                        name="Second", slug="second", side="client"
                    ),
                    encoding="utf-8",
                )

                self.assertEqual(
                    [mod.name for mod in transaction.staged_mods()],
                    ["First", "Second"],
                )

                transaction.unstage(Path("mods/first.pw.toml"))

                self.assertFalse(first.exists())
                self.assertTrue(second.exists())
                self.assertEqual(
                    [mod.name for mod in transaction.staged_mods()],
                    ["Second"],
                )
                transaction.discard()
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_unstage_restores_existing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            pack_root = packs / "demo"
            source = pack_root / "source"
            mods = source / "mods"
            mods.mkdir(parents=True)
            (source / "pack.toml").write_text("name = \"Demo\"\n", encoding="utf-8")
            (source / "index.toml").write_text(
                'hash-format = "sha256"\n', encoding="utf-8"
            )
            existing = mods / "existing.pw.toml"
            original = MOD_TOML.format(
                name="Existing", slug="existing", side="both"
            ).encode()
            existing.write_bytes(original)

            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "STATE_ROOT", root / ".huroshiki"),
                patch.object(
                    core,
                    "TRANSACTION_ROOT",
                    root / ".huroshiki" / "transactions",
                ),
                patch.object(core, "LOG_ROOT", root / ".huroshiki" / "logs"),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
            ]
            for item in patches:
                item.start()
            try:
                transaction = core.PackTransaction.create(
                    core.project_key("pack", "demo")
                )
                staged = transaction.source / "mods" / "existing.pw.toml"
                staged.write_text(
                    MOD_TOML.format(
                        name="Existing", slug="existing", side="server"
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(len(transaction.staged_mods()), 1)

                transaction.unstage(Path("mods/existing.pw.toml"))

                self.assertEqual(staged.read_bytes(), original)
                self.assertEqual(transaction.staged_mods(), [])
                transaction.discard()
            finally:
                for item in reversed(patches):
                    item.stop()


if __name__ == "__main__":
    unittest.main()
