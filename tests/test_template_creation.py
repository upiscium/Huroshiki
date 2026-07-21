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
