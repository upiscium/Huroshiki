from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


def metadata(name: str, slug: str, version: str, *, pin: bool = False) -> str:
    pinned = "pin = true\n" if pin else ""
    return f'''name = "{name}"
filename = "{slug}-{version}.jar"
side = "both"
{pinned}[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/{slug}.jar"
[update.modrinth]
mod-id = "{slug}"
version = "{version}"
'''


class TransactionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_text('name = "Demo"\n', encoding="utf-8")
        (self.source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        ]
        for item in self.patches:
            item.start()
        self.key = core.project_key("pack", "demo")

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_mod(self, slug: str, version: str = "v1", *, pin: bool = False) -> Path:
        path = self.source / "mods" / f"{slug}.pw.toml"
        path.write_text(metadata(slug.title(), slug, version, pin=pin), encoding="utf-8")
        return path

    @staticmethod
    def completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode)


class UpdateTransactionTest(TransactionTestCase):
    def test_no_candidates_reports_current_and_pinned(self) -> None:
        self.write_mod("current")
        self.write_mod("fixed", pin=True)
        (self.source / "mods" / "manual.pw.toml").write_text(
            '''name = "Manual"
filename = "manual.jar"
side = "both"
[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/manual.jar"
''',
            encoding="utf-8",
        )
        transaction = core.PackTransaction.create(self.key)
        with patch.object(
            core.subprocess,
            "run",
            side_effect=lambda command, **_: self.completed(command),
        ):
            candidates = transaction.prepare_updates()
        self.assertEqual(
            [(item.slug, item.status) for item in candidates],
            [
                ("current", "current"),
                ("fixed", "pinned"),
                ("manual", "unavailable"),
            ],
        )
        self.assertFalse(any(item.available for item in candidates))
        transaction.discard()

    def test_full_and_partial_selection_apply_only_selected_metadata(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[-2:] == ["update", "--all"]:
                for slug in ("first", "second"):
                    (cwd / "mods" / f"{slug}.pw.toml").write_text(
                        metadata(slug.title(), slug, "v2"), encoding="utf-8"
                    )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            self.assertEqual(
                [item.slug for item in candidates if item.available],
                ["first", "second"],
            )
            transaction.select_updates([Path("mods/first.pw.toml")])
            transaction.apply()

        self.assertIn('version = "v2"', first.read_text(encoding="utf-8"))
        self.assertIn('version = "v1"', second.read_text(encoding="utf-8"))

    def test_update_all_applies_every_candidate(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[-2:] == ["update", "--all"]:
                for slug in ("first", "second"):
                    (cwd / "mods" / f"{slug}.pw.toml").write_text(
                        metadata(slug.title(), slug, "v2"), encoding="utf-8"
                    )
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            self.assertEqual(core.update_all(self.key), 0)
        self.assertIn('version = "v2"', first.read_text(encoding="utf-8"))
        self.assertIn('version = "v2"', second.read_text(encoding="utf-8"))

    def test_update_failure_leaves_real_source_unchanged(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()

        def run(command, *, cwd, **_):
            (cwd / "mods" / "first.pw.toml").write_text(
                metadata("First", "first", "broken"), encoding="utf-8"
            )
            return self.completed(command, 7)

        with patch.object(core.subprocess, "run", side_effect=run):
            self.assertEqual(core.update_all(self.key), 7)
        self.assertEqual(target.read_bytes(), original)

    def test_discard_cancels_staged_update(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()

        def run(command, *, cwd, **_):
            (cwd / "mods" / "first.pw.toml").write_text(
                metadata("First", "first", "v2"), encoding="utf-8"
            )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            transaction.prepare_updates()
        transaction.discard()
        self.assertEqual(target.read_bytes(), original)

    def test_external_source_change_aborts_apply(self) -> None:
        target = self.write_mod("first")

        def run(command, *, cwd, **_):
            if command[-2:] == ["update", "--all"]:
                (cwd / "mods" / "first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            target.write_text(metadata("First", "first", "external"), encoding="utf-8")
            transaction.select_updates(
                item.relative_path for item in candidates if item.available
            )
            with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
                transaction.apply()
        self.assertIn("external", target.read_text(encoding="utf-8"))
        transaction.discard()


class RemoveTransactionTest(TransactionTestCase):
    def test_batch_remove_failure_leaves_every_real_mod(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:2] == ["packwiz", "remove"]:
                slug = command[2]
                if slug == "second":
                    return self.completed(command, 9)
                (cwd / "mods" / f"{slug}.pw.toml").unlink()
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            result = core.remove_installed_mods(self.key, ["first", "second"])
        self.assertEqual(result, 9)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_external_source_change_aborts_batch_remove(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:2] == ["packwiz", "remove"]:
                (cwd / "mods" / f"{command[2]}.pw.toml").unlink()
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            self.assertEqual(transaction.remove_mods(["first", "second"]), 0)
            second.write_text(
                metadata("Second", "second", "external"), encoding="utf-8"
            )
            with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
                transaction.apply()
        self.assertTrue(first.exists())
        self.assertIn("external", second.read_text(encoding="utf-8"))
        transaction.discard()

    def test_template_manifest_failure_is_atomic(self) -> None:
        template_root = self.templates / "base"
        template_root.mkdir(parents=True)
        manifest = template_root / "template.yaml"
        manifest.write_text(
            '''id: base
display_name: Base
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.0
mods:
  - name: First
    provider: modrinth
    project_id: first
    side: both
  - name: Second
    provider: curseforge
    project_id: second
    side: client
''',
            encoding="utf-8",
        )
        original = manifest.read_bytes()
        key = core.project_key("template", "base")
        with patch.object(
            packctl,
            "save_template_mods_raw",
            side_effect=packctl.ConfigError("write failed"),
        ):
            with self.assertRaisesRegex(packctl.ConfigError, "write failed"):
                core.remove_installed_mods(
                    key,
                    ["modrinth-first", "curseforge-second"],
                )
        self.assertEqual(manifest.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
