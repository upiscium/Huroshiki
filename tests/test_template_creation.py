from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


PACK_TOML = '''name = "Generated"
author = "tester"
version = "0.1.0"
pack-format = "packwiz:1.1.0"
[index]
file = "index.toml"
hash-format = "sha256"
hash = "placeholder"
[versions]
minecraft = "1.21.1"
neoforge = "21.1.999"
'''


class TemplateCreationTest(unittest.TestCase):
    def test_creation_uses_already_held_lock_without_self_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\nenabled: true\n"
                "minecraft: 1.21.1\nloader: neoforge\n"
                "reference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )

            def fake_packwiz(command, *, cwd=None, **kwargs):
                source = Path(cwd)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text(
                    'hash-format = "sha256"\n', encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(packctl, "run", side_effect=fake_packwiz),
                patch.object(core.subprocess, "run", side_effect=fake_packwiz),
            ):
                report = core.create_pack_from_template(
                    template_id="base",
                    project_id="generated",
                    display_name="Generated",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.999",
                )

            self.assertEqual(report.pack_key, "pack:generated")
            self.assertTrue((packs / "generated" / "pack.yaml").is_file())

    def test_invalid_url_is_rejected_before_pack_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Broken URL
    provider: url
    project_id: broken
    url: https://example.invalid/broken.zip
    side: both
''',
                encoding="utf-8",
            )
            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project") as create,
            ):
                with self.assertRaisesRegex(packctl.ConfigError, r"\.jar file"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            create.assert_not_called()
            self.assertFalse((packs / "generated").exists())

    def test_malformed_manifest_is_validated_before_pack_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Broken
    provider: unsupported
    project_id: broken
    side: both
''',
                encoding="utf-8",
            )
            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for item in patches:
                item.start()
            try:
                with patch.object(core, "create_project") as create:
                    with self.assertRaises(packctl.ConfigError):
                        core.create_pack_from_template(
                            template_id="base",
                            project_id="generated",
                            display_name="Generated",
                            minecraft="1.21.1",
                            loader="neoforge",
                            loader_version="21.1.999",
                        )
                create.assert_not_called()
                self.assertFalse((packs / "generated").exists())
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_fatal_error_after_creation_removes_destination_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Fatal
    provider: modrinth
    project_id: fatal
    side: both
''',
                encoding="utf-8",
            )
            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for item in patches:
                item.start()

            def fake_create(*args):
                source = packs / "generated" / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text("original index\n", encoding="utf-8")
                return 0

            try:
                with patch.object(core, "create_project", side_effect=fake_create), patch.object(
                    core.subprocess, "run", side_effect=OSError("packwiz unavailable")
                ):
                    with self.assertRaisesRegex(OSError, "packwiz unavailable"):
                        core.create_pack_from_template(
                            template_id="base",
                            project_id="generated",
                            display_name="Generated",
                            minecraft="1.21.1",
                            loader="neoforge",
                            loader_version="21.1.999",
                        )
                self.assertFalse((packs / "generated").exists())
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_fatal_error_reports_destination_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Fatal
    provider: modrinth
    project_id: fatal
    side: both
''',
                encoding="utf-8",
            )

            def fake_create(*args):
                source = packs / "generated" / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text("original", encoding="utf-8")
                return 0

            real_rmtree = core.shutil.rmtree
            destination = packs / "generated"

            def failed_rollback(path, *args, **kwargs):
                if Path(path) == destination:
                    raise OSError("destination is busy")
                return real_rmtree(path, *args, **kwargs)

            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project", side_effect=fake_create),
                patch.object(core.subprocess, "run", side_effect=OSError("packwiz unavailable")),
                patch.object(core.shutil, "rmtree", side_effect=failed_rollback),
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "failed to roll back.*destination is busy"
                ) as raised:
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn("packwiz unavailable", str(raised.exception.__cause__))

    def test_create_failure_does_not_claim_or_remove_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods: []
''',
                encoding="utf-8",
            )

            def failed_create(*args):
                destination = packs / "generated"
                destination.mkdir(parents=True)
                (destination / "diagnostic.txt").write_text("retained", encoding="utf-8")
                return 1

            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project", side_effect=failed_create),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "Failed to create"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            self.assertEqual(
                (packs / "generated" / "diagnostic.txt").read_text(), "retained"
            )

    def test_partial_install_keeps_successes_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Works
    provider: modrinth
    project_id: works
    side: both
  - name: Wrong loader version
    provider: curseforge
    project_id: "404"
    side: server
''',
                encoding="utf-8",
            )

            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for item in patches:
                item.start()

            def fake_create(*args):
                pack_root = packs / "generated"
                (pack_root / "source" / "mods").mkdir(parents=True)
                (pack_root / "source" / "pack.toml").write_text(
                    PACK_TOML, encoding="utf-8"
                )
                (pack_root / "source" / "index.toml").write_text(
                    'hash-format = "sha256"\n', encoding="utf-8"
                )
                (pack_root / "pack.yaml").write_text(
                    "id: generated\ndisplay_name: Generated\nenabled: true\n",
                    encoding="utf-8",
                )
                return 0

            real_run = subprocess.run

            def fake_run(command, *, cwd=None, text=None, capture_output=False, check=False):
                if command[-1] == "works":
                    (Path(cwd) / "mods" / "works.pw.toml").write_text(
                        '''name = "Works"\nfilename = "works.jar"\nside = "both"\n[download]\nhash-format = "sha256"\nhash = "00"\nurl = "https://example.invalid"\n[update.modrinth]\nmod-id = "works"\nversion = "v"\n''',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                if "--addon-id" in command:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        "No compatible files for the selected loader version",
                    )
                if command[-1] == "refresh":
                    return subprocess.CompletedProcess(command, 0, "", "")
                return real_run(
                    command,
                    cwd=cwd,
                    text=text,
                    capture_output=capture_output,
                    check=check,
                )

            try:
                with patch.object(core, "create_project", side_effect=fake_create), patch.object(
                    core.subprocess, "run", side_effect=fake_run
                ):
                    report = core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
                self.assertEqual(report.installed, ("Works",))
                self.assertEqual(len(report.failed), 1)
                self.assertIn("No compatible files", report.failed[0].reason)
                self.assertTrue(
                    (packs / "generated" / "source" / "mods" / "works.pw.toml").exists()
                )
            finally:
                for item in reversed(patches):
                    item.stop()


if __name__ == "__main__":
    unittest.main()
